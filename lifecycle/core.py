"""Trusted lifecycle policy, repository-scoped transport and durable outbox."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "<!-- agentic-lifecycle-v1\n"
SUFFIX = "\n-->"
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


class Unavailable(RuntimeError):
    pass


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
        }
        if not set(cfg) <= local_fields:
            raise ValueError("Public policy fields belong in the policy file")
        policy = read(cfg["policy_file"])
        if set(policy) & local_fields:
            raise ValueError("Private settings must not appear in public policy")
        cfg = {**policy, **cfg}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg.get("repository", "")):
        raise ValueError("Explicit repository required")
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
        raise ValueError("Issue needs exactly one known status label")
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
        "clarification",
    }:
        raise ValueError("Unknown record kind")
    if outcome not in {None, "ready_for_integration", "needs_changes", "blocked"}:
        raise ValueError("Unknown review outcome")
    if pr is not None and (type(pr) is not int or pr < 1):
        raise ValueError("Invalid PR number")
    if gate not in {None, "pass", "fail", "pending", "unavailable"}:
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
    return record


# Fixed public prose only. Never render worker summaries, errors, logs or argv.
STOP_REASONS = {
    "delivery_disabled": "Delivery is off for this repository. Owner: enable delivery in the service configuration, then use the Continue from block and re-approve to publish the reviewed work.",
    "delivery_configuration_invalid": "Delivery settings are incomplete. Nothing was dispatched. Ask the operator to configure complete delivery settings or explicitly turn delivery off, then re-approve.",
    "continuation_commit_unrecorded": "The named commit is not verified saved work from a permitted stop on this issue. Owner: use the commit from a valid continuation comment, or remove the section and re-approve to restart fresh.",
    "continuation_conflict": "Saved work conflicts with the target branch. Owner: arrange conflict resolution before re-approving, or remove the section to restart fresh.",
    "continuation_limit": "This story has reached its two-continuation limit. Owner: restart fresh (remove the Continue from section and re-approve), or split the story.",
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
for _check, _label in {
    "preflight": "Preflight", "build": "Build", "story_tests": "Story tests",
    "runner_checks": "Runner checks", "prerequisite": "A required prerequisite",
}.items():
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
        STOP_REASONS[f"worker_{_check}_attempts_exhausted_{_attempts}"] = (
            f"{_label} still failing after {_attempts} check attempts; "
            "owner: arrange further correction and authorize a new task."
        )


def safe_question(cfg, question):
    """Only an explicitly supplied public question may leave the builder result.

    Fail closed on private-looking text; never rewrite a question and call it a
    verbatim quote. Summaries, limitations and provider replies are not sources.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        return False
    if re.search(r"[/\\@<>\x00-\x08\x0b-\x1f\x7f]|[A-Za-z0-9_-]{40,}", question):
        return False
    # Sensitive values also appear in prose ("use account ...", "token ...").
    # Withhold the whole question rather than guessing where a value ends or
    # rewriting text that the owner is promised will be quoted verbatim.
    if re.search(r"(?i)(?:token|password|secret|api.key|account|login)\s*[:=]|\b(?:tokens?|passwords?|secrets?|api[ ._-]?keys?|accounts?|logins?|credentials?)\b|\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|AKIA[A-Z0-9]{16}|Bearer\s)", question):
        return False
    if re.search(r"(?im)\b(?:provider response|traceback|stdout|stderr|private key)\b", question):
        return False
    if any(name.lower() in question.lower() for name in cfg["identity_roles"]):
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
        if record.get("owner_question"):
            explanation += "\nBuilder question (verbatim):\n" + "\n".join(
                "> " + line for line in record["owner_question"].split("\n")
            ) + "\n"
        if record.get("failed_tests"):
            failures = record["failed_tests"]
            explanation += f"\nFailing tests: {failures['count']}. First failures: "
            explanation += ", ".join(failures["names"]) or "names unavailable"
            explanation += ". Owner: arrange a fix, then re-approve.\n"
    tail = ""
    if record.get("conflict_files"):
        explanation += "\nConflicting files (repository-relative):\n" + "\n".join(
            "- " + json.dumps(f, ensure_ascii=True) for f in record["conflict_files"]
        ) + "\n"
    if record.get("stop_save_failed"):
        explanation += "\nThe builder's work couldn't be saved completely; owner: ask the operator to reconcile it.\n"
    if record.get("continuation"):
        tail = ("\n\n### Continue from\nCommit: " + record["commit"]
                + "\nPaste this into the story and re-approve to continue from this work once #110 is in place; "
                  "without it, re-approval starts fresh.")
    return (
        "Task lifecycle record (metadata only; no approval implied).\n"
        + explanation
        + PREFIX
        + json.dumps(record, sort_keys=True)
        + SUFFIX
        + tail
    )


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
            )
            if body.split(SUFFIX, 1)[1] != record_body(expected).split(SUFFIX, 1)[1]:
                continue
            if (
                set(item) != set(expected) | {"from", "actor"}
                or item["key"] != expected["key"]
                or item["repository"] != cfg["repository"]
                or item["actor"] != actor
                or item["from"] not in STATES
            ):
                continue
            if expected_issue is not None and item["issue"] != expected_issue:
                continue
            if (
                item["kind"] in {"guard", "approval"}
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
                item["kind"] in {"guard", "approval"}
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


class GitHub:
    """Only this repository; no shell, credential discovery or arbitrary URLs."""

    def __init__(self, cfg, role="runner", token=None, *, integration_enabled=False):
        if type(integration_enabled) is not bool or (integration_enabled and role != "runner"):
            raise Unavailable("Invalid integration authentication opt-in")
        self.cfg, self.role = cfg, role
        self.integration_enabled = integration_enabled
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
        spec = importlib.util.spec_from_file_location(
            "_agentic_app_tokens", Path(__file__).with_name("app_tokens.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            self.token, self.token_expiry = module.mint(
                self.cfg, self.role, integration_enabled=self.integration_enabled
            )
        except module.AuthenticationError as error:
            raise Unavailable(str(error)) from None
        self.identity_verified = False

    def ensure_actor(self):
        if self.token_expiry is not None and time.time() >= self.token_expiry - 60:
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
        if not path.startswith("/") or ".." in path or "#" in path or "\\" in path:
            raise ValueError("Invalid repository API path")
        self.ensure_actor()
        if method != "GET":
            if not self.write_allowed(method, path):
                raise ValueError("Only repository issue/label writes are permitted")
            self.ensure_actor()
        url = "https://api.github.com/repos/" + self.cfg["repository"] + path
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
        raise ValueError(
            "Private state belongs to another repository or actor configuration"
        )


def bind_state(cfg):
    root = private(cfg["state_dir"])
    identity = state_identity(cfg)
    path = root / "identity.json"
    create(path, identity)
    if read(path) != identity:
        raise ValueError(
            "Private state belongs to another repository or actor configuration"
        )


def queue(cfg, record, *, role="runner", public_body=None):
    bind_state(cfg)
    if record["repository"] != cfg["repository"]:
        raise ValueError("Wrong outbox repository")
    folder = private(Path(cfg["state_dir"]) / "outbox")
    path = folder / (record["key"] + ".json")
    entry = dict(record=record, role=role, status="pending", comment_attempted=False)
    if public_body is not None:
        entry["public_body"] = public_body
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


def reject_transition(cfg, client, path, entry):
    record = entry["record"]
    entry.update(status="conflict", error="transition_not_allowed")
    message = f"Lifecycle transition {record['from']} → {record['to']} rejected by policy"
    if not entry.get("rejection_attempted"):
        entry["rejection_attempted"] = True
        save(path, entry)
        try:
            client.request("POST", f"/issues/{record['issue']}/comments", {"body": (
                f"Lifecycle transition {record['from']} → {record['to']} was rejected. "
                "The story stays at its current status because the required permission "
                "or verified owner-merge evidence is missing. Owner: reconcile the story "
                "before retrying."
            )})
        except Unavailable as exc:
            if not isinstance(exc, Uncertain):
                entry["rejection_attempted"] = False
            save(path, entry)
            raise ValueError(message + "; issue comment could not be confirmed") from None
    save(path, entry)
    raise ValueError(message)


def flush(cfg, client):
    bind_state(cfg)
    folder = private(Path(cfg["state_dir"]) / "outbox")
    with lock(Path(cfg["state_dir"]) / "outbox.lock") as acquired:
        if not acquired:
            return
        entries = [(p, read(p)) for p in folder.glob("*.json")]
        entries.sort(key=lambda x: (stamp(x[1]["record"]["timestamp"]), x[0].name))
        blocked_issues = set()
        supplied_client = client
        for path, entry in entries:
            if entry["status"] in {"synced", "superseded"}:
                continue
            expected_role = entry.get("role", "runner")
            try:
                client = (
                    supplied_client
                    if supplied_client.role == expected_role
                    else GitHub(cfg, expected_role)
                )
            except Unavailable:
                entry["status"] = "unsynced"
                save(path, entry)
                continue
            record = entry["record"]
            if record["issue"] in blocked_issues:
                continue
            if record["repository"] != cfg["repository"]:
                raise ValueError("Wrong outbox repository")
            try:
                remote = records(cfg, client.comments(record["issue"]), record["issue"])
                match = next((r for r in remote if r["key"] == record["key"]), None)
                if match:
                    entry.update(record=match, status="synced")
                else:
                    remote_issue = client.issue(record["issue"])
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
                    if not (restoring or attesting or assumption) and not permitted(
                        cfg, record["from"], record["to"], record["actor"]
                    ):
                        reject_transition(cfg, client, path, entry)
                    if record["from"] == "needs-owner" and record["to"] == "merged":
                        pr = client.request("GET", f"/pulls/{record['pr']}") if record["pr"] else None
                        if (expected_role not in {"workflow", "runner"} or pr is None
                                or not owner_merge_verified(cfg, record["issue"], remote, pr)
                                or record["commit"] != pr.get("merge_commit_sha")):
                            reject_transition(cfg, client, path, entry)
                    # Label replacement is idempotent; append-only comments are not.
                    if current != record["to"]:
                        client.set_state(record["issue"], record["to"])
                    if record["to"] in {"blocked", "needs-owner", "needs-primary"} and not entry.get(
                        "notified"
                    ):
                        client.notify(record["issue"])
                        entry["notified"] = True
                        save(path, entry)
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
                            {"body": entry.get("public_body", record_body(record))},
                        )
                    except Unavailable as exc:
                        if not isinstance(exc, Uncertain):
                            entry["comment_attempted"] = False
                        raise
                    entry["status"] = "synced"
                if record["to"] in {"blocked", "needs-owner", "needs-primary"} and not entry.get(
                    "notified"
                ):
                    client.notify(record["issue"])
                    entry["notified"] = True
                save(path, entry)
            except Unavailable:
                entry["status"] = "unsynced"
                save(path, entry)
                return


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
    mapping = {
        "queued": "dispatched",
        "running_worker": "in-progress",
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
    if target in {"blocked", "needs-owner", "needs-primary"}:
        worker = job.get("worker", {})
        supplied = worker.get("owner_question")
        if supplied is not None:
            if safe_question(cfg, supplied):
                question, reason = supplied, "owner_question"
            else:
                reason = "question_withheld"
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
            continuation=job.get("continuation", False),
            stop_save_failed=job.get("stop_save_failed", False),
        )
    ]


