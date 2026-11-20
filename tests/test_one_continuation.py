"""One continuation path: synthetic transport, local disposable repositories."""

import inspect
import json
from pathlib import Path
import re

import pytest

from lifecycle import core, service
from test_lifecycle_continuation import saved_story, newest_claim
from test_lifecycle_delivery import story, settings
from test_lifecycle_fetch import command
import test_runner as fixtures

ROOT = Path(__file__).resolve().parents[1]


def reapprove(hub):
    hub.event(1, "approved", "owner")
    hub.approve()


@pytest.mark.parametrize("reason", ["owner_question", "owner_merge_required",
                                    "worker_story_tests_unavailable", "execution_failed"])
def test_each_kind_continues_without_pasted_id(tmp_path, monkeypatch, reason):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    old["status"] = {"execution_failed": "failed", "worker_story_tests_unavailable": "blocked"}.get(reason, "needs_owner")
    old["blocked_reason"] = {"reason_code": reason, "reason": core.STOP_REASONS[reason]}
    path = (Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state"
            / "tasks" / (old["id"] + ".json"))
    core.save(path, old)
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    stop = next(r for r in records if r.get("continuation"))
    replacement = core.public_record(cfg, 1, old["id"], stop["round"],
                                     "needs-owner" if old["status"] == "needs_owner" else "blocked",
                                     commit=old["head_commit"], event=stop["event"],
                                     reason=reason, continuation=True)
    replacement.update(actor=stop["actor"], **{"from": stop["from"]})
    for comment in hub.comments_by_issue[1]:
        if stop["key"] in comment["body"]:
            comment["body"] = core.record_body(replacement)
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["continued_from"] == old["head_commit"]
    assert claim["previous_stop_reason"] == reason
    assert Path(claim["worktree"], "delivery.txt").exists()


@pytest.mark.parametrize("heading", ["## Owner decisions", "### Owner decisions"])
def test_restart_resets_count_and_discards_saved_work(tmp_path, monkeypatch, heading):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    directory = Path(cfg["state_dir"]) / "claims"
    for n in (1, 2):
        core.save(directory / f"past-{n}.json", dict(job=f"past-{n}", issue=1,
                  event=n, stage="finished", continued_from=old["head_commit"], cleaned=True))
    hub.issues[1]["body"] += "\n\n" + heading + "\nRestart: fresh"
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["restart_fresh"] and not claim.get("continued_from")
    assert command(claim["worktree"], "rev-parse", "HEAD") == old["base_commit"]
    service.poll(cfg, hub.client(), clock=lambda: 300)
    restarted = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    reapprove(hub)  # Same directive was dispatched: it is no longer new.
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    next_claim = newest_claim(cfg)
    assert not next_claim["restart_fresh"]
    assert next_claim["continued_from"] == restarted["head_commit"]
    assert next_claim["continuation_number"] == 1


@pytest.mark.parametrize("body,expected", [
    ("Restart: fresh", False), ("## Context\nRestart: fresh", False),
    ("## Owner decisions\nRestart: fresh ", False),
    ("## Owner decisions\n Restart: fresh", False),
    ("## Owner decisions\nRestart: Fresh", False),
    ("## Owner decisions\r\nRestart: fresh\r\n", True),
    ("### Owner decisions\nRestart: fresh\n## Elsewhere", True),
    ("## Owner decisions\n### Other\nRestart: fresh", True),
    ("### Owner decisions\n---\nRestart: fresh", False),
])
def test_exact_restart_directive(body, expected):
    assert service.restart_fresh(body) is expected


