"""Manual/polled lifecycle coordinator. No automatic service installation."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from lifecycle import app_tokens, core


def flush_outbox(cfg, client):
    """Policy rejections are durable issue-local outcomes; other errors still escape."""
    try:
        core.flush(cfg, client)
    except core.TransitionRejected:
        pass


def runner_module():
    spec = importlib.util.spec_from_file_location(
        "lifecycle_runner",
        Path(__file__).resolve().parents[1] / "runner/task_runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cfg, *args, check=True, worktree=None):
    from types import SimpleNamespace

    runner = runner_module()
    context = {
        "worktree": worktree or cfg["repository_root"],
        "agent_home": cfg["state_dir"],
        "agent": {"environment": {}},
    }
    try:
        output = runner.trusted_git(context, *args).decode()
        return SimpleNamespace(stdout=output, returncode=0)
    except runner.CommitRefused:
        if check:
            raise core.Unavailable("Managed worktree operation failed") from None
        return SimpleNamespace(stdout="", returncode=1)


def validate_local(cfg):
    for field in (
        "repository_root",
        "worktree_root",
        "runner_config",
        "primary_runner_config",
        "state_dir",
    ):
        if not Path(cfg.get(field, "")).is_absolute() or "<" in cfg[field]:
            raise ValueError("Populate absolute local service paths")
    base = Path(cfg["repository_root"]).resolve()
    for field in ("worktree_root", "state_dir"):
        location = Path(cfg[field]).resolve()
        if location == base or base in location.parents:
            raise ValueError(
                "Service state and owned worktrees must be outside source repository"
            )
    if not core.ID.fullmatch(cfg.get("base_branch", "main")):
        raise ValueError("Invalid base branch")


def claim_event(cfg, issue, events):
    current, event = core.canonical_state(cfg, events)
    if current != core.state(issue) or event is None or issue["state"] != "open":
        return None
    role = (
        "owner"
        if current == "approved"
        else "runner"
        if current == "needs-primary"
        else None
    )
    if role is None or event["actor"]["login"] != cfg["actors"][role]:
        return None
    if current == "approved":
        return event, False
    return event, True


def ensure_worktree(cfg, claim):
    path, branch = Path(claim["worktree"]), claim["branch"]
    listing = git(cfg, "worktree", "list", "--porcelain").stdout
    expected = "worktree " + str(path) + "\n"
    if expected not in listing:
        if path.exists():
            raise core.Unavailable(
                "Managed worktree path already exists; reconcile manually"
            )
        git(
            cfg,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            claim["start_commit"],
        )
    status = git(cfg, "branch", "--show-current", worktree=str(path))
    if status.stdout.strip() != branch:
        raise core.Unavailable("Managed branch changed")


def continuation_commit(body):
    matches = re.findall(r"^### Continue from\nCommit: ([0-9a-f]{40})$", body.replace("\r\n", "\n"), re.MULTILINE)
    return matches[0] if len(matches) == 1 else None


def stop_continuation(cfg, client, path, claim, reason, files=None):
    record = core.public_record(
        cfg, claim["issue"], claim["job"], 0, "blocked",
        event=claim["event"], reason=reason, conflict_files=files,
    )
    core.queue(cfg, record)
    flush_outbox(cfg, client)
    claim.update(stage="finished", error=reason)
    core.save(path, claim)


def prepare_continuation(cfg, claim):
    """Idempotent across a crash after the merge but before saving the claim."""
    ensure_worktree(cfg, claim)
    target = claim["review_base"]
    tree = claim["worktree"]
    if git(cfg, "merge-base", "--is-ancestor", target, "HEAD", check=False,
           worktree=tree).returncode == 0:
        return []
    identity = runner_module().commit_identity(core.read(cfg["runner_config"]))
    merged = git(cfg, "-c", "user.name=" + identity["name"],
                 "-c", "user.email=" + identity["email"],
                 "merge", "--no-ff", "--no-edit", target, check=False, worktree=tree)
    if merged.returncode:
        files = git(cfg, "diff", "--name-only", "--diff-filter=U", "-z",
                    worktree=tree).stdout.split("\0")
        files = sorted(f for f in files if f)
        if not files:
            raise core.Unavailable("Continuation merge failed without conflicts")
        return files
    return []


def fetch_target(cfg):
    """Fetch with an App-only credential in an isolated repository, never origin.

    Source config (helpers, URL rewrites, includes and headers) cannot see the
    credential. Only the fetched objects cross back into the managed repository.
    """
    try:
        if "runner" not in cfg.get("app_auth", {}):
            raise core.Unavailable("Runner App authentication required")
        template = core.read(cfg["runner_config"])
        template["lifecycle_config"] = cfg["config_path"]
        enabled = runner_module().integration_api().options(template) is not None
        token, _ = app_tokens.mint(
            cfg, "runner", fetch_enabled=True, integration_enabled=enabled
        )
        with tempfile.TemporaryDirectory(prefix="fetch-", dir=cfg["state_dir"]) as scratch:
            env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "LANG"}}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       HOME=scratch, XDG_CONFIG_HOME=scratch, GIT_TERMINAL_PROMPT="0")

            def isolated(*args, authenticated=False):
                command_env = dict(env)
                if authenticated:
                    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
                    command_env.update(
                        GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                        GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + auth,
                    )
                return subprocess.run(
                    ["git", "-C", scratch, "-c", "credential.helper=",
                     "-c", "core.hooksPath=" + os.devnull,
                     "-c", "http.followRedirects=false", *args],
                    env=command_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=True, timeout=120,
                ).stdout

            isolated("init", "--bare", "--template=")
            isolated("fetch", "--no-tags", "--no-recurse-submodules",
                     "https://github.com/" + cfg["repository"] + ".git",
                     "refs/heads/" + cfg.get("base_branch", "main"), authenticated=True)
            head = isolated("rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                raise core.Unavailable("Invalid fetched commit")
            git(cfg, "fetch", "--no-tags", "--no-recurse-submodules", scratch, head)
            return head
    except (app_tokens.AuthenticationError, core.Unavailable, OSError,
            ValueError, subprocess.SubprocessError):
        raise core.Unavailable("target_branch_fetch_failed") from None


def recover_start_commit(cfg, claim, runner):
    """Recover a legacy dispatch from durable work before contacting GitHub."""
    worktree = Path(claim["worktree"])
    job_path = (Path(cfg["state_dir"]) / "jobs" / claim["job"]
                / "runner-state" / "tasks" / (claim["job"] + ".json"))
    registered = ("worktree " + str(worktree) + "\n"
                  in git(cfg, "worktree", "list", "--porcelain").stdout)
    if not registered:
        if worktree.exists() or job_path.exists():
            raise core.Unavailable("Existing work requires reconciliation")
        return None
    if git(cfg, "branch", "--show-current", worktree=str(worktree)).stdout.strip() != claim["branch"]:
        raise core.Unavailable("Managed branch changed")
    head = git(cfg, "rev-parse", "HEAD^{commit}", worktree=str(worktree)).stdout.strip()
    if not job_path.exists():
        # Before enqueue no agent could have run: the owned branch is its start.
        return head
    job = runner.read(job_path)
    assignment = runner.verify_assignment(
        {"worktree": claim["worktree"], "branch": claim["branch"]}, job
    )
    binding = dict(issue=claim["issue"], event=claim["event"], primary=claim["primary"])
    start = assignment["base_commit"]
    if (job["id"] != claim["job"] or job.get("github_number") != claim["issue"]
            or assignment.get("lifecycle") != binding
            or assignment["task_text"] != claim["task"]
            or assignment["kind"] != "work" or assignment["after_task"] is not None
            or not isinstance(start, str) or not re.fullmatch(r"[0-9a-f]{40}", start)
            or head != (job.get("head_commit") or start)):
        raise ValueError("Claim/job recovery binding mismatch")
    git(cfg, "merge-base", "--is-ancestor", start, head, worktree=str(worktree))
    return start


def source_work(cfg, job_id):
    """Load only service-owned state and verify its issue/event assignment."""
    if not core.ID.fullmatch(job_id):
        raise ValueError("Invalid source job")
    root = Path(cfg["state_dir"]) / "jobs" / job_id
    config = runner_module().load_config(root / "config.json")
    job = core.read(root / "runner-state" / "tasks" / (job_id + ".json"))
    assignment = runner_module().verify_assignment(config, job)
    if (job["id"] != job_id or assignment["kind"] != "work"
            or config.get("lifecycle_config") != cfg["config_path"]
            or assignment.get("lifecycle", {}).get("issue") != job.get("github_number")):
        raise ValueError("Source job binding changed")
    return config, job


def delivery_template(cfg, claim):
    template = core.read(cfg["primary_runner_config"] if claim["primary"] else cfg["runner_config"])
    template["lifecycle_config"] = cfg["config_path"]
    # Service dispatch requires an explicit choice, including on Primary templates.
    if "integration" not in template:
        raise ValueError("Explicit delivery mode required")
    runner_module().integration_api().options(template)
    if claim["primary"]:
        source_config, source = source_work(cfg, claim["source_job"])
        binding = source["assignment"]["lifecycle"]
        if (binding["issue"] != claim["issue"] or binding["event"] != claim["source_event"]
                or source["assignment"]["task_text"] != claim["task"]
                or source.get("head_commit") != claim["start_commit"]):
            raise ValueError("Primary source binding changed")
        template["integration"] = source_config.get("integration", {"enabled": False})
    if "delivery_settings" in claim:
        template["integration"] = claim["delivery_settings"]
    runner_module().integration_api().options(template)
    return template


def resume_delivery(cfg, claim, config, job_path):
    """Rebind trusted review evidence to this newly approved delivery assignment."""
    runner = runner_module()
    _, source = source_work(cfg, claim["delivery_source"])
    job = runner.read(job_path)
    assignment = job["assignment"]
    assignment["base_commit"] = source["base_commit"]
    assignment["review_base"] = source.get("review_base", source["base_commit"])
    job.update(base_commit=assignment["base_commit"], review_base=assignment["review_base"],
               assignment_sha256=runner.digest(assignment), round=source["round"],
               delivery_source=source["id"])
    job["status"] = "running_worker"
    runner.save_job(config, job_path, job)
    for field in ("head_commit", "reviewed_head", "review", "review_session", "review_history",
                  "verification_runs", "runner_commits"):
        job[field] = source[field]
    # The existing transition protocol and delivery authorization still apply.
    for status in ("running_worker", "running_reviewer", "ready_for_integration"):
        job["status"] = status
        runner.save_job(config, job_path, job)


def delivery_candidate(cfg, claim, source_id, history):
    runner = runner_module()
    source_config, source = source_work(cfg, source_id)
    if not any(r["job"] == source_id and r["commit"] == claim["continued_from"]
               and r["kind"] == "review" and r["outcome"] == "ready_for_integration"
               and core.has_role(cfg, r["actor"], "runner") for r in history):
        return None, "review_record_missing"
    if source["github_number"] != claim["issue"]:
        raise ValueError("Foreign delivery source")
    if source.get("integration", {}).get("stage", "new") != "new":
        return None, "publication_already_started"
    # Only the continuation block may change when reusing the old review.
    def scope(text):
        text = re.sub(r"(?m)^### Continue from\nCommit: [0-9a-f]{40}$", "",
                      text.replace("\r\n", "\n"))
        # The story form replaces its optional empty answer when the owner
        # pastes the block; that wrapper is continuation metadata too.
        return re.sub(r"(?m)^### Saved work \(optional\)\n\n*(?:_No response_(?:\n|$))?\n*",
                      "", text).strip()
    if scope(source["assignment"]["task_text"]) != scope(claim["task"]):
        return None, "approved_scope_changed"
    try:
        runner.integration_api().reviewed(runner, source_config, source)
    except (ValueError, RuntimeError):
        return None, "review_evidence_unavailable"
    base = source.get("review_base", source["base_commit"])
    target = claim["review_base"]
    for file in (".github/review-gate.json", "gate/review_gate.py"):
        old = git(cfg, "show", base + ":" + file, check=False)
        new = git(cfg, "show", target + ":" + file, check=False)
        if (old.returncode, old.stdout) != (new.returncode, new.stdout):
            return None, "trusted_gate_changed"
    if target != base:
        return None, "target_branch_moved"
    return source_id, None


def verified_merge_head(cfg, client, issue):
    """Return only a reviewed PR head bound to an authenticated merge record."""
    if client is None:
        return None
    evidence = core.records(cfg, client.comments(issue["number"]), issue["number"])
    merges = [r for r in evidence if r["kind"] == "transition" and r["to"] == "merged"
              and r.get("pr") and any(core.has_role(cfg, r["actor"], role)
                                      for role in ("runner", "workflow"))]
    for record in reversed(merges):
        pr = client.request("GET", f"/pulls/{record['pr']}")
        if (pr.get("merged") is not True or record["commit"] != pr.get("merge_commit_sha")
                or pr["base"]["ref"] != cfg.get("base_branch", "main")
                or pr["head"].get("repo", {}).get("full_name") != cfg["repository"]
                or pr.get("user", {}).get("login") not in set(cfg["actors"].values())
                or issue["number"] not in {int(n) for n in re.findall(
                    r"\bCloses\s+#([1-9]\d*)\b", pr.get("body") or "", re.I)}
                or not any(r["kind"] == "transition" and r["to"] == "dispatched"
                           and core.has_role(cfg, r["actor"], "runner") for r in evidence)
                or not any(r["kind"] == "review" and r["outcome"] == "ready_for_integration"
                           and r["commit"] == pr["head"]["sha"] for r in evidence)):
            continue
        if record["from"] == "needs-owner" and not core.owner_merge_verified(
                cfg, issue["number"], evidence, pr):
            continue
        if core.permitted(cfg, record["from"], "merged", record["actor"]):
            return pr["head"]["sha"]
    return None


def cleanup(cfg, claim, issue, events=(), *, client=None):
    if claim.get("cleaned") or issue["state"] != "closed":
        return
    if not core.ID.fullmatch(claim["job"]):
        raise ValueError("Invalid cleanup job")
    # The most recent close/reopen event must attest an owner close.
    closing = [e for e in events if e.get("event") in {"closed", "reopened"}]
    owner_closed = bool(closing and closing[-1].get("event") == "closed"
                        and closing[-1].get("actor", {}).get("login") == cfg["actors"]["owner"])

    def remove_continuation():
        ref = "refs/agentic/continue/" + claim["job"]
        if git(cfg, "show-ref", "--verify", ref, check=False).returncode == 0:
            git(cfg, "update-ref", "--no-deref", "-d", ref)
            claim["removed_continuations"] = claim.get("removed_continuations", 0) + 1

    if owner_closed:
        remove_continuation()
    if core.state(issue) != "merged":
        claim["cleanup_pending"] = "closed_without_merge_requires_owner"
        return
    path = Path(claim["worktree"])
    if (
        path.parent.resolve() != Path(cfg["worktree_root"]).resolve()
        or path.name != claim["job"]
    ):
        raise ValueError("Worktree ownership binding changed")
    if path.exists():
        if (
            git(cfg, "branch", "--show-current", worktree=str(path)).stdout.strip()
            != claim["branch"]
            or claim.get("head")
            and git(cfg, "rev-parse", "HEAD", worktree=str(path)).stdout.strip()
            != claim["head"]
        ):
            claim["cleanup_pending"] = "history_changed_requires_owner"
            return
        if (
            "worktree " + str(path) + "\n"
            not in git(cfg, "worktree", "list", "--porcelain").stdout
        ):
            raise ValueError("Worktree registration unavailable")
        dirty = git(cfg, "status", "--porcelain", worktree=str(path)).stdout
        if dirty:
            claim["cleanup_pending"] = "dirty_worktree_requires_owner"
            return
        git(cfg, "worktree", "remove", "--", str(path))
        claim["removed_worktrees"] = claim.get("removed_worktrees", 0) + 1
    ref = "refs/heads/" + claim["branch"]
    if "branch " + ref + "\n" in git(cfg, "worktree", "list", "--porcelain").stdout:
        claim["cleanup_pending"] = "branch_in_use_requires_owner"
        return
    branch = git(cfg, "show-ref", "--verify", "--hash", ref, check=False)
    if branch.returncode == 0:
        head = branch.stdout.strip()
        try:
            target = fetch_target(cfg)
        except core.Unavailable:
            claim["cleanup_pending"] = "target_branch_fetch_failed"
            return
        ancestor = git(cfg, "merge-base", "--is-ancestor", head, target, check=False)
        if ancestor.returncode and head != verified_merge_head(cfg, client, issue):
            claim["cleanup_pending"] = "branch_not_verified_merged"
            return
        # Recheck after network/evidence collection, then let branch -d guard
        # checked-out branches at deletion too. update-ref alone bypasses that
        # guard. A detached worktree pins the merge check to the verified head
        # (including squash merges), retaining an unverified concurrent advance.
        if "branch " + ref + "\n" in git(cfg, "worktree", "list", "--porcelain").stdout:
            claim["cleanup_pending"] = "branch_in_use_requires_owner"
            return
        with tempfile.TemporaryDirectory(prefix="cleanup-", dir=cfg["state_dir"]) as scratch:
            git(cfg, "worktree", "add", "--detach", "--", scratch, head)
            try:
                # An undefined remote prevents stale/multi-valued upstream
                # settings from replacing the detached HEAD merge check.
                deleted = git(
                    cfg, "-c", "branch." + claim["branch"] + ".remote=cleanup-" + uuid.uuid4().hex,
                    "branch", "-d", "--", claim["branch"], check=False,
                    worktree=scratch,
                )
            finally:
                git(cfg, "worktree", "remove", "--", scratch)
        if deleted.returncode:
            in_use = "branch " + ref + "\n" in git(cfg, "worktree", "list", "--porcelain").stdout
            claim["cleanup_pending"] = (
                "branch_in_use_requires_owner" if in_use else "history_changed_requires_owner"
            )
            return
    remove_continuation()
    claim.update(cleaned=True)
    claim.pop("cleanup_pending", None)


def backfill_owner_merge(cfg, client, issue):
    if issue["state"] != "closed":
        return issue
    try:
        if core.state(issue) != "needs-owner":
            return issue
    except ValueError:
        return issue
    evidence = core.records(cfg, client.comments(issue["number"]), issue["number"])
    stops = [r for r in evidence if r["to"] in {"blocked", "needs-owner", "needs-primary"}
             and r.get("reason")]
    if not stops or stops[-1]["reason"] != "owner_merge_required":
        return issue
    stop = stops[-1]
    cache_path = Path(cfg["state_dir"]) / "owner-merge-backfill" / f"{issue['number']}.json"
    generation = {"updated_at": issue["updated_at"], "stop_key": stop["key"]}
    cached = core.read(cache_path) if cache_path.exists() else {}
    current = all(cached.get(k) == v for k, v in generation.items())
    if current and cached.get("outcome") == "not_verifiable":
        return issue
    number = stop["pr"] or (cached.get("pr") if current else None)
    candidates = ([{"number": number}] if number else client.pages("/pulls?state=closed"))
    for candidate in candidates:
        # Fetch the authoritative merged flag and head, rather than list metadata.
        pr = client.request("GET", f"/pulls/{candidate['number']}")
        if not core.owner_merge_verified(cfg, issue["number"], evidence, pr):
            continue
        # Retain the verified candidate before writes so interrupted or
        # uncertain publication retries never repeat the collection scan.
        core.save(cache_path, {**generation, "outcome": "verified", "pr": pr["number"]})
        record = core.public_record(cfg, issue["number"], f"pr-{pr['number']}", 0, "merged",
                                    commit=pr["merge_commit_sha"], pr=pr["number"])
        core.queue(cfg, record, role="runner")
        flush_outbox(cfg, client)
        return client.issue(issue["number"])
    # Only completed reads can establish a negative result. Transport
    # failures propagate and remain retryable instead of poisoning cache.
    core.save(cache_path, {**generation, "outcome": "not_verifiable"})
    return issue


def publish_poll(cfg, client, issues, at):
    """One discoverable, pinned comment; durable intents prevent duplicate POSTs.

    This runs before builder execution so long builds are visible from GitHub.
    PATCH and pin are idempotent; an ambiguous create must reconcile by marker.
    """
    root = Path(cfg["state_dir"])
    path = root / "public-heartbeat.json"
    journal = core.read(path) if path.exists() else {}
    marker = "<!-- agentic-service-status-v1 -->"
    standups = [i for i in issues if cfg.get("digest_config")
                and core.is_report(cfg, i) and "<!-- agentic-digest-v1:" in (i.get("body") or "")]
    statuses = [i for i in issues if i.get("user", {}).get("login") == cfg["actors"]["runner"]
                and marker in (i.get("body") or "")]
    targets = standups or statuses
    if targets:
        number = max(targets, key=lambda i: i["number"])["number"]
    else:
        if journal.get("issue_attempted"):
            raise core.Unavailable("Service status issue needs reconciliation")
        journal["issue_attempted"] = True
        core.save(path, journal)
        try:
            issue = client.request("POST", "/issues", {
                "title": "Lifecycle service status",
                "body": "The pinned comment shows the service's last poll. "
                        "If it stops updating, ask the operator to restore the service.\n" + marker,
            })
        except core.Unavailable as exc:
            if not isinstance(exc, core.Uncertain):
                journal["issue_attempted"] = False
                core.save(path, journal)
            raise
        number = issue["number"]
    comment_marker = "<!-- agentic-last-poll-v1 -->"
    comments = [c for c in client.comments(number)
                if c.get("user", {}).get("login") == cfg["actors"]["runner"]
                and comment_marker in c.get("body", "")]
    body = poll_body(cfg, at, journal.get("notices", []))
    if comments:
        comment = min(comments, key=lambda c: c["id"])
        client.request("PATCH", f"/issues/comments/{comment['id']}", {"body": body})
    else:
        if journal.get("comment_attempted_for") == number:
            raise core.Unavailable("Service status comment needs reconciliation")
        journal["comment_attempted_for"] = number
        core.save(path, journal)
        try:
            comment = client.request("POST", f"/issues/{number}/comments", {"body": body})
        except core.Unavailable as exc:
            if not isinstance(exc, core.Uncertain):
                journal.pop("comment_attempted_for", None)
                core.save(path, journal)
            raise
    if not comment.get("pin"):
        client.request("PUT", f"/issues/comments/{comment['id']}/pin")
    journal.update(issue=number, comment=comment["id"], last_poll=at)
    core.save(path, journal)


def poll_body(cfg, at, notices):
    return ("Lifecycle service last poll: " + timestamp(at)
            + ".\nExpected poll interval: " + str(cfg["poll_seconds"])
            + " seconds, plus time spent building or reviewing. If this time stops "
            "updating, ask the operator to restore the service; re-approving alone "
            "will not restart it.\n"
            + "".join(line + "\n" for line in notices)
            + "<!-- agentic-last-poll-v1 -->")


def report_recovery(cfg, client, failure, restart):
    """Publish only after the poll and stand-up finish; retain receipts on failure."""
    root = Path(cfg["state_dir"])
    path = root / "public-heartbeat.json"
    journal = core.read(path)
    notices = []
    if failure:
        notices.append(
            f"Polls first failed: {timestamp(failure['first_failed'])}; "
            f"recovered: {timestamp(time.time())}; "
            f"failed polls: {failure['failures']}; "
            f"exception class: {failure['exception_class']}."
        )
    if restart:
        notices.append(
            f"Service restarted after {restart['exception_class']} "
            f"in {restart['function']}."
        )
    if notices or journal.get("notices"):
        client.request("PATCH", f"/issues/comments/{journal['comment']}",
                       {"body": poll_body(cfg, journal["last_poll"], notices)})
        journal["notices"] = notices
        core.save(path, journal)
    for name in ("poll-failure.json", "service-error.json"):
        (root / name).unlink(missing_ok=True)


def error_origin(exc):
    """Only framework function names cross the private/public boundary."""
    framework = Path(__file__).resolve().parents[1]
    function = "run_loop"
    trace = exc.__traceback__
    while trace:
        code = trace.tb_frame.f_code
        filename = Path(code.co_filename).resolve()
        if any(filename.is_relative_to(framework / folder)
               for folder in ("lifecycle", "digest", "runner")):
            function = code.co_name
        trace = trace.tb_next
    return function


def poll(cfg, client, *, run_jobs=True, clock=time.time):
    validate_local(cfg)
    core.bind_state(cfg)
    root = core.private(cfg["state_dir"])
    claims_dir = core.private(root / "claims")
    core.private(cfg["worktree_root"])
    with core.lock(root / "service.lock") as acquired:
        if not acquired:
            return []
        cursor = root / "poll.json"
        if cursor.exists() and clock() < core.read(cursor)["next_poll"]:
            return []
        core.save(cursor, {"next_poll": clock() + cfg["poll_seconds"]})
        flush_outbox(cfg, client)
        all_issues = client.pages("/issues?state=all")
        publish_poll(cfg, client, all_issues, clock())
        issues = [
            i
            for i in all_issues
            if "pull_request" not in i and not core.is_report(cfg, i)
        ]
        issues = [backfill_owner_merge(cfg, client, i) for i in issues]
        by_number = {i["number"]: i for i in issues}
        for issue in issues:
            try:
                action = claim_event(cfg, issue, client.events(issue["number"]))
                if action and not action[1]:
                    proofs = core.records(
                        cfg, client.comments(issue["number"]), issue["number"]
                    )
                    proof = next(
                        (
                            r
                            for r in proofs
                            if r["kind"] == "approval"
                            and r["event"] == int(action[0]["id"])
                        ),
                        None,
                    )
                    if (
                        not proof
                        or proof["scope_sha256"]
                        != core.hashlib.sha256(
                            (issue.get("body") or "").encode()
                        ).hexdigest()
                    ):
                        action = None
            except ValueError:
                continue
            if not action:
                continue
            event, primary = action
            source_job = None
            if primary:
                history = core.records(
                    cfg, client.comments(issue["number"]), issue["number"]
                )
                escalated = [
                    r for r in history if r["to"] == "needs-primary" and r["commit"]
                ]
                source_job = next(
                    (
                        j
                        for j in jobs(cfg)
                        if escalated
                        and j["id"] == escalated[-1]["job"]
                        and j.get("head_commit") == escalated[-1]["commit"]
                    ),
                    None,
                )
                if source_job is None:
                    continue
            job = f"issue-{issue['number']}-event-{event['id']}"
            path = claims_dir / (job + ".json")
            if path.exists():
                continue
            # Persist the event-specific claim and frozen scope BEFORE any action.
            claim = dict(
                job=job,
                issue=issue["number"],
                event=int(event["id"]),
                primary=primary,
                task=issue.get("body") or "",
                stage="claimed",
                created_at=core.now(),
                branch="runner/" + job,
                worktree=str(Path(cfg["worktree_root"]) / job),
            )
            if source_job:
                claim.update(
                    start_commit=source_job["head_commit"],
                    review_base=source_job.get(
                        "review_base", source_job["base_commit"]
                    ),
                    primary_context=source_job.get("review_history", []),
                    source_job=source_job["id"], source_event=source_job["assignment"]["lifecycle"]["event"],
                )
            named = None if primary else continuation_commit(claim["task"])
            if named:
                history = core.records(cfg, client.comments(issue["number"]), issue["number"])
                local_jobs = {j["id"]: j for j in jobs(cfg)}
                saved = next((r for r in reversed(history)
                              if r["commit"] == named
                              and ((r.get("continuation", False) and r["to"] in {"needs-owner", "blocked"})
                                   or (r["kind"] == "review" and r["outcome"] == "ready_for_integration"))
                              and core.has_role(cfg, r["actor"], "runner")
                              and local_jobs.get(r["job"], {}).get("status") in {
                                  "needs_owner", "blocked", "ready_for_integration"}
                              and local_jobs[r["job"]].get("blocked_reason", {}).get("reason_code")
                              != "continuation_not_applied"
                              and not any(refusal.get("reason") in {"history_changed", "concurrent_change"}
                                          for refusal in local_jobs[r["job"]].get("commit_refusals", []))
                              and not any(run.get("reason") == "reviewed_worktree_changed"
                                          or run.get("source_unchanged") is False
                                          for run in local_jobs[r["job"]].get("verification_runs", []))
                              and local_jobs[r["job"]].get("head_commit") == named), None)
                count = sum(1 for p in claims_dir.glob("*.json")
                            if (c := core.read(p))["issue"] == issue["number"]
                            and c.get("continued_from"))
                if saved:
                    claim.update(start_commit=named, continued_from=named,
                                 continuation_number=count + 1,
                                 previous_stop_reason=saved["reason"] or "delivery_disabled",
                                 continuation_source=saved["job"])
                else:
                    claim["continuation_error"] = "continuation_commit_unrecorded"
                if count >= 2:
                    claim["continuation_error"] = "continuation_limit"
            core.create(path, claim)
        processed = []
        runner = runner_module()
        for path in sorted(claims_dir.glob("*.json")):
            claim = core.read(path)
            if claim["stage"] == "finished" and claim.get("cleaned"):
                continue
            issue = by_number.get(claim["issue"])
            if not issue:
                continue
            # The current all-issues listing already proves closed/final stories
            # cannot execute. Keep fresh evidence checks for claims that can.
            finished = core.finished_issue(issue)
            if not finished:
                issue = client.issue(claim["issue"])
                evidence = core.records(cfg, client.comments(claim["issue"]), claim["issue"])
                finished = core.finished_issue(issue, evidence)
            if finished:
                claim["stage"] = "finished"
                completed_path = (root / "jobs" / claim["job"] / "runner-state"
                                  / "tasks" / (claim["job"] + ".json"))
                if completed_path.exists():
                    claim["head"] = core.read(completed_path).get("head_commit")
                core.save(path, claim)
            if claim["stage"] == "finished":
                if not finished and run_jobs and claim.get("config"):
                    completed_config = runner.load_config(claim["config"])
                    completed_job = runner.read(
                        Path(completed_config["state_dir"]) / "tasks" / (claim["job"] + ".json")
                    )
                    if (completed_job.get("stop_saving")
                            or completed_job["status"] == "ready_for_integration"
                            or runner.integration_api().options(completed_config)):
                        runner.run_once(completed_config, task_id=claim["job"])
                events = client.events(claim["issue"])
                canonical, _ = core.canonical_state(cfg, events)
                if canonical == core.state(issue):
                    cleanup(cfg, claim, issue, events, client=client)
                else:
                    claim["cleanup_pending"] = "untrusted_close_state"
                core.save(path, claim)
                continue
            if claim["stage"] == "enqueued":
                recovery_config = runner.load_config(claim["config"])
                recovery_path = (
                    Path(recovery_config["state_dir"])
                    / "tasks"
                    / (claim["job"] + ".json")
                )
                previous = runner.read(recovery_path)
                if previous["status"] in runner.TERMINAL:
                    # Completion is durable in the job before the claim. Recover
                    # that boundary without replaying work or re-admitting it.
                    if previous.get("stop_saving") or previous["status"] == "ready_for_integration":
                        if not run_jobs:
                            continue
                        runner.run_once(recovery_config, task_id=claim["job"])
                        previous = runner.read(recovery_path)
                        if previous.get("stop_saving") or previous["status"] == "ready_for_integration":
                            continue
                    claim.update(stage="finished", head=previous.get("head_commit"))
                    core.save(path, claim)
                    continue
                if run_jobs and previous["status"].startswith("running_"):
                    # run_once only marks an unclean phase interrupted; never
                    # signals a persisted PID or starts a second agent.
                    runner.run_once(recovery_config)
                    claim.update(
                        stage="finished", error="interrupted_requires_reconciliation"
                    )
                    core.save(path, claim)
                    continue
            if claim["stage"] == "suspended":
                continue
            fresh = client.issue(claim["issue"])
            if fresh["state"] != "open" or (fresh.get("body") or "") != claim["task"]:
                claim.update(
                    stage="suspended", error="admission_changed_requires_reconciliation"
                )
                core.save(path, claim)
                continue
            if claim["stage"] == "claimed":
                if claim.get("continuation_error"):
                    stop_continuation(cfg, client, path, claim, claim["continuation_error"])
                    continue
                try:
                    template = delivery_template(cfg, claim)
                except (ValueError, OSError):
                    stop_continuation(cfg, client, path, claim, "delivery_configuration_invalid")
                    continue
                if "delivery_settings" not in claim:
                    claim["delivery_settings"] = template["integration"]
                    core.save(path, claim)
                if claim.get("continued_from"):
                    if "review_base" not in claim:
                        try:
                            claim["review_base"] = fetch_target(cfg)
                        except core.Unavailable:
                            claim["error"] = "target_branch_fetch_failed"
                            core.save(path, claim)
                            continue
                        claim.pop("error", None)
                        core.save(path, claim)
                    if "delivery_resume_checked" not in claim:
                        source, reason = delivery_candidate(
                            cfg, claim, claim["continuation_source"],
                            core.records(cfg, client.comments(claim["issue"]), claim["issue"]))
                        claim.update(delivery_resume_checked=True, delivery_source=source,
                                     delivery_resume_fallback=reason)
                        core.save(path, claim)
                    files = prepare_continuation(cfg, claim)
                    if files:
                        stop_continuation(cfg, client, path, claim, "continuation_conflict", files)
                        continue
                if "start_commit" not in claim:
                    try:
                        claim["start_commit"] = fetch_target(cfg)
                    except core.Unavailable:
                        claim["error"] = "target_branch_fetch_failed"
                        core.save(path, claim)
                        continue
                    claim.pop("error", None)
                    core.save(path, claim)
                record = core.public_record(
                    cfg,
                    claim["issue"],
                    claim["job"],
                    0,
                    "dispatched",
                    event=claim["event"],
                )
                outbox = core.queue(cfg, record)
                flush_outbox(cfg, client)
                if core.read(outbox)["status"] != "synced":
                    continue
                claim["stage"] = "dispatched"
                core.save(path, claim)
                fresh = client.issue(claim["issue"])
            if claim["stage"] in {"dispatched", "enqueued"}:
                remote_records = core.records(
                    cfg, client.comments(claim["issue"]), claim["issue"]
                )
                dispatches = [r for r in remote_records if r["to"] == "dispatched"]
                if (
                    core.state(fresh)
                    not in (
                        {"dispatched"}
                        if claim["stage"] == "dispatched"
                        else {
                            "dispatched",
                            "in-progress",
                            "committed",
                            "in-review",
                            "changes-requested",
                        }
                    )
                    or not dispatches
                    or dispatches[-1]["job"] != claim["job"]
                ):
                    claim.update(
                        stage="suspended",
                        error="admission_changed_requires_reconciliation",
                    )
                    core.save(path, claim)
                    continue
            if claim["stage"] == "dispatched":
                # Older dispatches may already have durable work. Preserve its
                # original start and receipts; fetch only before work exists.
                if "start_commit" not in claim:
                    start = recover_start_commit(cfg, claim, runner)
                    if start is None:
                        try:
                            start = fetch_target(cfg)
                        except core.Unavailable:
                            claim["error"] = "target_branch_fetch_failed"
                            core.save(path, claim)
                            continue
                    claim["start_commit"] = start
                    claim.pop("error", None)
                    core.save(path, claim)
                ensure_worktree(cfg, claim)
                job_root = core.private(root / "jobs" / claim["job"])
                template = delivery_template(cfg, claim)
                template.update(
                    worktree=claim["worktree"],
                    branch=claim["branch"],
                    state_dir=str(job_root / "runner-state"),
                    lifecycle_config=cfg["config_path"],
                )
                config_path = job_root / "config.json"
                core.save(config_path, template)
                run_config = runner.load_config(config_path)
                job_path = (
                    Path(run_config["state_dir"]) / "tasks" / (claim["job"] + ".json")
                )
                binding = dict(
                    issue=claim["issue"], event=claim["event"], primary=claim["primary"]
                )
                if not job_path.exists():
                    runner.enqueue(
                        run_config,
                        claim["job"],
                        claim["task"],
                        github_number=claim["issue"],
                        lifecycle=binding,
                        review_base=claim.get("review_base"),
                        primary_context=claim.get("primary_context"),
                        continuation=({"commit": claim["continued_from"],
                                       "number": claim["continuation_number"],
                                       "reason": claim["previous_stop_reason"]}
                                      if claim.get("continued_from") else None),
                    )
                else:
                    existing = runner.read(job_path)
                    if (
                        existing["assignment"].get("lifecycle") != binding
                        or existing["assignment"]["task_text"] != claim["task"]
                    ):
                        raise ValueError("Claim/job binding mismatch")
                if claim.get("delivery_source"):
                    existing = runner.read(job_path)
                    if existing["status"] == "queued":
                        resume_delivery(cfg, claim, run_config, job_path)
                claim.update(stage="enqueued", config=str(config_path))
                core.save(path, claim)
            if run_jobs and claim["stage"] == "enqueued":
                run_config = runner.load_config(claim["config"])
                runner.run_once(run_config)
                job = runner.read(
                    Path(run_config["state_dir"]) / "tasks" / (claim["job"] + ".json")
                )
                if job["status"] in runner.TERMINAL:
                    claim.update(stage="finished", head=job.get("head_commit"))
                core.save(path, claim)
            processed.append(claim["job"])
        completed = clock()
        core.save(cursor, {"next_poll": completed + cfg["poll_seconds"]})
        core.save(root / "heartbeat.json", {"last_poll": completed})
        return processed


def jobs(cfg):
    return [
        core.read(p)
        for p in (Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json")
    ]


def heartbeat(cfg):
    """Read only; missing state must remain missing."""
    core.validate_state(cfg)
    path = Path(cfg["state_dir"]) / "heartbeat.json"
    return core.read(path).get("last_poll") if path.exists() else None


def timestamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def serving(root):
    """Probe the lifetime lock without creating or modifying any file."""
    try:
        with (root / "serve.lock").open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def status(cfg, *, clock=time.time):
    root = Path(cfg["state_dir"])
    last = heartbeat(cfg)
    lines = ["Service: " + ("running" if serving(root) else "not running")]
    lines.append(
        "Last poll: never (state uninitialized)"
        if last is None
        else f"Last poll: {timestamp(last)}; {max(0, int(clock() - last))} seconds ago"
    )
    cursor = root / "poll.json"
    lines.append(
        "Next poll: "
        + (timestamp(core.read(cursor)["next_poll"]) if cursor.exists() else "unknown")
        + " (scheduled only while running)"
    )
    claims = [core.read(p) for p in sorted((root / "claims").glob("*.json"))]
    local_jobs = jobs(cfg)
    lines.append(
        f"Cleanup: {sum(c.get('removed_worktrees', 0) for c in claims)} worktrees removed; "
        f"{sum(c.get('removed_continuations', 0) for c in claims)} continuation references removed"
    )
    conflicts = [core.read(p) for p in (root / "outbox").glob("*.json")]
    lines.append("Rejected transitions: " + str(sum(
        e["status"] == "conflict" and e.get("error") == "transition_not_allowed"
        for e in conflicts
    )))
    lines.append("Active claims:")
    active = [
        f"  Issue #{c['issue']}: {c['job']} ({c['stage']})"
        for c in claims
        if c["stage"] != "finished"
    ]
    lines.extend(active or ["  none"])
    lines.append("Jobs:")

    def issue_number(job):
        return (
            job.get("assignment", {})
            .get("lifecycle", {})
            .get("issue", job.get("github_number", "unknown"))
        )

    lines.extend(
        [f"  Issue #{issue_number(j)}: {j['id']} ({j['status']})" for j in local_jobs]
        or ["  none"]
    )
    lines.append("Waiting for reconciliation:")
    pending = []
    for c in claims:
        if c.get("error") or c.get("cleanup_pending") or c["stage"] == "suspended":
            pending.append(
                f"  Issue #{c['issue']}: {c['job']} (claim or cleanup needs attention)"
            )
    for j in local_jobs:
        if (
            j.get("lifecycle_unsynced")
            or j["status"]
            in {
                "blocked",
                "failed",
                "interrupted",
                "needs_owner",
                "needs_primary_review",
            }
            or any(c.get("state") == "prepared" for c in j.get("runner_commits", []))
        ):
            pending.append(
                f"  Issue #{issue_number(j)}: {j['id']} (job needs attention)"
            )
    for folder, done, issue_field in (
        ("outbox", {"synced", "superseded"}, "issue"),
        ("followups", {"posted"}, "parent_issue"),
    ):
        for path in sorted((root / folder).glob("*.json")):
            item = core.read(path)
            if item["status"] not in done:
                number = item.get("record", item).get(issue_field, "unknown")
                pending.append(
                    f"  Issue #{number}: {folder} publication {item['status']}"
                )
    alert_path = root / "watchdog.json"
    if alert_path.exists() and core.read(alert_path).get("attempted"):
        pending.append("  Service outage alert awaiting publication reconciliation")
    lines.extend(pending or ["  none recorded locally"])
    return "\n".join(lines)


def watchdog(cfg, client, *, clock=time.time):
    """Owner-scheduled independently of serve and digest; never dispatches jobs."""
    threshold = cfg.get("heartbeat_alert_seconds")
    if threshold is None:
        return {"status": "disabled"}
    core.bind_state(cfg)
    root = Path(cfg["state_dir"])
    with core.lock(root / "watchdog.lock") as acquired:
        if not acquired:
            return {"status": "locked"}
        path = root / "watchdog.json"
        journal = core.read(path) if path.exists() else {}
        last = heartbeat(cfg)
        if last is not None and clock() - last <= threshold:
            core.save(path, {})
            return {"status": "healthy"}
        if journal.get("heartbeat", "missing") != last:
            journal = {"heartbeat": last}
        marker = (
            "<!-- agentic-service-alert-v1:"
            + core.key([cfg["repository"], last])
            + " -->"
        )
        if journal.get("issue"):
            return {"status": "already_alerted", "issue": journal["issue"]}
        existing = [
            i
            for i in client.pages("/issues?state=all")
            if i.get("user", {}).get("login") == cfg["actors"]["product"]
            and marker in (i.get("body") or "")
        ]
        if existing:
            journal.update(issue=existing[0]["number"], attempted=False)
            core.save(path, journal)
            return {"status": "already_alerted", "issue": journal["issue"]}
        if journal.get("attempted"):
            return {"status": "uncertain_requires_reconciliation"}
        journal["attempted"] = True
        core.save(path, journal)
        try:
            created = client.request(
                "POST",
                "/issues",
                {
                    "title": "[Urgent] Lifecycle service has stopped polling",
                    "body": "The lifecycle service has no recent successful poll. "
                    "The owner should inspect service status and reconcile before restarting.\n\n"
                    + marker,
                    "assignees": [cfg["actors"]["owner"]],
                },
            )
        except core.Unavailable as exc:
            if not isinstance(exc, core.Uncertain):
                journal["attempted"] = False
            core.save(path, journal)
            return {"status": "unsynced"}
        journal.update(issue=created["number"], attempted=False)
        core.save(path, journal)
        return {"status": "alerted", "issue": journal["issue"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "action",
        choices=[
            "run-once",
            "serve",
            "reconcile",
            "enqueue-only",
            "status",
            "watchdog",
        ],
    )
    args = parser.parse_args(argv)
    cfg = core.load_config(args.config)
    cfg["config_path"] = str(args.config.resolve())
    if args.action == "status":
        print(status(cfg))
        return 0
    if args.action == "watchdog":
        client = (
            core.GitHub(cfg, "product") if "heartbeat_alert_seconds" in cfg else None
        )
        print(json.dumps(watchdog(cfg, client)))
        return 0
    client = core.GitHub(cfg)
    if args.action == "reconcile":
        print(json.dumps(core.reconcile(cfg, jobs(cfg), client)))
    elif args.action == "serve":
        validate_local(cfg)
        core.bind_state(cfg)
        with core.lock(Path(cfg["state_dir"]) / "serve.lock") as acquired:
            if not acquired:
                raise core.Unavailable("Service is already running")
            run_loop(cfg, client, args.action)
    else:
        run_loop(cfg, client, args.action)
    return 0


def run_loop(cfg, client, action):
    serving = action == "serve"
    root = Path(cfg["state_dir"]) if serving else None
    failure = restart = None
    if serving:
        core.bind_state(cfg)
        failure_path = root / "poll-failure.json"
        restart_path = root / "service-error.json"
        failure = core.read(failure_path) if failure_path.exists() else None
        restart = core.read(restart_path) if restart_path.exists() else None
    wait = min(cfg["poll_seconds"], 600) if serving else None
    while True:
        try:
            previous_poll = heartbeat(cfg) if serving else None
            poll(cfg, client, run_jobs=action != "enqueue-only")
            if action != "enqueue-only" and cfg.get("digest_config"):
                from digest import standup

                digest_cfg = standup.load_config(cfg["digest_config"])
                standup.run_once(
                    digest_cfg, core.GitHub(digest_cfg["lifecycle"], "product")
                )
            if serving and heartbeat(cfg) != previous_poll:
                report_recovery(cfg, client, failure, restart)
                failure = restart = None
                wait = min(cfg["poll_seconds"], 600)
            delay = cfg["poll_seconds"] if serving else 0
        except core.Unavailable as exc:
            if not serving:
                raise
            at = time.time()
            failure = failure or {"first_failed": at, "failures": 0}
            failure.update(last_failed=at, failures=failure["failures"] + 1,
                           exception_class=type(exc).__name__)
            core.save(root / "poll-failure.json", failure)
            delay = wait
            wait = min(wait * 2, 600)
        except Exception as exc:
            if serving:
                core.save(root / "service-error.json",
                          {"exception_class": type(exc).__name__,
                           "function": error_origin(exc)})
            raise
        if action in {"run-once", "enqueue-only"}:
            break
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
