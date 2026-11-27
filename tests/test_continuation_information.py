"""Information-aware continuation limits, using synthetic saved-work evidence."""

from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle_continuation import newest_claim, saved_story
from test_one_continuation import reapprove


def continue_story(cfg, hub, tick):
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: tick)
    return newest_claim(cfg)


def system_stop(cfg, hub):
    """Replace the saved stop with an authenticated system-stop record."""
    old = max(service.jobs(cfg), key=lambda j: j["assignment"]["lifecycle"]["event"])
    old["blocked_reason"] = {"reason_code": "worker_story_tests_unavailable",
                             "reason": core.STOP_REASONS["worker_story_tests_unavailable"]}
    core.save(Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state"
              / "tasks" / (old["id"] + ".json"), old)
    stop = next(r for r in reversed(core.records(cfg, hub.comments_by_issue[1], 1))
                if r["job"] == old["id"] and r.get("continuation"))
    replacement = core.public_record(cfg, 1, old["id"], stop["round"], stop["to"],
                                     commit=old["head_commit"], event=stop["event"],
                                     reason="worker_story_tests_unavailable", continuation=True)
    replacement.update(actor=stop["actor"], **{"from": stop["from"]})
    for comment in hub.comments_by_issue[1]:
        if stop["key"] in comment["body"]:
            comment["body"] = core.record_body(replacement)


def test_changed_story_reapprovals_all_continue(tmp_path, monkeypatch):
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    for tick in range(200, 600, 100):
        hub.issues[1]["body"] += f"\nAnswer {tick}."
        claim = continue_story(cfg, hub, tick)
        assert claim.get("continued_from") and not claim.get("continuation_error")
        assert claim["continuation_number"] == 1


def test_system_stop_reapprovals_do_not_count(tmp_path, monkeypatch):
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    for tick in range(200, 600, 100):
        system_stop(cfg, hub)
        claim = continue_story(cfg, hub, tick)
        assert claim.get("continued_from") and not claim.get("continuation_error")
        assert claim["previous_stop_reason"] == "worker_story_tests_unavailable"
        assert claim["continuation_number"] == 1


def test_text_change_after_one_resets_count(tmp_path, monkeypatch):
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    assert continue_story(cfg, hub, 200)["continuation_number"] == 1
    hub.issues[1]["body"] += "\nOwner supplied the answer."
    assert continue_story(cfg, hub, 300)["continuation_number"] == 1
    assert continue_story(cfg, hub, 400)["continuation_number"] == 1
    assert continue_story(cfg, hub, 500)["continuation_number"] == 2
    assert continue_story(cfg, hub, 600)["continuation_error"] == "continuation_limit"


def test_uncounted_at_two_keeps_runner_number_two(tmp_path, monkeypatch):
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    assert continue_story(cfg, hub, 200)["continuation_number"] == 1
    assert continue_story(cfg, hub, 300)["continuation_number"] == 2
    system_stop(cfg, hub)
    claim = continue_story(cfg, hub, 400)
    assert claim.get("continued_from") and not claim.get("continuation_error")
    assert claim["continuation_number"] == 2


def budget_case(tmp_path, monkeypatch, history, *, task=None):
    """Exercise counting against genuine saved work and durable claim history."""
    cfg, hub, _, old = saved_story(tmp_path, monkeypatch)
    directory = Path(cfg["state_dir"]) / "claims"
    original = newest_claim(cfg)["task"]
    # Seed the synthetic sequence with an uncounted owner claim in its own scope.
    core.save(directory / "scope-seed.json", dict(
        job="scope-seed", issue=1, event=999, stage="finished",
        task=history[0].get("task") or original))
    for index, fields in enumerate(history):
        past = dict(job=f"past-{index}", issue=1, event=1000 + index,
                    stage="finished", task=original, continued_from=old["head_commit"],
                    previous_stop_reason="worker_reported_blocked")
        past.update(fields)
        for key in [key for key, value in past.items() if value is None]:
            del past[key]
        core.save(directory / (past["job"] + ".json"), past)
    claim = dict(job="current", issue=1, event=2000, primary=False,
                 task=original if task is None else task)
    service.continuation(cfg, hub.client(), claim, directory)
    return claim


