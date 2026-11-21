"""Trusted lifecycle policy, repository-scoped transport and durable outbox."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# Isolated direct execution omits the script's directory from sys.path.
# Add only the trusted checkout root, never a configured or working directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from lifecycle import app_tokens
    from lifecycle import private_content as check
except (ImportError, OSError, SyntaxError):
    raise SystemExit(
        "Lifecycle guard unavailable: cannot load lifecycle/private_content.py; "
        "restore the lifecycle package from the same kit release."
    ) from None

PREFIX = "<!-- agentic-lifecycle-v1\n"
SUFFIX = "\n-->"
SUPPORTED_RECORD_VERSIONS = frozenset({1})
STATES = {
    "draft",
    "approved",
    "dispatched",
    "in-progress",
    "committed",
    "in-review",
    "changes-requested",
    "needs-primary",
    "blocked",
    "needs-owner",
    "pr-open",
    "accepted",
    "merged",
}
SHA = re.compile(r"[0-9a-f]{40}")
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?")


class IntegrityError(ValueError):
    """Durable local bindings changed; never isolate as a story bug."""

    integrity_error = True


class StatusLabelError(ValueError):
    """An issue has no unambiguous known lifecycle state."""


class Unavailable(RuntimeError):
    pass


class TransitionRejected(ValueError):
    """A policy rejection, distinct from malformed data or programming errors."""


class Uncertain(Unavailable):
    """A write may have reached GitHub; never blindly repeat it."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stamp(value):
    value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    return value


def read(path):
    return json.loads(Path(path).read_text())


def private(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def save(path, value):
    path = Path(path)
    private(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create(path, value):
    """Publish a complete private JSON file once; never replace an existing claim."""
    path = Path(path)
    private(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".claim-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        os.unlink(temporary)


@contextlib.contextmanager
def lock(path):
    path = Path(path)
    private(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_config(path, *, local=True):
    cfg = read(path)
    if "policy_file" in cfg:
        if not local or not Path(cfg["policy_file"]).is_absolute():
            raise ValueError(
                "Public policy must be loaded from an absolute local reference"
            )
        local_fields = {
            "policy_file",
            "state_dir",
            "repository_root",
            "worktree_root",
            "runner_config",
            "primary_runner_config",
            "token_env",
            "product_token_env",
            "app_auth",
            "upgrade",
            "system_retry_minutes",
        }
        if not set(cfg) <= local_fields:
            raise ValueError("Public policy fields belong in the policy file")
        policy = read(cfg["policy_file"])
        if set(policy) & local_fields:
            raise ValueError("Private settings must not appear in public policy")
        cfg = {**policy, **cfg}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg.get("repository", "")):
        raise ValueError("Explicit repository required")
    if "upgrade" in cfg:
        from lifecycle.upgrade import validate
        validate(cfg["upgrade"])
    schedule = cfg.get("system_retry_minutes", [5, 20, 60])
    if (not isinstance(schedule, list) or len(schedule) > 5
            or any(type(n) is not int or not 1 <= n <= 1440 for n in schedule)):
        raise ValueError("system_retry_minutes must contain 0 to 5 integers from 1 to 1440")
    cfg["system_retry_minutes"] = schedule
    roles = cfg.get("actors", {})
    if set(roles) != {"owner", "runner", "product", "workflow"}:
        raise ValueError("Four explicit actor roles required")
    if any(not isinstance(v, str) or not LOGIN.fullmatch(v) for v in roles.values()):
        raise ValueError("Populate actor logins")
    if roles["owner"] in {roles["runner"], roles["product"], roles["workflow"]}:
        raise ValueError("Automated actors must be separate from owner")
    assignments = cfg.get("identity_roles", {})
    if not isinstance(assignments, dict) or not assignments:
        raise ValueError("Explicit identity-to-role mapping required")
    for identity, assigned in assignments.items():
        if (
            not LOGIN.fullmatch(identity)
            or not isinstance(assigned, list)
            or not assigned
            or not set(assigned) <= set(roles)
        ):
            raise ValueError("Unknown identity role")
        if "owner" in assigned and identity != roles["owner"]:
            raise ValueError("Only the designated owner has the owner role")
    if any(
        role not in assignments.get(identity, []) for role, identity in roles.items()
    ):
        raise ValueError("Each publishing identity must have its declared role")
    if "product" not in assignments.get(roles["owner"], []):
        raise ValueError("Owner must also have Product role")
    if roles["workflow"] != "github-actions[bot]":
        raise ValueError("Workflow actor must match GitHub Actions")
    if set(cfg.get("transitions", {})) != STATES:
        raise ValueError("Define transitions for every lifecycle state")
    for source, destinations in cfg["transitions"].items():
        if not isinstance(destinations, dict) or not set(destinations) <= STATES:
            raise ValueError("Unknown lifecycle transition")
        for allowed in destinations.values():
            if (
                not isinstance(allowed, list)
                or not allowed
                or not set(allowed) <= set(roles)
            ):
                raise ValueError("Transition must name allowed actor roles")
    # Configuration cannot accidentally grant owner approval to automation.
    for destinations in cfg["transitions"].values():
        for target in ("approved", "accepted"):
            permitted = {"owner"} | (
                {"product"}
                if target == "accepted" and cfg.get("delegate_acceptance")
                else set()
            )
            if target in destinations and not set(destinations[target]) <= permitted:
                raise ValueError("Approval and acceptance are owner-controlled")
    product_policy = cfg.get("product_policy", {})
    if (
        product_policy.get("citations_required") is not True
        or product_policy.get("repository_sources_only") is not True
        or not {"scope_change", "acceptance_criteria_change", "story_approval"}
        <= set(product_policy.get("owner_only", []))
    ):
        raise ValueError(
            "Product clarifications require repository citations and owner escalation"
        )
    patterns = product_policy.get("clarification_source_globs", [])
    if (
        not isinstance(patterns, list)
        or not patterns
        or any(
            not isinstance(p, str)
            or not p
            or p.startswith("/")
            or "\\" in p
            or any(part in {"", ".", ".."} for part in p.split("/"))
            for p in patterns
        )
    ):
        raise ValueError("Explicit repository clarification source patterns required")
    thresholds = cfg.get("stale_seconds", {})
    if set(thresholds) != STATES or any(
        type(v) is not int or v < 1 for v in thresholds.values()
    ):
        raise ValueError("Positive stale thresholds required for every state")
    pushback = cfg.get("pushback", {})
    if (
        set(pushback.get("blocking", [])) | set(pushback.get("minor", []))
        != {"P0", "P1", "P2"}
        or set(pushback.get("blocking", [])) & set(pushback.get("minor", []))
        or not {"P0", "P1"} <= set(pushback.get("blocking", []))
        or type(pushback.get("max_correction_rounds")) is not int
        or not 0 <= pushback["max_correction_rounds"] <= 2
    ):
        raise ValueError("P0/P1 must block; at most two correction rounds")
    if type(cfg.get("poll_seconds")) is not int or cfg["poll_seconds"] < 10:
        raise ValueError("Polling interval must be at least ten seconds")
    if "heartbeat_alert_seconds" in cfg and (
        type(cfg["heartbeat_alert_seconds"]) is not int
        or cfg["heartbeat_alert_seconds"] < cfg["poll_seconds"]
    ):
        raise ValueError("Heartbeat alert threshold must be at least one poll interval")
    if local:
        if not Path(cfg.get("state_dir", "")).is_absolute():
            raise ValueError("Private absolute lifecycle state required")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cfg.get("token_env", "")):
            raise ValueError("Explicit token environment name required")
    return cfg


def is_report(cfg, issue):
    labels = {x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])}
    return (
        issue.get("user", {}).get("login") == cfg["actors"]["runner"]
        and "<!-- agentic-service-status-v1 -->" in (issue.get("body") or "")
    ) or (
        issue.get("user", {}).get("login") == cfg["actors"]["product"]
        and "<!-- agentic-service-alert-v1:" in (issue.get("body") or "")
    ) or (
        has_role(cfg, issue.get("user", {}).get("login"), "product")
        and "report:standup" in labels
        and "<!-- agentic-digest-v1:" in (issue.get("body") or "")
    )


def state(issue):
    names = [x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])]
    statuses = [x[7:] for x in names if x.startswith("status:")]
    if len(statuses) != 1 or statuses[0] not in STATES:
        raise StatusLabelError("Issue needs exactly one known status label")
    return statuses[0]


def has_role(cfg, identity, role):
    return role in cfg["identity_roles"].get(identity, [])


def permitted(cfg, source, target, actor):
    allowed = cfg["transitions"].get(source, {}).get(target, [])
    if target == "approved" and actor != cfg["actors"]["owner"]:
        return False
    if (
        target == "accepted"
        and actor != cfg["actors"]["owner"]
        and not cfg.get("delegate_acceptance")
    ):
        return False
    return any(has_role(cfg, actor, role) for role in allowed)


