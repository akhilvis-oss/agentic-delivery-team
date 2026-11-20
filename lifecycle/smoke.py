"""Trusted live lifecycle harness. Candidate code runs only in a disposable child."""

from __future__ import annotations

import copy
import contextlib
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from lifecycle import app_tokens, core, private_content

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")
STAGES = ("admission", "approval", "build", "review", "pull-request", "review-gate", "owner-merge")


class PreparationFailed(core.Unavailable):
    """Candidate preparation failed before any scratch write or execution."""


def load_config(path):
    cfg = core.read(path)
    required = {"framework_repository", "repository", "agent_name", "lock_dir",
                "actors", "app_auth", "runner"}
    if (not required <= set(cfg) or set(cfg) - required - {"timeout_seconds", "trusted_gate_workflow",
            "framework_owner", "framework_runner", "daily_cap"}
            or any(not REPOSITORY.fullmatch(cfg[k]) for k in ("repository", "framework_repository"))
            or cfg["repository"] == cfg["framework_repository"]
            or not core.ID.fullmatch(cfg["agent_name"])
            or list(private_content.scan_text(cfg["agent_name"]))
            or not Path(cfg["lock_dir"]).is_absolute()):
        raise ValueError("Invalid smoke configuration")
    timeout = cfg.get("timeout_seconds", 1800)
    for field in ("framework_owner", "framework_runner"):
        if field in cfg and not core.LOGIN.fullmatch(cfg[field]):
            raise ValueError("Invalid framework identity")
    if (type(cfg.get("daily_cap", 12)) is not int or cfg.get("daily_cap", 12) < 1
            or cfg.get("framework_runner", cfg["actors"]["framework_runner"])
            != cfg["actors"]["framework_runner"]):
        raise ValueError("Invalid smoke watcher settings")
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("Invalid smoke timeout")
    if (set(cfg["actors"]) != {"runner", "owner", "framework_runner"}
            or any(not core.LOGIN.fullmatch(v) for v in cfg["actors"].values())
            or len(set(cfg["actors"].values())) != 3
            or set(cfg["app_auth"]) != {"runner", "owner", "framework_runner"}):
        raise ValueError("Three distinct App identities required")
    for settings in cfg["app_auth"].values():
        app_tokens.validate(settings, "runner")
    if len({s["app_id"] for s in cfg["app_auth"].values()}) != 3:
        raise ValueError("Three distinct Apps required")
    runner = cfg["runner"]
    fields = {"agent", "agent_home", "worker_model", "reviewer_model", "commit_identity",
              "verification", "profile", "phase_timeout_seconds", "max_rounds"}
    if (set(runner) - fields or not fields - {"phase_timeout_seconds", "max_rounds"} <= set(runner)
            or runner["agent"].get("environment", {}) not in (
                {}, {"CODEX_HOME": "{agent_home}"}, {"CLAUDE_CONFIG_DIR": "{agent_home}"})
            or set(runner["verification"]) - {"commands", "timeout_seconds"}):
        raise ValueError("Smoke runner settings must contain only agent and test configuration")
    return cfg


def environment(home):
    """Never inherit GitHub/provider credentials, import paths or Git configuration."""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
            "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0"}


def command(argv, *, cwd, env):
    try:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        raise core.Unavailable("Disposable install unavailable") from None


def export_candidate(cfg, candidate, root, env):
    if (not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}", candidate)
            or ".." in candidate or "//" in candidate):
        raise ValueError("Explicit candidate commit or ref required")
    source = root / "download"
    source.mkdir()
    command(["git", "init", "--bare", str(source)], cwd=root, env=env)
    command(["git", "-C", str(source), "fetch", "--no-tags", "--depth=1",
             "https://github.com/" + cfg["framework_repository"] + ".git", candidate], cwd=root, env=env)
    sha = command(["git", "-C", str(source), "rev-parse", "FETCH_HEAD^{commit}"], cwd=root, env=env).decode().strip()
    if not core.SHA.fullmatch(sha):
        raise core.Unavailable("Candidate commit unavailable")
    archive = command(["git", "-C", str(source), "archive", sha], cwd=root, env=env)
    install = root / "install"
    install.mkdir()
    # Export regular files only, with no traversal, links, devices or special modes.
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise core.Unavailable("Unsafe candidate archive")
            target = install / path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.extractfile(member).read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return install, sha