def sync_job(config_path, job, client=None):
    cfg = load_config(config_path)
    for record in job_records(cfg, job):
        queue(cfg, record)
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

    def ignore_snapshot():
        # This is not a rejection of the current label event. In particular do
        # not emit a rejection marker that guard_issue could apply to it.
        marker = "<!-- agentic-approval-snapshot-ignored:" + key(event) + " -->"
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
                "re-approve the current story.\n" + marker
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
                        "To retry, first remove every other status label, including "
                        "status:draft, in the issue's Labels picker. Then, as the owner, "
                        "remove status:approved if present and add it again, leaving it "
                        "as the only status label. The transition must still be allowed "
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
                         and e.get("created_at") == issue.get("updated_at")]
    if latest is not None and latest.get("created_at") != issue.get("updated_at"):
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
    candidates = [
        e
        for e in events
        if e.get("event") == "labeled"
        and e.get("label", {}).get("name") == "status:approved"
        and e.get("actor", {}).get("login") == cfg["actors"]["owner"]
        and e.get("created_at") == issue.get("updated_at")
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
        if source != "draft" or not permitted(
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


def guard_issue(cfg, client, number):
    events = client.events(number)
    canonical, event = canonical_state(cfg, events)
    issue = client.issue(number)
    if is_report(cfg, issue):
        return False
    # The DELETE from a rejected attestation emits its own webhook. It must
    # never cause the ordinary guard to put the unhonoured approval back.
    comments = client.comments(number) if canonical == "approved" else []
    proven = any(r["kind"] == "approval" and r["event"] == int(event["id"])
                 for r in records(cfg, comments, number)) if event else False
    if canonical == "approved" and event is not None and not proven and any(
            c.get("user", {}).get("login") == cfg["actors"]["workflow"]
            and (f"<!-- agentic-approval-rejected:{event['id']}:" in c.get("body", "")
                 or "<!-- agentic-approval-rejected:0:" in c.get("body", ""))
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
    if canonical in {"blocked", "needs-owner", "needs-primary"}:
        client.notify(number)
    try:
        actual = state(issue)
    except ValueError:
        actual = None
    if actual == canonical:
        # Runner stops already publish their job-bound reason (whose event is
        # the approval ID). Only manual stops need this label-event notice.
        if (canonical in {"blocked", "needs-owner", "needs-primary"}
                and not has_role(cfg, event.get("actor", {}).get("login"), "runner")
                and not any(
                r["to"] == canonical and r.get("reason") in STOP_REASONS
                and r["event"] == int(event["id"])
                for r in records(cfg, client.comments(number), number))):
            record = public_record(cfg, number, "label-stop", 0, canonical,
                                   event=int(event["id"]), kind="guard",
                                   reason="owner_requested_stop")
            queue(cfg, record, role="workflow")
            flush(cfg, client)
        return False
    client.set_state(number, canonical)
    record = public_record(
        cfg,
        number,
        "guard",
        0,
        canonical,
        event=max(
            (
                int(e["id"])
                for e in events
                if e.get("label", {}).get("name", "").startswith("status:")
            ),
            default=None,
        ),
        kind="guard",
        reason="invalid_status_change",
    )
    queue(cfg, record, role="workflow", public_body=(
        "The guard restored the last permitted status. Keep only one status label. "
        "To approve, the configured owner must add status:approved from a permitted "
        "state. If approval failed, first remove every other status label, including "
        "status:draft. Then, as the owner, remove status:approved if present and add "
        "it again, leaving it as the only status label. The transition must still "
        "be allowed from the last permitted status in the issue history.\n"
        + record_body(record)
    ))
    flush(cfg, client)
    return True


def trusted_gate(cfg, client, pr, *, strict_snapshot=False):
    """Unknown/partial/stale/name-colliding checks are never a passing gate."""
    try:
        # pull_request_target executes on the base commit, not the PR head.
        # Several PRs share that commit: collect all checks, then bind each run
        # separately to the PR number and reviewed head (and base for delivery).
        execution_sha = pr["base"]["sha"]
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
                    "The finding detail is retained privately. Primary triage is required; no approval implied.\n"
                    + marker
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
            issue["state"] != "open"
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
            raise ValueError("Admission changed; owner reconciliation required")
        dispatches = [
            r
            for r in records(cfg, client.comments(binding["issue"]), binding["issue"])
            if r["to"] == "dispatched"
        ]
        if not dispatches or dispatches[-1]["job"] != job["id"]:
            raise ValueError("Dispatch ownership changed")
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