def test_undispatched_claim_does_not_consume_new_restart(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    hub.issues[1]["body"] += "\n\n## Owner decisions\nRestart: fresh"
    core.save(Path(cfg["state_dir"]) / "claims" / "undispatched.json", dict(
        job="undispatched", issue=1, event=1, stage="finished", task=hub.issues[1]["body"],
        cleaned=True))
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert newest_claim(cfg)["restart_fresh"]


def test_legacy_block_is_ignored_and_dispatch_explains_it(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    hub.issues[1]["body"] += "\n\n### Continue from\nCommit: " + "a" * 40
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert newest_claim(cfg)["continued_from"] == old["head_commit"]
    assert any("Continuing from the latest saved work; the pasted block is ignored."
               in c["body"] for c in hub.comments_by_issue[1])
    assert any(r["to"] == "dispatched" and r["job"] == newest_claim(cfg)["job"]
               for r in core.records(cfg, hub.comments_by_issue[1], 1))
    assert not hasattr(service, "continuation_commit")


def test_reviewed_current_work_is_delivered_without_pasted_id(tmp_path, monkeypatch):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    before = list(calls)
    settings(cfg, "runner_config", {"enabled": True, "declared_tier": "standard"})
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)["delivery_source"] == old["id"]
    assert calls == before
    assert [p[0] for p in publications] == ["push", "pr", "checker"]


@pytest.mark.parametrize("stage", ["checking", "auto_merge_enabled"])
@pytest.mark.parametrize("restart", [False, True])
def test_open_pr_reapproval_stops_old_loop_once_and_leaves_auto_merge(tmp_path, monkeypatch, restart, stage):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch, enabled=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    assert old["integration"]["pr"] == 9
    old["integration"]["stage"] = stage
    core.save(Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state"
              / "tasks" / (old["id"] + ".json"), old)
    original_request = type(hub.client()).request

    def request(self, method, path, data=None):
        if method == "GET" and path == "/pulls/9":
            return {"state": "open", "auto_merge": {"enabled": True}}
        return original_request(self, method, path, data)

    monkeypatch.setattr(type(hub.client()), "request", request)
    if restart:
        hub.issues[1]["body"] += "\n\n## Owner decisions\nRestart: fresh"
    before = list(publications)
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["error"] == "pr_open_reapproval" and claim["stage"] == "finished"
    comments = list(hub.comments_by_issue[1])
    service.poll(cfg, hub.client(), clock=lambda: 300)
    service.poll(cfg, hub.client(), clock=lambda: 400)
    assert publications == before and hub.comments_by_issue[1] == comments
    assert len(service.jobs(cfg)) == 1
    old, = service.jobs(cfg)
    assert old["delivery_retired"]
    assert old["integration"]["stage"] == stage
    notices = [c for c in comments if "This story has an open PR." in c["body"]]
    assert len(notices) == 1 and notices[0]["body"].startswith("Needs your decision:")


def test_automatic_continuation_uses_one_path_and_main_settings_without_count(tmp_path, monkeypatch):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    execute = runner.execute

    def rework(config, state, path, job, role):
        if role == "reviewer":
            job["status"] = "running_reviewer"
            runner.save_job(config, path, job)
            return fixtures.result("needs_changes", "reviewer")
        return execute(config, state, path, job, role)

    seen = []
    original = service.continuation

    def continuation(*args, **kwargs):
        if not kwargs.get("prepare"):
            seen.append(args[2]["primary"])
        else:
            assert args[2].get("review_base")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "continuation", continuation)
    monkeypatch.setattr(runner, "execute", rework)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    before = len(calls)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    assert seen == [False, True] and claim["primary"]
    assert "continuation_number" not in claim
    assert not job["assignment"].get("continuation") and job["round"] == 1
    assert len(calls) == before + 1  # The helper records worker calls only here.
    assert core.read(claim["config"])["integration"] == {"enabled": False, "declared_tier": "standard"}


def test_no_owner_instruction_requires_pasted_commit():
    texts = [p.read_text() for root in [ROOT / "docs", ROOT / "starter-kit"]
             for p in root.rglob("*") if p.is_file() and not (root == ROOT / "docs" and p.relative_to(root).parts[0] == "design")]
    texts += list(core.STOP_REASONS.values())
    texts += [inspect.getsource(core.record_body)]
    from runner.integration import REASONS
    texts += list(REASONS.values())
    instruction = re.compile(r"paste[^.\n]*(?:commit|continue from)|copy[^.\n]*(?:commit (?:ID|SHA)|continue from)|"
                             r"(?:commit (?:ID|SHA)|continue from)[^.\n]*(?:paste|copy)", re.I)
    assert not [(m.group()) for text in texts for m in instruction.finditer(text)]


def parse_form_yaml(text):
    """Parse the form's YAML subset without adding a development dependency.

    Supports indented maps/sequences, JSON quoted scalars, plain scalars, bools
    and literal blocks. Rejects ambiguous colon-space inside a plain scalar.
    """
    lines = text.splitlines()
    def scalar(value):
        if value.startswith('"'):
            return json.loads(value)
        assert not value.startswith(("'", "[", "{", "&", "*", "!", "|", ">"))
        assert " #" not in value
        assert ": " not in value, "plain YAML scalar contains a mapping separator"
        return {"true": True, "false": False}.get(value, value)

    def parse(index, indent):
        sequence = lines[index].lstrip().startswith("- ")
        result = [] if sequence else {}
        while index < len(lines):
            line = lines[index]
            width = len(line) - len(line.lstrip())
            if width < indent:
                break
            assert width == indent
            content = line[indent:]
            if sequence:
                assert content.startswith("- ")
                item = {}
                result.append(item)
                content = content[2:]
                key, value = content.split(":", 1)
                assert re.fullmatch(r"[a-z_]+", key)
                item[key] = scalar(value.strip())
                index += 1
                if index < len(lines) and len(lines[index]) - len(lines[index].lstrip()) > indent:
                    tail, index = parse(index, indent + 2)
                    item.update(tail)
                continue
            key, value = content.split(":", 1)
            assert re.fullmatch(r"[a-z_]+", key) and key not in result
            value = value.strip()
            index += 1
            if value == "|":
                block = []
                while index < len(lines) and len(lines[index]) - len(lines[index].lstrip()) > indent:
                    block.append(lines[index][indent + 2:])
                    index += 1
                result[key] = "\n".join(block) + "\n"
            elif value:
                result[key] = scalar(value)
            else:
                result[key], index = parse(index, indent + 2)
        return result, index
    result, end = parse(0, 0)
    assert end == len(lines)
    return result