def candidate_config(cfg, root):
    """Construct from trusted defaults; never copy the framework authentication."""
    policy = core.read(ROOT / "templates/task-lifecycle.example.json")
    runner, owner = cfg["actors"]["runner"], cfg["actors"]["owner"]
    policy.update(repository=cfg["repository"], actors={"runner": runner, "product": runner,
                  "owner": owner, "workflow": "github-actions[bot]"},
                  identity_roles={runner: ["runner", "product"], owner: ["owner", "product"],
                                  "github-actions[bot]": ["workflow"]},
                  app_auth={"runner": copy.deepcopy(cfg["app_auth"]["runner"])},
                  poll_seconds=10,
                  trusted_gate_workflow=cfg.get("trusted_gate_workflow", ".github/workflows/pr-review-gate.yml"))
    policy.update({k: str(root / v) for k, v in {
        "state_dir": "state", "repository_root": "repository", "worktree_root": "worktrees",
        "runner_config": "runner.json", "primary_runner_config": "runner.json"}.items()})
    config_path = root / "lifecycle.json"
    core.save(config_path, policy)
    runner_cfg = copy.deepcopy(cfg["runner"])
    core.save(root / "profile.json", runner_cfg.pop("profile"))
    runner_cfg.update(profile=str(root / "profile.json"),
                      worktree=str(root / "unused-worktree"), branch="runner/smoke",
                      state_dir=str(root / "runner-state"), lifecycle_config=str(config_path),
                      integration={"enabled": True, "declared_tier": "standard",
                                   "public_evidence": (
                                       "## Security review\n"
                                       "Secrets: no private values published.\n"
                                       "External access/cost: none in the change; the run writes only "
                                       "to the scratch repository through the smoke Apps and uses "
                                       "one agent build and review.\n"
                                       "Data/provenance: the fixed smoke story and sanitized runner records.\n")},
                      github_receipts={"enabled": False})
    core.save(root / "runner.json", runner_cfg)
    return core.load_config(config_path), config_path