@pytest.mark.parametrize("middle", [
    {"primary": True},
    {"readiness_stopped": True},
    {"continued_from": None, "error": "continuation_limit"},
    {"continued_from": None, "stage": "claimed"},
    {"previous_stop_reason": "worker_story_tests_unavailable"},
])
def test_every_previous_claim_supplies_comparison_text(tmp_path, monkeypatch, middle):
    claim = budget_case(tmp_path, monkeypatch, [
        {"task": "old"}, dict(middle, task="new"), {"task": "new"},
    ], task="new")
    assert claim["continuation_number"] == 2


@pytest.mark.parametrize("missing", ["task", "previous_stop_reason", "both"])
def test_older_claims_conservatively_count(tmp_path, monkeypatch, missing):
    fields = {key: None for key in ("task", "previous_stop_reason")
              if key == missing or missing == "both"}
    if missing == "task":
        fields["previous_stop_reason"] = "worker_story_tests_unavailable"
    claim = budget_case(tmp_path, monkeypatch, [fields, fields])
    assert claim["continuation_error"] == "continuation_limit"


def test_stop_kind_reclassification_recomputes_history(tmp_path, monkeypatch):
    monkeypatch.setitem(core.STOP_KINDS, "worker_reported_blocked", "Waiting on the system")
    claim = budget_case(tmp_path, monkeypatch, [{}, {}, {}])
    assert claim["continuation_number"] == 1
    assert "continuation_error" not in claim


def test_service_refusal_does_not_reclassify_recorded_saved_stop(tmp_path, monkeypatch):
    fields = dict(previous_stop_reason="worker_story_tests_unavailable",
                  error="continuation_limit", continuation_error="continuation_limit")
    claim = budget_case(tmp_path, monkeypatch, [fields, fields, fields])
    assert claim["continuation_number"] == 1


@pytest.mark.parametrize("kind", ["Needs a fix", "Needs your decision", "Needs your merge"])
def test_every_non_system_kind_counts(tmp_path, monkeypatch, kind):
    monkeypatch.setitem(core.STOP_KINDS, "worker_reported_blocked", kind)
    claim = budget_case(tmp_path, monkeypatch, [{}, {}])
    assert claim["continuation_error"] == "continuation_limit"


@pytest.mark.parametrize("text", [
    "\r\n## Story\r\nTwo words.\r\n\r\n## Owner decisions\r\nAnswer\r\n",
    " \t\n## Story\nTwo words.\n\n## Owner decisions\nAnswer\n\t ",
    "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n\n### Continue from\nCommit: " + "a" * 40,
    "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n\n### Saved work (optional)\n\n_No response_\n",
    "## Story\nTwo words.\n\n## Owner decisions\nAnswer\nRestart: fresh\n",
])
def test_continuation_metadata_and_outer_whitespace_do_not_reset(tmp_path, monkeypatch, text):
    original = "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n"
    if "Restart: fresh" in text:
        original += "Restart: fresh\n"
    claim = budget_case(tmp_path, monkeypatch, [
        {"task": original, "was_dispatched": True},
        {"task": original, "was_dispatched": True},
    ], task=text)
    assert claim["continuation_error"] == "continuation_limit"


@pytest.mark.parametrize("text", [
    "## Story\nTwo  words.\n\n## Owner decisions\nAnswer\n",
    "## Story\nTwo words.\n\n## Owner decisions\nanswer\n",
    "## Story\nTwo words. \n\n## Owner decisions\nAnswer\n",
    "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n\n### continue from\nCommit: " + "a" * 40,
    "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n\n## Notes\nRestart: fresh\n",
])
def test_internal_whitespace_and_case_are_information(tmp_path, monkeypatch, text):
    original = "## Story\nTwo words.\n\n## Owner decisions\nAnswer\n"
    claim = budget_case(tmp_path, monkeypatch, [{"task": original}, {"task": original}], task=text)
    assert claim["continuation_number"] == 1
    assert "continuation_error" not in claim


def test_edit_reverted_before_approval_still_counts(tmp_path, monkeypatch):
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    continue_story(cfg, hub, 200)
    continue_story(cfg, hub, 300)
    original = hub.issues[1]["body"]
    hub.issues[1]["body"] += "\nTemporary edit."
    hub.issues[1]["body"] = original
    assert continue_story(cfg, hub, 400)["continuation_error"] == "continuation_limit"


def test_limit_remedy_is_exact():
    assert core.STOP_REASONS["continuation_limit"] == (
        "This story has reached its two-continuation limit without new information. "
        "Owner: add the missing information under Owner decisions and re-approve, "
        "restart fresh (add Restart: fresh under Owner decisions and re-approve), "
        "or split the story."
    )
