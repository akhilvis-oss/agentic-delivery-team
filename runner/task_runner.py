"""Explicit, bounded local agent delegation. No automatic integration or replay."""

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
import signal
import subprocess
import tempfile
import time

# Adapter compatibility envelope: inspected CLI flags, synthetic protocol tests.
# Deliberately narrow; extending it requires reviewing flags and lifecycle fixtures.
CLI_MIN = "0.164.0-alpha.11.1"
CLI_MAX_EXCLUSIVE = "0.164.0-alpha.11.2"
PROTOCOL = "codex-jsonl-v1"
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
SHA = re.compile(r"[0-9a-f]{40}")
TERMINAL = {"ready_for_integration", "needs_primary_review", "blocked", "failed", "interrupted"}
SUMMARIES = {
    "queued": "Assignment queued; no approval implied.",
    "running_worker": "Worker executing bounded assignment.",
    "running_reviewer": "Independent read-only review in progress.",
    "ready_for_integration": "Independent review accepted the committed range; owner approval required.",
    "needs_primary_review": "Correction limit reached; primary review required.",
    "blocked": "A prerequisite or compatibility check blocked the assignment.",
    "failed": "Execution or integrity verification failed; inspect private local state.",
    "interrupted": "Previous execution interrupted; reconcile manually, never replay automatically.",
}


class Blocked(ValueError):
    """A known prerequisite failure before a model call."""