class SmokeGitHub(core.GitHub):
    """Harness-only writes: issue approval, closing PRs and deleting owned refs."""

    def write_allowed(self, method, path):
        return super().write_allowed(method, path) or (
            method == "PATCH" and bool(re.fullmatch(r"/pulls/[1-9]\d*", path)))

    def guard_runs(self, query=None, *, timeout):
        """Read Actions with the guard wait's remaining request budget.

        Admission already authenticated this client. A rejected or expiring App
        token refreshes before the next read; this transport never makes writes.
        """
        if query is not None and not re.fullmatch(r"(?:queued|in_progress|waiting|pending|requested|success)", query):
            raise ValueError("Invalid guard run query")
        if self.token_expiry is not None and time.time() >= self.token_expiry - 300:
            self.ensure_actor()
        status = "" if query is None else f"&status={query}"
        req = urllib.request.Request(
            "https://api.github.com/repos/" + self.cfg["repository"]
            + f"/actions/runs?event=issues{status}&per_page=1",
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json"})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.invalidate_app_token()
            raise core.Unavailable("Issue workflow activity unavailable") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise core.Unavailable("Issue workflow activity unavailable") from None

    def request(self, method, path, data=None):
        if method != "DELETE":
            return super().request(method, path, data)
        if not re.fullmatch(r"/git/refs/(?:heads/runner|agentic/continue)/issue-[1-9]\d*-event-[1-9]\d*", path):
            raise ValueError("Only smoke story refs may be deleted")
        self.ensure_actor()
        req = urllib.request.Request("https://api.github.com/repos/" + self.cfg["repository"] + path,
                                     method="DELETE", headers={"Authorization": "Bearer " + self.token,
                                     "Accept": "application/vnd.github+json"})
        try:
            with self.opener.open(req, timeout=30):
                return None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.invalidate_app_token()
            if exc.code == 404:
                return None
            raise core.Unavailable("Smoke reference cleanup unavailable") from None
        except OSError:
            raise core.Unavailable("Smoke reference cleanup unavailable") from None


class StatusGitHub(core.GitHub):
    def refresh_app_token(self):
        self.token, self.token_expiry = app_tokens.get(self.cfg, "runner", status_only=True)
        self.identity_verified = False

    def write_allowed(self, method, path):
        return method == "POST" and bool(re.fullmatch(r"/statuses/[0-9a-f]{40}", path))


def clients(cfg, policy):
    runner = SmokeGitHub(policy, integration_enabled=True)
    owner_cfg = {"repository": cfg["repository"], "actors": {
        "runner": cfg["actors"]["owner"], "owner": cfg["actors"]["runner"]},
        "app_auth": {"runner": cfg["app_auth"]["owner"]}}
    return runner, SmokeGitHub(owner_cfg)


def status_client(cfg):
    return StatusGitHub({"repository": cfg["framework_repository"], "actors": {
        "runner": cfg["actors"]["framework_runner"], "owner": cfg["actors"]["owner"]},
        "app_auth": {"runner": cfg["app_auth"]["framework_runner"]}})


def run_count(result):
    if (not isinstance(result, dict) or type(result.get("total_count")) is not int
            or result["total_count"] < 0 or not isinstance(result.get("workflow_runs"), list)
            or len(result["workflow_runs"]) != min(result["total_count"], 1)):
        raise core.Unavailable("Issue workflow activity unavailable")
    return result["total_count"]


def latest_run(result):
    if not run_count(result):
        return 0
    row = result["workflow_runs"][0]
    if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1:
        raise core.Unavailable("Issue workflow identity unavailable")
    return row["id"]


def guard_snapshot(client, *, clock, sleep):
    """Read a consistent pre-filing run identity, or return None after two minutes."""
    deadline = clock() + 120
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        try:
            previous = latest_run(client.guard_runs(timeout=min(30, remaining)))
            if clock() < deadline:
                return previous
        except core.Unavailable:
            pass
        observed = clock()
        if observed >= deadline:
            return None
        sleep(min(5, deadline - observed))


def guard_idle(client, previous_run, *, clock, sleep):
    """Wait at most two minutes for scratch issue workflows to drain.

    Actions does not bind issue runs to issue numbers. Admission requires an
    empty scratch repository, so conservatively include every issue workflow.
    """
    deadline = clock() + 120
    while True:
        busy = False
        processed = False
        # Require a successful run newer than the pre-filing snapshot. Empty
        # results can mean that GitHub has not made the opened event visible yet.
        for status in ("success", "queued", "in_progress", "waiting", "pending", "requested"):
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            try:
                result = client.guard_runs(status, timeout=min(30, remaining))
                if status == "success":
                    processed = latest_run(result) > previous_run
                else:
                    busy |= run_count(result) > 0
            except core.Unavailable:
                # An unreadable or inconsistent observation cannot prove idle.
                busy = True
            if clock() >= deadline:
                return False
        if processed and not busy:
            return True
        observed = clock()
        if observed >= deadline:
            return False
        sleep(min(5, deadline - observed))


def approve_once(client, number):
    try:
        client.request("POST", f"/issues/{number}/labels", {"labels": ["status:approved"]})
    except core.Uncertain:
        labels = {label["name"] if isinstance(label, dict) else label
                  for label in client.issue(number)["labels"]}
        if "status:approved" not in labels:
            raise core.Unavailable("Approval was not confirmed") from None


def evidence(policy, client, number):
    """Return contiguous progress, a terminal flag and a fixed public stop category."""
    history = core.records(policy, client.comments(number), number)
    stop = next((r for r in reversed(history) if r["to"] in {"blocked", "needs-owner", "needs-primary"}), None)
    category = stop["reason"] if stop else "pending"
    terminal = stop is not None
    labels = {label["name"] if isinstance(label, dict) else label
              for label in client.issue(number).get("labels", [])}
    stop_labels = labels & {"status:blocked", "status:needs-owner"}
    if any(not any(r["to"] == label.removeprefix("status:") for r in history)
           for label in stop_labels):
        terminal, category = False, "stop_unrecorded"
    approvals = [r for r in history if r["kind"] == "approval"]
    if not approvals:
        if "status:approved" in labels:
            return 0, False, "approval_unrecorded"
        return 0, terminal, category
    approval = approvals[-1]
    job = f"issue-{number}-event-{approval['event']}"
    records = [r for r in history if r["job"] == job and r["event"] == approval["event"]]
    if not (any(r["to"] == "dispatched" for r in records)
            and any(r["to"] == "committed" and r["commit"] for r in records)):
        return 1, terminal, category
    reviews = [r for r in records if r["kind"] == "review" and r["outcome"] == "ready_for_integration"
               and r["commit"] and not any(r["severities"].get(p, 0) for p in ("P0", "P1"))]
    if not reviews:
        return 2, terminal, category
    review = reviews[-1]
    if review["commit"] not in {r["commit"] for r in records if r["to"] == "committed"}:
        return 2, terminal, category
    prs = [p for p in client.pages("/pulls?state=all")
           if p.get("head", {}).get("ref") == "runner/" + job]
    if len(prs) != 1:
        return 3, terminal, category
    pr = client.request("GET", f"/pulls/{prs[0]['number']}")
    if (pr["user"]["login"] != policy["actors"]["runner"]
            or pr["head"]["sha"] != review["commit"]
            or pr["head"]["repo"]["full_name"] != policy["repository"]
            or pr["base"]["repo"]["full_name"] != policy["repository"]
            or pr["base"]["ref"] != policy.get("base_branch", "main")
            or not re.search(rf"\bCloses\s+#{number}\b", pr.get("body") or "", re.I)):
        return 3, terminal, category
    gate = core.trusted_gate(policy, client, pr, strict_snapshot=True)
    if gate != "pass":
        return 4, terminal or gate == "fail", "gate_failed" if gate == "fail" else category
    fresh = client.request("GET", f"/pulls/{pr['number']}")
    if (fresh["head"]["sha"] != pr["head"]["sha"]
            or fresh["base"]["sha"] != pr["base"]["sha"]
            or fresh["state"] != pr["state"] or fresh.get("body") != pr.get("body")):
        return 4, True, "unavailable"
    if (not stop or stop not in records or stop["to"] != "needs-owner"
            or stop["reason"] != "owner_merge_required" or stop["commit"] != review["commit"]
            or stop["pr"] != pr["number"] or pr["state"] != "open" or pr.get("merged")
            or core.state(client.issue(number)) != "needs-owner"):
        return 5, terminal, category
    return 6, True, "owner_merge_required"


def cleanup(policy, client, number):
    """Attempt every cleanup action even if an earlier GitHub write fails."""
    errors = []
    def attempt(call):
        try:
            call()
        except Exception as error:
            diagnose("cleanup", error)
            errors.append(True)
    # Admission required an empty scratch repo; still restrict deletions to this issue.
    prs = []
    refs = []
    attempt(lambda: prs.extend(client.pages("/pulls?state=all")))
    for namespace in ("heads/runner", "agentic/continue"):
        def collect(namespace=namespace):
            seen = set()
            for page in range(1, 1001):
                batch = client.request("GET", f"/git/matching-refs/{namespace}/issue-{number}-event-?per_page=100&page={page}")
                if not isinstance(batch, list):
                    raise core.Unavailable("Reference collection unavailable")
                for ref in batch:
                    if ref["ref"] in seen:
                        raise core.Unavailable("Reference collection unstable")
                    seen.add(ref["ref"])
                    refs.append(ref)
                if len(batch) < 100:
                    return
            raise core.Unavailable("Reference collection exceeds safety bound")
        attempt(collect)
    for pr in prs:
        branch = pr.get("head", {}).get("ref", "")
        if re.fullmatch(rf"runner/issue-{number}-event-[1-9]\d*", branch):
            if pr["state"] == "open":
                attempt(lambda pr=pr: client.request("PATCH", f"/pulls/{pr['number']}", {"state": "closed"}))
    attempt(lambda: client.request("PATCH", f"/issues/{number}", {"state": "closed"}))
    reports = []
    attempt(lambda: reports.extend(client.pages("/issues?state=open")))
    for report in reports:
        if (report.get("user", {}).get("login") == policy["actors"]["runner"]
                and "<!-- agentic-service-status-v1 -->" in (report.get("body") or "")):
            attempt(lambda report=report: client.request("PATCH", f"/issues/{report['number']}", {"state": "closed"}))
    for ref in refs:
        name = ref.get("ref", "")
        if re.fullmatch(rf"refs/(?:heads/runner|agentic/continue)/issue-{number}-event-[1-9]\d*", name):
            attempt(lambda name=name: client.request("DELETE", "/git/" + name))
    return not errors


def start_child(install, config_path, env):
    with (config_path.parent / "candidate.log").open("ab") as log:
        os.fchmod(log.fileno(), 0o600)
        return subprocess.Popen([sys.executable, "-m", "lifecycle.service", "--config", str(config_path), "serve"],
                                cwd=install, env=env, stdout=log, stderr=log,
                                start_new_session=True)


def stop_child(child):
    # Python's SIGINT unwinds the candidate runner's finally blocks. Its agents
    # and verification commands own separate sessions, which those blocks reap.
    # SIGTERM would skip that cleanup and could leave an agent alive.
    try:
        os.killpg(child.pid, signal.SIGINT)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=10)
    except ProcessLookupError:
        child.wait(timeout=10)


