"""Manual/polled lifecycle coordinator. No automatic service installation."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lifecycle import app_tokens, core, upgrade


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


def claim_event(cfg, issue, events, changed=True):
    # Defer the event request until changed/open/admission checks pass.
    if not changed or not core.can_start(issue) or core.state(issue) not in {"approved", "needs-primary"}:
        return None
    current, event = core.canonical_state(cfg, events() if callable(events) else events)
    if current != core.state(issue) or event is None or not core.can_start(issue):
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


def restart_fresh(body):
    """An exact directive in the Owner decisions section starts over."""
    body = body.replace("\r\n", "\n")
    section = runner_module().integration_api().story_section(body, "Owner decisions", trim=False)
    return section is not None and "Restart: fresh" in section.splitlines()


def continuation(cfg, client, claim, claims_dir, *, prepare=False):
    """One continuation path; prepare only after the target has been fetched."""
    if prepare:
        if "delivery_resume_checked" not in claim:
            source, reason = delivery_candidate(
                cfg, claim, claim["continuation_source"],
                core.records(cfg, client.comments(claim["issue"]), claim["issue"]))
            claim.update(delivery_resume_checked=True, delivery_source=source,
                         delivery_resume_fallback=reason)
            core.save(claims_dir / (claim["job"] + ".json"), claim)
        return prepare_continuation(cfg, claim)
    history = core.records(cfg, client.comments(claim["issue"]), claim["issue"])
    previous = [core.read(p) for p in claims_dir.glob("*.json")]
    previous = [c for c in previous if c["issue"] == claim["issue"]
                and c["event"] < claim["event"]]
    dispatched = {r["job"] for r in history if r["to"] == "dispatched"
                  and core.has_role(cfg, r["actor"], "runner")}
    earlier = [c for c in previous if c["job"] in dispatched
               or c.get("was_dispatched") or c.get("config")
               or c["stage"] in {"dispatched", "enqueued"}]
    last = max(earlier, key=lambda c: c["event"], default=None)
    fresh = (not claim["primary"] and restart_fresh(claim["task"])
             and (last is None or not restart_fresh(last["task"])))
    claim["restart_fresh"] = fresh
    boundary = max((c["event"] for c in earlier if c.get("restart_fresh")), default=0)
    local = [j for j in jobs(cfg) if j.get("github_number") == claim["issue"]]
    # Retire the previous delivery loop before either a restart or budget stop.
    for old in local:
        number = old.get("integration", {}).get("pr")
        if number and client.request("GET", f"/pulls/{number}").get("state") == "open":
            old["delivery_retired"] = True
            job_path = (Path(cfg["state_dir"]) / "jobs" / old["id"]
                        / "runner-state" / "tasks" / (old["id"] + ".json"))
            core.save(job_path, old)
            claim["continuation_error"] = "pr_open_reapproval"
            return
    if fresh:
        return
    saved = [j for j in local if j.get("head_commit")
             and (j.get("continuation") or j["status"] in
                  {"ready_for_integration", "needs_primary_review"})
             and j.get("assignment", {}).get("lifecycle", {}).get("event", 0) >= boundary]
    if not saved:
        return False if claim["primary"] else None
    source = max(saved, key=lambda j: (j.get("created_at", ""),
                                      j["assignment"]["lifecycle"]["event"], j["id"]))
    head = source["head_commit"]
    targets = {"needs_owner": "needs-owner", "blocked": "blocked",
               "failed": "blocked", "needs_primary_review": "needs-primary"}

    def matches(record):
        if (record["job"] != source["id"] or record["commit"] != head
                or not core.has_role(cfg, record["actor"], "runner")):
            return False
        if source["status"] == "ready_for_integration":
            return record["kind"] == "review" and record["outcome"] == "ready_for_integration"
        return (record["to"] == targets.get(source["status"])
                and (source["status"] == "needs_primary_review" or record.get("continuation")))

    valid = (any(matches(r) for r in history)
             and source.get("blocked_reason", {}).get("reason_code") != "continuation_not_applied"
             and not any(r.get("reason") in {"history_changed", "concurrent_change"}
                         for r in source.get("commit_refusals", []))
             and not any(r.get("reason") == "reviewed_worktree_changed"
                         or r.get("source_unchanged") is False
                         for r in source.get("verification_runs", [])))
    try:
        _, checked = source_work(cfg, source["id"])
        valid = (valid and checked == source and isinstance(head, str)
                 and re.fullmatch(r"[0-9a-f]{40}", head) is not None)
        if claim["primary"]:
            valid = valid and source["assignment"]["task_text"] == claim["task"]
        if valid:
            git(cfg, "cat-file", "-e", head + "^{commit}")
    except (ValueError, OSError, KeyError, core.Unavailable) as exc:
        if getattr(exc, "integrity_error", False):
            raise
        valid = False
    if not valid:
        claim["continuation_error"] = "continuation_commit_unrecorded"
        return
    count = sum(1 for c in previous if c.get("continued_from")
                and not c.get("primary") and c["event"] >= boundary
                and not c.get("readiness_stopped")
                and not any(j["id"] == c["job"] and j.get("readiness_stopped") for j in local))
    if not claim["primary"] and count >= 2:
        claim["continuation_error"] = "continuation_limit"
        return
    reason = next((r["reason"] for r in reversed(history) if matches(r) and r["reason"]),
                  "delivery_disabled")
    claim.update(start_commit=head, continued_from=head, continuation_source=source["id"],
                 previous_stop_reason=reason)
    if claim["primary"]:
        claim.update(primary_context=source.get("review_history", []),
                     source_job=source["id"],
                     source_event=source["assignment"]["lifecycle"]["event"])
    else:
        claim["continuation_number"] = count + 1


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
        token, _ = app_tokens.get(
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
                try:
                    return subprocess.run(
                        ["git", "-C", scratch, "-c", "credential.helper=",
                         "-c", "core.hooksPath=" + os.devnull,
                         "-c", "http.followRedirects=false", *args],
                        env=command_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=True, timeout=120,
                    ).stdout
                except (OSError, subprocess.SubprocessError):
                    if authenticated:
                        app_tokens.invalidate(token)
                    raise

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
        raise core.IntegrityError("Claim/job recovery binding mismatch")
    git(cfg, "merge-base", "--is-ancestor", start, head, worktree=str(worktree))
    return start


def source_work(cfg, job_id):
    """Load only service-owned state and verify its issue/event assignment."""
    if not core.ID.fullmatch(job_id):
        raise core.IntegrityError("Invalid source job")
    root = Path(cfg["state_dir"]) / "jobs" / job_id
    config = runner_module().load_config(root / "config.json")
    job = core.read(root / "runner-state" / "tasks" / (job_id + ".json"))
    assignment = runner_module().verify_assignment(config, job)
    if (job["id"] != job_id or assignment["kind"] != "work"
            or config.get("lifecycle_config") != cfg["config_path"]
            or assignment.get("lifecycle", {}).get("issue") != job.get("github_number")):
        raise core.IntegrityError("Source job binding changed")
    return config, job


def delivery_template(cfg, claim):
    template = core.read(cfg["primary_runner_config"] if claim["primary"] else cfg["runner_config"])
    main = core.read(cfg["runner_config"])
    template["lifecycle_config"] = cfg["config_path"]
    if "integration" not in main:
        raise ValueError("Explicit delivery mode required")
    template["integration"] = main["integration"]
    runner_module().integration_api().options(template)
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
        raise core.IntegrityError("Foreign delivery source")
    if source.get("integration", {}).get("stage", "new") != "new":
        return None, "publication_already_started"
    # Only continuation metadata may change when reusing the old review.
    def scope(text):
        text = re.sub(r"(?m)^### Continue from\nCommit: [0-9a-f]{40}$", "",
                      text.replace("\r\n", "\n"))
        # The story form replaces its optional empty answer when the owner
        # pastes the block; that wrapper is continuation metadata too.
        # Only a directive within Owner decisions is continuation metadata.
        owner = None
        seen_owner = False
        kept = []
        for line in text.splitlines(keepends=True):
            heading = re.match(r"^(#{1,6})[ \t]+(.*?)[ \t]*\n?$", line)
            if heading:
                level = len(heading[1])
                if owner is not None and level <= owner:
                    owner = None
                if not seen_owner and level in {2, 3} and heading[2].lower() == "owner decisions":
                    owner = level
                    seen_owner = True
            elif re.fullmatch(r"---[ \t]*\n?", line):
                owner = None
            if owner is None or line.rstrip("\n") != "Restart: fresh":
                kept.append(line)
        text = "".join(kept)
        return re.sub(r"(?m)^### Saved work \(optional\)\n\n*(?:_No response_(?:\n|$))?\n*",
                      "", text).strip()
    if scope(source["assignment"]["task_text"]) != scope(claim["task"]):
        return None, "approved_scope_changed"
    try:
        runner.integration_api().reviewed(runner, source_config, source)
    except Exception as exc:
        if getattr(exc, "integrity_error", False):
            raise
        if not isinstance(exc, (ValueError, RuntimeError)):
            raise
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
        raise core.IntegrityError("Invalid cleanup job")
    # The most recent close/reopen event must attest an owner close.
    closing = [e for e in events if e.get("event") in {"closed", "reopened"}]
    owner_closed = bool(closing and closing[-1].get("event") == "closed"
                        and closing[-1].get("actor", {}).get("login") == cfg["actors"]["owner"])

    def remove_continuation():
        ref = "refs/agentic/continue/" + claim["job"]
        if git(cfg, "show-ref", "--verify", ref, check=False).returncode == 0:
            git(cfg, "update-ref", "--no-deref", "-d", ref)
            claim["removed_continuations"] = claim.get("removed_continuations", 0) + 1

    finish = (next((r for r in core.records(cfg, client.comments(issue["number"]),
                                           issue["number"]) if r["kind"] == "finish"), None)
              if client is not None else None)
    closed_before_finish = owner_closed and (finish is None or
        int(closing[-1]["id"]) < finish["event"])
    if closed_before_finish:
        remove_continuation()
    if core.state(issue) != "merged":
        claim["cleanup_pending"] = "closed_without_merge_requires_owner"
        return
    path = Path(claim["worktree"])
    if (
        path.parent.resolve() != Path(cfg["worktree_root"]).resolve()
        or path.name != claim["job"]
    ):
        raise core.IntegrityError("Worktree ownership binding changed")
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
            raise core.Unavailable("Worktree registration unavailable")
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
        if ancestor.returncode and finish is not None:
            claim.update(cleaned=True, owner_finish_kept_unmerged=True)
            claim.pop("cleanup_pending", None)
            return
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
    """Publish a discoverable, pinned comment on a best-effort basis.

    This runs before builder execution so long builds are visible from GitHub.
    PATCH and pin are idempotent. After a complete listing without a marker,
    retry even an ambiguous create: duplicate status artifacts are harmless.
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
    created = None
    if targets:
        number = max(targets, key=lambda i: i["number"])["number"]
    else:
        issue = client.request("POST", "/issues", {
            "title": "Lifecycle service status",
            "body": "The pinned comment shows the service's last update. "
                    "If it stops updating, ask the operator to restore the service.\n" + marker,
        })
        number = issue["number"]
        created = issue
    comment_marker = "<!-- agentic-last-poll-v1 -->"
    comments = [c for c in client.comments(number)
                if c.get("user", {}).get("login") == cfg["actors"]["runner"]
                and comment_marker in c.get("body", "")]
    body = poll_body(cfg, at, journal.get("notices", []))
    content = poll_body(cfg, 0, journal.get("notices", []))
    edited = False
    if comments:
        comment = min(comments, key=lambda c: c["id"])
        if (journal.get("content") != content or journal.get("comment") != comment["id"]
                or at - journal.get("last_edit", 0) >= 600):
            comment = client.request("PATCH", f"/issues/comments/{comment['id']}", {"body": body})
            edited = True
    else:
        comment = client.request("POST", f"/issues/{number}/comments", {"body": body})
        edited = True
    if not comment.get("pin"):
        client.request("PUT", f"/issues/comments/{comment['id']}/pin")
    journal.update(issue=number, comment=comment["id"], last_poll=at)
    if edited:
        journal.update(last_edit=at, content=content)
    journal.pop("issue_attempted", None)
    journal.pop("comment_attempted_for", None)
    core.save(path, journal)
    return created


