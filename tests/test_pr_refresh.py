"""Same-job PR refresh: offline Git repositories and synthetic GitHub only."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lifecycle import core, service
from test_lifecycle_delivery import story
from test_lifecycle_fetch import command
import test_runner as fixtures

REAL_REFRESH_PHASE = fixtures.runner.execute_phase


@pytest.fixture
def refresh_story(tmp_path, monkeypatch):
    def build(owner=False):
        cfg, hub, runner, calls, publications = story(
            tmp_path, monkeypatch, enabled=True
        )
        service.poll(cfg, hub.client(), clock=lambda: 100)
        (job,) = service.jobs(cfg)
        root = Path(cfg["state_dir"]) / "jobs" / job["id"]
        config = runner.load_config(root / "config.json")
        path = root / "runner-state/tasks" / (job["id"] + ".json")
        repo = {"full_name": cfg["repository"]}
        pr = {
            "number": 9,
            "node_id": "synthetic-pr",
            "state": "open",
            "merged": False,
            "body": job["integration"]["body"],
            "head": {
                "sha": job["reviewed_head"],
                "ref": config["branch"],
                "repo": repo,
            },
            "base": {"sha": job["base_commit"], "ref": "main", "repo": repo},
            "mergeable_state": "behind",
            "auto_merge": {"enabled": True},
        }
        hub.prs[9] = pr
        hub.event(1, "pr-open", "workflow")
        if owner:
            job.update(
                status="needs_owner",
                blocked_reason={
                    "reason_code": "owner_merge_required",
                    "reason": "Owner merge.",
                },
            )
            job["integration"].update(stage="owner", reason="owner_merge_required")
            runner.save_job(config, path, job)
        else:
            job["integration"]["stage"] = "auto_merge_enabled"
            runner.save_job(config, path, job)
        events = []

        class API:
            role = "runner"

            def __init__(self):
                self.cfg = cfg
                self.base = hub.client()

            def issue(self, n):
                return self.base.issue(n)

            def comments(self, n):
                return self.base.comments(n)

            def events(self, n):
                return self.base.events(n)

            def set_state(self, n, state):
                return self.base.set_state(n, state)

            def notify(self, n):
                return None

            def request(self, method, endpoint, data=None):
                if method != "GET":
                    events.append((method, endpoint))
                if method == "PATCH" and endpoint == "/pulls/9":
                    pr["body"] = data["body"]
                    return deepcopy(pr)
                if endpoint == "/pulls/9/reviews":
                    return {}
                if method == "GET" and endpoint == "/":
                    return {"allow_auto_merge": True}
                return self.base.request(method, endpoint, data)

            def push(self, tool, settings, head):
                events.append(("push", head))
                pr["head"]["sha"] = head

            def enable_auto_merge(self, value):
                events.append(("enable", value["head"]["sha"]))
                return True

            def disable_auto_merge(self, value):
                events.append(("disable", value["number"]))
                pr["auto_merge"] = None
                return True

        api = API()
        reviews = []

        def review(settings, state, job_path, view, role):
            assert role == "reviewer"
            assert view["status"] == job["status"] and view["round"] == job["round"]
            assert settings.get("_refresh_context")
            reviews.append((view["review_base"], view["head_commit"], str(state)))
            return fixtures.result("ready_for_integration", "reviewer")

        monkeypatch.setattr(runner, "execute_phase", review)

        def target(conflict=False):
            source = Path(cfg["repository_root"])
            (source / ("delivery.txt" if conflict else "target.txt")).write_text(
                "target advance\n"
            )
            command(source, "add", ".")
            command(source, "commit", "-qm", "advance target")
            tip = command(source, "rev-parse", "HEAD")
            pr["base"]["sha"] = tip
            return tip

        tip = target()
        return SimpleNamespace(
            cfg=cfg,
            hub=hub,
            runner=runner,
            config=config,
            path=path,
            job=job,
            api=api,
            pr=pr,
            tip=tip,
            target=target,
            events=events,
            reviews=reviews,
            calls=calls,
        )

    return build


def refresh(ctx, fetch=None):
    return ctx.runner.integration_api().refresh(
        ctx.runner.self_module(),
        ctx.config,
        ctx.path,
        ctx.job,
        ctx.cfg,
        ctx.api,
        deepcopy(ctx.pr),
        fetch or (lambda: ctx.tip),
    )


@pytest.mark.parametrize("owner", [False, True])
def test_refresh_same_job_new_head_tests_review_and_one_record(refresh_story, owner):
    c = refresh_story(owner)
    old, round_ = c.job["head_commit"], c.job["round"]
    before = core.records(c.cfg, c.hub.comments_by_issue[1], 1)
    assert refresh(c)
    head = c.job["head_commit"]
    assert head != old and command(c.config["worktree"], "rev-parse", "HEAD^1") == old
    assert command(c.config["worktree"], "rev-parse", "HEAD^2") == c.tip
    assert c.job["round"] == round_ and not c.job.get("continuation")
    assert c.reviews[0][:2] == (c.tip, head)
    assert "refresh-1" in c.job["review_session"]
    assert c.job["verification_runs"][-1]["status"] == "passed"
    assert c.job["verification_runs"][-1]["head"] == head
    assert "Tested head: " + head in c.pr["body"]
    after = core.records(c.cfg, c.hub.comments_by_issue[1], 1)
    new = [r for r in after if r["key"] not in {v["key"] for v in before}]
    assert len(new) == 1 and new[0]["kind"] == "review" and new[0]["commit"] == head
    assert new[0]["pr"] == 9 and new[0]["to"] == ("needs-owner" if owner else "pr-open")
    assert c.events.index(("disable", 9)) < next(
        i for i, v in enumerate(c.events) if v[0] == "push"
    )
    assert c.job["integration"]["stage"] == ("owner" if owner else "created")
    if owner:
        assert any(
            "Refreshed and re-reviewed at " + head + "." in row["body"]
            for row in c.hub.comments_by_issue[1]
        )


@pytest.mark.parametrize("abort_fails", [False, True])
def test_conflict_refresh_aborts_cleanly_or_fails_uncertain(
    refresh_story, monkeypatch, abort_fails
):
    c = refresh_story()
    old, body = c.job["head_commit"], c.pr["body"]
    c.tip = c.target(conflict=True)
    real = c.runner.trusted_git

    def command_(config, *args, **kwargs):
        if abort_fails and args == ("merge", "--abort"):
            raise RuntimeError("synthetic abort failure")
        return real(config, *args, **kwargs)

    monkeypatch.setattr(c.runner, "trusted_git", command_)
    assert refresh(c)
    assert c.job["blocked_reason"]["reason_code"] == (
        "publication_uncertain" if abort_fails else "continuation_conflict"
    )
    assert not any(e[0] == "push" for e in c.events) and c.pr["body"] == body
    if not abort_fails:
        assert command(c.config["worktree"], "rev-parse", "HEAD") == old
        assert command(c.config["worktree"], "status", "--porcelain") == ""
        assert any(
            "target branch now conflicts" in x["body"]
            for x in c.hub.comments_by_issue[1]
        )


@pytest.mark.parametrize("failure", ["tests", "review"])
def test_failed_refresh_restores_pr_head_without_push(
    refresh_story, monkeypatch, failure
):
    c = refresh_story()
    old, body = c.job["head_commit"], c.pr["body"]
    if failure == "tests":
        monkeypatch.setattr(
            c.runner, "verify_required_tests", lambda *a: {"status": "failed"}
        )
    else:
        monkeypatch.setattr(
            c.runner,
            "execute_phase",
            lambda *a: fixtures.result("needs_changes", "reviewer"),
        )
    assert refresh(c)
    assert c.job["blocked_reason"]["reason_code"] == (
        "required_check_failed" if failure == "tests" else "reviewer_reported_blocked"
    )
    assert command(c.config["worktree"], "rev-parse", "HEAD") == old
    assert command(c.config["worktree"], "status", "--porcelain") == ""
    assert not any(e[0] == "push" for e in c.events) and c.pr["body"] == body


@pytest.mark.parametrize("owner", [False, True])
def test_foreign_moved_head_is_not_refreshed(refresh_story, owner):
    c = refresh_story(owner)
    old_status = c.job["status"]
    c.pr["head"]["sha"] = "f" * 40
    assert not refresh(c, lambda: pytest.fail("foreign head must not fetch"))
    assert c.job["status"] == (old_status if owner else "needs_owner")
    if not owner:
        assert c.job["blocked_reason"]["reason_code"] == "stale_head"
    assert (
        not any(e[0] in {"push", "disable", "PATCH"} for e in c.events)
        and not c.reviews
    )


def test_interrupted_refresh_is_not_replayed(refresh_story):
    c = refresh_story()
    c.job["refreshes"] = [{"sequence": 1, "stage": "merging", "intent": "merge"}]
    assert refresh(c, lambda: pytest.fail("interrupted refresh must not replay"))
    assert c.job["blocked_reason"]["reason_code"] == "publication_uncertain"
    assert (
        not any(e[0] in {"push", "disable", "PATCH"} for e in c.events)
        and not c.reviews
    )


@pytest.mark.parametrize("change", ["other_pr", "title", "section", "matrix"])
def test_edit_body_refuses_unapproved_rights(change):
    api = fixtures.runner.integration_api()
    body = "## Test evidence\nTested head: old\n\n## Acceptance evidence\nold\n\n## Security review\nFixed.\n"
    job = {"integration": {"pr": 9, "body": body}}
    writes = []
    client = SimpleNamespace(request=lambda *args: writes.append(args))
    number, payload = 9, {"body": body.replace("old", "new")}
    if change == "other_pr":
        number = 10
    if change == "title":
        payload["title"] = "new"
    if change == "section":
        payload["body"] += "\n## Other\nChanged.\n"
    if change == "matrix":
        payload["body"] += "\n## Acceptance and evidence matrix\nChanged.\n"
    with pytest.raises(ValueError):
        api.edit_pr_body(client, job, number, payload, "standard")
    assert not writes


def test_edit_body_allows_only_bound_evidence_sections():
    api = fixtures.runner.integration_api()
    old = "## Test evidence\nold\n\n## Acceptance evidence\nold\n\n## Security review\nfixed\n"
    job = {"integration": {"pr": 9, "body": old}}
    writes = []
    client = SimpleNamespace(request=lambda *args: writes.append(args))
    api.edit_pr_body(client, job, 9, {"body": old.replace("old", "new")}, "standard")
    assert writes == [("PATCH", "/pulls/9", {"body": old.replace("old", "new")})]
    assert not hasattr(client, "_pr_body_write")


@pytest.mark.parametrize(
    "heading", ["##\tSecurity review", "  ## Security review", "# Security review"]
)
def test_evidence_edit_cannot_escape_with_markdown_heading_variants(heading):
    api = fixtures.runner.integration_api()
    old = "## Test evidence\nold\n\n## Acceptance evidence\nold\n"
    job = {"integration": {"pr": 9, "body": old}}
    client = SimpleNamespace(
        request=lambda *args: pytest.fail("escaped body must not send")
    )
    with pytest.raises(ValueError):
        api.edit_pr_body(
            client,
            job,
            9,
            {"body": old + heading + "\nChanged authority.\n"},
            "standard",
        )


@pytest.mark.parametrize("owner", [False, True])
def test_refreshed_exact_head_is_accepted_by_merge_lifecycle(refresh_story, owner):
    c = refresh_story(owner)
    assert refresh(c)
    c.pr.update(
        state="closed",
        merged=True,
        merge_commit_sha="d" * 40,
        user={"login": c.cfg["actors"]["runner"]},
    )
    core.pr_event(
        c.cfg,
        c.hub.client("workflow"),
        {"action": "closed", "pull_request": deepcopy(c.pr)},
    )
    assert core.state(c.hub.issues[1]) == "merged"
    assert (
        service.verified_merge_head(c.cfg, c.api, c.hub.issues[1])
        == c.job["head_commit"]
    )


@pytest.mark.parametrize("mutation", ["job", "head", "kind"])
def test_flush_and_reader_refuse_foreign_refresh_record(refresh_story, mutation):
    c = refresh_story()
    assert refresh(c)
    job = "different-job" if mutation == "job" else c.job["id"]
    head = "f" * 40 if mutation == "head" else c.job["head_commit"]
    record = core.public_record(
        c.cfg,
        1,
        job,
        c.job["round"],
        "pr-open",
        commit=head,
        event=c.job["assignment"]["lifecycle"]["event"],
        kind="transition" if mutation == "kind" else "review",
        outcome="ready_for_integration",
        pr=9,
    )
    record.update({"from": "pr-open", "actor": c.cfg["actors"]["runner"]})
    if mutation == "job":
        assert (
            not core.records(
                c.cfg,
                [
                    *c.hub.comments_by_issue[1],
                    {
                        "user": {"login": c.cfg["actors"]["runner"]},
                        "body": core.record_body(record),
                    },
                ],
                1,
            )[-1]["job"]
            == job
        )
    c.job["refreshes"][-1]["stage"] = "publishing_review"
    c.job["reviewed_head"] = c.job["refreshes"][-1]["pr_head"]
    c.runner.save(c.path, c.job)
    entry = core.queue(c.cfg, record, notice_context={"refresh": 1})
    with pytest.raises(core.TransitionRejected):
        core.flush(c.cfg, c.api)
    assert core.read(entry)["status"] == "conflict"
    assert not any(
        core.PREFIX in x["body"] and record["key"] in x["body"]
        for x in c.hub.comments_by_issue[1]
    )


@pytest.mark.parametrize(
    "reason", ["owner_question", "pr_open_reapproval", "publication_uncertain"]
)
def test_other_owner_stop_and_retired_delivery_never_refresh(refresh_story, reason):
    c = refresh_story(True)
    c.job["integration"]["reason"] = reason
    assert not refresh(c, lambda: pytest.fail("other stop must not fetch"))
    c.job["integration"]["reason"] = "owner_merge_required"
    c.job["delivery_retired"] = True
    assert not refresh(c, lambda: pytest.fail("retired job must not fetch"))
    assert not c.events and not c.reviews


@pytest.mark.parametrize("kind", ["integrity", "unavailable"])
def test_refresh_preserves_service_wide_failures(refresh_story, monkeypatch, kind):
    c = refresh_story()
    api = c.runner.integration_api()
    error = (
        core.IntegrityError("synthetic integrity")
        if kind == "integrity"
        else core.Unavailable("synthetic outage")
    )

    def fail(*args):
        raise error

    monkeypatch.setattr(api, "reviewed", fail)
    with pytest.raises(type(error)) as caught:
        refresh(c)
    assert caught.value is error
    assert not c.events and c.job["status"] == "ready_for_integration"


@pytest.mark.parametrize("owner", [False, True])
def test_service_poll_refreshes_before_delivery_and_reuses_holder_read(
    refresh_story, monkeypatch, owner
):
    c = refresh_story(owner)
    api = c.runner.integration_api()
    monkeypatch.setattr(api, "client_for", lambda *args: c.api)
    before_claims = list((Path(c.cfg["state_dir"]) / "claims").glob("*.json"))
    service.poll(c.cfg, c.hub.client(), clock=lambda: 200)
    (job,) = service.jobs(c.cfg)
    assert (
        job["head_commit"] != c.job["base_commit"]
        and job["refreshes"][-1]["stage"] == "done"
    )
    assert len(c.reviews) == 1 and len(before_claims) == len(
        list((Path(c.cfg["state_dir"]) / "claims").glob("*.json"))
    )
    c.pr["mergeable_state"] = "clean"
    c.hub.calls.clear()
    service.poll(c.cfg, c.hub.client(), clock=lambda: 300)
    assert sum(x[:2] == ("GET", "/pulls/9") for x in c.hub.calls) == 1
    assert len(c.reviews) == 1


def test_review_findings_are_in_refresh_stop_notice(refresh_story, monkeypatch):
    c = refresh_story()
    monkeypatch.setattr(
        c.runner,
        "execute_phase",
        lambda *args: fixtures.result("needs_changes", "reviewer"),
    )
    assert refresh(c)
    assert any(
        "Open finding: P1" in x["body"] and "correct this" in x["body"]
        for x in c.hub.comments_by_issue[1]
    )


@pytest.mark.parametrize("failed_check", ["head", "index", "worktree"])
def test_failed_post_abort_check_is_uncertain_and_never_pushes(
    refresh_story, monkeypatch, failed_check
):
    c = refresh_story()
    c.tip = c.target(conflict=True)
    aborted = [False]
    real_trusted, real_git, real_dirty = (
        c.runner.trusted_git,
        c.runner.git,
        c.runner.dirty_paths,
    )

    def trusted(config, *args, **kwargs):
        value = real_trusted(config, *args, **kwargs)
        if args == ("merge", "--abort"):
            aborted[0] = True
        if (
            aborted[0]
            and failed_check == "index"
            and args == ("diff", "--cached", "--name-only")
        ):
            return b"not-clean"
        return value

    def git(config, *args):
        if aborted[0] and failed_check == "head" and args == ("rev-parse", "HEAD"):
            return "f" * 40
        return real_git(config, *args)

    monkeypatch.setattr(c.runner, "trusted_git", trusted)
    monkeypatch.setattr(c.runner, "git", git)
    monkeypatch.setattr(
        c.runner,
        "dirty_paths",
        lambda config: ["not-clean"]
        if aborted[0] and failed_check == "worktree"
        else real_dirty(config),
    )
    assert refresh(c)
    assert c.job["blocked_reason"]["reason_code"] == "publication_uncertain"
    assert not any(e[0] == "push" for e in c.events)
    assert not any(
        r.get("reason") == "continuation_conflict"
        for r in core.records(c.cfg, c.hub.comments_by_issue[1], 1)
    )


def test_auto_merge_waits_for_new_head_merge_evidence(refresh_story, monkeypatch):
    c = refresh_story()
    api = c.runner.integration_api()
    monkeypatch.setattr(api, "client_for", lambda *args: c.api)
    service.poll(c.cfg, c.hub.client(), clock=lambda: 200)
    assert not any(e[0] == "enable" for e in c.events)
    c.pr["mergeable_state"] = "clean"
    monkeypatch.setattr(api, "merge_evidence", lambda *args: "pass")
    service.poll(c.cfg, c.hub.client(), clock=lambda: 300)
    (job,) = service.jobs(c.cfg)
    assert ("enable", job["reviewed_head"]) in c.events
    assert job["integration"]["stage"] == "auto_merge_enabled"


def test_release_detection_uses_fetched_target_not_original_base(refresh_story):
    c = refresh_story()
    source = Path(c.cfg["repository_root"])
    (source / "CHANGELOG.md").write_text("Target release already reviewed.\n")
    command(source, "add", ".")
    command(source, "commit", "-qm", "target release")
    c.tip = command(source, "rev-parse", "HEAD")
    assert refresh(c)
    assert not c.runner.integration_api().release_work(
        c.runner, c.config, c.job, c.config["integration"]
    )


@pytest.mark.parametrize(
    "mutation", ["newest_stop", "dispatch", "scope", "settings", "pr"]
)
def test_refresh_admission_refuses_changed_authority(refresh_story, mutation):
    c = refresh_story(mutation == "newest_stop")
    if mutation == "newest_stop":
        record = core.public_record(
            c.cfg,
            1,
            c.job["id"],
            c.job["round"],
            "needs-owner",
            commit=c.job["head_commit"],
            event=102,
            reason="pr_open_reapproval",
        )
        record.update({"from": "needs-owner", "actor": c.cfg["actors"]["runner"]})
        core.queue(c.cfg, record)
        core.flush(c.cfg, c.api)
    elif mutation == "dispatch":
        record = core.public_record(
            c.cfg, 1, "newer-dispatched-job", 0, "dispatched", event=999
        )
        record.update({"from": "approved", "actor": c.cfg["actors"]["runner"]})
        c.hub.comments_by_issue[1].append(
            {
                "user": {"login": c.cfg["actors"]["runner"]},
                "body": core.record_body(record),
            }
        )
    elif mutation == "scope":
        c.hub.issues[1]["body"] += "\nChanged scope."
    elif mutation == "settings":
        c.config["integration"]["public_evidence"] = (
            c.config["integration"].get("public_evidence", "") + "\nChanged settings."
        )
    elif mutation == "pr":
        c.pr["number"] = 10
    c.events.clear()
    assert not refresh(c, lambda: pytest.fail("inadmissible refresh must not fetch"))
    assert not c.events and not c.reviews


def test_review_record_must_sync_before_push(refresh_story, monkeypatch):
    c = refresh_story()
    monkeypatch.setattr(core, "flush", lambda *args: None)
    assert refresh(c)
    assert c.job["blocked_reason"]["reason_code"] == "publication_uncertain"
    assert not any(e[0] == "push" for e in c.events)


@pytest.mark.parametrize(
    "tier,allowed", [("standard", False), ("control-critical", True)]
)
def test_matrix_edit_only_for_control_critical(tier, allowed):
    api = fixtures.runner.integration_api()
    old = "## Test evidence\nold\n\n## Acceptance and evidence matrix\nold\n"
    new = old.replace("matrix\nold", "matrix\nnew")
    client = SimpleNamespace(request=lambda *args: None)
    job = {"integration": {"pr": 9, "body": old}}
    if allowed:
        api.edit_pr_body(client, job, 9, {"body": new}, tier)
    else:
        with pytest.raises(ValueError):
            api.edit_pr_body(client, job, 9, {"body": new}, tier)


def test_delivery_client_exact_body_patch_capability_and_issue_refusal():
    api = fixtures.runner.integration_api()
    sent = []

    class Base:
        def __init__(self, cfg):
            pass

        def write_allowed(self, method, path):
            return method == "PATCH" and path == "/issues/1"

        def request(self, *args):
            prepared = self.consume_write_payload(*args)
            if prepared is None and not self.write_allowed(args[0], args[1]):
                raise ValueError("Transport refuses unscoped writes")
            sent.append((*args[:2], json.loads(prepared)) if prepared is not None else args)

    client = api.client_for(SimpleNamespace(GitHub=Base), {})
    assert client.write_allowed("POST", "/pulls")
    assert client.write_allowed("POST", "/pulls/9/reviews")
    assert client.write_allowed("PUT", "/pulls/9/merge")
    for method, route in [
        ("PATCH", "/pulls/9"),
        ("PATCH", "/issues/1"),
        ("POST", "/git/refs"),
        ("POST", "/releases"),
    ]:
        assert not client.write_allowed(method, route)
    for route in ["/pulls/9", "/issues/1"]:
        with pytest.raises(ValueError):
            client.request("PATCH", route, {"body": "unscoped"})
    old = "## Test evidence\nold\n\n## Security review\nfixed\n"
    api.edit_pr_body(
        client,
        {"integration": {"pr": 9, "body": old}},
        9,
        {"body": old.replace("old", "new")},
        "standard",
    )
    assert sent == [("PATCH", "/pulls/9", {"body": old.replace("old", "new")})]
    assert not client.write_allowed("PATCH", "/pulls/9")


def test_refresh_uses_real_read_only_phase_folder_without_running_status(
    refresh_story, monkeypatch
):
    c = refresh_story(True)
    monkeypatch.setattr(c.runner, "execute_phase", REAL_REFRESH_PHASE)
    script = """import json,pathlib,sys