class Cancelled(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def save(path, value):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def path_name(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Expected a repository-relative POSIX path")
    return value


def git(config, *args):
    # Never consult inherited Git configuration overrides or another repository.
    return subprocess.check_output(["git", "-C", config["worktree"], *args],
                                   env=environment(config), text=True, stderr=subprocess.DEVNULL).rstrip("\n")


def environment(config):
    # Allowlist instead of guessing every provider's API-key name. HOME retains
    # its original meaning; agent auth is selected only via dedicated CODEX_HOME.
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update(CODEX_HOME=config["codex_home"], PYTHONDONTWRITEBYTECODE="1")
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[key] = "2"
    return env


@contextlib.contextmanager
def lock(path):
    path = Path(path)
    private_dir(path.parent)
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


def load_profile(path):
    profile = read(path)
    required = {"worker_prompt", "reviewer_prompt", "scope", "extra_scopes", "coordination_files"}
    if not isinstance(profile, dict) or set(profile) != required:
        raise ValueError("Profile requires prompts, scope, extra_scopes and coordination_files")
    for key in ("worker_prompt", "reviewer_prompt", "scope"):
        if not isinstance(profile[key], str) or not profile[key].strip():
            raise ValueError("Profile prompts and scope must be nonempty strings")
    if not isinstance(profile["extra_scopes"], dict):
        raise ValueError("extra_scopes must map scope names to instructions")
    for key, value in profile["extra_scopes"].items():
        if not ID.fullmatch(key) or not isinstance(value, str) or not value.strip():
            raise ValueError("Invalid optional scope")
    if not isinstance(profile["coordination_files"], list):
        raise ValueError("coordination_files must be an explicit list")
    for name in profile["coordination_files"]:
        path_name(name)
    return profile


def load_config(path):
    config = read(path)
    for key in ("worktree", "state_dir", "codex_home", "codex_bin", "profile",
                "branch", "worker_model", "reviewer_model"):
        if not isinstance(config.get(key), str) or not config[key].strip() or "<" in config[key]:
            raise ValueError("Config requires populated paths, branch and agent models")
    for key in ("worktree", "state_dir", "codex_home", "codex_bin", "profile"):
        if not Path(config[key]).is_absolute():
            raise ValueError("Config paths must be absolute")
    worktree = Path(config["worktree"]).resolve()
    for key in ("state_dir", "codex_home"):
        location = Path(config[key]).resolve()
        if location == worktree or worktree in location.parents:
            raise ValueError("Private state and agent home must be outside the worktree")
    if Path(config["state_dir"]).resolve() == Path(config["codex_home"]).resolve():
        raise ValueError("Agent home and runner state must be separate directories")
    if config["branch"] in {"main", "master"}:
        raise ValueError("Runner requires a feature branch")
    if type(config.get("max_rounds", 2)) is not int or not 1 <= config.get("max_rounds", 2) <= 2:
        raise ValueError("At most two worker/review rounds are permitted")
    timeout = config.get("phase_timeout_seconds", 1800)
    if type(timeout) is not int or not 60 <= timeout <= 3600:
        raise ValueError("Phase timeout must be between one minute and one hour")
    receipts = config.get("github_receipts", {"enabled": False})
    if type(receipts.get("enabled")) is not bool:
        raise ValueError("GitHub receipts require an explicit enabled boolean")
    repository = receipts.get("repository", "")
    if receipts["enabled"] and (not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
                                or any(part in {".", ".."} for part in repository.split("/"))):
        raise ValueError("GitHub receipts require an explicit repository")
    config["project_profile"] = load_profile(config["profile"])
    if config.get("worker_start", "new") not in {"new", "fork", "resume"}:
        raise ValueError("worker_start must be new, fork or resume")
    if config.get("worker_start", "new") != "new" and not config.get("worker_thread_id"):
        raise ValueError("Fork/resume needs an explicit seed thread")
    return config


def version_key(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", value)
    if not match:
        raise Blocked("Unrecognised agent CLI version format")
    pre = match[4]
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return (*map(int, match.group(1, 2, 3)), 1 if pre is None else 0, parts)


def supported_version(value):
    return version_key(CLI_MIN) <= version_key(value) < version_key(CLI_MAX_EXCLUSIVE)


def check_cli(config):
    try:
        result = subprocess.run([config["codex_bin"], "--version"], env=environment(config),
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Blocked("Agent CLI version probe failed; no model call made") from exc
    match = re.fullmatch(r"codex-cli (\S+)\s*", result.stdout)
    if not match or not supported_version(match[1]):
        raise Blocked(f"Unsupported agent CLI; supported range is [{CLI_MIN}, {CLI_MAX_EXCLUSIVE})")
    return match[1]


def timestamp(value):
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Missing timezone")
    return stamp


def active_threads(config, required_thread=None):
    """Fail closed on unknown rollout/lifecycle formats, including partial writes."""
    active = []
    known = set()
    stopped = {}
    for job_path in (Path(config["state_dir"]) / "tasks").glob("*.json"):
        job = read(job_path)
        if job.get("owned_stop_at") and not job.get("child_pid") and job.get("worker_thread_id"):
            thread, stamp = job["worker_thread_id"], timestamp(job["owned_stop_at"])
            stopped[thread] = max(stopped.get(thread, stamp), stamp)
    sessions = Path(config["codex_home"]) / "sessions"
    for path in sessions.rglob("*.jsonl"):
        meta, event, latest = None, None, None
        try:
            if path.is_symlink():
                raise ValueError("Symlink rollout")
            for line in path.read_text().splitlines():
                row = json.loads(line)
                stamp = timestamp(row["timestamp"])
                latest = max(latest, stamp) if latest else stamp
                payload = row["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("Unknown payload")
                if row["type"] == "session_meta":
                    if (not isinstance(payload.get("id"), str) or not payload["id"]
                            or not isinstance(payload.get("cwd"), str)
                            or not supported_version(payload.get("cli_version", ""))):
                        raise ValueError("Unknown session metadata")
                    if meta is None:  # Forks may append copies of parent metadata.
                        meta = payload
                elif row["type"] == "event_msg":
                    kind = payload.get("type")
                    if kind in {"task_started", "task_complete", "turn_aborted"}:
                        event = kind
                    elif kind not in {"token_count", "agent_message", "user_message",
                                      "agent_reasoning", "agent_reasoning_raw_content"}:
                        raise ValueError("Unknown lifecycle event")
                elif row["type"] not in {"response_item", "turn_context", "compacted"}:
                    raise ValueError("Unknown rollout record")
            if not meta or event is None:
                raise ValueError("Missing session/lifecycle metadata")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Blocked("Unrecognised session/lifecycle format; no model call made") from exc
        if meta["cwd"] == config["worktree"]:
            known.add(meta["id"])
        if meta["cwd"] == config["worktree"] and event == "task_started":
            stop = stopped.get(meta["id"])
            if not stop or not latest or latest > stop:
                active.append(meta["id"])
    if required_thread is not None and required_thread not in known:
        raise Blocked("Seed or managed session metadata unavailable for this worktree")
    return active


def schema(role):
    fields = {
        "status": {"type": "string", "enum": ["ready_for_review", "blocked"] if role == "worker"
                   else ["ready_for_integration", "needs_changes", "blocked"]},
        "summary": {"type": "string"},
        "tests": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    }
    if role == "reviewer":
        finding = {key: {"type": "string"} for key in ("severity", "path", "detail")}
        fields["findings"] = {"type": "array", "items": {"type": "object", "properties": finding,
                              "required": list(finding), "additionalProperties": False}}
    return {"type": "object", "properties": fields, "required": list(fields),
            "additionalProperties": False}


def validate_result(result, role):
    spec = schema(role)
    if not isinstance(result, dict) or set(result) != set(spec["required"]):
        raise ValueError("Incomplete structured result")
    if result["status"] not in spec["properties"]["status"]["enum"]:
        raise ValueError("Invalid result status")
    if not isinstance(result["summary"], str):
        raise ValueError("Invalid summary")
    for key in ("tests", "limitations"):
        if not isinstance(result[key], list) or any(not isinstance(v, str) for v in result[key]):
            raise ValueError("Invalid result list")
    if role == "reviewer":
        if not isinstance(result["findings"], list):
            raise ValueError("Invalid findings")
        for finding in result["findings"]:
            if (not isinstance(finding, dict) or set(finding) != {"severity", "path", "detail"}
                    or any(not isinstance(v, str) for v in finding.values())):
                raise ValueError("Invalid finding")
        if result["status"] == "ready_for_integration" and result["findings"]:
            raise ValueError("Review cannot approve with unresolved findings")


def dirty_paths(config):
    raw = git(config, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    records = iter(raw.split("\0"))
    paths = []
    for row in records:
        if not row:
            continue
        paths.append(row[3:])
        if "R" in row[:2] or "C" in row[:2]:
            paths.append(next(records))
    return paths


def fingerprint(config):
    h = hashlib.sha256()
    for args in [("rev-parse", "HEAD"), ("branch", "--show-current"),
                 ("diff", "HEAD", "--binary"), ("diff", "--cached", "--binary")]:
        h.update(git(config, *args).encode())
    # Include ignored files too: the reviewer must not mutate an ignored artifact.
    for name in sorted(filter(None, git(config, "ls-files", "--others", "-z").split("\0"))):
        h.update(name.encode())
        path = Path(config["worktree"]) / name
        if path.is_symlink():
            h.update(os.readlink(path).encode())
        else:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(block)
    return h.hexdigest()


def command(config, role, output, output_schema):
    cmd = [config["codex_bin"]]
    if role == "reviewer":
        cmd += ["--ask-for-approval", "never"]
    cmd += ["exec", "--ignore-user-config", "--cd", config["worktree"],
            "--model", config[f"{role}_model"], "--json", "--color", "never",
            "--output-schema", str(output_schema), "--output-last-message", str(output)]
    if role == "worker":
        cmd += ["--approve-for-me"]
        if config.get("worker_start", "new") != "new":
            cmd += [config["worker_start"], config["worker_thread_id"]]
    else:
        cmd += ["--sandbox", "read-only"]
    return cmd + ["-"]


def prompt_for(config, job, role):
    profile = job["assignment"]["profile"]
    text = (f"Bounded assignment {job['id']}, round {job['round']}.\n"
            f"Required worktree: {config['worktree']}; branch: {config['branch']}.\n"
            "Do not spawn agents, change branches, reset others' work, push, merge, install "
            "dependencies or control services. Never bypass sandbox or approval controls. "
            "Use the assigned definitions; escalate design decisions to the primary.\n"
            + profile["scope"] + "\n" + profile[f"{role}_prompt"]
            + "\nIMMUTABLE ASSIGNMENT:\n" + job["assignment"]["task_text"])
    if role == "worker":
        for scope in job["assignment"]["scopes"]:
            text += "\nExplicit worker-only scope: " + profile["extra_scopes"][scope]
        text += "\nCommit only task changes. Report ready_for_review only after local commits and tests."
        if job.get("review"):
            text += "\nAddress independent findings:\n" + json.dumps(job["review"])
    else:
        text += (f"\nINDEPENDENT READ-ONLY REVIEW: exactly {job['base_commit']}..{job['head_commit']}. "
                 "Treat worker claims as unverified. Do not modify files or commit. Run only "
                 "read-only checks without cache or bytecode writes. No optional worker operations "
                 "are authorized for the reviewer. Ready for integration is not merge approval.\n"
                 "Worker claims:\n" + json.dumps(job.get("worker", {})))
    return text + "\nReturn only the supplied result schema.\n"


def parse_events(path):
    """Reject unknown exec lifecycle formats rather than interpreting them as success."""
    types, thread = [], None
    try:
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            kind = row["type"]
            if kind == "thread.started":
                if thread is not None or not isinstance(row.get("thread_id"), str) or not row["thread_id"]:
                    raise ValueError("Invalid thread event")
                thread = row["thread_id"]
            elif kind not in {"turn.started", "turn.completed", "turn.failed", "error",
                              "item.started", "item.updated", "item.completed"}:
                raise ValueError("Unknown exec event")
            types.append(kind)
    except (ValueError, KeyError, TypeError) as exc:
        raise Blocked("Unrecognised exec session/lifecycle format") from exc
    if (not thread or types[:2] != ["thread.started", "turn.started"]
            or types[-1:] != ["turn.completed"] or types.count("turn.completed") != 1
            or "turn.failed" in types or "error" in types):
        raise RuntimeError("Agent did not report a completed turn")
    return thread


def stop_group(proc):
    # Only this Popen's freshly-created process group is ever signalled.
    for sig, timeout in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 2)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        # Root may exit before descendants; send KILL to the same owned group too.
    proc.wait()


def execute(config, state, job_path, job, role):
    folder = state / "runs" / job["id"] / f"round_{job['round']}_{role}"
    if folder.exists():
        raise RuntimeError("Phase output exists; never reuse a previous result")
    private_dir(folder)
    output, output_schema = folder / "result.json", folder / "schema.json"
    save(output_schema, schema(role))
    prompt_path = folder / "prompt.txt"
    fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(prompt_for(config, job, role))
    job.update(status="running_" + role, updated_at=now())
    save_job(config, job_path, job)
    run_config = dict(config)
    thread_file = state / "worker_thread.json"
    if role == "worker" and thread_file.exists():
        run_config.update(worker_start="resume", worker_thread_id=read(thread_file)["thread_id"])
    proc, owned_stop = None, False
    try:
        with prompt_path.open("rb") as stdin:
            stdout_fd = os.open(folder / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stderr_fd = os.open(folder / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
                proc = subprocess.Popen(command(run_config, role, output, output_schema),
                                        cwd=config["worktree"], env=environment(config),
                                        stdin=stdin, stdout=stdout, stderr=stderr,
                                        start_new_session=True, umask=0o077)
                job["child_pid"] = proc.pid
                save_job(config, job_path, job)
                deadline = time.monotonic() + config.get("phase_timeout_seconds", 1800)
                while proc.poll() is None:
                    # Capture an owned worker identity even if subsequently cancelled.
                    if role == "worker" and not job.get("worker_thread_id"):
                        for line in (folder / "events.jsonl").read_text().splitlines()[:10]:
                            try:
                                event = json.loads(line)
                                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                                    job["worker_thread_id"] = event["thread_id"]
                            except ValueError:
                                pass
                    if (state / "CANCEL").exists():
                        owned_stop = True
                        raise Cancelled("Cancelled by local coordinator CANCEL file")
                    if time.monotonic() >= deadline:
                        owned_stop = True
                        raise TimeoutError(f"{role} exceeded phase time limit")
                    time.sleep(0.05)
        if proc.returncode:
            raise RuntimeError("Agent process failed; inspect private phase logs")
        thread = parse_events(folder / "events.jsonl")
        if role == "worker":
            if run_config.get("worker_start") == "resume" and thread != run_config["worker_thread_id"]:
                raise Blocked("Resumed worker reported a different session identity")
            job["worker_thread_id"] = thread
            save(thread_file, {"thread_id": thread, "updated_at": now()})
        elif thread == job.get("worker_thread_id"):
            raise RuntimeError("Reviewer reused worker identity")
        result = read(output)
        validate_result(result, role)
        return result
    finally:
        if proc is not None:
            stop_group(proc)
        if owned_stop:
            job["owned_stop_at"] = now()
        job.pop("child_pid", None)


class GitHubClient:
    def post(self, repository, number, receipt):
        # Only fixed runner summaries are eligible, never agent-produced summaries.
        body = (f"Task receipt: {receipt['status']}\nCommits: {receipt['base_commit']}.."
                f"{receipt['head_commit']}\nSummary: {receipt['summary']}\nRun: {receipt['timestamp']}")
        subprocess.run(["gh", "api", "--method", "POST", f"repos/{repository}/issues/{number}/comments",
                        "--input", "-"], input=json.dumps({"body": body}), text=True,
                       capture_output=True, check=True, timeout=30)


def save_job(config, path, job):
    save(path, job)
    receipt = {"status": job["status"], "base_commit": job["base_commit"],
               "head_commit": job.get("head_commit"), "summary": SUMMARIES[job["status"]],
               "timestamp": job["updated_at"]}
    save(Path(config["state_dir"]) / "receipts" / f"{job['id']}.json", receipt)


def publish_receipt(config, path, job, client=None):
    options = config.get("github_receipts", {"enabled": False})
    if (not options.get("enabled") or not job.get("github_number")
            or job["status"] not in TERMINAL or job.get("receipt_delivery")):
        return
    # Mark before sending: crash/transport uncertainty must not cause duplicate posts.
    job["receipt_delivery"] = "attempted"
    save_job(config, path, job)
    try:
        (client or GitHubClient()).post(options["repository"], job["github_number"],
                                       read(Path(config["state_dir"]) / "receipts" / f"{job['id']}.json"))
        job["receipt_delivery"] = "posted"
    except Exception:
        job["receipt_delivery"] = "unconfirmed; inspect GitHub before any manual retry"
    save_job(config, path, job)


def enqueue(config, task_id, task_text, *, kind="work", base=None, head=None, delay=0,
            after_task=None, scopes=(), github_number=None):
    if not ID.fullmatch(task_id) or not 0 <= delay <= 86400 or kind not in {"work", "review"}:
        raise ValueError("Invalid task ID, kind or delay")
    state = private_dir(config["state_dir"])
    private_dir(state / "tasks")
    if after_task and (not ID.fullmatch(after_task) or after_task == task_id
                       or not (state / "tasks" / f"{after_task}.json").is_file() or kind == "review"):
        raise ValueError("Dependency must name another existing work task")
    profile = config["project_profile"]
    if any(scope not in profile["extra_scopes"] for scope in scopes) or kind == "review" and scopes:
        raise ValueError("Unknown scope or extra scopes requested for read-only review")
    if github_number is not None and (type(github_number) is not int or github_number < 1):
        raise ValueError("Invalid linked GitHub issue/PR number")
    current = git(config, "rev-parse", "HEAD")
    if kind == "review":
        if not base or not head or not SHA.fullmatch(base) or not SHA.fullmatch(head) or head != current:
            raise ValueError("Review-only needs exact full base/head SHAs with HEAD at the reviewed head")
        git(config, "merge-base", "--is-ancestor", base, head)
    else:
        base, head = current, None
    assignment = {"task_text": task_text, "task_sha256": hashlib.sha256(task_text.encode()).hexdigest(),
                  "base_commit": base, "head_commit": head, "profile": profile, "scopes": list(scopes),
                  "kind": kind, "after_task": after_task, "worktree": config["worktree"],
                  "branch": config["branch"]}
    job = {"id": task_id, "status": "queued", "created_at": now(), "updated_at": now(),
           "base_commit": base, "assignment": assignment, "assignment_sha256": digest(assignment),
           "not_before_epoch": time.time() + delay, "round": 0, "github_number": github_number}
    if head:
        job["head_commit"] = head
    with lock(state / "enqueue.lock") as acquired:
        path = state / "tasks" / f"{task_id}.json"
        if not acquired or path.exists():
            raise ValueError("Task ID exists or another enqueue is running")
        save_job(config, path, job)
    return path


def verify_assignment(config, job):
    assignment = job["assignment"]
    if (digest(assignment) != job["assignment_sha256"]
            or hashlib.sha256(assignment["task_text"].encode()).hexdigest() != assignment["task_sha256"]
            or config["worktree"] != assignment["worktree"] or config["branch"] != assignment["branch"]
            or not assignment["after_task"] and job["base_commit"] != assignment["base_commit"]
            or assignment["kind"] == "review" and job["head_commit"] != assignment["head_commit"]):
        raise Blocked("Immutable assignment binding changed")
    return assignment


def ensure_reviewable(config, job):
    if (git(config, "branch", "--show-current") != config["branch"]
            or git(config, "rev-parse", "HEAD") != job["head_commit"]):
        raise RuntimeError("Branch or HEAD changed before review")
    git(config, "merge-base", "--is-ancestor", job["base_commit"], job["head_commit"])
    allowed = job["assignment"]["profile"]["coordination_files"]
    if any(name not in allowed for name in dirty_paths(config)):
        raise RuntimeError("Uncommitted files outside allowed coordination files")


def run_once(config, state=None, client=None):
    state = private_dir(state or config["state_dir"])
    private_dir(state / "tasks")
    with lock(state / "runner.lock") as acquired:
        if not acquired:
            return
        jobs = [(p, read(p)) for p in sorted((state / "tasks").glob("*.json"))]
        for path, job in jobs:
            if job["status"].startswith("running_"):
                job.update(status="interrupted", error="Previous runner stopped; reconcile child and worktree",
                           updated_at=now())
                save_job(config, path, job)
                publish_receipt(config, path, job, client)
                return
        if (state / "PAUSE").exists() or (state / "CANCEL").exists():
            return
        if any(j["status"] == "interrupted" and not j.get("reconciled") for _, j in jobs):
            return
        queued = [(p, j) for p, j in jobs if j["status"] == "queued"
                  and j.get("not_before_epoch", 0) <= time.time()]
        if not queued:
            return
        path, job = queued[0]
        try:
            assignment = verify_assignment(config, job)
            dependency = assignment["after_task"]
            if dependency:
                predecessor = read(state / "tasks" / f"{dependency}.json")
                if predecessor["status"] != "ready_for_integration":
                    job.update(waiting_for_task=dependency, updated_at=now())
                    save_job(config, path, job)
                    return
                reviewed = predecessor.get("head_commit")
                if (not reviewed or predecessor.get("reviewed_head") != reviewed
                        or git(config, "rev-parse", "HEAD") != reviewed or dirty_paths(config)):
                    raise Blocked("Dependency requires the exact reviewed clean head")
                validate_result(predecessor.get("review"), "reviewer")
                if predecessor["review"]["status"] != "ready_for_integration":
                    raise Blocked("Dependency requires the exact reviewed clean head")
                job.update(base_commit=reviewed, dependency_reviewed_head=reviewed)
                job.pop("waiting_for_task", None)
            expected = job.get("head_commit") if assignment["kind"] == "review" else job["base_commit"]
            if (git(config, "branch", "--show-current") != config["branch"]
                    or git(config, "rev-parse", "HEAD") != expected):
                raise Blocked("Branch or HEAD changed since enqueue")
            job["cli_version"] = check_cli(config)
            job["protocol"] = PROTOCOL
            managed = Path(config["state_dir"]) / "worker_thread.json"
            seed = (read(managed)["thread_id"] if managed.exists() else config.get("worker_thread_id"))
            busy = active_threads(config, seed if assignment["kind"] == "work" else None)
            if busy:
                job.update(waiting_for_threads=busy, updated_at=now())
                save_job(config, path, job)
                return
            job.pop("waiting_for_threads", None)
            for attempt in range(1, config.get("max_rounds", 2) + 1):
                job["round"] = attempt
                if assignment["kind"] == "work":
                    check_cli(config)
                    if active_threads(config):
                        raise Blocked("Worktree became busy before worker phase")
                    before_head = git(config, "rev-parse", "HEAD")
                    job["worker"] = execute(config, state, path, job, "worker")
                    validate_result(job["worker"], "worker")
                    if job["worker"]["status"] == "blocked":
                        job["status"] = "blocked"
                        break
                    job["head_commit"] = git(config, "rev-parse", "HEAD")
                    if job["head_commit"] == before_head:
                        raise RuntimeError("Worker reported delivery without a new commit")
                ensure_reviewable(config, job)
                check_cli(config)
                if active_threads(config):
                    raise Blocked("Worktree became busy before reviewer phase")
                before = fingerprint(config)
                try:
                    job["review"] = execute(config, state, path, job, "reviewer")
                finally:
                    if fingerprint(config) != before:
                        raise RuntimeError("Worktree changed during read-only review")
                validate_result(job["review"], "reviewer")
                job["reviewed_head"] = job["head_commit"]
                verdict = job["review"]["status"]
                if verdict != "needs_changes" or assignment["kind"] == "review":
                    job["status"] = "needs_primary_review" if verdict == "needs_changes" else verdict
                    break
                if attempt == config.get("max_rounds", 2):
                    job["status"] = "needs_primary_review"
            job["updated_at"] = now()
        except Blocked as exc:
            job.update(status="blocked", error=str(exc), updated_at=now())
        except Exception:
            job.update(status="failed", error="Execution or integrity check failed; inspect private logs",
                       updated_at=now())
        save_job(config, path, job)
        publish_receipt(config, path, job, client)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    add = commands.add_parser("enqueue")
    add.add_argument("--id", required=True)
    add.add_argument("--task", type=Path, required=True)
    add.add_argument("--kind", choices=["work", "review"], default="work")
    add.add_argument("--base")
    add.add_argument("--head")
    add.add_argument("--delay-seconds", type=int, default=0)
    add.add_argument("--after-task")
    add.add_argument("--scope", action="append", default=[])
    add.add_argument("--github-number", type=int)
    commands.add_parser("run-once")
    commands.add_parser("status")
    ack = commands.add_parser("acknowledge-interruption")
    ack.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        state = private_dir(config["state_dir"])
        private_dir(config["codex_home"])
        private_dir(state / "tasks")
        if args.action == "enqueue":
            enqueue(config, args.id, args.task.read_text(), kind=args.kind, base=args.base, head=args.head,
                    delay=args.delay_seconds, after_task=args.after_task, scopes=args.scope,
                    github_number=args.github_number)
            print("Assignment queued; local receipt written")
        elif args.action == "run-once":
            run_once(config, state)
        elif args.action == "acknowledge-interruption":
            if not ID.fullmatch(args.id):
                raise ValueError("Invalid task ID")
            with lock(state / "runner.lock") as acquired:
                if not acquired:
                    raise ValueError("Runner is busy")
                path = state / "tasks" / f"{args.id}.json"
                job = read(path)
                if job["status"] != "interrupted":
                    raise ValueError("Only interrupted jobs can be reconciled")
                job.update(reconciled=True, updated_at=now())
                save_job(config, path, job)
        else:
            for path in sorted((state / "receipts").glob("*.json")):
                print(json.dumps({"id": path.stem, **read(path)}))
    except Exception:
        print("FAIL: invalid configuration or local state; inspect private files")
        return 2
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise Cancelled("Coordinator interrupted; reconcile before retry")

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
