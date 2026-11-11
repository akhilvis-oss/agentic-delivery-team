"""Delivery policy with synthetic clients and temporary repositories only."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import test_runner as fixtures

runner = fixtures.runner
setup = fixtures.setup
integration = runner.integration_api()

STORY = "## Acceptance criteria\n- AC-1: Preserve this exact criterion.\n- AC-2: Keep continuation text\n  exactly as approved.\n\n## Out of scope\nOther work."
PUBLIC = ("## Security review\nSecrets: synthetic fixtures only.\n"
          "External access/cost: synthetic clients only.\nData/provenance: temporary test data.\n")


class Client:
    def __init__(self, job, config):
        self.job, self.config = job, config
        self.writes, self.pushed, self.notified = [], [], []
        self.reviews = []
        self.failure = None
        self.gate_state = "pass"
        self.check_state = "success"
        self.minimum = "standard"
        self.pr = None
        self.auto = True
        self.reads = 0
        self.drift_on_read = None
        self.comment_rows = []

    def issue(self, number):
        return {"body": STORY, "labels": [{"name": "status:in-review"}]}

    def push(self, runner, config, head):
        if self.failure == "push":
            raise RuntimeError("private push error /not/public")
        self.pushed.append(head)

    def notify(self, number):
        self.notified.append(number)

    def comments(self, number):
        return self.comment_rows

    def pages(self, path):
        assert path.endswith("/reviews")
        return self.reviews

    def enable_auto_merge(self, pr):
        self.writes.append(("AUTO", pr["head"]["sha"], None))
        if self.failure == "auto":
            raise RuntimeError("private auto error")
        return self.auto

    def disable_auto_merge(self, pr):
        self.writes.append(("DISABLE", pr["node_id"], None))
        if self.failure == "disable":
            raise RuntimeError("synthetic cancellation unavailable")

    def request(self, method, path, data=None):
        if method != "GET":
            self.writes.append((method, path, deepcopy(data)))
            if self.failure == path or self.failure == "all":
                raise RuntimeError("private payload must not escape")
            if path == "/pulls":
                repo = {"full_name": "example/kit"}
                self.pr = {"number": 9, "id": 9, "node_id": "PR_fixture", "body": data["body"],
                           "head": {"sha": self.job["head_commit"], "ref": self.config["branch"], "repo": repo},
                           "base": {"sha": self.job["base_commit"], "ref": "main", "repo": repo},
                           "state": "open", "merged": False, "changed_files": 1,
                           "mergeable": True, "mergeable_state": "clean", "draft": False,
                           "updated_at": "2026-01-01T00:00:00Z"}
                return deepcopy(self.pr)
            if path.endswith("/reviews"):
                self.reviews.append({**data, "id": 20, "user": {"login": "runner-bot"}, "state": "COMMENTED"})
                return self.reviews[-1]
            if path.endswith("/merge"):
                return {"merged": True}
            return {"html_url": "https://github.com/example/kit/issues/1#issuecomment-11"}
        if path == "/pulls/9":
            self.reads += 1
            if self.reads == self.drift_on_read:
                self.pr["head"]["sha"] = "f" * 40
            return deepcopy(self.pr)
        if path.startswith("/contents/"):
            rules = runner.read(fixtures.ROOT / ".github/review-gate.json")
            rules["repo_url"] = "https://github.com/example/kit"
            rules["tiers"]["rules"] = [{"tier": self.minimum, "patterns": ["*"], "reason": "synthetic"}]
            return {"content": base64.b64encode(json.dumps(rules).encode()).decode()}
        if path.startswith("/pulls/9/files"):
            return [{"id": 1, "filename": "delivery.txt", "status": "added"}]
        if "/check-runs?" in path:
            return {"total_count": 1, "check_runs": [{
                "id": 10, "name": "checks", "head_sha": self.job["head_commit"],
                "status": "in_progress" if self.check_state == "pending" else "completed",
                "conclusion": self.check_state, "app": {"slug": "github-actions"},
                "details_url": "https://github.com/example/kit/actions/runs/5/job/6"}]}
        if path == "/actions/workflows/ci.yml":
            return {"id": 5, "path": ".github/workflows/ci.yml"}
        if path == "/actions/runs/5":
            return {"workflow_id": 5, "event": "pull_request", "head_sha": self.job["head_commit"],
                    "pull_requests": [self.pr]}
        raise AssertionError(path)


@pytest.fixture
def delivery(setup, monkeypatch):
    config, state, path, _ = setup
    monkeypatch.setattr(runner, "execute", fixtures.execute_delivery)
    runner.run_once(config)
    job = runner.read(path)
    assignment = job["assignment"]
    assignment["task_text"] = STORY
    assignment["task_sha256"] = hashlib.sha256(STORY.encode()).hexdigest()
    assignment["lifecycle"] = {"issue": 1, "event": 2, "primary": False}
    job["assignment_sha256"] = runner.digest(assignment)
    job["github_number"] = 1
    job["verification_runs"] = [{"head": job["head_commit"], "status": "passed", "results": [
        {"reported_counts": {"passed": 2}, "argv": ["/private/not-public"], "output_tail": "PRIVATE-OUTPUT"}]}]
    config["lifecycle_config"] = str(state / "synthetic-lifecycle.json")
    config["integration"] = {"enabled": True, "declared_tier": "standard", "public_evidence": PUBLIC}
    cfg = {"repository": "example/kit", "actors": {"runner": "runner-bot", "owner": "owner"},
           "base_branch": "main"}
    core = runner.lifecycle_api()
    monkeypatch.setattr(core, "load_config", lambda _: cfg)
    monkeypatch.setattr(core, "sync_job", lambda *args: None)
    monkeypatch.setattr(core, "admit_phase", lambda *args: True)
    client = Client(job, config)
    monkeypatch.setattr(core, "trusted_gate", lambda *args, **kwargs: client.gate_state)
    runner.save_job(config, path, job)
    return config, path, job, client


def advance(delivery):
    cfg, path, job, client = delivery
    integration.advance(runner.self_module(), cfg, path, job, client)
    return job


def test_create_pr_and_checker_bound_to_exact_head(delivery):
    cfg, path, job, client = delivery
    advance(delivery)
    assert job["integration"]["stage"] == "auto_merge_enabled"
    assert client.pushed == [job["head_commit"]]
    body = client.pr["body"]
    assert STORY.split("## Out of scope")[0].split("\n", 1)[1].rstrip() in body
    assert "## Acceptance evidence" in body and "Tested head: " + job["head_commit"] in body
    assert "PRIVATE-OUTPUT" not in body and "/private/not-public" not in body
    review = client.reviews[0]
    assert review["body"].startswith("Checker: PASS\n")
    assert review["commit_id"] == job["reviewed_head"]
    assert job["review_session"] in review["body"] and "#issuecomment-11" in review["body"]
    writes = deepcopy(client.writes)
    advance(delivery)
    assert client.writes == writes  # Repeated polls never repeat publication.
    assert runner.read(path)["integration"]["stage"] == "auto_merge_enabled"


@pytest.mark.parametrize("tier", integration.TIERS)
def test_merge_categories(delivery, tier):
    cfg, _, job, client = delivery
    cfg["integration"]["declared_tier"] = tier
    client.minimum = tier
    if tier == "domain-critical":
        cfg["integration"]["public_evidence"] += (
            "\n## Failure-mode verification\nFailure cases: synthetic failures.\n"
            "Recovery: repeated polls.\nConcurrency: head drift tests.\n")
    advance(delivery)
    assert any(w[0] == "AUTO" for w in client.writes) == (tier != "control-critical")
    if tier == "control-critical":
        assert job["status"] == "needs_owner" and 9 in client.notified


@pytest.mark.parametrize("change", ["review_head", "session", "test_head", "failed_tests", "findings", "history", "commit", "issue"])
def test_invalid_review_never_pushes_or_records_pass(delivery, change):
    _, _, job, client = delivery
    if change == "review_head":
        job["reviewed_head"] = "a" * 40
    elif change == "session":
        job["review_session"] = "other-session"
    elif change == "test_head":
        job["verification_runs"][-1]["head"] = "a" * 40
    elif change == "failed_tests":
        job["verification_runs"][-1]["status"] = "failed"
    elif change == "findings":
        job["review"]["findings"] = [{"severity": "P1"}]
    elif change == "history":
        job["review_history"] = []
    elif change == "issue":
        job["github_number"] = 999
    else:
        job["runner_commits"] = []
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert not client.pushed and not client.reviews


@pytest.mark.parametrize("phase", ["push", "/issues/1/comments", "/pulls", "/pulls/9/reviews", "auto"])
def test_each_publication_failure_stops_safely(delivery, phase):
    _, path, job, client = delivery
    client.failure = phase
    advance(delivery)
    assert job["status"] == "blocked"
    assert job["integration"]["reason"] == "publication_failed"
    public = runner.public_receipt(runner.read(Path(path).parents[1] / "receipts/task.json"))
    assert public["blocked_reason"]["reason_code"] == "publication_failed"
    assert "private payload" not in json.dumps(public)
    writes = len(client.writes)
    advance(delivery)
    assert len(client.writes) == writes


def test_pending_checks_poll_without_replaying_agents_or_writes(delivery):
    _, _, job, client = delivery
    client.check_state = "pending"
    advance(delivery)
    assert job["integration"]["stage"] == "checking"
    assert job["status"] == "ready_for_integration"
    writes = len(client.writes)
    client.check_state = "success"
    advance(delivery)
    assert len(client.writes) == writes + 1
    assert job["integration"]["stage"] == "auto_merge_enabled"


@pytest.mark.parametrize("which", ["gate", "checks", "category"])
def test_required_failures_have_fixed_reasons_and_states(delivery, which):
    _, _, job, client = delivery
    if which == "gate":
        client.gate_state = "fail"
    elif which == "checks":
        client.check_state = "failure"
    else:
        client.minimum = "control-critical"
    advance(delivery)
    assert job["status"] == ("needs_owner" if which == "category" else "blocked")
    assert job["integration"]["reason"] == ("gate_category_higher" if which == "category" else "required_check_failed")
    assert not any(w[0] == "AUTO" for w in client.writes)


@pytest.mark.parametrize("read", [1, 2, 5])
def test_concurrent_head_change_stops(delivery, read):
    _, _, job, client = delivery
    client.drift_on_read = read
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert not any(w[0] == "AUTO" for w in client.writes)
    if read == 1:
        assert not client.reviews


def test_uncertain_write_intent_is_not_replayed(delivery):
    _, _, job, client = delivery
    job["integration"] = {"stage": "new", "intent": "push"}
    advance(delivery)
    assert job["integration"]["reason"] == "publication_uncertain"
    assert not client.pushed


def test_direct_merge_clean_fallback_is_sha_pinned_and_recorded(delivery):
    _, _, job, client = delivery
    client.auto = False
    advance(delivery)
    assert job["integration"]["stage"] == "merged"
    merge = next(w for w in client.writes if w[1] == "/pulls/9/merge")
    assert merge[2] == {"sha": job["head_commit"], "merge_method": "merge"}
    assert client.writes[-1][1] == "/issues/9/comments"
    assert job["review_session"] in client.writes[-1][2]["body"]


def test_release_left_to_owner(delivery):
    cfg, _, job, client = delivery
    cfg["integration"]["release"] = True
    advance(delivery)
    assert job["status"] == "needs_owner" and not any(w[0] == "AUTO" for w in client.writes)


def test_second_provider_required_only_when_configured(delivery):
    cfg, _, job, client = delivery
    cfg["integration"].update(declared_tier="domain-critical", reviewer_provider="first",
                              second_reviewer={"actor": "second-bot", "provider": "second"})
    cfg["integration"]["public_evidence"] += (
        "\n## Failure-mode verification\nFailure cases: synthetic.\nRecovery: polling.\nConcurrency: drift.\n")
    advance(delivery)
    assert job["integration"]["stage"] == "checking"
    client.reviews.append({"id": 22, "commit_id": job["head_commit"], "state": "COMMENTED",
                           "user": {"login": "second-bot"}, "body": "Second review: PASS\nProvider: second"})
    advance(delivery)
    assert job["integration"]["stage"] == "auto_merge_enabled"


def test_contradictory_review_prevents_merge(delivery):
    _, _, job, client = delivery
    client.comment_rows.append({"body": "Checker: BLOCKED\nNeeds a decision."})
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert not any(w[0] == "AUTO" for w in client.writes)


@pytest.mark.parametrize("auto", [True, False])
@pytest.mark.parametrize("source", ["comment", "review"])
@pytest.mark.parametrize("reviewer", ["Checker", "Second review"])
@pytest.mark.parametrize("verdict", ["BLOCKED", "CHANGES REQUIRED"])
def test_negative_verdict_in_either_channel_blocks_both_merge_paths(
    delivery, auto, source, reviewer, verdict
):
    _, _, job, client = delivery
    client.auto = auto
    row = {"body": f"{reviewer}: {verdict}\nNeeds a decision.",
           "state": "COMMENTED", "commit_id": job["reviewed_head"]}
    (client.reviews if source == "review" else client.comment_rows).append(row)
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert job["integration"]["reason"] == "merge_conditions_missing"
    assert not any(w[0] in {"AUTO", "PUT"} for w in client.writes)


@pytest.mark.parametrize("auto", [True, False])
@pytest.mark.parametrize("reviewer", ["Checker", "Second review"])
@pytest.mark.parametrize("review_state", ["DISMISSED", "PENDING", "CHANGES_REQUESTED", None])
def test_inactive_pass_never_authorizes_either_merge_path(delivery, auto, reviewer, review_state):
    cfg, _, job, client = delivery
    client.auto = auto
    if reviewer == "Second review":
        cfg["integration"].update(
            declared_tier="domain-critical", reviewer_provider="first",
            second_reviewer={"actor": "second-bot", "provider": "second"})
        cfg["integration"]["public_evidence"] += (
            "\n## Failure-mode verification\nFailure cases: synthetic.\nRecovery: polling.\nConcurrency: drift.\n")
    client.check_state = "pending"
    advance(delivery)
    if reviewer == "Checker":
        client.reviews[0]["state"] = review_state
    else:
        client.reviews.append({"commit_id": job["reviewed_head"], "state": review_state,
                               "user": {"login": "second-bot"},
                               "body": "Second review: PASS\nProvider: second"})
    client.check_state = "success"
    advance(delivery)
    assert job["integration"]["stage"] != "auto_merge_enabled"
    assert not any(w[0] in {"AUTO", "PUT"} for w in client.writes)


def test_active_approved_checker_pass_can_authorize_merge(delivery):
    _, _, job, client = delivery
    client.check_state = "pending"
    advance(delivery)
    client.reviews[0]["state"] = "APPROVED"
    client.check_state = "success"
    advance(delivery)
    assert job["integration"]["stage"] == "auto_merge_enabled"


@pytest.mark.parametrize("change", ["negative_verdict", "dismissed_pass"])
def test_direct_merge_rechecks_review_validity_after_clean_status_rejection(delivery, monkeypatch, change):
    _, _, job, client = delivery
    def reject_auto_merge(pr):
        if change == "negative_verdict":
            client.reviews.append({"state": "COMMENTED", "body": "Checker: BLOCKED"})
        else:
            client.reviews[0]["state"] = "DISMISSED"
        return False
    monkeypatch.setattr(client, "enable_auto_merge", reject_auto_merge)
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert job["integration"]["reason"] == "merge_conditions_missing"
    assert not any(w[0] == "PUT" for w in client.writes)


def test_integration_opt_in_and_pool_exclusion():
    assert integration.options({}) is None
    with pytest.raises(ValueError):
        integration.options({"integration": {"enabled": True}})
    with pytest.raises(ValueError):
        integration.options({"integration": {"enabled": True, "declared_tier": "standard"},
                             "lifecycle_config": "synthetic", "pool_slot": "slot"})


def test_run_once_polls_delivery_without_replaying_agents(delivery, monkeypatch):
    cfg, path, job, client = delivery
    monkeypatch.setattr(integration, "client_for", lambda *args: client)
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("must not replay a phase"))
    runner.run_once(cfg, task_id=job["id"])
    assert runner.read(path)["integration"]["stage"] == "auto_merge_enabled"


def test_offline_admission_never_publishes(delivery, monkeypatch):
    _, _, job, client = delivery
    monkeypatch.setattr(runner.lifecycle_api(), "admit_phase", lambda *args: False)
    advance(delivery)
    assert not client.pushed and job["status"] == "needs_owner"


def test_unavailable_identity_never_publishes(delivery, monkeypatch):
    cfg, path, job, client = delivery
    def unavailable(*args):
        raise RuntimeError("synthetic unavailable identity")
    monkeypatch.setattr(integration, "client_for", unavailable)
    integration.advance(runner.self_module(), cfg, path, job)
    assert job["status"] == "blocked" and not client.pushed


def test_github_write_permissions_are_narrow():
    from types import SimpleNamespace
    class Base:
        def __init__(self, cfg):
            pass
        def write_allowed(self, method, path):
            return False
    client = integration.client_for(SimpleNamespace(GitHub=Base), {})
    assert client.write_allowed("POST", "/pulls")
    assert client.write_allowed("POST", "/pulls/1/reviews")
    assert client.write_allowed("PUT", "/pulls/1/merge")
    assert not client.write_allowed("PATCH", "/pulls/1")
    assert not client.write_allowed("POST", "/git/refs")
    assert not client.write_allowed("POST", "/releases")


def test_push_uses_only_verified_identity_and_disposable_git(tmp_path, monkeypatch):
    from types import SimpleNamespace
    seen = []
    class Base:
        def __init__(self, cfg):
            self.token = "fixture-value"
        def ensure_actor(self):
            seen.append("verified")
    client = integration.client_for(SimpleNamespace(GitHub=Base), {"repository": "example/kit"})
    def copy(config, head, folder):
        checkout = folder / "checkout"
        checkout.mkdir()
        return checkout
    def run(argv, **kwargs):
        assert seen == ["verified"]
        assert argv[-1] == "a" * 40 + ":refs/heads/feature"
        assert "https://github.com/example/kit.git" in argv and "--force" not in argv
        assert "fixture-value" not in json.dumps(argv)
        assert "OWNER_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == integration.os.devnull
        assert "GIT_CONFIG_VALUE_0" in kwargs["env"]
        seen.append(argv)
    monkeypatch.setenv("OWNER_TOKEN", "synthetic-owner-value")
    monkeypatch.setattr(integration.subprocess, "run", run)
    config = {"state_dir": str(tmp_path), "branch": "feature"}
    client.push(SimpleNamespace(verification_copy=copy), config, "a" * 40)
    assert len(seen) == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["head", "body", "checks", "review", "review_body", "dismissed"])
def test_changed_evidence_cancels_auto_merge(delivery, change):
    _, _, job, client = delivery
    advance(delivery)
    if change == "head":
        client.pr["head"]["sha"] = "f" * 40
    elif change == "body":
        client.pr["body"] += "\nchanged"
    elif change == "checks":
        client.check_state = "failure"
    elif change == "review_body":
        client.reviews.append({"state": "COMMENTED", "body": "Checker: BLOCKED"})
    elif change == "dismissed":
        client.reviews[0]["state"] = "DISMISSED"
    else:
        client.comment_rows.append({"body": "Checker: CHANGES REQUIRED"})
    advance(delivery)
    assert any(w[0] == "DISABLE" for w in client.writes)
    assert job["status"] in {"needs_owner", "blocked"}
    assert sum(w[0] == "AUTO" for w in client.writes) == 1


def test_uncertain_auto_merge_cancellation_is_not_replayed(delivery):
    _, _, job, client = delivery
    advance(delivery)
    client.check_state = "failure"
    client.failure = "disable"
    advance(delivery)
    assert job["integration"]["reason"] == "auto_merge_disable_uncertain"
    assert job["status"] == "needs_owner"
    advance(delivery)
    assert sum(w[0] == "DISABLE" for w in client.writes) == 1


@pytest.mark.parametrize("path", ["CHANGELOG.md", "docs/releases/v1.md", "docs/release-notes.md"])
def test_release_notes_paths_require_owner_even_without_flag(delivery, monkeypatch, path):
    cfg, _, job, client = delivery
    actual_git = runner.git
    def git(config, *args):
        if args[:2] == ("diff", "--name-only"):
            return path
        return actual_git(config, *args)
    monkeypatch.setattr(runner, "git", git)
    advance(delivery)
    assert job["status"] == "needs_owner" and not any(w[0] == "AUTO" for w in client.writes)


def test_delivery_policy_changes_require_reconciliation(delivery):
    cfg, _, job, client = delivery
    client.check_state = "pending"
    advance(delivery)
    cfg["integration"]["declared_tier"] = "light"
    advance(delivery)
    assert job["status"] == "needs_owner"
    assert not any(w[0] == "AUTO" for w in client.writes)


@pytest.mark.parametrize("result,expected", [
    ({"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "fixture"}}}}, True),
    ({"errors": [{"type": "UNPROCESSABLE", "message": "Pull request is in clean status"}]}, False),
    ({"errors": [{"type": "FORBIDDEN", "message": "Permission denied"}]}, "rejected"),
    ({"errors": [{"type": "UNPROCESSABLE", "message": "Other validation failure"}]}, "rejected"),
])
def test_only_specific_clean_status_rejection_allows_direct_fallback(result, expected):
    import io
    from types import SimpleNamespace
    core = runner.lifecycle_api()
    class Base:
        def __init__(self, cfg):
            self.token = "fixture"
            self.opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(json.dumps(result).encode()))
        def ensure_actor(self):
            pass
    client = integration.client_for(SimpleNamespace(
        GitHub=Base, Unavailable=core.Unavailable, Uncertain=core.Uncertain), {})
    if expected == "rejected":
        with pytest.raises(core.Unavailable):
            client.enable_auto_merge({"node_id": "fixture"})
    else:
        assert client.enable_auto_merge({"node_id": "fixture"}) is expected


def test_unconfirmed_lifecycle_review_never_publishes(delivery):
    _, _, job, client = delivery
    job["lifecycle_unsynced"] = True
    advance(delivery)
    assert job["status"] == "needs_owner" and not client.pushed
    assert not any(w[1] == "/pulls" for w in client.writes)