def test_story_form_parses_and_has_no_saved_work_field():
    text = (ROOT / "starter-kit/.github/ISSUE_TEMPLATE/story.yml").read_text()
    form = parse_form_yaml(text)
    assert set(form) == {"name", "description", "title", "body"}
    assert form["name"] == "Story" and len(form["body"]) == 11
    fields = {row["id"]: row for row in form["body"] if "id" in row}
    assert fields["story"]["validations"]["required"] is True
    assert fields["criteria"]["attributes"]["value"] == "- AC-1:\n- AC-2:\n"
    assert "continue_from" not in fields
    assert "Restart: fresh" in fields["owner_decisions"]["attributes"]["description"]
    with pytest.raises(AssertionError, match="plain YAML scalar"):
        parse_form_yaml(text.replace('description: "Optional.', 'description: Optional.'))


@pytest.mark.parametrize("corruption", ["assignment", "missing_head", "missing_record"])
def test_newest_saved_integrity_failure_never_starts_fresh(tmp_path, monkeypatch, corruption):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    path = (Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state"
            / "tasks" / (old["id"] + ".json"))
    if corruption == "assignment":
        old["assignment"]["task_text"] += "Changed"
    elif corruption == "missing_head":
        old["head_commit"] = "a" * 40
    else:
        hub.comments_by_issue[1] = [c for c in hub.comments_by_issue[1]
                                    if '"continuation": true' not in c["body"]]
    core.save(path, old)
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["error"] == "continuation_commit_unrecorded"
    assert not Path(claim["worktree"]).exists()


def test_newer_job_without_saved_head_is_skipped(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    newer = dict(old, id="newer-unsaved", created_at="2099-01-01T00:00:00+00:00")
    newer.pop("head_commit")
    core.save(Path(cfg["state_dir"]) / "jobs" / newer["id"] / "runner-state"
              / "tasks" / (newer["id"] + ".json"), newer)
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert newest_claim(cfg)["continuation_source"] == old["id"]


def test_removing_dispatched_restart_directive_preserves_delivery_review(tmp_path, monkeypatch):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    hub.issues[1]["body"] += "\n\n## Owner decisions\nRestart: fresh"
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    hub.issues[1]["body"] = hub.issues[1]["body"].replace("Restart: fresh", "")
    settings(cfg, "runner_config", {"enabled": True, "declared_tier": "standard"})
    before = list(calls)
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)["delivery_source"] == old["id"] and calls == before


def test_restart_text_outside_decisions_changes_reviewed_scope(tmp_path, monkeypatch):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    hub.issues[1]["body"] += "\n\n## Notes\n"
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    hub.issues[1]["body"] += "Restart: fresh"
    settings(cfg, "runner_config", {"enabled": True, "declared_tier": "standard"})
    before = list(calls)
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    assert not claim["restart_fresh"]
    assert claim["delivery_resume_fallback"] == "approved_scope_changed"
    assert [role for _, role in calls[len(before):]] == ["worker", "reviewer"]


def test_undispatched_restart_does_not_hide_existing_saved_work(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    hub.counter += 1
    core.save(Path(cfg["state_dir"]) / "claims" / "stopped-restart.json", dict(
        job="stopped-restart", issue=1, event=hub.counter, stage="finished",
        task=hub.issues[1]["body"] + "\n\n## Owner decisions\nRestart: fresh",
        restart_fresh=True, cleaned=True))
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert not claim["restart_fresh"]
    assert claim["continued_from"] == old["head_commit"]


@pytest.mark.parametrize("proof", ["config", "was_dispatched", "stage"])
def test_local_dispatched_claim_counts_when_public_record_is_missing(tmp_path, monkeypatch, proof):
    from test_lifecycle import configured_service
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    directory = Path(cfg["state_dir"]) / "claims"
    text = "## Owner decisions\nRestart: fresh"
    previous = dict(job="past", issue=1, event=1, task=text, stage="finished")
    previous[proof] = {"config": cfg["runner_config"], "was_dispatched": True,
                       "stage": "dispatched"}[proof]
    core.save(directory / "past.json", previous)
    claim = dict(job="current", issue=1, event=2, task=text, primary=False)
    service.continuation(cfg, hub.client(), claim, directory)
    assert claim["restart_fresh"] is False