def owner_finish_notices(cfg, claims=None):
    if claims is None:
        claims = [core.read(p) for p in sorted((Path(cfg["state_dir"]) / "claims").glob("*.json"))]
    return [f"Issue #{c['issue']}: owner finish kept unmerged work" for c in claims
            if c.get("owner_finish_kept_unmerged")
            and git(cfg, "show-ref", "--verify", "refs/heads/" + c["branch"],
                    check=False).returncode == 0]


def poll_body(cfg, at, notices):
    return ("Lifecycle service last poll (last update): " + timestamp(at)
            + ".\nExpected poll interval: " + str(cfg["poll_seconds"])
            + " seconds, plus time spent building or reviewing. If this time stops "
            "updating, ask the operator to restore the service; re-approving alone "
            "will not restart it.\n"
            + "System retry schedule (minutes): "
            + (", ".join(map(str, cfg.get("system_retry_minutes", [5, 20, 60]))) or "disabled") + ".\n"
            + "".join(line + "\n" for line in notices + owner_finish_notices(cfg)
                      + waiting_lines(cfg)
                      + [queue_wait_text(n, wait) for n, wait in sorted(queue_waiting(cfg).items())])
            + (cfg["_upgrade_notice"] + "\n" if cfg.get("_upgrade_notice") else "")
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
        journal.update(last_edit=journal["last_poll"], content=poll_body(cfg, 0, notices))
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
               for folder in ("lifecycle", "digest", "runner")) and re.fullmatch(
                   r"[A-Za-z_][A-Za-z0-9_]{0,79}", code.co_name):
            function = code.co_name
        trace = trace.tb_next
    return function