def public_record(
    cfg,
    issue,
    job,
    round_number,
    target,
    *,
    commit=None,
    event=None,
    kind="transition",
    outcome=None,
    severities=None,
    reason=None,
    pr=None,
    gate=None,
    scope_sha256=None,
    approved_by=None,
    owner_question=None,
    failed_tests=None,
    continuation=False,
    stop_save_failed=False,
    conflict_files=None,
    stop_details=None,
    exception_class=None,
    function=None,
):
    if (
        type(issue) is not int
        or issue < 1
        or not ID.fullmatch(job)
        or type(round_number) is not int
        or not 0 <= round_number <= 3
        or target not in STATES
        or commit is not None
        and not SHA.fullmatch(commit)
        or event is not None
        and (type(event) is not int or event < 1)
    ):
        raise ValueError("Malformed lifecycle metadata")
    if kind not in {
        "transition",
        "review",
        "guard",
        "assumption",
        "approval",
        "finish",
        "clarification",
    }:
        raise ValueError("Unknown record kind")
    if outcome not in {None, "ready_for_integration", "needs_changes", "blocked"}:
        raise ValueError("Unknown review outcome")
    if pr is not None and (type(pr) is not int or pr < 1):
        raise ValueError("Invalid PR number")
    if gate not in {None, "pass", "fail", "pending", "unavailable", "not_checked"}:
        raise ValueError("Invalid gate result")
    if kind == "approval":
        if (
            target != "approved"
            or approved_by != cfg["actors"]["owner"]
            or event is None
            or not isinstance(scope_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", scope_sha256)
        ):
            raise ValueError("Approval needs owner event and scope digest")
    elif scope_sha256 is not None or approved_by is not None:
        raise ValueError("Approval metadata only belongs to approval records")
    if kind == "finish" and (
        target != "merged" or event is None or reason != "finished_by_owner"
        or job != "owner-finish" or round_number != 0 or commit is not None
        or pr is not None or gate is not None or outcome is not None
        or severities not in (None, {}) or owner_question is not None
        or failed_tests is not None or continuation or stop_save_failed
        or conflict_files is not None or stop_details is not None
    ):
        raise ValueError("Finish needs an owner label event and fixed metadata")
    counts = severities or {}
    if set(counts) - {"P0", "P1", "P2"} or any(
        type(n) is not int or n < 0 for n in counts.values()
    ):
        raise ValueError("Invalid severity counts")
    if target in {"blocked", "needs-owner", "needs-primary"}:
        reason = reason or "execution_failed"
        if reason not in STOP_REASONS:
            raise ValueError("Unknown stop reason")
    if reason is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
        raise ValueError("Reason must be a fixed code")
    record = dict(
        version=1,
        repository=cfg["repository"],
        issue=issue,
        job=job,
        round=round_number,
        to=target,
        commit=commit,
        event=event,
        kind=kind,
        outcome=outcome,
        severities=counts,
        reason=reason,
        pr=pr,
        gate=gate,
        scope_sha256=scope_sha256,
        approved_by=approved_by,
        timestamp=now(),
    )
    record["key"] = key(
        [
            issue,
            job,
            round_number,
            target,
            kind,
            event,
            outcome,
            reason,
            commit,
            pr,
            gate,
            scope_sha256,
        ]
    )
    if owner_question is not None or failed_tests is not None:
        if target not in {"blocked", "needs-owner", "needs-primary"}:
            raise ValueError("Stop details require a stopped task")
        if owner_question is not None:
            if isinstance(owner_question, str):
                owner_question = inert_mentions(owner_question)
            if not safe_question(cfg, owner_question):
                raise ValueError("Question is not safe for publication")
            record["owner_question"] = owner_question
        if failed_tests is not None:
            if safe_failures(failed_tests) != failed_tests:
                raise ValueError("Invalid public test summary")
            record["failed_tests"] = failed_tests
        record["key"] = key([record["key"], owner_question, failed_tests])
    if continuation or stop_save_failed:
        if (target not in {"blocked", "needs-owner"}
                or type(continuation) is not bool or type(stop_save_failed) is not bool
                or continuation and commit is None):
            raise ValueError("Invalid stop preservation details")
        record.update(continuation=continuation, stop_save_failed=stop_save_failed)
        record["key"] = key([record["key"], continuation, stop_save_failed])
    if conflict_files is not None:
        if (reason != "continuation_conflict" or not isinstance(conflict_files, list)
                or not conflict_files or any(
                    not isinstance(f, str) or not f or f.startswith("/")
                    or "\\" in f or ":" in f
                    or any(p in {"", ".", ".."} for p in f.split("/"))
                    or any(ord(c) < 32 or ord(c) == 127 for c in f)
                    for f in conflict_files)):
            raise ValueError("Invalid conflict file names")
        record["conflict_files"] = conflict_files
        record["key"] = key([record["key"], conflict_files])
    if stop_details is not None:
        if target not in {"blocked", "needs-owner", "needs-primary"}:
            raise ValueError("Stop details require a stopped task")
        record["stop_details"] = public_stop_details(cfg, stop_details)
        record["key"] = key([record["key"], record["stop_details"]])
    if exception_class is not None or function is not None:
        if (reason != "unexpected_error"
                or exception_class is not None and (not isinstance(exception_class, str)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_class))
                or function is not None and (not isinstance(function, str)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", function))):
            raise ValueError("Invalid unexpected error metadata")
        if exception_class is not None:
            record["exception_class"] = exception_class
        if function is not None:
            record["function"] = function
        record["key"] = key([record["key"], exception_class, function])
    return record


# Fixed reason codes; agent prose is sanitized separately before publication.
STOP_REASONS = {
    "agent_sign_in_failed": "The agent couldn't sign in. Owner: ask the operator to restore the agent's sign-in, then re-approve.",
    "agent_usage_limit": "The agent reached its usage limit. Owner: re-approve once the limit resets.",
    "unexpected_error": "An unexpected error stopped this story only. Owner: ask the operator to investigate, then re-approve.",
    "readiness_question": 'Readiness needs your decision before building. Owner: answer under "Owner decisions", then re-approve.',
    "checks_worktree_changed": "The worktree changed while the required checks ran. Owner: re-approve to build the story again.",
    "commit_refused": "The runner refused to commit this change. This kind of change needs an owner-delivered PR. Owner: arrange that PR before re-approving.",
    "delivery_disabled": "Delivery is off for this repository. Owner: enable delivery in the service configuration, then re-approve to publish the reviewed work.",
    "delivery_configuration_invalid": "Delivery settings are incomplete. Nothing was dispatched. Ask the operator to configure complete delivery settings or explicitly turn delivery off, then re-approve.",
    "continuation_commit_unrecorded": "The latest saved work could not be verified on this issue. Owner: ask the operator to reconcile it, or add Restart: fresh under Owner decisions and re-approve.",
    "continuation_conflict": "Saved work conflicts with the target branch. Owner: arrange conflict resolution before re-approving, or add Restart: fresh under Owner decisions and re-approve.",
    "continuation_limit": "This story has reached its two-continuation limit. Owner: restart fresh (add Restart: fresh under Owner decisions and re-approve), or split the story.",
    "pr_open_reapproval": "This story has an open PR. Owner: close the PR and re-approve to rebuild, or merge it and add `status:merged` to finish (#184).",
    "continuation_not_applied": "The continued commit is missing from the built head. Owner: reconcile the history before re-approving.",
    "owner_question": 'The builder needs your decision. Owner: add the answer to the story under "Owner decisions", then re-approve.',
    "question_withheld": 'The builder asked a question containing potentially private content, so it could not be published. Owner: write "Please ask a public-safe question" under "Owner decisions", then re-approve.',
    "scope_change": "A scope decision needs you. Owner: record the scope under Owner decisions, then re-approve.",
    "acceptance_criteria_change": "An acceptance criteria decision needs you. Owner: update the story and re-approve.",
    "story_approval": "The story needs your approval. Owner: review the story and re-approve.",
    "clarification_unverified": "Repository evidence could not answer the question. Owner: add the answer under Owner decisions, then re-approve.",
    "product_request_unclassified": "The request needs an owner decision. Owner: clarify the story under Owner decisions, then re-approve.",
    "invalid_status_change": "The guard restored a stopped task after an invalid label change. Owner: read the preceding stop message, resolve it, then re-approve.",
    "approval_not_attested": "Approval could not be verified and its label was removed. Owner: resolve conflicting labels, then remove and re-add status:approved to re-approve.",
    "owner_requested_stop": "The task was stopped by a permitted label change. Owner: add any decision under Owner decisions, resolve the reason for stopping, then re-approve.",
    "repository_evidence": "Repository evidence is available, but the task still needs your decision. Owner: answer under Owner decisions, then re-approve.",
    "prerequisite_failed": "A required tool or safety check couldn't run or failed; owner: fix the prerequisite and authorize a new task.",
    "assignment_preflight": "Assignment safety checks stopped the task; owner: reconcile the assignment before authorizing a new task.",
    "worker_reported_blocked": "The builder could not finish a required check; owner: ask the operator to fix setup, then re-approve.",
    "reviewer_reported_blocked": "Independent review could not accept the work; owner: resolve the outstanding review before authorizing further correction.",
    "execution_failed": "Execution stopped or was interrupted; owner: ask the operator to reconcile the interrupted work, then re-approve.",
    "publication_failed": "Publishing the reviewed work failed; owner: reconcile GitHub before retrying.",
    "publication_uncertain": "Publishing may have completed; owner: inspect GitHub before retrying.",
    "stale_head": "The reviewed head changed; owner: arrange a fresh review.",
    "gate_category_higher": "The gate requires a higher review category; owner: reconcile the review evidence.",
    "required_check_failed": "A runner or pull request check failed; owner: inspect the checks before authorizing correction.",
    "merge_conditions_missing": "Required merge evidence is missing; owner: reconcile the evidence before merging.",
    "owner_merge_required": "The reviewed work requires an owner merge; owner: inspect the evidence and merge when satisfied.",
    "auto_merge_disable_uncertain": "Auto-merge cancellation is unconfirmed; owner: inspect the pull request before proceeding.",
    "pr_closed_unmerged": "The pull request closed without merging; owner: decide whether further work is needed.",
    "approved_scope_changed": "The approved scope changed; owner: review the new scope and approve it before further work.",
}
# Explicit classification: adding a reason requires choosing its kind here.
# Keep reason texts and persisted records independent of this presentation table.
STOP_KINDS = {
    "agent_sign_in_failed": "Waiting on the system",
    "agent_usage_limit": "Waiting on the system",
    "unexpected_error": "Needs a fix",
    "readiness_question": "Needs your decision",
    "checks_worktree_changed": "Needs a fix",
    "owner_question": "Needs your decision",
    "question_withheld": "Needs your decision",
    "scope_change": "Needs your decision",
    "acceptance_criteria_change": "Needs your decision",
    "story_approval": "Needs your decision",
    "clarification_unverified": "Needs your decision",
    "product_request_unclassified": "Needs your decision",
    "repository_evidence": "Needs your decision",
    "approved_scope_changed": "Needs your decision",
    "pr_closed_unmerged": "Needs your decision",
    "owner_requested_stop": "Needs your decision",
    "continuation_limit": "Needs your decision",
    "pr_open_reapproval": "Needs your decision",
    "owner_merge_required": "Needs your merge",
    "commit_refused": "Needs a fix",
    "delivery_disabled": "Needs a fix",
    "delivery_configuration_invalid": "Needs a fix",
    "continuation_commit_unrecorded": "Needs a fix",
    "continuation_conflict": "Needs a fix",
    "continuation_not_applied": "Needs a fix",
    "invalid_status_change": "Needs a fix",
    "approval_not_attested": "Needs a fix",
    "prerequisite_failed": "Needs a fix",
    "assignment_preflight": "Needs a fix",
    "worker_reported_blocked": "Needs a fix",
    "reviewer_reported_blocked": "Needs a fix",
    "execution_failed": "Needs a fix",
    "publication_failed": "Needs a fix",
    "publication_uncertain": "Needs a fix",
    "stale_head": "Needs a fix",
    "gate_category_higher": "Needs a fix",
    "required_check_failed": "Needs a fix",
    "merge_conditions_missing": "Needs a fix",
    "auto_merge_disable_uncertain": "Needs a fix",
}
for _check, _label in {
    "preflight": "Preflight", "build": "Build", "story_tests": "Story tests",
    "runner_checks": "Runner checks", "prerequisite": "A required prerequisite",
}.items():
    STOP_KINDS["worker_" + _check] = "Needs a fix"
    STOP_KINDS["worker_" + _check + "_unavailable"] = "Waiting on the system"
    STOP_KINDS["worker_" + _check + "_unrelated_failure"] = "Needs a fix"
    STOP_REASONS["worker_" + _check] = (
        _label + ": worker reported blocked without a specific cause; "
        "owner: ask the operator to resolve the failed check, then re-approve."
    )

    STOP_REASONS["worker_" + _check + "_unavailable"] = (
        _label + " couldn't run because a required tool, dependency or environment was unavailable; "
        "owner: restore the prerequisite and authorize a new task."
    )
    STOP_REASONS["worker_" + _check + "_unrelated_failure"] = (
        _label + " failed for a reason unrelated to the worker's changes; "
        "owner: resolve the existing failure and authorize a new task."
    )
    for _attempts in range(1, 11):
        STOP_KINDS[f"worker_{_check}_attempts_exhausted_{_attempts}"] = "Needs a fix"
        STOP_REASONS[f"worker_{_check}_attempts_exhausted_{_attempts}"] = (
            f"{_label} still failing after {_attempts} check attempts; "
            "owner: arrange further correction and authorize a new task."
        )


def stop_notice(reason, body):
    """Add the kind only at transport time; non-stop prose stays unchanged."""
    kind = STOP_KINDS.get(reason)
    return kind + ": " + body if kind else body


def inert_mentions(text):
    """Break mention syntax idempotently, leaving email addresses readable."""
    return re.sub(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})|@(?!\u200d)",
                  lambda match: match[1] or "@\u200d", text)