def description(agent, passed, progress, category):
    # All fields are allowlisted; never interpolate exceptions, agent prose or API text.
    if category not in set(core.STOP_REASONS) | {"pending", "timeout", "child_stopped", "unavailable",
                                          "cleanup_failed", "gate_failed", "busy", "stop_unrecorded",
                                          "approval_unrecorded"}:
        category = "unavailable"
    text = (f"{'PASS' if passed else 'FAIL'}; furthest={STAGES[progress]}; "
            f"next={STAGES[min(progress + 1, 6)]}; stop={category}.")
    if list(private_content.scan_text(text)):
        raise ValueError("Smoke result is not public")
    return text


def diagnose(step, error):
    """Local stderr only; public statuses and comments never include exceptions."""
    if getattr(error, "_smoke_diagnosed", False):
        return
    error._smoke_diagnosed = True
    origin = "unknown"
    trace = error.__traceback__
    while trace is not None:
        module = trace.tb_frame.f_globals.get("__name__", "")
        if module.startswith(("lifecycle.", "runner.")):
            origin = module + "." + trace.tb_frame.f_code.co_name
        trace = trace.tb_next
    message = " ".join(str(error).splitlines())
    print(f"Smoke step={step}; {type(error).__name__}: {message}; function={origin}",
          file=sys.stderr)