def dependency_numbers(task):
    """Only the first Depends-on line of the named section is authoritative."""
    active = None
    for line in task.splitlines():
        heading = re.fullmatch(r"(#{1,6})\s+(.+?)\s*", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if active is not None and level <= active:
                return ()
            if active is None and level in {2, 3} and title == "Dependencies":
                active = level
            continue
        if active is not None and line.startswith("Depends on"):
            value = line[len("Depends on"):].split(".", 1)[0].strip()
            if value == "nothing":
                return ()
            if not re.fullmatch(r"#[1-9]\d*(?:(?:, | and |, and )#[1-9]\d*)*", value):
                raise ValueError("Dependency text is not an issue list")
            return tuple(dict.fromkeys(int(n) for n in re.findall(r"#([1-9]\d*)", value)))
    return ()


def queue_waiting(cfg):
    """Status/digest reads existing queue state without creating any files."""
    core.validate_state(cfg)
    latest = {}
    for path in (Path(cfg["state_dir"]) / "claims").glob("*.json"):
        claim = core.read(path)
        number = claim.get("issue")
        if type(number) is not int or number <= 0:
            continue
        if number not in latest or claim.get("event", 0) > latest[number].get("event", 0):
            latest[number] = claim
    return {n: c["waiting"] for n, c in latest.items()
            if c.get("stage") == "claimed" and isinstance(c.get("waiting"), dict)
            and len(c["waiting"]) == 1
            and next(iter(c["waiting"])) in {"dependency", "story"}
            and type(next(iter(c["waiting"].values()))) is int
            and next(iter(c["waiting"].values())) > 0}


def queue_wait_text(number, waiting):
    target = next(iter(waiting.values()))
    return (f"#{number}: waiting for #{target}" if "dependency" in waiting
            else f"#{number}: waiting for the story in progress, #{target}")


class StoryQueue:
    """One poll's admission facts; unchanged dependencies never need fresh reads."""

    def __init__(self, cfg, client, rows, changed, claims_dir):
        self.cfg, self.client, self.rows = cfg, client, rows
        self.changed, self.claims_dir = changed, claims_dir
        self.path = Path(cfg["state_dir"]) / "queue-facts.json"
        self.facts = core.read(self.path) if self.path.exists() else {}
        self.prs = {}
        cfg["_queue_prs"] = self.prs  # The authoritative slot read is reusable by refresh.

    def fact(self, number, field):
        issue = self.rows[number]
        generation = ([issue.get("state"), issue.get("closed_at")] if field == "close_events" else
                      [issue.get("updated_at"), issue.get("state"), issue.get("state_reason"),
                       issue.get("closed_at"), issue.get("labels"),
                       core.hashlib.sha256((issue.get("body") or "").encode()).hexdigest()])
        saved = self.facts.setdefault(str(number), {})
        entry = saved.get(field)
        if not entry or entry["generation"] != generation:
            value = (self.client.events(number) if field in {"events", "close_events"} else
                     core.records(self.cfg, self.client.comments(number), number))
            entry = {"generation": generation, "value": value}
            saved[field] = entry
            core.save(self.path, self.facts)
        return entry["value"]

    def priority(self, claim):
        issue = self.rows[claim["issue"]]
        if not any(label.get("name") == "priority:high" for label in issue.get("labels", [])):
            return False
        events = [e for e in self.fact(claim["issue"], "events")
                  if e.get("event") in {"labeled", "unlabeled"}
                  and e.get("label", {}).get("name") == "priority:high"]
        event = max(events, key=lambda e: e.get("id", 0), default={})
        actor = event.get("actor", {}).get("login")
        return (event.get("event") == "labeled"
                and (actor == self.cfg["actors"]["owner"] or core.has_role(self.cfg, actor, "product")))

    def order(self, path):
        claim = core.read(path)
        if claim["stage"] != "claimed":
            return (0, False, 0, claim["issue"], claim.get("event", 0))
        approval = claim["event"]
        if claim.get("primary"):
            previous = [core.read(p) for p in self.claims_dir.glob("*.json")]
            approval = max((c["event"] for c in previous
                            if c["issue"] == claim["issue"] and not c.get("primary")), default=approval)
        return (1, not self.priority(claim) if claim["issue"] in self.rows else True,
                approval, claim["issue"], claim["event"])

    def references(self, claim):
        """Validate the dependency graph using already-listed issues, never PR reads."""
        root = claim["issue"]
        refs = dependency_numbers(claim["task"])
        seen = set()

        def visit(number, trail):
            if number == root:
                return f"#{number}: this story" if not trail else f"#{number}: a cycle"
            issue = self.rows.get(number)
            if issue is None or core.is_report(self.cfg, issue):
                return f"#{number}: an unknown issue"
            if "pull_request" in issue:
                return f"#{number}: a pull request"
            if number in trail:
                return f"#{number}: a cycle"
            if number in seen:
                return None
            seen.add(number)
            for dependency in dependency_numbers(issue.get("body") or ""):
                problem = visit(dependency, trail | {number})
                if problem:
                    return problem
            return None

        for number in refs:
            problem = visit(number, set())
            if problem:
                return refs, problem
        return refs, None

    def merged(self, number):
        issue = self.rows[number]
        if any(label.get("name") in {"status:merged", "status:accepted"}
               for label in issue.get("labels", [])):
            return True
        history = self.fact(number, "records")
        if any(r["to"] == "merged" for r in history) or (history and history[-1]["to"] == "accepted"):
            return True
        if issue.get("state") != "closed":
            return False
        closes = [e for e in self.fact(number, "close_events") if e.get("event") == "closed"]
        last = max(closes, key=lambda e: e.get("id", 0), default={})
        return (last.get("actor", {}).get("login") == self.cfg["actors"]["owner"]
                and last.get("state_reason") in {None, "completed"}
                and issue.get("state_reason") == "completed")

    def holder(self, claim):
        local = jobs(self.cfg)
        by_job = {job["id"]: job for job in local}
        claims = [core.read(p) for p in self.claims_dir.glob("*.json")]
        stopped_claims = {c["job"] for c in claims
                          if c["stage"] == "finished" and c.get("error") == "unexpected_error"}
        candidates = set()

        def holding(job):
            number = job.get("integration", {}).get("pr")
            if type(number) is int and number > 0:
                listed = self.rows.get(number)
                owner = job.get("assignment", {}).get("lifecycle", {}).get("issue", job.get("github_number"))
                # PRs are included in the all-issues listing. Fall back to the
                # owning story generation when an older cache has no PR row.
                pr_listed = listed is not None and "pull_request" in listed
                signal = listed if pr_listed else self.rows.get(owner, {})
                generation = [pr_listed, signal.get("updated_at"), signal.get("state"),
                              signal.get("closed_at"), signal.get("labels")]
                released = self.facts.setdefault("released_slots", {})
                if number not in self.prs:
                    if released.get(str(number)) == generation:
                        return False
                    self.prs[number] = self.client.request("GET", f"/pulls/{number}")
                pr = self.prs[number]
                if pr.get("state") not in {"open", "closed"}:
                    raise core.Unavailable("Slot pull request state unavailable")
                if pr["state"] == "closed":
                    if released.get(str(number)) != generation:
                        released[str(number)] = generation
                        core.save(self.path, self.facts)
                    return False
                if str(number) in released:
                    released.pop(str(number))
                    core.save(self.path, self.facts)
                return True
            # The error boundary retires claims without resaving runner jobs.
            # Their stale retry cannot retain a slot, but an open PR above can.
            if job["id"] in stopped_claims:
                return False
            stopped = job["status"] in {"blocked", "needs_owner", "failed", "cancelled", "interrupted"}
            retry = isinstance(job.get("retry"), dict) and "not_before" in job["retry"]
            return not stopped or retry

        for previous in claims:
            n = previous["issue"]
            if n == claim["issue"] or self.rows.get(n, {}).get("state") == "closed":
                continue
            if (previous["stage"] in {"dispatched", "enqueued"}
                    and (previous["job"] not in by_job or holding(by_job[previous["job"]]))):
                candidates.add(n)
        for job in local:
            n = job.get("github_number", job.get("assignment", {}).get("lifecycle", {}).get("issue"))
            if (type(n) is int and n != claim["issue"]
                    and self.rows.get(n, {}).get("state") != "closed" and holding(job)):
                candidates.add(n)
        return min(candidates, default=None)

    def decide(self, claim):
        try:
            refs, problem = self.references(claim)
        except ValueError:
            refs, problem = (), "text that isn't a list of #N"
        if problem:
            return None, ("This story's Dependencies line has a reference that can't be used ("
                          + problem + "). Owner: fix the line, then re-approve.")
        for number in refs:
            if self.merged(number):
                continue
            if self.rows[number].get("state") == "closed":
                return None, (f"This story depends on #{number}, which closed without merging. "
                    f"Owner: if #{number} was finished by hand, add `status:merged` to it (#184); "
                    "otherwise remove the dependency or change the story. Then re-approve.")
            return {"dependency": number}, None
        holder = self.holder(claim)
        return ({"story": holder} if holder is not None else None), None

    def stop(self, path, claim, body):
        record = core.public_record(self.cfg, claim["issue"], claim["job"], 0, "needs-owner",
                                    event=claim["event"], reason="dependency_closed_unmerged")
        record.update({"from": core.state(self.rows[claim["issue"]]),
                       "actor": self.cfg["actors"]["runner"]})
        core.queue(self.cfg, record, public_body=body + "\n" + core.record_body(record))
        flush_outbox(self.cfg, self.client)
        claim.update(stage="finished")
        claim.pop("waiting", None)
        claim.pop("error", None)
        core.save(path, claim)

def local_errors(cfg):
    return sorted((Path(cfg["state_dir"]) / "story-errors").glob("*.json"))


def remember_error(cfg, number, exc):
    details = {"issue": number, "exception_class": exception_name(exc),
               "function": error_origin(exc)}
    folder = core.private(Path(cfg["state_dir"]) / "story-errors")
    core.create(folder / (core.key(details) + ".json"), details)


def exception_name(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"


@contextlib.contextmanager
def story_boundary(cfg, client, issue, failed, skipped, claims, *, claim=None,
                   approval=None, observed=None):
    """Isolate bugs; unavailable transports and broken bindings still escape."""
    number = issue["number"]
    try:
        yield
    except core.StatusLabelError:
        skipped.add(number)
    except Exception as exc:
        if isinstance(exc, core.Unavailable) or getattr(exc, "integrity_error", False):
            raise
        failed.add(number)
        issue = observed.get("issue", issue) if observed is not None else issue
        try:
            current = core.state(issue)
        except core.StatusLabelError:
            skipped.add(number)
            return
        candidate = claim
        event = approval.get("event") if approval is not None else None
        if claim is None and current in {"approved", "needs-primary"}:
            if event is None:
                remember_error(cfg, number, exc)
                return
            candidate = next((c for c in claims if c["issue"] == number
                              and c["event"] == int(event["id"])
                              and c["stage"] not in {"finished", "suspended"}), None)
        elif candidate is None:
            candidate = next((c for c in claims if c["issue"] == number
                              and c["stage"] not in {"finished", "suspended"}), None)
        active = (current != "pr-open" and (
            candidate is not None and candidate["stage"] not in {"finished", "suspended"}
            or claim is None and current in {"approved", "needs-primary"}
            and approval is not None and approval.get("event") is not None))
        if not active:
            remember_error(cfg, number, exc)
            return
        if candidate is None:
            event = int(approval["event"]["id"])
            candidate = dict(issue=number, event=event,
                             job=f"issue-{number}-event-{event}", stage="claimed")
        record = core.public_record(
            cfg, number, candidate["job"], 0, "blocked", event=candidate["event"],
            reason="unexpected_error", exception_class=exception_name(exc),
            function=error_origin(exc))
        # Persist terminal ownership before any transport. Never save the runner
        # job: that could emit its own, duplicate stop on a later poll.
        core.queue(cfg, record)
        candidate.update(stage="finished", error="unexpected_error")
        core.save(Path(cfg["state_dir"]) / "claims" / (candidate["job"] + ".json"), candidate)
        try:
            flush_outbox(cfg, client)
        except core.Unavailable:
            raise
        except Exception as flush_error:
            if getattr(flush_error, "integrity_error", False):
                raise
            remember_error(cfg, number, flush_error)


def waiting_jobs(cfg):
    active = {core.read(p)["job"] for p in (Path(cfg["state_dir"]) / "claims").glob("*.json")
              if core.read(p)["stage"] == "enqueued"}
    return [job for job in jobs(cfg) if job["id"] in active and job["status"] == "waiting_system"]


def waiting_lines(cfg):
    total = len(cfg.get("system_retry_minutes", [5, 20, 60]))
    lines = []
    for job in waiting_jobs(cfg):
        retry = job["retry"]
        due = dt.datetime.fromtimestamp(retry["not_before"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"#{job['github_number']}: waiting on the system ({retry['code']}), "
                     f"retry {retry['attempt']} of {total} no earlier than {due}")
    return sorted(lines)


def cancel_waiting_claim(cfg, client, claim, *, fresh=None, history=None):
    """Apply AC-7 to changes observed by the scan or later admission reads."""
    if claim["stage"] != "enqueued" or not claim.get("config"):
        return False
    tool = runner_module()
    path = (Path(cfg["state_dir"]) / "jobs" / claim["job"] / "runner-state"
            / "tasks" / (claim["job"] + ".json"))
    if not path.exists():
        return False
    job = core.read(path)
    if job["status"] != "waiting_system":
        return False
    fresh = client.issue(claim["issue"]) if fresh is None else fresh
    history = (core.records(cfg, client.comments(claim["issue"]), claim["issue"])
               if history is None else history)
    dispatches = [r for r in history if r["to"] == "dispatched"]
    events = client.events(claim["issue"])
    _, latest = core.canonical_state(cfg, events)
    running = {"dispatched", "in-progress", "committed", "in-review", "changes-requested"}
    owner_action = (core.state(fresh) not in running
                    or latest is not None and int(latest["id"]) > claim["event"]
                    and latest.get("actor", {}).get("login") == cfg["actors"]["owner"])
    local = (fresh["state"] != "open" or owner_action or not core.can_start(fresh, history)
             or not dispatches or dispatches[-1]["job"] != claim["job"])
    changed = (fresh.get("body") or "") != claim["task"]
    if not local and not changed:
        return False
    retry = job.pop("retry")
    job.update(status="needs_owner", blocked_reason=tool.block_reason(retry["code"]),
               updated_at=tool.now())
    if local:
        # Deliberately discard this attempt's unpublished continuation.
        job["retry_local_end"] = True
        for field in ("head_commit", "continuation", "stop_saving", "stop_ready"):
            job.pop(field, None)
        core.save(path, job)
    else:
        config = tool.load_config(claim["config"])
        tool.verify_assignment(config, job)
        if not tool.service_claim_owns(cfg, config, job):
            raise core.IntegrityError("Waiting claim/job binding changed")
        expected = job.get("system_retry_context", {}).get("stop_expected")
        if expected is not None:
            tool.save_at_stop(config, path, job, job.get("head_commit") or expected)
        tool.save_job(config, path, job)
        tool.publish_receipt(config, path, job)
    claim.update(stage="finished", head=job.get("head_commit"))
    claim.pop("error", None)
    core.save(Path(cfg["state_dir"]) / "claims" / (claim["job"] + ".json"), claim)
    return True


def cancel_waiting(cfg, client, claims, issues, failed, skipped):
    """Retire waiting attempts before continuation can inspect a new claim."""
    for claim in claims:
        if claim["stage"] != "enqueued" or not claim.get("config"):
            continue
        issue = issues.get(claim["issue"])
        if issue is None or claim["issue"] in failed | skipped:
            continue
        with story_boundary(cfg, client, issue, failed, skipped, claims, claim=claim):
            cancel_waiting_claim(cfg, client, claim)


def poll(cfg, client, *, run_jobs=True, clock=time.time, upgrade_enabled=False,
         pending_hook=None, full_list=False):
    validate_local(cfg)
    core.bind_state(cfg)
    root = core.private(cfg["state_dir"])
    claims_dir = core.private(root / "claims")
    core.private(cfg["worktree_root"])
    with core.lock(root / "service.lock") as acquired:
        if not acquired:
            return []
        try:
            ctx = upgrade.context(cfg) if upgrade_enabled else None
        except Exception:
            ctx = None
            cfg["_upgrade_notice"] = upgrade.UNREADABLE
        pending = upgrade.is_pending(ctx)
        if pending and pending_hook is not None:
            pending_hook(ctx)
        cursor = root / "poll.json"
        if not pending and not full_list and cursor.exists() and clock() < core.read(cursor)["next_poll"]:
            return []
        core.save(cursor, {"next_poll": clock() + cfg["poll_seconds"]})
        def upgrade_read_started():
            cfg["_upgrade_last_check"] = clock()

        last_check = cfg.get("_upgrade_last_check")
        if (upgrade_enabled and (last_check is None or clock() >= last_check + 600)
                and upgrade.attempt(cfg, ctx, upgrade_read_started)):
            return upgrade.SWITCH
        flush_outbox(cfg, client)
        listing_at = clock()
        cache = cfg.get("_poll_cache")
        journal_path = root / "public-heartbeat.json"
        journal = core.read(journal_path) if journal_path.exists() else {}
        full = (full_list or cache is None or listing_at - cache["full_at"] >= 3600
                or journal.get("issue") not in cache["issues"]
                or "completed_listing_at" not in cache)
        route = "/issues?state=all"
        if not full:
            since = timestamp(cache["completed_listing_at"] - 300).replace("+00:00", "Z")
            route += "&since=" + since
        updates = client.pages(route)
        changed = {i["number"] for i in updates}
        rows = {} if full else dict(cache["issues"])
        rows.update((i["number"], i) for i in updates)
        cache = {"issues": rows, "full_at": listing_at if full else cache["full_at"],
                 **({"completed_listing_at": cache["completed_listing_at"]}
                    if cache and "completed_listing_at" in cache else {})}
        cfg["_poll_cache"] = cache
        all_issues = list(rows.values())
        heartbeat_failure = root / "public-heartbeat-failure.json"
        try:
            created = publish_poll(cfg, client, all_issues, clock())
            if created:
                rows[created["number"]] = created
        except Exception as exc:
            # Status publication is advisory; no message or agent text is saved.
            core.save(heartbeat_failure, {"exception_class": type(exc).__name__})
            if isinstance(exc.__context__, urllib.error.HTTPError) and exc.__context__.code == 404:
                updates = client.pages("/issues?state=all")
                rows = {i["number"]: i for i in updates}
                cache.update(issues=rows, full_at=listing_at)
                all_issues = list(rows.values())
                changed = set(rows)
            if pending:
                return upgrade.DEFER
        else:
            heartbeat_failure.unlink(missing_ok=True)
            if pending:
                upgrade.confirm(cfg, ctx)
        issues = [
            i
            for i in all_issues
            if "pull_request" not in i and not core.is_report(cfg, i)
        ]
        failed, skipped = set(), set()
        error_paths = local_errors(cfg)
        for error_path in error_paths:
            number = core.read(error_path)["issue"]
            changed.add(number)
            if number not in rows:
                try:
                    item = client.issue(number)
                except core.StatusLabelError:
                    skipped.add(number)
                    continue
                except Exception as exc:
                    if isinstance(exc, core.Unavailable) or getattr(exc, "integrity_error", False):
                        raise
                    remember_error(cfg, number, exc)
                    failed.add(number)
                    continue
                rows[number] = item
                issues.append(item)
        claims = [core.read(p) for p in sorted(claims_dir.glob("*.json"))]
        reconciled = []
        for issue in issues:
            with story_boundary(cfg, client, issue, failed, skipped, claims):
                core.state(issue)
                if issue["number"] in changed:
                    issue = backfill_owner_merge(cfg, client, issue)
            reconciled.append(issue)
        issues = reconciled
        by_number = {i["number"]: i for i in issues}
        cancel_waiting(cfg, client, claims, by_number, failed, skipped)
        for issue in issues:
            if issue["number"] in failed | skipped:
                continue
            approval = {}
            with story_boundary(cfg, client, issue, failed, skipped, claims,
                                approval=approval):
                core.state(issue)
                try:
                    action = claim_event(cfg, issue, lambda: client.events(issue["number"]),
                                         issue["number"] in changed)
                    if action:
                        approval["event"] = action[0]
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
                            not core.can_start(issue, proofs)
                            or not proof
                            or proof["scope_sha256"]
                            != core.hashlib.sha256(
                                (issue.get("body") or "").encode()
                            ).hexdigest()
                        ):
                            action = None
                except core.StatusLabelError:
                    raise
                if not action:
                    continue
                event, primary = action
                if primary:
                    history = core.records(cfg, client.comments(issue["number"]), issue["number"])
                    if not core.can_start(issue, history):
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
                if continuation(cfg, client, claim, claims_dir) is False:
                    continue
                core.create(path, claim)
        processed = []
        runner = runner_module()
        queue = StoryQueue(cfg, client, rows, changed, claims_dir)
        ordered = []
        for path in sorted(claims_dir.glob("*.json")):
            claim = core.read(path)
            issue = by_number.get(claim["issue"])
            if not issue or claim["issue"] in failed | skipped:
                continue
            with story_boundary(cfg, client, issue, failed, skipped, claims, claim=claim):
                ordered.append((queue.order(path), path))
        for _, path in sorted(ordered):
            claim = core.read(path)
            if claim["stage"] == "finished" and claim.get("cleaned"):
                continue
            issue = by_number.get(claim["issue"])
            if not issue:
                continue
            if claim["issue"] in failed | skipped:
                continue
            observed = {"issue": issue}
            with story_boundary(cfg, client, issue, failed, skipped, claims, claim=claim,
                                observed=observed):
                core.state(issue)
                if claim["stage"] == "claimed" and core.can_start(issue):
                    waiting, problem = queue.decide(claim)
                    if problem:
                        queue.stop(path, claim, problem)
                        continue
                    if waiting:
                        claim["waiting"] = waiting
                        core.save(path, claim)
                        continue
                    claim.pop("waiting", None)
                # The current all-issues listing already proves closed/final stories
                # cannot execute. Keep fresh evidence checks for claims that can.
                finished = not core.can_start(issue)
                if not finished:
                    issue = client.issue(claim["issue"])
                    observed["issue"] = issue
                    evidence = core.records(cfg, client.comments(claim["issue"]), claim["issue"])
                    finished = not core.can_start(issue, evidence)
                if cancel_waiting_claim(cfg, client, claim, fresh=issue,
                                        history=evidence if not finished else None):
                    finished = not core.can_start(issue)
                if finished:
                    claim["stage"] = "finished"
                    claim.pop("waiting", None)
                    completed_path = (root / "jobs" / claim["job"] / "runner-state"
                                      / "tasks" / (claim["job"] + ".json"))
                    if completed_path.exists():
                        claim["head"] = core.read(completed_path).get("head_commit")
                    core.save(path, claim)
                if claim["stage"] == "finished":
                    # Unexpected stops permanently end runner work, but still
                    # use the normal evidence-bound cleanup below.
                    if (claim.get("error") != "unexpected_error"
                            and not finished and run_jobs and claim.get("config")):
                        completed_config = runner.load_config(claim["config"])
                        completed_job = runner.read(
                            Path(completed_config["state_dir"]) / "tasks" / (claim["job"] + ".json")
                        )
                        if (not completed_job.get("retry_local_end")
                                and not completed_job.get("delivery_retired") and (completed_job.get("stop_saving")
                                or completed_job["status"] == "ready_for_integration"
                                or runner.integration_api().options(completed_config))):
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
                    if previous.get("delivery_retired"):
                        claim.update(stage="finished", head=previous.get("head_commit"))
                        core.save(path, claim)
                        continue
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
                        if previous.get("readiness_stopped"):
                            claim["readiness_stopped"] = True
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
                observed["issue"] = fresh
                if cancel_waiting_claim(cfg, client, claim, fresh=fresh):
                    continue
                if not core.can_start(fresh) or (fresh.get("body") or "") != claim["task"]:
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
                    except core.IntegrityError:
                        raise
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
                        files = continuation(cfg, client, claim, claims_dir, prepare=True)
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
                    notice = {}
                    if "### Continue from" in claim["task"]:
                        record.update({"from": core.state(fresh), "actor": cfg["actors"]["runner"]})
                        notice["public_body"] = (core.record_body(record)
                            + "\nContinuing from the latest saved work; the pasted block is ignored.")
                    outbox = core.queue(cfg, record, **notice)
                    flush_outbox(cfg, client)
                    if core.read(outbox)["status"] != "synced":
                        continue
                    claim["stage"] = "dispatched"
                    claim["was_dispatched"] = True
                    core.save(path, claim)
                    fresh = client.issue(claim["issue"])
                    observed["issue"] = fresh
                if claim["stage"] in {"dispatched", "enqueued"}:
                    remote_records = core.records(
                        cfg, client.comments(claim["issue"]), claim["issue"]
                    )
                    dispatches = [r for r in remote_records if r["to"] == "dispatched"]
                    if cancel_waiting_claim(cfg, client, claim, fresh=fresh, history=remote_records):
                        continue
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
                                          if claim.get("continued_from") and not claim["primary"] else None),
                        )
                    else:
                        existing = runner.read(job_path)
                        if (
                            existing["assignment"].get("lifecycle") != binding
                            or existing["assignment"]["task_text"] != claim["task"]
                        ):
                            raise core.IntegrityError("Claim/job binding mismatch")
                    if claim.get("delivery_source"):
                        existing = runner.read(job_path)
                        if existing["status"] == "queued":
                            resume_delivery(cfg, claim, run_config, job_path)
                    claim.update(stage="enqueued", config=str(config_path))
                    core.save(path, claim)
                if run_jobs and claim["stage"] == "enqueued":
                    run_config = runner.load_config(claim["config"])
                    run_config["_service_retry_clock"] = clock
                    runner.run_once(run_config)
                    job = runner.read(
                        Path(run_config["state_dir"]) / "tasks" / (claim["job"] + ".json")
                    )
                    if job["status"] in runner.TERMINAL:
                        claim.update(stage="finished", head=job.get("head_commit"))
                        if job.get("readiness_stopped"):
                            claim["readiness_stopped"] = True
                    core.save(path, claim)
                processed.append(claim["job"])
        for error_path in error_paths:
            number = core.read(error_path)["issue"]
            if number in by_number and number not in failed | skipped:
                error_path.unlink()
        completed = clock()
        core.save(cursor, {"next_poll": completed + cfg["poll_seconds"]})
        core.save(root / "heartbeat.json", {"last_poll": completed})
        cache["completed_listing_at"] = listing_at
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
    lines.extend("  " + queue_wait_text(n, wait) for n, wait in queue_waiting(cfg).items())
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
    pending = ["  " + line for line in owner_finish_notices(cfg, claims)]
    pending.extend(f"  Issue #{e['issue']}: {e['exception_class']} in {e['function']}"
                   for p in local_errors(cfg) if (e := core.read(p)))
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
            "smoke",
            "smoke-watch",
            "upgrade-check",
            "upgrade-clear",
        ],
    )
    parser.add_argument("commit", nargs="?")
    parser.add_argument("--candidate")
    args = parser.parse_args(argv)
    if args.commit is not None and args.action != "upgrade-clear":
        parser.error("commit requires upgrade-clear")
    if args.action == "upgrade-check":
        return upgrade.check(args.config)
    if args.action == "smoke-watch":
        if args.candidate is not None:
            parser.error("--candidate requires smoke")
        from lifecycle import smoke_watch

        return smoke_watch.main(args.config)
    if args.action == "smoke":
        from lifecycle import smoke

        return smoke.main(args.config, args.candidate)
    if args.candidate is not None:
        parser.error("--candidate requires smoke")
    cfg = core.load_config(args.config)
    cfg["config_path"] = str(args.config.resolve())
    if args.action == "upgrade-clear":
        if args.commit is None:
            parser.error("upgrade-clear requires a commit")
        upgrade.clear(cfg, args.commit)
        return 0
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
            options = {"upgrade_enabled": True, "pending_hook": upgrade.start_pending_timer
                       } if serving and "upgrade" in cfg else {}
            if action == "run-once":
                options["full_list"] = True
            result = poll(cfg, client, run_jobs=action != "enqueue-only", **options)
            if result is upgrade.SWITCH:
                return 0
            if (result is not upgrade.DEFER and action != "enqueue-only"
                    and cfg.get("digest_config")):
                from digest import standup

                digest_cfg = standup.load_config(cfg["digest_config"])
                standup.run_once(
                    digest_cfg, core.GitHub(digest_cfg["lifecycle"], "product")
                )
            if (serving and heartbeat(cfg) != previous_poll
                    and not (root / "public-heartbeat-failure.json").exists()):
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
