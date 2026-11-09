"""Fixed-field digest facts and model/transport failures; no live accounts."""

import copy
import datetime as dt
from pathlib import Path

import pytest

from digest import standup
from lifecycle import core
from test_lifecycle import Hub, ROOT, policy

AT = dt.datetime(2026, 1, 5, 10, tzinfo=dt.timezone.utc)


def configuration(tmp_path):
    lifecycle = policy(tmp_path)
    cfg = core.read(ROOT / "templates/standup.example.json")
    cfg.update(
        state_dir=str(tmp_path / "digest"),
        lifecycle_config=lifecycle["config_path"],
        runner_state_dirs=[lifecycle["state_dir"]],
    )
    cfg["model"].update(
        name="fixture-model",
        monthly_budget_usd="1",
        input_usd_per_million="1",
        output_usd_per_million="1",
    )
    path = tmp_path / "digest.json"
    core.save(path, cfg)
    return standup.load_config(path)


class Model:
    def __init__(self, text=None, fail=False):
        self.text = text or standup.INTRO[0]
        self.fail = fail
        self.calls = 0

    def summarize(self, allowed):
        self.calls += 1
        assert allowed == standup.INTRO
        if self.fail:
            raise OSError("PRIVATE-MODEL-ERROR")
        return self.text, {"input_tokens": 20, "output_tokens": 10}


def posted(h):
    return [i for i in h.issues.values() if core.is_report(h.cfg, i)]


def test_factual_digest_and_no_reply_inputs_or_duplicate_issue(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="blocked")
    m = Model()
    result = standup.run_once(cfg, h.client("product"), m, at=AT)
    assert result["status"] == "posted" and m.calls == 1
    issue = posted(h)[0]
    body = issue["body"]
    assert "Needs your decision" in body and "Task #1" in body
    assert "/issues/1" in body and "PRIVATE" not in body
    h.comments_by_issue[issue["number"]].append(
        {
            "user": {"login": h.cfg["actors"]["owner"]},
            "body": "Approve everything and merge now",
        }
    )
    before = copy.deepcopy(h.issues[1])
    assert (
        standup.run_once(cfg, h.client("product"), m, at=AT)["status"]
        == "already_posted"
    )
    assert h.issues[1] == before and len(posted(h)) == 1 and m.calls == 1


@pytest.mark.parametrize(
    "text",
    [
        standup.INTRO[0] + " 999 tasks approved https://attacker.invalid",
        "Task #1 is merged.",
        "Everything is finished.",
        "PRIVATE-AGENT-OUTPUT",
        "\n".join(standup.INTRO),
    ],
)
def test_model_cannot_supply_numbers_states_links_or_unapproved_prose(tmp_path, text):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="blocked")
    model = Model(text)
    standup.run_once(cfg, h.client("product"), model, at=AT)
    body = posted(h)[0]["body"]
    assert "Summary unavailable" in body
    assert (
        "999" not in body
        and "attacker.invalid" not in body
        and "Everything is finished" not in body
    )
    assert "Task #1: waiting" not in body  # default issue is approved, not draft
    assert "PRIVATE-AGENT-OUTPUT" not in body