def failed_runs_directory(cfg):
    # Separate from candidate repositories and service state, private to this OS user.
    return core.private(Path(tempfile.gettempdir()) / f"lifecycle-smoke-failures-{os.getuid()}")


@contextlib.contextmanager
def workspace(cfg):
    result = {"passed": False}
    with tempfile.TemporaryDirectory(prefix="lifecycle-smoke-") as folder:
        root = Path(folder).resolve()
        try:
            yield root, result
        finally:
            if not result["passed"]:
                parent = failed_runs_directory(cfg)
                fd = os.open(parent / ".retention.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "a+") as handle:
                    # Different scratch repositories may finish concurrently.
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    destination = parent / f"{time.time_ns():020d}-{uuid.uuid4().hex}"
                    shutil.move(str(root), destination)
                    print(f"Smoke failure evidence: {destination}", file=sys.stderr)
                    runs = sorted(p for p in parent.iterdir()
                                  if re.fullmatch(r"[0-9]{20}-[0-9a-f]{32}", p.name)
                                  and p.is_dir() and not p.is_symlink())
                    for old in runs[:-3]:
                        shutil.rmtree(old)


def run(cfg, candidate, *, clock=time.monotonic, sleep=time.sleep, completed=None):
    progress, category, passed = 0, "unavailable", False
    with core.lock(Path(cfg["lock_dir"]) / (core.key(cfg["repository"]) + ".lock")) as acquired:
        if not acquired:
            print("smoke/" + cfg["agent_name"] + ": " + description(cfg["agent_name"], False, 0, "busy"))
            return 1
        with workspace(cfg) as (root, result):
            try:
                home = core.private(root / "home")
                env = environment(home)
                install, sha = export_candidate(cfg, candidate, root, env)
            except Exception as error:
                diagnose("preparation", error)
                failure = PreparationFailed("Candidate preparation unavailable")
                failure._smoke_diagnosed = True
                raise failure from None
            policy = runner = None
            number = child = None
            marker = "<!-- agentic-smoke-run:" + uuid.uuid4().hex + " -->"
            creation_attempted = False
            step = "admission"
            try:
                policy, config_path = candidate_config(cfg, root)
                runner, owner = clients(cfg, policy)
                if runner.pages("/issues?state=open") or runner.pages("/pulls?state=open"):
                    category = "busy"
                elif runner.request("GET", "/").get("allow_auto_merge") is True:
                    category = "unavailable"
                else:
                    step = "clone"
                    command(["git", "clone", "--", "https://github.com/" + cfg["repository"] + ".git",
                             str(root / "repository")], cwd=root, env=env)
                    story = (ROOT / "tests/smoke/story.md").read_text() + "\n" + marker
                    step = "approval"
                    previous_run = guard_snapshot(runner, clock=clock, sleep=sleep)
                    if previous_run is None:
                        category = "busy"
                    else:
                        creation_attempted = True
                        issue = owner.request("POST", "/issues", {"title": "Lifecycle smoke story",
                                              "body": story, "labels": ["status:draft"]})
                        number = issue["number"]
                        step = "guard-idle"
                        if not guard_idle(runner, previous_run, clock=clock, sleep=sleep):
                            category = "busy"
                        else:
                            step = "approval"
                            approve_once(owner, number)
                            deadline = clock() + cfg.get("timeout_seconds", 1800)
                            step = "start-child"
                            child = start_child(install, config_path, env)
                            unrecorded_since = None
                            unrecorded_category = None
                            while True:
                                step = "evidence"
                                progress, terminal, category = evidence(policy, runner, number)
                                observed = clock()
                                if category in {"stop_unrecorded", "approval_unrecorded"}:
                                    if unrecorded_since is None or category != unrecorded_category:
                                        unrecorded_since = observed
                                    unrecorded_category = category
                                    if observed - unrecorded_since >= 180:
                                        break
                                else:
                                    unrecorded_since = None
                                    unrecorded_category = None
                                if observed >= deadline:
                                    category = "timeout"
                                    break
                                if terminal:
                                    passed = progress == 6
                                    break
                                if child.poll() is not None:
                                    category = "child_stopped"
                                    break
                                sleep(min(5, max(0, deadline - clock())))
            except (Exception, KeyboardInterrupt) as error:
                diagnose(step, error)
                category = "unavailable"
            finally:
                try:
                    if child is not None:
                        stop_child(child)
                except Exception as error:
                    diagnose("stop-child", error)
                    passed, category = False, "cleanup_failed"
                if creation_attempted and number is None:
                    # A create may succeed despite a lost response. Discover by
                    # the per-run marker; never repeat the write or approve it.
                    try:
                        orphaned = [i for i in runner.pages("/issues?state=all")
                                    if i.get("user", {}).get("login") == cfg["actors"]["owner"]
                                    and marker in (i.get("body") or "") and "pull_request" not in i]
                        for issue in orphaned:
                            if not cleanup(policy, runner, issue["number"]):
                                category = "cleanup_failed"
                    except Exception as error:
                        diagnose("cleanup-discovery", error)
                        category = "cleanup_failed"
                if number is not None and not cleanup(policy, runner, number):
                    passed, category = False, "cleanup_failed"
            text = description(cfg["agent_name"], passed, progress, category)
            print("smoke/" + cfg["agent_name"] + ": " + text)
            if category == "busy":
                return 1
            if completed is not None:
                completed(sha, "success" if passed else "failure", text)
            try:
                status_client(cfg).request("POST", "/statuses/" + sha, {
                    "state": "success" if passed else "failure", "context": "smoke/" + cfg["agent_name"],
                    "description": text})
            except Exception as error:
                diagnose("status", error)
                print("Commit status permission missing or unavailable; result could not be posted.")
                return 1
            result["passed"] = passed
            return 0 if passed else 1


def main(config_path, candidate):
    try:
        return run(load_config(config_path), candidate)
    except Exception as error:
        diagnose("configuration/preparation", error)
        print("Smoke refused: configuration, candidate or GitHub prerequisite unavailable.")
        return 1
