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
    return record


def record_body(record):
    return (
        "Task lifecycle record (metadata only; no approval implied).\n"
        + PREFIX
        + json.dumps(record, sort_keys=True)
        + SUFFIX
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
        if actor not in trusted or body.count(PREFIX) != 1 or not body.endswith(SUFFIX):
            continue
        try:
            item = json.loads(body.split(PREFIX)[1][: -len(SUFFIX)])
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
            )
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
                item["from"] != item["to"] or item["reason"] != "invalid_status_change"
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
                        entry.update(status="conflict", error="transition_not_allowed")
                        save(path, entry)
                        blocked_issues.add(record["issue"])
                        continue
                    # Label replacement is idempotent; append-only comments are not.
                    if current != record["to"]:
                        client.set_state(record["issue"], record["to"])
                    if record["to"] in {"blocked", "needs-owner"} and not entry.get(
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
                if record["to"] in {"blocked", "needs-owner"} and not entry.get(
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
            reason=integration.get("reason") or (
                "execution_failed" if status in {"failed", "interrupted"} else None),
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
        or event.get("sender", {}).get("login") != cfg["actors"]["owner"]
    ):
        return
    issue = event["issue"]
    events = client.events(issue["number"])
    # Bind the webhook snapshot to a unique owner label event. Missing or
    # ambiguous timestamp evidence fails closed, rather than guessing an event.
    candidates = [
        e
        for e in events
        if e.get("event") == "labeled"
        and e.get("label", {}).get("name") == "status:approved"
        and e.get("actor", {}).get("login") == cfg["actors"]["owner"]
        and e.get("created_at") == issue.get("updated_at")
    ]
    canonical, latest = canonical_state(cfg, events)
    if len(candidates) != 1 or latest != candidates[0] or canonical != "approved":
        raise Unavailable(
            "Approval event snapshot is unavailable or ambiguous; re-approve"
        )
    record = public_record(
        cfg,
        issue["number"],
        "approval",
        0,
        "approved",
        event=int(latest["id"]),
        kind="approval",
        scope_sha256=hashlib.sha256((issue.get("body") or "").encode()).hexdigest(),
        approved_by=cfg["actors"]["owner"],
    )
    queue(cfg, record, role="workflow")
    flush(cfg, client)


def guard_issue(cfg, client, number):
    events = client.events(number)
    canonical, event = canonical_state(cfg, events)
    issue = client.issue(number)
    if is_report(cfg, issue):
        return False
    if canonical in {"blocked", "needs-owner"}:
        client.notify(number)
    try:
        actual = state(issue)
    except ValueError:
        actual = None
    if actual == canonical:
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
    queue(cfg, record, role="workflow")
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
    for issue in managed:
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
