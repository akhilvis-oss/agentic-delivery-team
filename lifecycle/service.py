"""Manual/polled lifecycle coordinator. No automatic service installation."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
from pathlib import Path
import time

from lifecycle import core


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
            claim.get("start_commit", cfg["base_branch"]),
        )
    status = git(cfg, "branch", "--show-current", worktree=str(path))
    if status.stdout.strip() != branch:
        raise core.Unavailable("Managed branch changed")


def cleanup(cfg, claim, issue):
    if claim.get("cleaned") or issue["state"] != "closed":
        return
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
    # Never force-delete unmerged history. Operator refreshes the local base first.
    deleted = git(cfg, "branch", "-d", "--", claim["branch"], check=False)
    if deleted.returncode:
        claim["cleanup_pending"] = "branch_not_in_local_base"
    else:
        claim.update(cleaned=True)
        claim.pop("cleanup_pending", None)


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
        core.flush(cfg, client)
        issues = [
            i
            for i in client.pages("/issues?state=all")
            if "pull_request" not in i and not core.is_report(cfg, i)
        ]
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
                )
            core.create(path, claim)
        processed = []
        runner = runner_module()
        for path in sorted(claims_dir.glob("*.json")):
            claim = core.read(path)
            issue = by_number.get(claim["issue"])
            if not issue:
                continue
            if claim["stage"] == "finished":
                canonical, _ = core.canonical_state(cfg, client.events(claim["issue"]))
                if canonical == core.state(issue):
                    cleanup(cfg, claim, issue)
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
                record = core.public_record(
                    cfg,
                    claim["issue"],
                    claim["job"],
                    0,
                    "dispatched",
                    event=claim["event"],
                )
                outbox = core.queue(cfg, record)
                core.flush(cfg, client)
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
                ensure_worktree(cfg, claim)
                job_root = core.private(root / "jobs" / claim["job"])
                template = core.read(
                    cfg["primary_runner_config"]
                    if claim["primary"]
                    else cfg["runner_config"]
                )
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
                    )
                else:
                    existing = runner.read(job_path)
                    if (
                        existing["assignment"].get("lifecycle") != binding
                        or existing["assignment"]["task_text"] != claim["task"]
                    ):
                        raise ValueError("Claim/job binding mismatch")
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
    while True:
        poll(cfg, client, run_jobs=action != "enqueue-only")
        if action != "enqueue-only" and cfg.get("digest_config"):
            from digest import standup

            digest_cfg = standup.load_config(cfg["digest_config"])
            standup.run_once(
                digest_cfg, core.GitHub(digest_cfg["lifecycle"], "product")
            )
        if action in {"run-once", "enqueue-only"}:
            break
        time.sleep(cfg["poll_seconds"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