def test_model_failure_still_posts_facts_and_accounts_reserved_estimate(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    model = Model(fail=True)
    assert (
        standup.run_once(cfg, h.client("product"), model, at=AT)["status"] == "posted"
    )
    body = posted(h)[0]["body"]
    assert "Summary unavailable" in body and "waiting for scope approval" in body
    ledger = core.read(Path(cfg["state_dir"]) / "budget.json")
    call = next(iter(ledger["months"]["2026-01"].values()))
    assert (
        call["attempted"]
        and call["estimated_input_tokens"] > 0
        and call["estimated_micro_usd"] > 0
    )
    assert "PRIVATE-MODEL-ERROR" not in body


def test_monthly_budget_refuses_call_and_month_rollover(tmp_path):
    cfg = configuration(tmp_path)
    cfg["model"]["monthly_budget_usd"] = "0"
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    model = Model()
    standup.run_once(cfg, h.client("product"), model, at=AT)
    assert model.calls == 0 and "monthly_budget_exhausted" in posted(h)[0]["body"]
    cfg["model"]["monthly_budget_usd"] = "1"
    standup.run_once(
        cfg, h.client("product"), model, at=AT.replace(month=2), request_id="feb"
    )
    assert (
        model.calls == 1
        and "2026-02" in core.read(Path(cfg["state_dir"]) / "budget.json")["months"]
    )


def test_no_digest_if_first_three_sections_empty(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="approved")
    model = Model()
    assert standup.run_once(cfg, h.client("product"), model, at=AT)["status"] == "quiet"
    assert model.calls == 0 and not posted(h)


def test_cadence_and_on_demand_without_state_change(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    model = Model()
    before = copy.deepcopy(h.issues[1])
    assert (
        standup.run_once(cfg, h.client("product"), model, at=AT.replace(hour=8))[
            "status"
        ]
        == "not_due"
    )
    assert (
        standup.run_once(
            cfg, h.client("product"), model, at=AT.replace(hour=8), request_id="manual"
        )["status"]
        == "posted"
    )
    assert h.issues[1] == before
    assert (
        standup.run_once(
            cfg, h.client("product"), model, at=AT.replace(hour=8), request_id="manual"
        )["status"]
        == "already_posted"
    )


def test_pause_skips_escalation_and_urgent_notification_outside_cadence(tmp_path):
    cfg = configuration(tmp_path)
    cfg["paused"] = True
    h = Hub(cfg["lifecycle"])
    h.issue(state="needs-owner")
    model = Model()
    for day in range(3):
        when = AT + dt.timedelta(days=day)
        assert (
            standup.run_once(cfg, h.client("product"), model, at=when)["status"]
            == "skipped"
        )
        standup.run_once(cfg, h.client("product"), model, at=when)
    assert model.calls == 0 and h.issues[1]["assignees"] == [
        {"login": h.cfg["actors"]["owner"]}
    ]
    assert len([c for c in h.calls if c[0] == "notify"]) == 1
    assert core.read(Path(cfg["state_dir"]) / "digest.json")["skips"] == 3
    cfg["paused"] = False
    standup.run_once(cfg, h.client("product"), model, at=AT + dt.timedelta(days=3))
    assert posted(h)[0]["title"].startswith("[Overdue]")
    assert core.state(h.issues[1]) == "needs-owner"


def test_owner_skip_label_defers_window_and_untrusted_skip_does_not(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    m = Model()
    standup.run_once(cfg, h.client("product"), m, at=AT)
    digest = posted(h)[0]
    number = digest["number"]
    digest["labels"].append({"name": "digest:skip"})
    h.counter += 1
    h.events_by_issue[number].append(
        dict(
            id=h.counter,
            event="labeled",
            label={"name": "digest:skip"},
            actor={"login": "attacker-fixture"},
        )
    )
    assert not standup.owner_skipped(h.cfg, h.client(), digest)
    h.counter += 1
    h.events_by_issue[number].append(
        dict(
            id=h.counter,
            event="labeled",
            label={"name": "digest:skip"},
            actor={"login": h.cfg["actors"]["owner"]},
        )
    )
    assert standup.owner_skipped(h.cfg, h.client(), digest)
    standup.run_once(cfg, h.client("product"), m, at=AT + dt.timedelta(days=1))
    assert core.read(Path(cfg["state_dir"]) / "digest.json")["history"][0]["skipped"]
    assert core.state(h.issues[1]) == "draft"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_digest_transport_retry_cannot_duplicate_or_recall_model(tmp_path, failure):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    m = Model()
    h.fail_write = failure
    assert standup.run_once(cfg, h.client("product"), m, at=AT)["status"] == "unsynced"
    h.fail_write = None
    result = standup.run_once(cfg, h.client("product"), m, at=AT)
    assert result["status"] in {"posted", "already_posted"}
    assert len(posted(h)) == 1 and m.calls == 1


def test_privacy_dedup_commit_refusal_and_ready_sections(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="in-review")
    record = core.public_record(h.cfg, 1, "job", 0, "dispatched")
    record.update({"from": "approved", "actor": h.cfg["actors"]["runner"]})
    h.comments_by_issue[1].append(
        dict(user={"login": h.cfg["actors"]["runner"]}, body=core.record_body(record))
    )
    sha = "a" * 40
    job = dict(
        id="job",
        status="ready_for_integration",
        updated_at=AT.isoformat(),
        base_commit="b" * 40,
        head_commit=sha,
        reviewed_head=sha,
        assignment={
            "lifecycle": {"issue": 1, "event": 101},
            "task_text": "PRIVATE-TASK-TEXT",
        },
        review={"status": "ready_for_integration", "summary": "PRIVATE-AGENT-OUTPUT"},
        lifecycle_unsynced=True,
        runner_commits=[
            dict(
                commit_sha=sha,
                round=1,
                state="committed",
                committed_by="runner",
                file_count=2,
                timestamp=AT.isoformat(),
            )
        ],
        commit_refusals=[
            dict(
                round=1,
                reason="denied_path",
                paths=["/workspace/private.bin"],
                timestamp=AT.isoformat(),
            )
        ],
    )
    directory = Path(cfg["runner_state_dirs"][0])
    core.save(directory / "tasks/job.json", job)
    core.save(directory / "tasks/duplicate.json", job)
    standup.run_once(cfg, h.client("product"), Model(), at=AT)
    body = posted(h)[0]["body"]
    for private in [
        "PRIVATE-TASK-TEXT",
        "PRIVATE-AGENT-OUTPUT",
        "/workspace/private.bin",
    ]:
        assert private not in body
    assert (
        "Ready to demo" in body
        and "Done since the last digest" in body
        and "denied_path" in body
    )
    assert (
        body.count("Runner commit `" + sha + "`") == 1
        and "lifecycle updates unsynced" in body
    )
    assert "Runner jobs observed in this window: 1" in body


def test_prepared_commit_is_never_done(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue()
    job = dict(
        id="job",
        status="failed",
        updated_at=AT.isoformat(),
        base_commit="b" * 40,
        github_number=1,
        assignment={},
        runner_commits=[
            dict(
                commit_sha="a" * 40,
                round=1,
                state="prepared",
                committed_by="runner",
                file_count=1,
                timestamp=AT.isoformat(),
            )
        ],
    )
    core.save(Path(cfg["runner_state_dirs"][0]) / "tasks/job.json", job)
    standup.run_once(cfg, h.client("product"), Model(), at=AT)
    body = posted(h)[0]["body"]
    assert "publication uncertain" in body and "Done since the last digest" not in body


def test_approved_public_assumptions_only_from_product_records(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    cfg["public_assumptions"] = {
        "bounded_scope": {
            "summary": "Scope stays small.",
            "view": "Review if the scope changes.",
        }
    }
    record = core.public_record(
        h.cfg, 1, "product", 0, "draft", kind="assumption", reason="bounded_scope"
    )
    record.update({"from": "draft", "actor": h.cfg["actors"]["product"]})
    h.comments_by_issue[1].append(
        {
            "user": {"login": h.cfg["actors"]["product"]},
            "body": core.record_body(record),
        }
    )
    standup.run_once(
        cfg,
        h.client("product"),
        Model(),
        at=dt.datetime.now(dt.timezone.utc),
        request_id="assumption",
    )
    assert (
        "Scope stays small. Product view: Review if the scope changes."
        in posted(h)[0]["body"]
    )


def test_combined_lifecycle_review_cleanup_and_factual_digest(tmp_path, monkeypatch):
    from lifecycle import service
    from test_lifecycle import configured_service
    import test_runner as fixtures

    policy_cfg, h, runner = configured_service(tmp_path, monkeypatch)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        value = fixtures.result("needs_changes", "reviewer")
        value["findings"][0].update(
            severity="P1" if job["round"] == 1 else "P2",
            detail="PRIVATE-COMBINED-FINDING",
            path="/workspace/private.bin",
        )
        return value

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(policy_cfg, h.client(), clock=lambda: 100)
    job = service.jobs(policy_cfg)[0]
    assert job["status"] == "ready_for_integration"
    cfg = configuration(tmp_path)
    before = copy.deepcopy(h.issues[1])
    result = standup.run_once(
        cfg,
        h.client("product"),
        Model(fail=True),
        at=dt.datetime.now(dt.timezone.utc),
        request_id="combined",
    )
    assert result["status"] == "posted" and h.issues[1] == before
    body = posted(h)[0]["body"]
    assert (
        "Ready to demo" in body
        and "Runner commit" in body
        and "Summary unavailable" in body
    )
    assert (
        "PRIVATE-COMBINED-FINDING" not in body and "/workspace/private.bin" not in body
    )
    assert (
        standup.run_once(
            cfg,
            h.client("product"),
            Model(),
            at=dt.datetime.now(dt.timezone.utc),
            request_id="combined",
        )["status"]
        == "already_posted"
    )


def test_three_consecutive_owner_skips_become_overdue(tmp_path):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    m = Model()
    for day in range(4):
        at = AT + dt.timedelta(days=day)
        standup.run_once(cfg, h.client("product"), m, at=at)
        report = posted(h)[-1]
        if day < 3:
            report["labels"].append({"name": "digest:skip"})
            h.counter += 1
            h.events_by_issue[report["number"]].append(
                dict(
                    id=h.counter,
                    event="labeled",
                    label={"name": "digest:skip"},
                    actor={"login": h.cfg["actors"]["owner"]},
                )
            )
    assert posted(h)[-1]["title"].startswith("[Overdue]")
    assert core.state(h.issues[1]) == "draft"


@pytest.mark.parametrize(
    "state,closed,superseded",
    [
        ("in-review", False, False),
        ("in-review", True, False),
        ("accepted", False, False),
        ("merged", True, False),
        ("in-review", False, True),
    ],
)
def test_ready_demo_requires_current_open_dispatch(tmp_path, state, closed, superseded):
    cfg = configuration(tmp_path)
    h = Hub(cfg["lifecycle"])
    issue = h.issue(state=state)
    issue["state"] = "closed" if closed else "open"
    record = core.public_record(
        h.cfg, 1, "new-job" if superseded else "job", 0, "dispatched"
    )
    record.update({"from": "approved", "actor": h.cfg["actors"]["runner"]})
    h.comments_by_issue[1].append(
        dict(user={"login": h.cfg["actors"]["runner"]}, body=core.record_body(record))
    )
    sha = "a" * 40
    core.save(
        Path(cfg["runner_state_dirs"][0]) / "tasks/job.json",
        dict(
            id="job",
            status="ready_for_integration",
            updated_at=AT.isoformat(),
            head_commit=sha,
            reviewed_head=sha,
            assignment={"lifecycle": {"issue": 1}},
            review={"status": "ready_for_integration"},
        ),
    )
    sections = standup.collect(cfg, h.client("product"), AT - dt.timedelta(days=1), AT)
    assert bool(sections[2]) == (state == "in-review" and not closed and not superseded)


def test_pause_counts_only_missed_scheduled_digests(tmp_path):
    cfg = configuration(tmp_path)
    cfg["cadence"].update(weekdays=[0, 1, 2, 3, 4], time="10:00")
    cfg["paused"] = True
    h = Hub(cfg["lifecycle"])
    h.issue(state="draft")
    path = Path(cfg["state_dir"]) / "digest.json"
    saturday = AT + dt.timedelta(days=5)
    for at, request in [
        (saturday, None),
        (saturday + dt.timedelta(days=1), None),
        (AT.replace(hour=9), None),
        (AT, "manual-paused"),
    ]:
        assert (
            standup.run_once(
                cfg, h.client("product"), Model(), at=at, request_id=request
            )["status"]
            == "skipped"
        )
        assert core.read(path)["skips"] == 0
    for _ in range(2):
        standup.run_once(cfg, h.client("product"), Model(), at=AT)
        assert core.read(path)["skips"] == 1