def public_quote(cfg, text, limit):
    """Sanitize before truncating; never trust exemptions in agent-supplied prose."""
    text = text.replace(check.ALLOW_MARKER, "[removed: exemption]")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "[removed: control character]", text)
    for name, pattern in check.CHECKS:
        if name == "home-directory path":
            # Remove the rest of the path too, not just the home directory.
            pattern = re.compile(pattern.pattern + r"[^\s\"'<>]*")
        elif name == "private key":
            # The scanner detects the header, but the whole key is private.
            # An incomplete block consumes the remaining text to fail closed.
            pattern = re.compile(
                pattern.pattern + r".*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
        text = pattern.sub("[removed: local path]" if name == "home-directory path"
                           else "[removed: " + name + "]", text)
    text = re.sub(r"(?<![\w])(?:/(?:[^\s/]+/)*[^\s<>\"']+|[A-Za-z]:\\[^\s]+|~/[^\s]+)",
                  "[removed: local path]", text)
    parts = re.split(r"(\[removed: [^\]\n]+\])", text)
    identities = "|".join(re.escape(name) for name in sorted(cfg["identity_roles"], key=len, reverse=True))
    if identities:
        for i in range(0, len(parts), 2):
            parts[i] = re.sub(identities, "[removed: account name]", parts[i], flags=re.I)
    text = "".join(parts)
    text = re.sub(r"(?im)^.*(?:provider response|traceback|stdout|stderr|prompt text|private log).*$",
                  "[removed: log or prompt text]", text)
    # The scanner is the final authority, including any future added checks.
    flagged = {number: name for number, name in check.scan_text(text)}
    text = "\n".join("[removed: " + flagged[n] + "]" if n in flagged else line
                     for n, line in enumerate(text.split("\n"), 1))
    # Neutralize HTML, including lifecycle record delimiters, idempotently.
    text = text.replace("<", "[less-than]").replace(">", "[greater-than]")
    text = inert_mentions(text)
    marker = " [truncated]"
    return text if len(text) <= limit else text[:limit - len(marker)] + marker


def public_stop_details(cfg, details):
    if (not isinstance(details, dict)
            or "summary" in details and not isinstance(details["summary"], str)
            or type(details.get("more", 0)) is not int or details.get("more", 0) < 0):
        raise ValueError("Invalid public stop details")
    if "findings" in details and (
            not isinstance(details["findings"], list) or any(
                not isinstance(f, dict) or not isinstance(f.get("detail"), str)
                or f.get("severity") not in {"P0", "P1", "P2"}
                or "criterion" in f and not isinstance(f["criterion"], str)
                for f in details["findings"])):
        raise ValueError("Invalid public findings")
    if "refusal" in details:
        refusal = details["refusal"]
        if (not isinstance(refusal, dict) or not isinstance(refusal.get("reason"), str)
                or not isinstance(refusal.get("paths"), list)
                or type(refusal.get("more", 0)) is not int or refusal.get("more", 0) < 0
                or any(not isinstance(p, str) for p in refusal["paths"])):
            raise ValueError("Invalid public refusal")
    result = {}
    if "summary" in details:
        result["summary"] = public_quote(cfg, details["summary"], 1500)
    if "findings" in details:
        result["findings"] = [dict(
            severity=f["severity"] if f["severity"] in {"P0", "P1", "P2"} else "unspecified",
            criterion=public_quote(cfg, f.get("criterion") or ", ".join(
                dict.fromkeys(re.findall(r"\bAC-[0-9]+\b", f["detail"])))
                or "Criterion not supplied", 100),
            detail=public_quote(cfg, f["detail"], 600)) for f in details["findings"][:10]]
        result["more"] = details.get("more", 0) + max(0, len(details["findings"]) - 10)
    if "refusal" in details:
        refusal = details["refusal"]
        paths, remaining = [], 6000
        for name in refusal["paths"]:
            if remaining < 20:
                break
            name = public_quote(cfg, name, min(600, remaining))
            paths.append(name)
            remaining -= len(name) + 4
        result["refusal"] = dict(reason=public_quote(cfg, refusal["reason"], 100), paths=paths,
                                 more=refusal.get("more", 0) + len(refusal["paths"]) - len(paths))
    return result


def safe_question(cfg, question):
    """Only an explicitly supplied public question may leave the builder result.

    Fail closed on private-looking text; never rewrite a question and call it a
    verbatim quote, apart from making mentions inert. Summaries, limitations
    and provider replies are not sources.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        return False
    if any(name.lower() in question.lower() for name in cfg["identity_roles"]):
        return False
    if re.search(r"[A-Za-z0-9_-]{40,}", question):
        return False
    if public_quote(cfg, question, 2000) != inert_mentions(question):
        return False
    # Mentions are made inert when constructing the public record. Mask only
    # mention syntax for validation; email addresses and paths still fail closed.
    question = re.sub(r"@\u200d[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?",
                      "[mention]", inert_mentions(question))
    if re.search(r"[/\\@<>\x00-\x08\x0b-\x1f\x7f]|[A-Za-z0-9_-]{40,}", question):
        return False
    # Sensitive values also appear in prose ("use account ...", "token ...").
    # Withhold the whole question rather than guessing where a value ends or
    # rewriting text that the owner is promised will be quoted verbatim.
    if re.search(r"(?i)(?:token|password|secret|api.key|account|login)\s*[:=]|\b(?:tokens?|passwords?|secrets?|api[ ._-]?keys?|accounts?|logins?|credentials?)\b|\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|AKIA[A-Z0-9]{16}|Bearer\s)", question):
        return False
    if re.search(r"(?im)\b(?:provider response|traceback|stdout|stderr|private key)\b", question):
        return False
    if public_quote(cfg, question, 2000) != question:
        return False
    return True


def safe_failures(value):
    """Publish counts and bare test functions, never node paths or parameters."""
    if (not isinstance(value, dict) or set(value) != {"count", "names"}
            or type(value["count"]) is not int or value["count"] < 1
            or not isinstance(value["names"], list)):
        return None
    names = list(dict.fromkeys(n for n in value["names"]
                              if isinstance(n, str) and re.fullmatch(r"test_[a-zA-Z0-9_]{1,100}", n)))[:5]
    return {"count": value["count"], "names": names}


def record_body(record):
    explanation = ""
    if record["to"] in {"blocked", "needs-owner", "needs-primary"}:
        explanation = STOP_REASONS.get(record.get("reason"), STOP_REASONS["execution_failed"]) + "\n"
        if record.get("reason") == "unexpected_error" and record.get("function"):
            explanation = ("An unexpected error stopped this story only, in "
                           + record["function"]
                           + ". Owner: ask the operator to investigate, then re-approve.\n")
        if record.get("owner_question"):
            explanation += "\nBuilder question (verbatim):\n" + "\n".join(
                "> " + line for line in record["owner_question"].split("\n")
            ) + "\n"
        if record.get("failed_tests"):
            failures = record["failed_tests"]
            explanation += f"\nFailing tests: {failures['count']}. First failures: "
            explanation += ", ".join(failures["names"]) or "names unavailable"
            explanation += ". Owner: arrange a fix, then re-approve.\n"
    details = record.get("stop_details", {})
    if "summary" in details:
        explanation += "\nBuilder stop summary (quoted):\n" + "\n".join(
            "> " + line for line in details["summary"].split("\n")) + "\n"
    for finding in details.get("findings", []):
        explanation += "\nOpen finding: " + finding["severity"] + " — " + finding["criterion"] + "\n"
        explanation += "\n".join("> " + line for line in finding["detail"].split("\n")) + "\n"
    if details.get("more"):
        explanation += f"\nand {details['more']} more\n"
    if "refusal" in details:
        explanation += "\nCommit refusal: " + details["refusal"]["reason"] + "\nRefused repository-relative paths:\n"
        explanation += "\n".join("- " + json.dumps(name, ensure_ascii=False)
                                 for name in details["refusal"]["paths"]) + "\n"
        if details["refusal"].get("more"):
            explanation += f"and {details['refusal']['more']} more refused paths [truncated]\n"
    tail = ""
    if record.get("conflict_files"):
        explanation += "\nConflicting files (repository-relative):\n" + "\n".join(
            "- " + json.dumps(f, ensure_ascii=True) for f in record["conflict_files"]
        ) + "\n"
    if record.get("stop_save_failed"):
        explanation += "\nThe builder's work couldn't be saved completely; owner: ask the operator to reconcile it.\n"
    if record.get("continuation"):
        tail = ("\n\nSaved work is available. Re-approve to continue it, or add "
                "Restart: fresh under Owner decisions and re-approve to restart fresh.")
    return (
        "Task lifecycle record (public details; no approval implied).\n"
        + explanation
        + PREFIX
        + json.dumps(record, sort_keys=True, ensure_ascii=False)
        + SUFFIX
        + tail
    )


def finding_notice(cfg, findings):
    details = public_stop_details(cfg, {"findings": findings})
    text = ""
    for finding in details["findings"]:
        text += "\nOpen finding: " + finding["severity"] + " — " + finding["criterion"] + "\n"
        text += "\n".join("> " + line for line in finding["detail"].split("\n")) + "\n"
    if details.get("more"):
        text += f"\nand {details['more']} more\n"
    return text


def notice_body(cfg, client, entry):
    """Render optional job context at transport time without altering the record."""
    record = entry["record"]
    body = entry.get("public_body", record_body(record))
    context = entry.get("notice_context", {})
    extra = ""
    if (record["kind"] == "review" and context.get("findings")
            and not record.get("stop_details", {}).get("findings")):
        extra += finding_notice(cfg, context["findings"])
    if context.get("delivery") and record.get("reason"):
        from runner.integration import REASONS, smoke_notice

        reason = record["reason"]
        message = REASONS.get(reason, STOP_REASONS[reason])
        if reason == "owner_merge_required" and record.get("pr"):
            message += f"\nhttps://github.com/{cfg['repository']}/pull/{record['pr']}"
            if context.get("smoke_reporting"):
                message += "\n" + smoke_notice(client, cfg, context.get("reviewed_head", ""))
        message += "\nReviewed head: " + context.get("reviewed_head", "unavailable")
        extra += message + "\n\n"
    # Budget only supplemental prose. Never truncate the record's data block.
    available = 65535 - len(stop_notice(record.get("reason"), body))
    if len(extra) > available:
        marker = "\n[truncated]\n"
        extra = extra[:max(0, available - len(marker))] + marker if available >= len(marker) else ""
    return stop_notice(record.get("reason"), extra + body)


def records(cfg, comments, expected_issue=None):
    trusted = {
        identity
        for identity in cfg["identity_roles"]
        if any(has_role(cfg, identity, r) for r in ("runner", "product", "workflow"))
    }
    found = {}
    for comment in comments:
        actor = comment.get("user", {}).get("login")
        body = comment.get("body", "")
        if actor not in trusted or body.count(PREFIX) != 1 or body.count(SUFFIX) != 1:
            continue
        try:
            item = json.loads(body.split(PREFIX)[1].split(SUFFIX)[0])
            if not isinstance(item, dict):
                continue
            if (item.get("job") == "guard" and item.get("kind") == "guard"
                    and item.get("reason") == "invalid_status_change"):
                continue
            if type(item["version"]) is not int or item["version"] not in SUPPORTED_RECORD_VERSIONS:
                continue
            created = comment.get("created_at")
            updated = comment.get("updated_at")
            # Retain finality even if a finished record was edited later.
            if (item["to"] not in {"merged", "accepted"}
                    and created is not None and updated is not None
                    and stamp(updated) > stamp(created)):
                continue
            expected = public_record(
                cfg,
                item["issue"],
                item["job"],
                item["round"],
                item["to"],
                commit=item["commit"],
                event=item["event"],
                kind=item["kind"],
                outcome=item["outcome"],
                severities=item["severities"],
                reason=item["reason"],
                pr=item["pr"],
                gate=item["gate"],
                scope_sha256=item["scope_sha256"],
                approved_by=item["approved_by"],
                owner_question=item.get("owner_question"),
                failed_tests=item.get("failed_tests"),
                continuation=item.get("continuation", False),
                stop_save_failed=item.get("stop_save_failed", False),
                conflict_files=item.get("conflict_files"),
                stop_details=item.get("stop_details"),
                exception_class=item.get("exception_class"),
                function=item.get("function"),
            )
            if (
                set(item) != set(expected) | {"from", "actor"}
                or item["key"] != expected["key"]
                or item["repository"] != cfg["repository"]
                or item["actor"] != actor
                or item["from"] not in STATES
            ):
                continue
            if item["to"] == "merged" and actor == cfg["actors"]["owner"]:
                continue
            if item["kind"] == "finish" and item["from"] == "merged":
                continue
            if expected_issue is not None and item["issue"] != expected_issue:
                continue
            if (
                item["kind"] in {"guard", "approval", "finish"}
                and actor != cfg["actors"]["workflow"]
            ):
                continue
            if item["kind"] == "review" and not has_role(cfg, actor, "runner"):
                continue
            if item["kind"] in {"assumption", "clarification"} and not has_role(
                cfg, actor, "product"
            ):
                continue
            special = (
                item["kind"] in {"guard", "approval", "finish"}
                and actor == cfg["actors"]["workflow"]
                or item["kind"] in {"assumption", "clarification"}
                and has_role(cfg, actor, "product")
                and item["from"] == item["to"]
            )
            if not special and not permitted(cfg, item["from"], item["to"], actor):
                continue
            if item["kind"] == "guard" and (
                item["from"] != item["to"] or item["reason"] not in {
                    "invalid_status_change", "owner_requested_stop"}
            ):
                continue
            stamp(item["timestamp"])
            found[item["key"]] = item
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(found.values(), key=lambda x: (stamp(x["timestamp"]), x["key"]))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicGitHub:
    """Unauthenticated, repository-scoped upgrade reads; no credential discovery."""

    def __init__(self, repository):
        if not isinstance(repository, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
            raise ValueError("Invalid public repository")
        self.repository = repository
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, data=None):
        allowed = re.fullmatch(
            r"/(?:commits/main|commits/[0-9a-f]{40}/statuses"
            r"(?:\?per_page=100&page=[1-9][0-9]*)?|"
            r"compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40})", path)
        if method != "GET" or data is not None or not allowed:
            raise ValueError("Only public upgrade GET requests are permitted")
        req = urllib.request.Request(
            "https://api.github.com/repos/" + self.repository + path,
            method="GET",
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                self.has_next_page = 'rel="next"' in response.headers.get("Link", "")
                return json.loads(response.read())
        except (OSError, ValueError):
            # Includes 403 and 429: unavailable reads never refuse a candidate.
            raise Unavailable("Public GitHub read unavailable") from None

    def pages(self, path):
        result, seen = [], set()
        for page in range(1, 1001):
            batch = self.request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise Unavailable("Incomplete GitHub collection")
            for item in batch:
                if (not isinstance(item, dict) or type(item.get("id")) is not int
                        or item["id"] in seen):
                    raise Unavailable("Partial, duplicate or unstable GitHub collection")
                seen.add(item["id"])
            result.extend(batch)
            # Do not probe an empty page at exact multiples of 100. Never follow
            # a server-supplied URL: construct the next repository path ourselves.
            if not self.has_next_page:
                return result
        raise Unavailable("GitHub collection exceeds safety bound")


class GitHub:
    """Only this repository; no shell, credential discovery or arbitrary URLs."""

    def __init__(self, cfg, role="runner", token=None, *, integration_enabled=False):
        if type(integration_enabled) is not bool or (integration_enabled and role != "runner"):
            raise Unavailable("Invalid integration authentication opt-in")
        self.cfg, self.role = cfg, role
        self.integration_enabled = integration_enabled
        self.token_options = {"integration_enabled": integration_enabled}
        self.use_app_auth = token is None and role in cfg.get("app_auth", {})
        self.token_expiry = None
        self.token = (
            token
            if token is not None
            else os.environ.get(
                cfg.get("product_token_env", "PRODUCT_GITHUB_TOKEN")
                if role == "product"
                else cfg.get("token_env", "RUNNER_GITHUB_TOKEN")
            )
        )
        if self.use_app_auth:
            self.refresh_app_token()
        if not self.token or cfg["actors"][role] == cfg["actors"]["owner"]:
            raise Unavailable("Dedicated automation token required")
        if any(ord(c) < 33 for c in self.token):
            raise Unavailable("Invalid automation token")
        self.opener = urllib.request.build_opener(NoRedirect)
        self.identity_verified = False

    def refresh_app_token(self):
        # Also works when core.py is executed in isolated mode by the guard or
        # imported by the standalone runner; no path from config is executable.
        try:
            self.token, self.token_expiry = app_tokens.get(
                self.cfg, self.role, **self.token_options
            )
        except app_tokens.AuthenticationError as error:
            raise Unavailable(str(error)) from None
        self.identity_verified = False

    def invalidate_app_token(self):
        app_tokens.invalidate(self.token)
        if self.use_app_auth:
            self.token_expiry = 0
        self.identity_verified = False
    def smoke_statuses(self, head):
        # Ordinary job-sync clients and delivery clients share the existing
        # isolated status-only scope; issue/delivery tokens do not gain it.
        return app_client(self.cfg, status_only=True).pages(
            f"/commits/{head}/statuses")

    def ensure_actor(self):
        if self.token_expiry is not None and time.time() >= self.token_expiry - 300:
            self.refresh_app_token()
        if self.identity_verified:
            return
        # Fixed read-only GraphQL query authenticates the token before ANY write.
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            method="POST",
            data=b'{"query":"query { viewer { login } }"}',
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                actor = json.loads(response.read())["data"]["viewer"]["login"]
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.invalidate_app_token()
            raise Unavailable("Cannot verify automation identity; no write made") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise Unavailable(
                "Cannot verify automation identity; no write made"
            ) from None
        if (
            actor != self.cfg["actors"][self.role]
            or actor == self.cfg["actors"]["owner"]
        ):
            raise Unavailable("Wrong automation identity; no write made: " + str(actor))
        self.identity_verified = True

    def write_allowed(self, method, path):
        if method == "DELETE":
            return bool(re.fullmatch(r"/issues/[1-9]\d*/labels/status%3A(?:draft|approved)", path))
        if method == "PATCH" and re.fullmatch(r"/issues/comments/[1-9]\d*", path):
            return True
        if method == "PUT" and re.fullmatch(r"/issues/comments/[1-9]\d*/pin", path):
            return True
        return bool(re.fullmatch(
            r"/(?:labels|issues(?:/[1-9]\d*(?:/(?:comments|assignees|labels))?)?)", path
        ))

    def request(self, method, path, data=None):
        comparison = method == "GET" and re.fullmatch(r"/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}", path)
        if (not path.startswith("/") or (".." in path and not comparison)
                or "#" in path or "\\" in path):
            raise ValueError("Invalid repository API path")
        self.ensure_actor()
        if method != "GET":
            if not self.write_allowed(method, path):
                raise ValueError("Only repository issue/label writes are permitted")
            self.ensure_actor()
        suffix = "" if method == "GET" and path == "/" else path
        url = "https://api.github.com/repos/" + self.cfg["repository"] + suffix
        req = urllib.request.Request(
            url,
            method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.invalidate_app_token()
            if method != "GET" and exc.code >= 500:
                raise Uncertain("GitHub write outcome unknown") from None
            raise Unavailable("GitHub request rejected") from None
        except (OSError, ValueError):
            error = Unavailable if method == "GET" else Uncertain
            raise error("GitHub transport unavailable") from None
        if (
            method == "POST"
            and (path == "/issues" or path.endswith("/comments"))
            and isinstance(value, dict)
            and "user" in value
        ):
            # Response attribution must agree with configured automation identity.
            if value["user"]["login"] != self.cfg["actors"][self.role]:
                raise Uncertain("Unexpected automation identity; stop and reconcile")
        return value

    def pages(self, path):
        separator = "&" if "?" in path else "?"
        result = []
        seen = set()
        for page in range(1, 1001):
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise Unavailable("Incomplete GitHub collection")
            for item in batch:
                if (
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or item["id"] in seen
                ):
                    raise Unavailable(
                        "Partial, duplicate or unstable GitHub collection"
                    )
                seen.add(item["id"])
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise Unavailable("GitHub collection exceeds safety bound")

    def issue(self, number):
        return self.request("GET", f"/issues/{number}")

    def comments(self, number):
        return self.pages(f"/issues/{number}/comments")

    def events(self, number):
        return self.pages(f"/issues/{number}/events")

    def set_state(self, number, target):
        issue = self.issue(number)
        labels = [x["name"] if isinstance(x, dict) else x for x in issue["labels"]]
        labels = [x for x in labels if not x.startswith("status:")] + [
            "status:" + target
        ]
        return self.request("PUT", f"/issues/{number}/labels", {"labels": labels})

    def notify(self, number):
        if self.cfg["actors"]["owner"].endswith("[bot]"):
            return None
        if any(
            a["login"] == self.cfg["actors"]["owner"]
            for a in self.issue(number).get("assignees", [])
        ):
            return None
        return self.request(
            "POST",
            f"/issues/{number}/assignees",
            {"assignees": [self.cfg["actors"]["owner"]]},
        )


def app_client(cfg, role="runner", **token_options):
    token, expiry = app_tokens.get(cfg, role, **token_options)
    client = GitHub(cfg, role, token=token)
    client.use_app_auth = True
    client.token_options = token_options
    client.token_expiry = expiry
    return client


def state_identity(cfg):
    return {
        "repository": cfg["repository"],
        "actors": cfg["actors"],
        "identity_roles": cfg["identity_roles"],
    }


def validate_state(cfg):
    """Check an existing ownership binding without initializing or modifying state."""
    path = Path(cfg["state_dir"]) / "identity.json"
    if path.exists() and read(path) != state_identity(cfg):
        raise IntegrityError(
            "Private state belongs to another repository or actor configuration"
        )


def bind_state(cfg):
    root = private(cfg["state_dir"])
    identity = state_identity(cfg)
    path = root / "identity.json"
    create(path, identity)
    if read(path) != identity:
        raise IntegrityError(
            "Private state belongs to another repository or actor configuration"
        )


def queue(cfg, record, *, role="runner", public_body=None, notice_context=None):
    bind_state(cfg)
    if record["repository"] != cfg["repository"]:
        raise IntegrityError("Wrong outbox repository")
    folder = private(Path(cfg["state_dir"]) / "outbox")
    path = folder / (record["key"] + ".json")
    entry = dict(record=record, role=role, status="pending", comment_attempted=False)
    if public_body is not None:
        entry["public_body"] = public_body
    if notice_context is not None:
        entry["notice_context"] = notice_context
    create(path, entry)
    return path


def owner_merge_verified(cfg, issue, evidence, pr):
    """Narrow authority for informational owner-merge reconciliation."""
    stops = [r for r in evidence if r["to"] in {"blocked", "needs-owner", "needs-primary"}
             and r.get("reason")]
    return bool(
        stops and stops[-1]["to"] == "needs-owner"
        and stops[-1]["reason"] == "owner_merge_required"
        and stops[-1]["pr"] in {None, pr["number"]}
        and any(r["kind"] == "transition" and r["to"] == "dispatched"
                and has_role(cfg, r["actor"], "runner") for r in evidence)
        and pr.get("merged") is True
        and pr["base"]["ref"] == cfg.get("base_branch", "main")
        and pr["head"].get("repo", {}).get("full_name") == cfg["repository"]
        and pr.get("user", {}).get("login") in set(cfg["actors"].values())
        and issue in {int(n) for n in re.findall(
            r"\bCloses\s+#([1-9]\d*)\b", pr.get("body") or "", re.I)}
        and any(r["kind"] == "review" and r["outcome"] == "ready_for_integration"
                and r["commit"] == pr["head"]["sha"] for r in evidence)
    )


def can_start(issue, records=()):
    """One admission rule for dispatch, continuation, recovery and publication."""
    return not (issue["state"] == "closed" or state(issue) in {"merged", "accepted"}
                or any(record["to"] == "merged" for record in records)
                or bool(records and records[-1]["to"] == "accepted"))


def rejection_body(record):
    return (
        f"Lifecycle transition {record['from']} → {record['to']} was rejected. "
        "The story stays at its current status because the required permission "
        "or verified owner-merge evidence is missing. Owner: reconcile the story "
        "before retrying."
    )


def confirm_rejection(cfg, client, path, entry):
    """Reconcile an attempted append before considering any retry, including legacy state."""
    record = entry["record"]
    body = rejection_body(record)
    marker = f"<!-- agentic-rejection:{record['key']} -->"
    try:
        if entry.get("rejection_attempted"):
            comments = client.comments(record["issue"])
            entry["rejection_confirmed"] = any(
                c.get("user", {}).get("login") == cfg["actors"][client.role]
                and c.get("body") == (body + "\n" + marker if entry.get("rejection_marker") else body)
                for c in comments
            )
        else:
            entry.update(rejection_attempted=True, rejection_marker=True)
            save(path, entry)
            try:
                client.request("POST", f"/issues/{record['issue']}/comments",
                               {"body": body + "\n" + marker})
            except Unavailable as exc:
                if not isinstance(exc, Uncertain):
                    entry["rejection_attempted"] = False
                raise
            entry["rejection_confirmed"] = True
    except Unavailable:
        pass
    save(path, entry)
    return entry.get("rejection_confirmed", False)


def reject_transition(cfg, client, path, entry):
    record = entry["record"]
    entry.update(status="conflict", error="transition_not_allowed")
    save(path, entry)
    confirmed = confirm_rejection(cfg, client, path, entry)
    message = f"Lifecycle transition {record['from']} → {record['to']} rejected by policy"
    if not confirmed:
        message += "; issue comment could not be confirmed"
    raise TransitionRejected(message)


def flush(cfg, client):
    bind_state(cfg)
    folder = private(Path(cfg["state_dir"]) / "outbox")
    with lock(Path(cfg["state_dir"]) / "outbox.lock") as acquired:
        if not acquired:
            return
        entries = [(p, read(p)) for p in folder.glob("*.json")]
        entries.sort(key=lambda x: (stamp(x[1]["record"]["timestamp"]), x[0].name))
        blocked_issues = set()
        rejections = []
        supplied_client = client
        for path, entry in entries:
            if entry["status"] in {"synced", "superseded"}:
                continue
            if entry["status"] == "conflict" and entry.get("rejection_confirmed"):
                continue
            record = entry["record"]
            if record["issue"] in blocked_issues:
                continue
            expected_role = entry.get("role", "runner")
            try:
                client = (
                    supplied_client
                    if supplied_client.role == expected_role
                    else GitHub(cfg, expected_role)
                )
            except Unavailable:
                if entry["status"] != "conflict":
                    entry["status"] = "unsynced"
                blocked_issues.add(record["issue"])
                save(path, entry)
                continue
            if record["repository"] != cfg["repository"]:
                raise IntegrityError("Wrong outbox repository")
            rejecting = (entry["status"] == "conflict"
                         and entry.get("error") == "transition_not_allowed")
            try:
                remote_issue = client.issue(record["issue"])
                # Verified owner-merge backfill is informational, not job recovery.
                backfill = (record["to"] == "merged" and record["pr"] is not None
                            and record["job"] == f"pr-{record['pr']}")
                job_record = expected_role == "runner" and not backfill
                if job_record and not can_start(remote_issue):
                    entry.update(status="superseded", error="finished_issue")
                    save(path, entry)
                    continue
                remote = records(cfg, client.comments(record["issue"]), record["issue"])
                if job_record and not can_start(remote_issue, remote):
                    entry.update(status="superseded", error="finished_issue")
                    save(path, entry)
                    continue
                if rejecting:
                    if not confirm_rejection(cfg, client, path, entry):
                        blocked_issues.add(record["issue"])
                    continue
                match = next((r for r in remote if r["key"] == record["key"]), None)
                if match:
                    entry.update(record=match, status="synced")
                else:
                    remote_issue = client.issue(record["issue"])
                    if job_record and not can_start(remote_issue, remote):
                        entry.update(status="superseded", error="finished_issue")
                        save(path, entry)
                        continue
                    current = state(remote_issue)
                    # A PR-open event may arrive after the runner handed the
                    # story to the owner. Guard transport too, so queued intents
                    # and workflow retries cannot undo that escalation.
                    if (expected_role == "workflow" and record["pr"] is not None
                            and record["to"] == "pr-open" and current == "needs-owner"):
                        entry.update(status="superseded", error="owner_handoff_preserved")
                        save(path, entry)
                        continue
                    canonical, latest = canonical_state(
                        cfg, client.events(record["issue"])
                    )
                    newer_approval = (
                        current == canonical == "approved"
                        and latest is not None
                        and record["event"] is not None
                        and record["pr"] is None
                        and record["kind"] in {"transition", "review"}
                        and int(latest["id"]) > record["event"]
                        and latest["actor"]["login"] == cfg["actors"]["owner"]
                    )
                    proof = next(
                        (
                            r
                            for r in remote
                            if newer_approval
                            and r["kind"] == "approval"
                            and r["event"] == int(latest["id"])
                            and r["scope_sha256"]
                            == hashlib.sha256(
                                (remote_issue.get("body") or "").encode()
                            ).hexdigest()
                        ),
                        None,
                    )
                    if proof is not None:
                        entry.update(
                            status="superseded", superseded_by=int(latest["id"])
                        )
                        save(path, entry)
                        continue
                    if "from" not in record:
                        record.update(
                            {"from": current, "actor": cfg["actors"][client.role]}
                        )
                        save(path, entry)
                    if current not in {record["from"], record["to"]}:
                        entry.update(status="conflict", error="remote_state_changed")
                        save(path, entry)
                        blocked_issues.add(record["issue"])
                        continue
                    if record["to"] == "merged" and record["actor"] == cfg["actors"]["owner"]:
                        reject_transition(cfg, client, path, entry)
                    finishing = record["kind"] == "finish"
                    if finishing:
                        finish = owner_finish_event(cfg, client.events(record["issue"]))
                        valid = records(cfg, [{"user": {"login": record["actor"]},
                                               "body": record_body(record)}], record["issue"])
                        if (expected_role != "workflow" or not valid or finish is None
                                or finish[0] != record["from"]
                                or int(finish[1]["id"]) != record["event"]
                                or any(r["to"] == "merged" for r in remote)):
                            reject_transition(cfg, client, path, entry)
                    attesting = (
                        record["kind"] == "approval"
                        and record["actor"] == cfg["actors"]["workflow"]
                        and record["from"] == record["to"] == "approved"
                    )
                    assumption = (
                        record["kind"] in {"assumption", "clarification"}
                        and record["actor"] == cfg["actors"]["product"]
                        and record["from"] == record["to"]
                    )
                    restoring = (
                        record["kind"] == "guard"
                        and record["actor"] == cfg["actors"]["workflow"]
                        and record["to"]
                        == canonical_state(cfg, client.events(record["issue"]))[0]
                    )
                    if not (restoring or attesting or assumption or finishing) and not permitted(
                        cfg, record["from"], record["to"], record["actor"]
                    ):
                        reject_transition(cfg, client, path, entry)
                    if (not finishing and record["from"] == "needs-owner"
                            and record["to"] == "merged"):
                        pr = client.request("GET", f"/pulls/{record['pr']}") if record["pr"] else None
                        if (expected_role not in {"workflow", "runner"} or pr is None
                                or not owner_merge_verified(cfg, record["issue"], remote, pr)
                                or record["commit"] != pr.get("merge_commit_sha")):
                            reject_transition(cfg, client, path, entry)
                    # Label replacement is idempotent; append-only comments are not.
                    if current != record["to"]:
                        client.set_state(record["issue"], record["to"])
                    if cfg.get("uncertain_workflow_resume"):
                        entry["comment_attempted"] = True
                    if entry["comment_attempted"]:
                        entry.update(
                            status="uncertain", error="comment_requires_reconciliation"
                        )
                        save(path, entry)
                        blocked_issues.add(record["issue"])
                        continue
                    entry["comment_attempted"] = True
                    save(
                        path, entry
                    )  # BEFORE sending; an ambiguous POST is never replayed.
                    try:
                        client.request(
                            "POST",
                            f"/issues/{record['issue']}/comments",
                            {"body": notice_body(cfg, client, entry)},
                        )
                    except Unavailable as exc:
                        if not isinstance(exc, Uncertain):
                            entry["comment_attempted"] = False
                        raise
                    entry["status"] = "synced"
                # The explanatory record is durable before optional notification.
                save(path, entry)
                if record["to"] in {"blocked", "needs-owner", "needs-primary"} and not entry.get(
                    "notified"
                ) and not entry.get("notification_attempted"):
                    # Persist before calling: even an ambiguous failure is never retried.
                    entry["notification_attempted"] = True
                    save(path, entry)
                    try:
                        client.notify(record["issue"])
                        entry["notified"] = True
                    except Unavailable:
                        entry["notification_error"] = "owner_notification_failed"
                save(path, entry)
            except TransitionRejected as exc:
                rejections.append(str(exc))
                blocked_issues.add(record["issue"])
            except Unavailable:
                if rejecting:
                    blocked_issues.add(record["issue"])
                    continue
                entry["status"] = "unsynced"
                save(path, entry)
                if rejections:
                    raise TransitionRejected("; ".join(rejections)) from None
                return
        if rejections:
            raise TransitionRejected("; ".join(rejections))


def latest_event(cfg, events, target, role):
    status_events = [
        e
        for e in events
        if e.get("event") in {"labeled", "unlabeled"}
        and e.get("label", {}).get("name", "").startswith("status:")
    ]
    labeled = [e for e in status_events if e["event"] == "labeled"]
    if not labeled:
        return None
    newest = max(labeled, key=lambda e: int(e["id"]))
    if (
        newest["label"]["name"] == "status:" + target
        and newest.get("actor", {}).get("login") == cfg["actors"][role]
    ):
        return newest
    return None


def job_records(cfg, job):
    binding = job["assignment"].get("lifecycle")
    if not binding:
        return []
    number, event = binding["issue"], binding["event"]
    status = job["status"]
    if status == "waiting_system" or job.get("retry_local_end") or job.get("retry_phase_running"):
        return []
    mapping = {
        "queued": "dispatched",
        "running_worker": "in-progress",
        "running_readiness": "in-progress",
        "running_reviewer": "in-review",
        "changes_requested": "changes-requested",
        "needs_primary_review": "needs-primary",
        "needs_owner": "needs-owner",
        "blocked": "blocked",
        "failed": "blocked",
        "interrupted": "blocked",
        "ready_for_integration": "in-review",
    }
    target = mapping[status]
    integration = job.get("integration", {})
    if status == "ready_for_integration" and integration.get("pr"):
        # PR-open/merged transitions belong to the trusted workflow identity.
        # The exact-head review was synced before publication. Polling delivery
        # must not try to move that workflow-owned state back to in-review.
        return []
    committed = [c for c in job.get("runner_commits", []) if c["state"] == "committed"]
    head = job.get("head_commit")
    if (
        status == "running_worker"
        and committed
        and committed[-1]["round"] == job["round"]
    ):
        target = "committed"
    review = job.get("review", {})
    counts = {
        p: sum(f["severity"] == p for f in review.get("findings", []))
        for p in ("P0", "P1", "P2")
    }
    outcome = (
        review.get("status")
        if status
        in {
            "ready_for_integration",
            "changes_requested",
            "needs_owner",
            "needs_primary_review",
        }
        else None
    )
    reason = integration.get("reason")
    if reason not in STOP_REASONS:
        reason = None
    if target in {"blocked", "needs-owner", "needs-primary"} and reason is None:
        worker = job.get("worker", {})
        blocked = job.get("blocked_reason", {})
        if worker.get("status") == "blocked":
            category = "worker_" + str(worker.get("blocked_check"))
            reason = category if category in STOP_REASONS else "worker_reported_blocked"
            cause = worker.get("blocked_cause")
            if isinstance(cause, str) and cause in {"unavailable", "attempts_exhausted", "unrelated_failure"}:
                candidate = category + "_" + cause
                if cause == "attempts_exhausted":
                    attempts = job.get("worker_check_attempts")
                    if type(attempts) is int and 1 <= attempts <= 10:
                        candidate += "_" + str(attempts)
                if candidate in STOP_REASONS:
                    reason = candidate
        elif isinstance(blocked, dict) and blocked.get("reason_code") in STOP_REASONS:
            reason = blocked["reason_code"]
        elif status in {"needs_owner", "needs_primary_review"}:
            reason = "reviewer_reported_blocked"
        else:
            reason = "execution_failed"
    question = None
    failures = None
    details = None
    if target in {"blocked", "needs-owner", "needs-primary"}:
        worker = job.get("worker", {})
        supplied = worker.get("owner_question")
        if supplied is not None:
            if isinstance(supplied, str):
                supplied = inert_mentions(supplied)
            if safe_question(cfg, supplied):
                question, reason = supplied, "owner_question"
            else:
                reason = "question_withheld"
        details = {}
        if job.get("readiness_stopped") and reason == "readiness_question":
            supplied = "\n".join(job["readiness"]["questions"])
            if safe_question(cfg, supplied):
                question, reason = supplied, "readiness_question"
            else:
                question, reason = None, "question_withheld"
        if worker.get("status") == "blocked" and isinstance(worker.get("summary"), str):
            details["summary"] = worker["summary"]
        if job.get("readiness_sessions"):
            details["summary"] = ("Readiness cost: one extra agent session for this approval."
                                  + ("\n" + details["summary"] if details.get("summary") else ""))
        if review.get("findings"):
            details["findings"] = review["findings"]
        if job.get("commit_refusals"):
            details["refusal"] = job["commit_refusals"][-1]
            reason = "commit_refused"
        failures = safe_failures(worker.get("failed_tests"))
        results = (job.get("verification_runs") or [{}])[-1].get("results", [])
        failed = [r for r in results if r.get("status") == "failed"]
        count = sum(r.get("reported_counts", {}).get("failed", 0)
                    for r in failed if isinstance(r.get("reported_counts"), dict))
        if count:
            names = [n for r in failed for n in (r.get("failed_tests") or {}).get("names", [])]
            failures = safe_failures({"count": count, "names": names})
    return [
        public_record(
            cfg,
            number,
            job["id"],
            job["round"],
            target,
            commit=head,
            event=event,
            kind="review" if outcome else "transition",
            outcome=outcome,
            severities=counts if outcome else {},
            reason=reason,
            pr=integration.get("pr") if reason == "owner_merge_required" else None,
            owner_question=question,
            failed_tests=failures,
            stop_details=details,
            continuation=job.get("continuation", False),
            stop_save_failed=job.get("stop_save_failed", False),
        )
    ]


def sync_job(config_path, job, client=None):
    cfg = load_config(config_path)
    for record in job_records(cfg, job):
        context = {}
        if record["kind"] == "review" and job.get("review", {}).get("findings"):
            context["findings"] = job["review"]["findings"]
        if job.get("integration", {}).get("reason"):
            context.update(delivery=True, reviewed_head=job.get("reviewed_head", "unavailable"),
                           smoke_reporting=job["integration"].get("smoke_reporting", False))
        queue(cfg, record, notice_context=context)
    try:
        flush(cfg, client or GitHub(cfg))
        if any(
            f["severity"] in cfg["pushback"]["minor"]
            for r in job.get("review_history", [])
            for f in r["result"]["findings"]
        ):
            followups(cfg, job, GitHub(cfg, "product"))
    except Unavailable:
        pass
    pending = [read(p) for p in (Path(cfg["state_dir"]) / "outbox").glob("*.json")]
    pending_followups = [
        read(p) for p in (Path(cfg["state_dir"]) / "followups").glob("*.json")
    ]
    job["lifecycle_unsynced"] = any(
        x["record"]["job"] == job["id"] and x["status"] not in {"synced", "superseded"}
        for x in pending
    ) or any(
        x["job"] == job["id"] and x["status"] != "posted" for x in pending_followups
    )


def reconcile(cfg, jobs, client):
    result = []
    for job in jobs:
        binding = job["assignment"].get("lifecycle")
        if not binding:
            continue
        try:
            remote = state(client.issue(binding["issue"]))
            local_records = job_records(cfg, job)
            if local_records:
                expected = local_records[-1]["to"]
            elif job["status"] == "ready_for_integration" and job.get("integration", {}).get("pr"):
                # Publication hands transitions to the workflow. Report drift
                # without queuing a runner transition or changing remote state.
                expected = "merged" if job["integration"].get("stage") == "merged" else "pr-open"
            else:
                continue
            if expected != remote:
                result.append(
                    dict(
                        issue=binding["issue"],
                        job=job["id"],
                        local=expected,
                        remote=remote,
                    )
                )
        except (Unavailable, ValueError):
            result.append(
                dict(issue=binding["issue"], job=job["id"], error="remote_unavailable")
            )
    return result


def canonical_state(cfg, events):
    """Only authenticated label-event actors can advance the canonical state."""
    current = "draft"
    latest = None
    for event in sorted(events, key=lambda e: int(e["id"])):
        if event.get("event") != "labeled":
            continue
        name = event.get("label", {}).get("name", "")
        if not name.startswith("status:"):
            continue
        target = name[7:]
        if target in STATES and permitted(
            cfg, current, target, event.get("actor", {}).get("login")
        ):
            current, latest = target, event
    return current, latest


def attest_approval(cfg, client, event):
    if (
        event.get("action") != "labeled"
        or event.get("label", {}).get("name") != "status:approved"
    ):
        return
    bind_state(cfg)
    with lock(Path(cfg["state_dir"]) / "approval.lock") as acquired:
        if not acquired:
            raise Unavailable("Approval attestation is already running")
        return _attest_approval(cfg, client, event)


def _attest_approval(cfg, client, event):
    issue = event["issue"]
    events = []
    latest = None

    def instant(value):
        if not isinstance(value, str):
            return None
        try:
            return stamp(value).astimezone(dt.timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None

    snapshot_time = instant(issue.get("updated_at"))

    def matches(value):
        parsed = instant(value)
        return parsed is not None and snapshot_time is not None and parsed == snapshot_time

    def ignore_snapshot():
        # This is not a rejection of the current label event. In particular do
        # not emit a rejection marker that guard_issue could apply to it.
        marker = "<!-- agentic-approval-snapshot-ignored:" + key(event) + " -->"
        diagnostic = ""
        latest_time = instant(latest.get("created_at")) if latest is not None else None
        if snapshot_time is not None and latest_time is not None and snapshot_time != latest_time:
            gap = abs(snapshot_time - latest_time)
            seconds = gap.days * 86400 + gap.seconds + bool(gap.microseconds)
            diagnostic = f"Snapshot time differs from the latest approval event by {seconds} seconds.\n"
        try:
            comments = client.comments(issue["number"])
        except Unavailable:
            comments = []
        if not any(marker in c.get("body", "") and c.get("user", {}).get("login")
                   == cfg["actors"]["workflow"] for c in comments):
            client.request("POST", f"/issues/{issue['number']}/comments", {"body": (
                "A delayed or conflicting approval snapshot was ignored. "
                "It could not be bound to the current approval, so the guard preserved "
                "the current label and any existing approval proof. Owner: if this story "
                "is still waiting for approval, remove and re-add status:approved to "
                "re-approve the current story.\n" + diagnostic + marker
            )})
        raise Unavailable("Approval snapshot is stale or ambiguous; current approval preserved")

    def statuses(snapshot):
        names = [x["name"] if isinstance(x, dict) else x for x in snapshot["labels"]]
        return sorted(x for x in names if x.startswith("status:"))

    def reject_labels(message="Approval needs valid status labels and an owner label event"):
        # Preserve a proven current event, never a historical approval cycle.
        # Without history, even an identical story and timestamp cannot prove
        # this is a replay: a new approval may share both in the same second.
        # Re-read available history before removal to detect a newer cycle too.
        try:
            comments = client.comments(issue["number"])
        except Unavailable:
            # Missing proof is not permission to leave an unusable approval
            # label behind. Reads can fail while removal and explanation work.
            comments = []
        proofs = [r for r in records(cfg, comments, issue["number"]) if r["kind"] == "approval"]
        if latest is not None and any(r["event"] >= int(latest["id"]) for r in proofs):
            ignore_snapshot()
        if events:
            try:
                _, head = canonical_state(cfg, client.events(issue["number"]))
            except Unavailable:
                # Unreadable history cannot prove an approval. Continue the
                # rejection; available proof above still protects a newer cycle.
                pass
            else:
                if head != latest:
                    ignore_snapshot()
        # Remove only the approval we cannot honour, preserving all other labels.
        removal_error = None
        try:
            client.request("DELETE", f"/issues/{issue['number']}/labels/status%3Aapproved")
        except Unavailable as exc:
            removal_error = exc
        rejected = int(latest["id"]) if latest is not None else 0
        marker = f"<!-- agentic-approval-rejected:{rejected}:" + key(issue) + " -->"
        try:
            comments = client.comments(issue["number"])
        except Unavailable:
            comments = []
        removal = ("The guard removed status:approved because it cannot honour that label. "
                   if removal_error is None else
                   "The guard could not confirm removal of status:approved. "
                   "Owner: remove that label yourself before retrying. ")
        if not any(marker in c.get("body", "") and c.get("user", {}).get("login")
                   == cfg["actors"]["workflow"] for c in comments):
            try:
                client.request(
                    "POST",
                    f"/issues/{issue['number']}/comments",
                    {"body": (
                        "Approval could not be attested: the owner event, status labels or "
                        "approved story snapshot were unavailable or ambiguous. " + removal
                        + "Only the configured owner can approve. "
                        "To retry, resolve conflicting status labels; status:draft may stay. "
                        "Then, as the owner, remove status:approved if present and add it again. "
                        "The guard replaces draft. The transition must still be allowed "
                        "from the last permitted status in the issue history.\n" + marker
                    )},
                )
            except Unavailable:
                if removal_error is not None:
                    raise removal_error
                raise
        if removal_error is not None:
            raise removal_error
        raise Unavailable(message)

    owner = event.get("sender", {}).get("login") == cfg["actors"]["owner"]
    pair = ["status:approved", "status:draft"]
    try:
        events = client.events(issue["number"])
        canonical, latest = canonical_state(cfg, events)
        live = client.issue(issue["number"])
        snapshots = [statuses(issue), statuses(live)]
    except Unavailable:
        reject_labels("Approval evidence unavailable; re-approve")
    timestamp_matches = [e for e in events
                         if e.get("event") == "labeled"
                         and e.get("label", {}).get("name") == "status:approved"
                         and matches(e.get("created_at"))]
    if latest is not None and not matches(latest.get("created_at")):
        ignore_snapshot()
    # IDs identify cycles, but the webhook has no label-event ID. When multiple
    # cycles share a timestamp, its body must also agree with the live snapshot;
    # otherwise an old webhook could attest a newer event with obsolete scope.
    if len(timestamp_matches) > 1 and (issue.get("body") or "") != (live.get("body") or ""):
        ignore_snapshot()
    if any(s != ["status:approved"] and not (owner and s == pair) for s in snapshots):
        reject_labels()
    if not owner:
        reject_labels()
    # Bind the webhook snapshot to a unique owner label event. Missing or
    # ambiguous timestamp evidence fails closed, rather than guessing an event.
    # Event IDs order transitions even when GitHub timestamps share a second.
    # Only approvals since the last permitted non-approved state can describe
    # this approval cycle. Two approvals in that cycle remain ambiguous.
    boundary = 0
    current = "draft"
    for item in sorted(events, key=lambda e: int(e["id"])):
        target = item.get("label", {}).get("name", "")[7:]
        if item.get("event") == "labeled" and target in STATES and permitted(
                cfg, current, target, item.get("actor", {}).get("login")):
            current = target
            if target != "approved":
                boundary = int(item["id"])
    # A removed approval ends its attempt even if canonical history remains
    # approved. A fresh owner click must not inherit same-second ambiguity.
    boundary = max([boundary] + [int(e["id"]) for e in events
                               if e.get("event") == "unlabeled"
                               and e.get("label", {}).get("name") == "status:approved"])
    candidates = [
        e
        for e in events
        if e.get("event") == "labeled"
        and e.get("label", {}).get("name") == "status:approved"
        and e.get("actor", {}).get("login") == cfg["actors"]["owner"]
        and matches(e.get("created_at"))
        and int(e["id"]) > boundary
    ]
    if len(candidates) != 1 or latest != candidates[0] or canonical != "approved":
        reject_labels("Approval event snapshot is unavailable or ambiguous; re-approve")
    scope = hashlib.sha256((issue.get("body") or "").encode()).hexdigest()
    try:
        comments = client.comments(issue["number"])
    except Unavailable:
        reject_labels("Approval proof unavailable; re-approve")
    proofs = [r for r in records(cfg, comments, issue["number"])
              if r["kind"] == "approval" and r["event"] == int(latest["id"])]
    # Include uncertain/pending publications: a lost response must never allow
    # a second body to be queued for the same event under a different key.
    intents = [read(p)["record"] for p in (Path(cfg["state_dir"]) / "outbox").glob("*.json")]
    bound = proofs + [r for r in intents if r["issue"] == issue["number"]
                     and r["kind"] == "approval" and r["event"] == int(latest["id"])]
    if any(r["scope_sha256"] != scope for r in bound):
        ignore_snapshot()
    if proofs:
        return
    if pair in snapshots:
        source, _ = canonical_state(
            cfg, [e for e in events if int(e["id"]) < int(latest["id"])]
        )
        if source not in {"draft", "approved"} or not permitted(
            cfg, source, "approved", event["sender"]["login"]
        ):
            reject_labels()
        if snapshots[1] == pair:
            # Remove only draft; never overwrite a concurrent status or other label.
            try:
                client.request("DELETE", f"/issues/{issue['number']}/labels/status%3Adraft")
            except Unavailable:
                reject_labels("Approval label cleanup failed; re-approve")
        if statuses(client.issue(issue["number"])) != ["status:approved"]:
            reject_labels()
    record = public_record(
        cfg,
        issue["number"],
        "approval",
        0,
        "approved",
        event=int(latest["id"]),
        kind="approval",
        scope_sha256=scope,
        approved_by=cfg["actors"]["owner"],
    )
    path = queue(cfg, record, role="workflow")
    flush(cfg, client)
    if read(path)["status"] != "synced" and not any(
            r["key"] == record["key"]
            for r in records(cfg, client.comments(issue["number"]), issue["number"])):
        reject_labels("Approval publication could not be confirmed; re-approve")


def owner_finish_event(cfg, events):
    """Return the first permitted merge only when its label actor is the owner."""
    current = "draft"
    for event in sorted(events, key=lambda e: int(e["id"])):
        if event.get("event") != "labeled":
            continue
        name = event.get("label", {}).get("name", "")
        target = name[7:] if name.startswith("status:") else None
        actor = event.get("actor", {}).get("login")
        if target in STATES and permitted(cfg, current, target, actor):
            if target == "merged":
                return (current, event) if actor == cfg["actors"]["owner"] else None
            current = target
    return None


def finish_by_owner(cfg, client, number, events):
    finish = owner_finish_event(cfg, events)
    if finish is None:
        return False
    bind_state(cfg)
    with lock(Path(cfg["state_dir"]) / f"finish-{number}.lock") as acquired:
        if not acquired:
            return True
        return _finish_by_owner(cfg, client, number, finish)


def _finish_by_owner(cfg, client, number, finish):
    issue = client.issue(number)
    if is_report(cfg, issue):
        return False
    source, event = finish
    evidence = records(cfg, client.comments(number), number)
    proof = next((r for r in evidence if r["kind"] == "finish"
                  and r["event"] == int(event["id"])), None)
    if proof is None and any(r["to"] == "merged" for r in evidence):
        return False
    # Replace conflicting labels before the outbox reads the current state.
    issue = client.issue(number)
    labels = [x["name"] if isinstance(x, dict) else x for x in issue["labels"]]
    if [name for name in labels if name.startswith("status:")] != ["status:merged"]:
        client.set_state(number, "merged")
    if proof is None:
        record = public_record(cfg, number, "owner-finish", 0, "merged",
                               kind="finish", event=int(event["id"]),
                               reason="finished_by_owner")
        record.update({"from": source, "actor": cfg["actors"]["workflow"]})
        queue(cfg, record, role="workflow")
        flush(cfg, client)
    # A successful POST alone is insufficient: confirm trusted issue evidence.
    if any(r["kind"] == "finish" and r["event"] == int(event["id"])
           for r in records(cfg, client.comments(number), number)):
        if client.issue(number)["state"] == "open":
            client.request("PATCH", f"/issues/{number}", {"state": "closed"})
    return True


def restoration_noticed(cfg, comments, number, event, target):
    """Recognize only an unedited workflow notice's exact raw restoration block.

    Restoration records intentionally remain outside records(), including older
    parseable shapes. Opening prose conveys no authority.
    """
    for comment in comments:
        body = comment.get("body", "")
        if (comment.get("user", {}).get("login") != cfg["actors"]["workflow"]
                or body.count(PREFIX) != 1 or body.count(SUFFIX) != 1):
            continue
        try:
            if (not isinstance(comment.get("updated_at"), str)
                    or not isinstance(comment.get("created_at"), str)
                    or stamp(comment["updated_at"]) != stamp(comment["created_at"])):
                continue
            item = json.loads(body.split(PREFIX)[1].split(SUFFIX)[0])
            if not isinstance(item, dict):
                continue
            if (item.get("job") == "guard" and item.get("kind") == "guard"
                    and item.get("reason") == "invalid_status_change"
                    and item.get("repository") == cfg["repository"]
                    and type(item.get("issue")) is int and item["issue"] == number
                    and type(item.get("event")) is int and item["event"] == int(event["id"])
                    and item.get("to") == target):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def guard_issue(cfg, client, number):
    events = client.events(number)
    canonical, event = canonical_state(cfg, events)
    issue = client.issue(number)
    if is_report(cfg, issue):
        return False
    if finish_by_owner(cfg, client, number, events):
        return False
    labels = {x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])}
    statuses = {label for label in labels if label.startswith("status:")}
    fresh_after_removal = bool(event and any(
        e.get("event") == "unlabeled"
        and e.get("label", {}).get("name") == "status:approved"
        and int(e["id"]) < int(event["id"]) for e in events))
    # The DELETE from a rejected attestation emits its own webhook. It must
    # never cause the ordinary guard to put the unhonoured approval back.
    comments = client.comments(number) if canonical == "approved" else []
    proven = any(r["kind"] == "approval" and r["event"] == int(event["id"])
                 for r in records(cfg, comments, number)) if event else False
    if canonical == "approved" and event is not None and not proven and any(
            c.get("user", {}).get("login") == cfg["actors"]["workflow"]
            and (f"<!-- agentic-approval-rejected:{event['id']}:" in c.get("body", "")
                 or "<!-- agentic-approval-rejected:0:" in c.get("body", "")
                 and ("status:approved" not in labels or not fresh_after_removal))
            for c in comments):
        if not permitted(cfg, canonical, "needs-owner", cfg["actors"]["workflow"]):
            return False
        client.set_state(number, "needs-owner")
        record = public_record(cfg, number, "approval-rejected", 0, "needs-owner",
                               event=int(event["id"]), reason="approval_not_attested")
        record.update({"from": canonical, "actor": cfg["actors"]["workflow"]})
        queue(cfg, record, role="workflow")
        flush(cfg, client)
        return True
    # Reconcile labels from current history, but only the authenticated owner
    # approval webhook can bind the approved body. A live body can have changed
    # within the event's timestamp second; never manufacture a snapshot here.
    # Remove draft alone so the pending webhook still sees the same approval
    # event, rather than emitting a replacement approved label event.
    if canonical == "approved" and statuses == {"status:approved", "status:draft"}:
        client.request("DELETE", f"/issues/{number}/labels/status%3Adraft")
        issue = client.issue(number)
    if canonical in {"blocked", "needs-owner", "needs-primary"}:
        client.notify(number)
    try:
        actual = state(issue)
    except ValueError:
        actual = None
    if canonical in {"blocked", "needs-owner", "needs-primary"}:
        actor = event.get("actor", {}).get("login")
        # Compare concrete logins: the owner can also hold the Product role.
        noticed = actor == cfg["actors"]["runner"]
        comments = client.comments(number)  # Re-read after the producer's label write.
        remote = records(cfg, comments, number)
        if actor == cfg["actors"]["workflow"]:
            noticed = noticed or restoration_noticed(cfg, comments, number, event, canonical)
        noticed = noticed or any(r["job"] == "label-stop" and r["event"] == int(event["id"])
                                 and r["to"] == canonical for r in remote)
        if actor != cfg["actors"]["owner"] and actor in {
                cfg["actors"]["product"], cfg["actors"]["workflow"]}:
            previous = max((e for e in events if e.get("event") == "labeled"
                            and e.get("label", {}).get("name", "").startswith("status:")
                            and int(e["id"]) < int(event["id"])),
                           key=lambda e: int(e["id"]), default=None)
            previous = previous or event
            noticed = noticed or any(
                r["actor"] == actor and r["to"] == canonical
                and r.get("reason") in STOP_REASONS
                and r["reason"] != "approval_not_attested"
                and stamp(r["timestamp"]) >= stamp(previous["created_at"])
                for r in remote)
        if not noticed:
            if actual != canonical:
                client.set_state(number, canonical)
            record = public_record(cfg, number, "label-stop", 0, canonical,
                                   event=int(event["id"]), kind="guard",
                                   reason="owner_requested_stop")
            queue(cfg, record, role="workflow")
            flush(cfg, client)
            return actual != canonical
    if actual == canonical:
        return False
    client.set_state(number, canonical)
    # Bind to the event just written, never to the rejected prior change.
    restored_event = None
    try:
        written = [e for e in client.events(number) if e.get("event") == "labeled"
                   and int(e["id"]) > max((int(old["id"]) for old in events), default=0)
                   and e.get("actor", {}).get("login") == cfg["actors"]["workflow"]
                   and e.get("label", {}).get("name") == "status:" + canonical]
        if written:
            restored_event = int(max(written, key=lambda e: int(e["id"]))["id"])
    except Unavailable:
        pass  # No identity is safer than claiming an unrelated event.
    record = public_record(
        cfg,
        number,
        "guard",
        0,
        canonical,
        event=restored_event,
        kind="guard",
        reason="invalid_status_change",
    )
    queue(cfg, record, role="workflow", public_body=(
        "The guard restored the last permitted status. Keep only one status label. "
        "To approve, the configured owner must add status:approved from a permitted "
        "state. If approval failed, resolve conflicting labels; status:draft may stay. "
        "Then, as the owner, remove status:approved if present and add it again. "
        "The guard replaces draft. The transition must still "
        "be allowed from the last permitted status in the issue history.\n"
        + record_body(record)
    ))
    flush(cfg, client)
    return True


def trusted_gate(cfg, client, pr, *, strict_snapshot=False):
    """Unknown/partial/stale/name-colliding checks are never a passing gate."""
    try:
        # GitHub attaches the trusted check to the PR head even though the
        # workflow executes trusted base code. Bind its run to this PR too.
        execution_sha = pr["head"]["sha"]
        runs, total, ids = [], None, set()
        for page in range(1, 1001):
            result = client.request(
                "GET",
                f"/commits/{execution_sha}/check-runs?filter=all&per_page=100&page={page}",
            )
            if total is None:
                total = result["total_count"]
            if type(total) is not int or result["total_count"] != total:
                return "unavailable"
            for check in result["check_runs"]:
                if check["id"] in ids:
                    return "unavailable"
                ids.add(check["id"])
                runs.append(check)
            if len(result["check_runs"]) < 100:
                break
        if len(runs) != total:
            return "unavailable"
        path = cfg.get("trusted_gate_workflow", ".github/workflows/pr-review-gate.yml")
        workflow = client.request(
            "GET", "/actions/workflows/" + urllib.parse.quote(Path(path).name)
        )
        if workflow["path"] != path:
            return "unavailable"
        candidates = []
        prefix = "https://github.com/" + cfg["repository"] + "/actions/runs/"
        for check in runs:
            if (
                check["name"] != "review-gate"
                or check.get("app", {}).get("slug") != "github-actions"
                or check.get("head_sha") != execution_sha
            ):
                continue
            details = check.get("details_url", "")
            match = re.fullmatch(
                re.escape(prefix) + r"([1-9]\d*)/job/[1-9]\d*", details
            )
            if not match:
                continue
            run = client.request("GET", "/actions/runs/" + match[1])
            bindings = run.get("pull_requests", [])
            if (
                run["workflow_id"] != workflow["id"]
                or run["event"] != "pull_request_target"
                or run.get("path") != path
                or run["head_sha"] != execution_sha
                or not any(
                    p["number"] == pr["number"]
                    and p["head"]["sha"] == pr["head"]["sha"]
                    for p in bindings
                )
            ):
                continue
            if strict_snapshot and not any(
                p["number"] == pr["number"]
                and p["head"]["sha"] == pr["head"]["sha"]
                and p.get("base", {}).get("sha") == pr["base"]["sha"]
                for p in bindings
            ):
                continue
            candidates.append(check)
        if not candidates:
            return "unavailable"
        latest = max(candidates, key=lambda c: c["id"])
        return (
            "pending"
            if latest["status"] != "completed"
            else "pass"
            if latest["conclusion"] == "success"
            else "fail"
        )
    except (Unavailable, KeyError, TypeError, ValueError):
        return "unavailable"


def pr_event(cfg, client, event):
    incoming = event["pull_request"]
    number = incoming["number"]
    pr = client.request("GET", f"/pulls/{number}")
    if pr["head"]["sha"] != incoming["head"]["sha"] or pr["body"] != incoming["body"]:
        raise Unavailable("PR changed during lifecycle event")
    issues = sorted(
        {
            int(n)
            for n in re.findall(r"\bCloses\s+#([1-9]\d*)\b", pr.get("body") or "", re.I)
        }
    )
    managed = {}
    for issue in issues:
        evidence = records(cfg, client.comments(issue), issue)
        if any(
            r["kind"] == "transition"
            and r["to"] == "dispatched"
            and has_role(cfg, r["actor"], "runner")
            for r in evidence
        ):
            managed[issue] = evidence
        else:
            print(f"Issue #{issue}: not a lifecycle task; skipped")
    if not managed:
        if not issues:
            print("not a lifecycle task; skipped")
        return
    if pr["base"]["ref"] != cfg.get("base_branch", "main"):
        raise Unavailable("PR targets an unsupported lifecycle base")
    if event["action"] == "closed":
        target = "merged" if pr["merged"] else "needs-owner"
        gate = "not_checked"
    else:
        target = "pr-open"
        gate = trusted_gate(cfg, client, pr)
    if pr.get("head", {}).get("repo", {}).get("full_name") != cfg["repository"]:
        raise Unavailable("Foreign PR cannot drive lifecycle state")
    if pr.get("user", {}).get("login") not in set(cfg["actors"].values()):
        raise Unavailable("Untrusted PR actor")
    for evidence in managed.values():
        reviewed = [
            r
            for r in evidence
            if r["kind"] == "review"
            and r["outcome"] == "ready_for_integration"
            and r["commit"] == pr["head"]["sha"]
        ]
        if not reviewed:
            raise Unavailable("PR head has no trusted successful task review")
    # Validate every managed target before creating any transition intents.
    # Unmanaged links never enter the outbox or trigger a flush of older work.
    for issue, evidence in managed.items():
        if target == "merged" and state(client.issue(issue)) == "needs-owner":
            if not owner_merge_verified(cfg, issue, evidence, pr):
                client.request("POST", f"/issues/{issue}/comments", {"body": (
                    "The PR merged, but this story remains at needs-owner. "
                    "Only a verified stop waiting for an owner merge can be recorded as merged. "
                    "Owner: resolve the latest stop reason."
                )})
                continue
        record = public_record(
            cfg,
            issue,
            f"pr-{number}",
            0,
            target,
            commit=pr.get("merge_commit_sha") if pr["merged"] else pr["head"]["sha"],
            reason="pr_closed_unmerged" if target == "needs-owner" else None,
            pr=number,
            gate=gate,
            event=int(os.environ["GITHUB_RUN_ID"])
            if os.environ.get("GITHUB_RUN_ID")
            else None,
        )
        queue(cfg, record, role="workflow")
    flush(cfg, client)


def core_body_changed(event):
    return "body" in event.get("changes", {})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "action",
        choices=[
            "setup-labels",
            "flush",
            "guard-event",
            "guard-identity",
            "assumption",
        ],
    )
    parser.add_argument("--event", type=Path)
    parser.add_argument("--issue", type=int)
    parser.add_argument("--code")
    args = parser.parse_args(argv)
    cfg = load_config(
        args.config, local=args.action not in {"guard-event", "guard-identity"}
    )
    if args.action == "guard-event":
        cfg["state_dir"] = str(Path(os.environ["RUNNER_TEMP"]) / "task-lifecycle")
        cfg["uncertain_workflow_resume"] = (
            int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1
        )
    role = (
        "workflow"
        if args.action in {"guard-event", "guard-identity"}
        else "product"
        if args.action == "assumption"
        else "runner"
    )
    client = GitHub(cfg, role)
    if args.action == "guard-identity":
        client.ensure_actor()
        print(json.dumps({"identity": cfg["actors"]["workflow"], "preflight": "PASS"}))
    elif args.action == "assumption":
        if args.code not in cfg.get("public_assumption_codes", []):
            raise ValueError("Assumption code must be approved in local config")
        record = public_record(
            cfg,
            args.issue,
            "product",
            0,
            state(client.issue(args.issue)),
            kind="assumption",
            reason=args.code,
        )
        queue(cfg, record, role=role)
        flush(cfg, client)
    elif args.action == "setup-labels":
        existing = {x["name"] for x in client.pages("/labels")}
        for name in sorted(STATES):
            if "status:" + name not in existing:
                client.request(
                    "POST", "/labels", {"name": "status:" + name, "color": "ededed"}
                )
        for name in ("report:standup", "digest:skip"):
            if name not in existing:
                client.request("POST", "/labels", {"name": name, "color": "ededed"})
    elif args.action == "guard-event":
        event = read(args.event)
        if event["repository"]["full_name"] != cfg["repository"]:
            raise ValueError("Wrong event repository")
        if "pull_request" in event:
            pr_event(cfg, client, event)
        else:
            number = event["issue"]["number"]
            if finish_by_owner(cfg, client, number, client.events(number)):
                return 0
            attest_approval(cfg, client, event)
            if event.get("action") == "edited" and core_body_changed(event):
                issue = client.issue(event["issue"]["number"])
                if state(issue) in {"approved", "dispatched"}:
                    record = public_record(
                        cfg,
                        issue["number"],
                        "scope-edit",
                        0,
                        "needs-owner",
                        reason="approved_scope_changed",
                    )
                    queue(cfg, record, role=role)
                    flush(cfg, client)
            guard_issue(cfg, client, event["issue"]["number"])
    else:
        flush(cfg, client)
    return 0


def followups(cfg, job, client):
    binding = job["assignment"].get("lifecycle")
    if not binding:
        return
    folder = private(Path(cfg["state_dir"]) / "followups")
    for review in job.get("review_history", []):
        for index, finding in enumerate(review["result"]["findings"]):
            if finding["severity"] not in cfg["pushback"]["minor"]:
                continue
            identity = key(
                [binding["issue"], job["id"], review["round"], review["head"], index]
            )
            path = folder / (identity + ".json")
            record = (
                read(path)
                if path.exists()
                else dict(
                    status="pending",
                    attempted=False,
                    parent_issue=binding["issue"],
                    job=job["id"],
                )
            )
            if record["status"] == "posted":
                continue
            marker = "<!-- agentic-followup-v1:" + identity + " -->"
            matches = [
                i
                for i in client.pages("/issues?state=all")
                if i.get("user", {}).get("login") == cfg["actors"]["product"]
                and marker in (i.get("body") or "")
            ]
            if matches:
                record.update(status="posted", issue=matches[0]["number"])
            elif record["attempted"]:
                record["status"] = "uncertain"
            else:
                record.update(status="pending", attempted=True)
                save(path, record)
                body = (
                    f"Minor follow-up from #{binding['issue']}, job `{job['id']}`, "
                    f"round {review['round']}, finding {index + 1}.\n"
                    f"Reviewed commit: `{review['head']}`.\n"
                    "Primary triage is required; no approval implied.\n"
                    + finding_notice(cfg, [finding]) + "\n" + marker
                )
                try:
                    created = client.request(
                        "POST",
                        "/issues",
                        {
                            "title": f"Minor follow-up for issue #{binding['issue']}",
                            "body": body,
                            "labels": ["status:draft"],
                        },
                    )
                    record.update(status="posted", issue=created["number"])
                except Unavailable as exc:
                    record["status"] = "unsynced"
                    if not isinstance(exc, Uncertain):
                        record["attempted"] = False
                    save(path, record)
                    raise
            save(path, record)


def admit_phase(config_path, job):
    """Existing admitted jobs can work offline; observed revocations stop new phases."""
    cfg = load_config(config_path)
    binding = job["assignment"]["lifecycle"]
    try:
        client = GitHub(cfg)
        issue = client.issue(binding["issue"])
        current = state(issue)
        published = job.get("integration", {}).get("pr") and job["integration"].get("stage") in {
            "created", "checking", "auto_merge_enabled", "direct_merge_recheck",
        }
        if (
            not can_start(issue)
            or (issue.get("body") or "") != job["assignment"]["task_text"]
            or current
            not in ({
                "dispatched",
                "in-progress",
                "committed",
                "in-review",
                "changes-requested",
            } | ({"pr-open"} if published else set()))
        ):
            raise IntegrityError("Admission changed; owner reconciliation required")
        evidence = records(cfg, client.comments(binding["issue"]), binding["issue"])
        if not can_start(issue, evidence):
            raise IntegrityError("Admission changed; owner reconciliation required")
        dispatches = [r for r in evidence if r["to"] == "dispatched"]
        if not dispatches or dispatches[-1]["job"] != job["id"]:
            raise IntegrityError("Dispatch ownership changed")
        return True
    except Unavailable:
        return False


def canonical_severity(value):
    mapping = {
        "critical": "P0",
        "high": "P1",
        "medium": "P1",
        "low": "P2",
        "minor": "P2",
    }
    result = mapping.get(value, value)
    if result not in {"P0", "P1", "P2"}:
        raise ValueError("Unknown severity requires primary review")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