prompt=sys.stdin.read()
assert 'INDEPENDENT READ-ONLY REVIEW' in prompt
assert 'never' in sys.argv and 'read-only' in sys.argv
out=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])
out.write_text(json.dumps(dict(status='ready_for_integration',summary='synthetic refresh review',tests=[],limitations=[],findings=[])))
"""
    fixtures.use_fake(c.config, Path(c.config["state_dir"]), script)
    before = core.records(c.cfg, c.hub.comments_by_issue[1], 1)
    assert refresh(c)
    root = Path(c.config["state_dir"]) / "refreshes" / c.job["id"] / "1"
    assert (root / "runs" / c.job["id"] / "refresh_1_reviewer" / "result.json").exists()
    assert c.runner.read(root / "job.json")["status"] == "needs_owner"
    assert c.runner.read(c.path)["status"] == "needs_owner"
    after = core.records(c.cfg, c.hub.comments_by_issue[1], 1)
    assert len(after) == len(before) + 1
    assert not any(
        r["to"] in {"dispatched", "in-progress", "in-review"}
        for r in after
        if r["key"] not in {v["key"] for v in before}
    )


@pytest.mark.parametrize("owner", [False, True])
def test_service_interrupted_refresh_is_uncertain_even_when_pr_is_clean(
    refresh_story, monkeypatch, owner
):
    c = refresh_story(owner)
    c.job["refreshes"] = [{"sequence": 1, "stage": "pushing", "intent": "push"}]
    c.runner.save(c.path, c.job)
    c.pr["mergeable_state"] = "clean"
    c.pr["head"]["sha"] = "f" * 40
    monkeypatch.setattr(c.runner.integration_api(), "client_for", lambda *args: c.api)
    service.poll(c.cfg, c.hub.client(), clock=lambda: 200)
    (job,) = service.jobs(c.cfg)
    assert job["blocked_reason"]["reason_code"] == "publication_uncertain"
    assert not c.reviews and not any(e[0] == "push" for e in c.events)


def test_newer_owner_stop_wins_over_private_interrupted_refresh(refresh_story):
    c = refresh_story(True)
    c.job["refreshes"] = [{"sequence": 1, "stage": "merging", "intent": "merge"}]
    record = core.public_record(
        c.cfg,
        1,
        c.job["id"],
        c.job["round"],
        "needs-owner",
        commit=c.job["head_commit"],
        event=102,
        reason="owner_question",
    )
    record.update({"from": "needs-owner", "actor": c.cfg["actors"]["runner"]})
    core.queue(c.cfg, record)
    core.flush(c.cfg, c.api)
    c.events.clear()
    assert not refresh(c, lambda: pytest.fail("newer owner stop must prevent replay"))
    stops = [
        r for r in core.records(c.cfg, c.hub.comments_by_issue[1], 1) if r.get("reason")
    ]
    assert stops[-1]["reason"] == "owner_question" and not c.events


def test_owner_stop_during_review_is_not_overwritten_or_pushed(
    refresh_story, monkeypatch
):
    c = refresh_story(True)
    original = c.runner.refresh_review

    def owner_stopped(config, job, attempt):
        result = original(config, job, attempt)
        record = core.public_record(
            c.cfg,
            1,
            job["id"],
            job["round"],
            "needs-owner",
            commit=job["head_commit"],
            event=102,
            reason="owner_question",
        )
        record.update({"from": "needs-owner", "actor": c.cfg["actors"]["runner"]})
        core.queue(c.cfg, record)
        core.flush(c.cfg, c.api)
        return result

    monkeypatch.setattr(c.runner, "refresh_review", owner_stopped)
    assert refresh(c)
    stops = [
        r for r in core.records(c.cfg, c.hub.comments_by_issue[1], 1) if r.get("reason")
    ]
    assert stops[-1]["reason"] == "owner_question"
    assert not any(e[0] in {"push", "PATCH"} for e in c.events)


def test_service_refresh_uses_canonical_core_exception_identity(
    refresh_story, monkeypatch
):
    import importlib.util

    c = refresh_story()
    spec = importlib.util.spec_from_file_location(
        "synthetic_alternate_core", Path(core.__file__)
    )
    alternate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alternate)
    monkeypatch.setattr(c.runner, "_LIFECYCLE", alternate)
    monkeypatch.setattr(c.runner.integration_api(), "client_for", lambda *args: c.api)
    error = core.Unavailable("synthetic target fetch outage")

    def unavailable(config):
        raise error

    monkeypatch.setattr(service, "fetch_target", unavailable)
    with pytest.raises(core.Unavailable) as caught:
        service.poll(c.cfg, c.hub.client(), clock=lambda: 200)
    assert caught.value is error
    assert c.runner.read(c.path)["status"] == "ready_for_integration"
    assert not c.events


@pytest.mark.parametrize("context", [1, True, "1"])
def test_refresh_execution_never_runs_a_builder(refresh_story, context):
    c = refresh_story()
    selected = {**c.config, "_refresh_context": context}
    with pytest.raises(c.runner.Blocked, match="reviewer"):
        REAL_REFRESH_PHASE(
            selected, Path(c.config["state_dir"]), c.path, c.job, "worker"
        )


def test_refresh_rollback_preserves_unexpected_tracked_work(refresh_story, monkeypatch):
    c = refresh_story()
    file = Path(c.config["worktree"]) / "delivery.txt"

    def changed_review(*args):
        file.write_text("Unexpected work to reconcile.\n")
        return fixtures.result("needs_changes", "reviewer")

    monkeypatch.setattr(c.runner, "execute_phase", changed_review)
    assert refresh(c)
    assert c.job["blocked_reason"]["reason_code"] == "publication_uncertain"
    assert file.read_text() == "Unexpected work to reconcile.\n"
    assert not any(e[0] == "push" for e in c.events)
