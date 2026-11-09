"""Explicit, bounded local agent delegation. No automatic integration or replay."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import string
import subprocess
import tempfile
import time

PROTOCOL = "agent-file-v1"
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
    """A prerequisite or safety failure requiring operator attention."""


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
    return trusted_git(config, *args).decode().rstrip("\n")


def environment(config):
    # Allowlist instead of guessing every provider's API-key name. HOME retains
    # its original meaning; agent-specific home selection is explicit config.
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update({key: expand(value, config) for key, value in config["agent"].get("environment", {}).items()})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
    if (not isinstance(profile, dict) or not required <= set(profile)
            or set(profile) - required - {"commit_policy"}):
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
    commit_policy(profile)
    return profile


def load_config(path):
    config = read(path)
    for key in ("worktree", "state_dir", "agent_home", "profile",
                "branch", "worker_model", "reviewer_model"):
        if not isinstance(config.get(key), str) or not config[key].strip() or "<" in config[key]:
            raise ValueError("Config requires populated paths, branch and agent models")
    for key in ("worktree", "state_dir", "agent_home", "profile"):
        if not Path(config[key]).is_absolute():
            raise ValueError("Config paths must be absolute")
    worktree = Path(config["worktree"]).resolve()
    for key in ("state_dir", "agent_home"):
        location = Path(config[key]).resolve()
        if location == worktree or worktree in location.parents:
            raise ValueError("Private state and agent home must be outside the worktree")
    if Path(config["state_dir"]).resolve() == Path(config["agent_home"]).resolve():
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
    if "commit_identity" in config:
        commit_identity(config)
    config["project_profile"] = load_profile(config["profile"])
    validate_agent(config)
    return config


# ---------- trusted runner-owned commits ----------

DEFAULT_COMMIT_POLICY = {
    "deny_patterns": [".env*", "*secret*", "*credential*", "id_rsa*", "id_dsa*", "id_ecdsa*",
                      "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx"],
    "max_files": 100, "max_file_bytes": 1024 * 1024, "max_total_bytes": 10 * 1024 * 1024,
}


class CommitRefused(Blocked):
    def __init__(self, reason, paths=()):
        super().__init__("Runner commit refused: " + reason)
        self.reason = reason
        self.paths = sorted(set(paths))


def commit_policy(profile):
    supplied = profile.get("commit_policy", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_COMMIT_POLICY):
        raise ValueError("Invalid commit_policy fields")
    policy = {**DEFAULT_COMMIT_POLICY, **supplied}
    patterns = policy["deny_patterns"]
    if (not isinstance(patterns, list)
            or any(not isinstance(p, str) or not p.strip() for p in patterns)):
        raise ValueError("Commit deny patterns must be nonempty strings")
    policy["deny_patterns"] = sorted(set(DEFAULT_COMMIT_POLICY["deny_patterns"] + patterns))
    for key in ("max_files", "max_file_bytes", "max_total_bytes"):
        if type(policy[key]) is not int or policy[key] < 1:
            raise ValueError("Commit limits must be positive integers")
    return policy


def commit_identity(config):
    identity = config.get("commit_identity")
    if (not isinstance(identity, dict) or set(identity) != {"name", "email"}
            or any(not isinstance(v, str) or not v.strip() or "<" in v or ">" in v
                   or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in identity.values())
            or not re.fullmatch(r"[^\s@]+@[^\s@]+", identity["email"])):
        raise CommitRefused("configure_runner_identity")
    return identity


def trusted_git(config, *args, data=None, extra_env=None, allowed=(0,)):
    # No hooks, signing, fsmonitor, global configuration or attribute filters are
    # run by this path. Bytes are hashed directly, not passed to `git add`.
    env = environment(config)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(extra_env or {})
    # Resolve only the effective ignore-file path as data. Do not inherit global
    # executable configuration into operational Git commands.
    read_env = environment(config)
    if "XDG_CONFIG_HOME" in os.environ:
        read_env.setdefault("XDG_CONFIG_HOME", os.environ["XDG_CONFIG_HOME"])
    ignored_config = subprocess.run(
        ["git", "-C", config["worktree"], "config", "--includes", "--path", "--get", "core.excludesFile"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=read_env)
    if ignored_config.returncode not in (0, 1):
        raise CommitRefused("git_configuration_unavailable")
    default_ignore = Path(read_env.get("XDG_CONFIG_HOME", str(Path(read_env.get("HOME", str(Path.home()))) / ".config"))) / "git/ignore"
    ignore_path = (ignored_config.stdout.decode().rstrip("\n") if ignored_config.returncode == 0
                   else str(default_ignore))
    configured = subprocess.run(
        ["git", "-C", config["worktree"], "config", "--local", "--includes", "--null", "--name-only", "--get-regexp",
         r"^filter\..*\.(clean|smudge|process|required)$"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    if configured.returncode not in (0, 1):
        raise CommitRefused("git_configuration_unavailable")
    keys = [entry.decode() for entry in configured.stdout.split(b"\0") if entry]
    if any(any(ord(c) < 32 or ord(c) == 127 for c in key) for key in keys):
        raise CommitRefused("git_configuration_unavailable")
    filters = {key.rsplit(".", 1)[0] for key in keys}
    overrides = []
    for section in sorted(filters):
        for variable in ("clean", "smudge", "process", "required"):
            overrides += ["-c", section + "." + variable + "=" + ("false" if variable == "required" else "")]
    literal = [] if args[0] == "check-ignore" else ["--literal-pathspecs"]
    safe_args = list(args)
    if args[0] in {"diff", "diff-tree", "show", "log"}:
        safe_args[1:1] = ["--no-ext-diff", "--no-textconv"]
    result = subprocess.run(["git", *literal, "-C", config["worktree"],
                             "-c", "core.hooksPath=" + os.devnull,
                             "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
                             "-c", "core.excludesFile=" + ignore_path, *overrides, *safe_args],
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    if result.returncode not in allowed:
        raise CommitRefused("git_operation_failed")
    return result.stdout


def checked_commit_path(name, root):
    try:
        path_name(name)
    except ValueError as exc:
        raise CommitRefused("outside_or_invalid_path", [name]) from exc
    if any(part.casefold().rstrip(" .") == ".git" for part in name.split("/")):
        raise CommitRefused("git_metadata", [name])
    path = root / name
    # Reject parent symlinks, even internal ones, to keep all file opens no-follow.
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise CommitRefused("symlink_parent", [name])
    return path


def denied(name, policy):
    return any(fnmatch.fnmatchcase(value.casefold(), pattern.casefold())
               for value in [name, *name.split("/")] for pattern in policy["deny_patterns"])


def stable_stat(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot_entry(config, root, name, previous, policy):
    path = checked_commit_path(name, root)
    if denied(name, policy):
        raise CommitRefused("denied_path", [name])
    if previous and previous[0] == "160000":
        raise CommitRefused("submodule_not_supported", [name])
    try:
        info = path.lstat()
    except FileNotFoundError:
        if previous is None:
            raise CommitRefused("concurrent_change", [name])
        return ("0", None)
    if stat.S_ISDIR(info.st_mode):
        raise CommitRefused("submodule_or_directory_not_supported", [name])
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        try:
            resolved = path.resolve().relative_to(root).as_posix()
        except (ValueError, RuntimeError, OSError) as exc:
            raise CommitRefused("external_or_invalid_symlink", [name]) from exc
        checked_commit_path(resolved, root)
        if denied(resolved, policy):
            raise CommitRefused("denied_symlink_target", [name])
        if stable_stat(path.lstat()) != stable_stat(info):
            raise CommitRefused("concurrent_change", [name])
        content = os.fsencode(target)
        mode = "120000"
    elif stat.S_ISREG(info.st_mode):
        # Walk parent directories using dir_fd + O_NOFOLLOW too: a concurrent
        # replacement of a directory cannot redirect the read outside the root.
        descriptors = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            for component in name.split("/")[:-1]:
                descriptors.append(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=descriptors[-1]))
            fd = os.open(name.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptors[-1])
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino)
                        != (info.st_dev, info.st_ino)):
                    raise CommitRefused("concurrent_change", [name])
                content = handle.read(policy["max_file_bytes"] + 1)
                if stable_stat(os.fstat(handle.fileno())) != stable_stat(before):
                    raise CommitRefused("concurrent_change", [name])
        except OSError as exc:
            raise CommitRefused("unreadable_or_changed_path", [name]) from exc
        finally:
            for directory in reversed(descriptors):
                os.close(directory)
        mode = "100755" if info.st_mode & 0o111 else "100644"
    else:
        raise CommitRefused("special_file_not_supported", [name])
    if len(content) > policy["max_file_bytes"]:
        raise CommitRefused("file_size_limit", [name])
    return mode, content


@contextlib.contextmanager
def commit_locks(config):
    git_dir = Path(trusted_git(config, "rev-parse", "--absolute-git-dir").decode().strip())
    owned = []
    try:
        for name in ("index.lock",):
            path = git_dir / name
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError as exc:
                raise CommitRefused("git_lock_busy") from exc
            owned.append((path, fd, os.fstat(fd).st_ino))
        yield git_dir, owned[-1][0], owned[-1][1]
    finally:
        for path, fd, inode in reversed(owned):
            os.close(fd)
            if path.exists() and path.lstat().st_ino == inode:
                path.unlink()


def commit_preflight(config, job):
    commit_identity(config)
    staged = list(filter(None, trusted_git(config, "diff", "--cached", "--name-only", "-z").decode().split("\0")))
    if staged:
        raise CommitRefused("preexisting_staged_changes", staged)
    allowed = set(job["assignment"]["profile"]["coordination_files"])
    dirty = [name for name in dirty_paths(config) if name not in allowed]
    if dirty:
        raise CommitRefused("preexisting_worktree_changes", dirty)


def runner_commit(config, job_path, job, expected_head):
    identity = commit_identity(config)
    profile = job["assignment"]["profile"]
    policy = commit_policy(profile)
    root = Path(config["worktree"]).resolve()
    if Path(trusted_git(config, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise CommitRefused("worktree_must_be_repository_root")
    with commit_locks(config) as (git_dir, index_lock, index_fd):
        if (git(config, "branch", "--show-current") != config["branch"]
                or git(config, "rev-parse", "HEAD") != expected_head):
            raise CommitRefused("history_changed")
        staged = trusted_git(config, "diff", "--cached", "--name-only", "-z", expected_head).decode().split("\0")
        if any(staged):
            raise CommitRefused("preexisting_staged_changes", filter(None, staged))
        old = {}
        for row in trusted_git(config, "ls-tree", "-r", "-z", expected_head).decode().split("\0"):
            if row:
                meta, name = row.split("\t", 1)
                mode, _, oid = meta.split()
                old[name] = (mode, oid)
        paths = set(filter(None, trusted_git(config, "diff", "--no-renames", "--name-only", "-z", expected_head).decode().split("\0")))
        paths.update(filter(None, trusted_git(config, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")))
        for name in paths:
            checked_commit_path(name, root)
        ignored = set()
        if paths:
            ignored = {name.removeprefix("./") for name in trusted_git(
                config, "check-ignore", "--no-index", "-z", "--stdin",
                data=b"".join(os.fsencode("./" + p) + b"\0" for p in sorted(paths)),
                allowed=(0, 1)).decode().split("\0") if name}
        selected = sorted(paths - ignored - set(profile["coordination_files"]))
        if not selected:
            raise CommitRefused("no_committable_changes")
        if len(selected) > policy["max_files"]:
            raise CommitRefused("file_count_limit", selected)
        snapshots = {name: snapshot_entry(config, root, name, old.get(name), policy) for name in selected}
        if sum(len(data) for _, data in snapshots.values() if data is not None) > policy["max_total_bytes"]:
            raise CommitRefused("total_size_limit", selected)
        fd, index_path = tempfile.mkstemp(prefix="commit-index-", dir=Path(config["state_dir"]))
        os.close(fd)
        os.unlink(index_path)
        index_env = {"GIT_INDEX_FILE": index_path}
        try:
            trusted_git(config, "read-tree", expected_head, extra_env=index_env)
            for name, (mode, data) in snapshots.items():
                oid = (trusted_git(config, "hash-object", "-w", "--no-filters", "--stdin", data=data).decode().strip()
                       if data is not None else "0" * 40)
                trusted_git(config, "update-index", "-z", "--index-info", extra_env=index_env,
                            data=f"{mode} {oid}\t{name}\0".encode())
            tree = trusted_git(config, "write-tree", extra_env=index_env).decode().strip()
            if tree == trusted_git(config, "rev-parse", expected_head + "^{tree}").decode().strip():
                raise CommitRefused("no_committable_changes", selected)
            message = f"runner: job {job['id']} kind {job['assignment']['kind']} round {job['round']}\n"
            author_env = {"GIT_AUTHOR_NAME": identity["name"], "GIT_AUTHOR_EMAIL": identity["email"],
                          "GIT_COMMITTER_NAME": identity["name"], "GIT_COMMITTER_EMAIL": identity["email"]}
            sha = trusted_git(config, "commit-tree", tree, "-p", expected_head,
                              data=message.encode(), extra_env=author_env).decode().strip()
            if snapshots != {name: snapshot_entry(config, root, name, old.get(name), policy) for name in selected}:
                raise CommitRefused("concurrent_change", selected)
            if (git(config, "branch", "--show-current") != config["branch"]
                    or git(config, "rev-parse", "HEAD") != expected_head):
                raise CommitRefused("history_changed", selected)
            # Persist an intent before publication. A crash leaves an explicit
            # prepared/uncertain record for reconciliation, never a silent replay.
            record = {"commit_sha": sha, "job_id": job["id"], "job_kind": job["assignment"]["kind"],
                      "round": job["round"], "file_count": len(selected), "committed_by": "runner",
                      "timestamp": now(), "state": "prepared"}
            job.setdefault("runner_commits", []).append(record)
            save_job(config, job_path, job)
            with open(index_path, "rb") as handle, os.fdopen(os.dup(index_fd), "wb") as target:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            try:
                # Address the configured branch explicitly; never dereference a
                # potentially moved HEAD. The old SHA is a compare-and-swap lease.
                trusted_git(config, "update-ref", "--no-deref",
                            "refs/heads/" + config["branch"], sha, expected_head)
            except CommitRefused as exc:
                record["state"] = "not_published"
                raise CommitRefused("history_changed", selected) from exc
            job["head_commit"] = sha
            record["state"] = "committed"
            save_job(config, job_path, job)
            os.replace(index_lock, git_dir / "index")
            return sha
        finally:
            for name in (index_path, index_path + ".lock"):
                if os.path.exists(name):
                    os.unlink(name)


def record_refusal(config, job_path, job, refusal):
    job.setdefault("commit_refusals", []).append({
        "job_id": job["id"], "job_kind": job["assignment"]["kind"], "round": job["round"],
        "reason": refusal.reason, "paths": refusal.paths, "timestamp": now(),
    })
    job.update(status="blocked", error=str(refusal), updated_at=now())
    save_job(config, job_path, job)


def public_receipt(receipt):
    public = {key: receipt[key] for key in ("status", "base_commit", "head_commit", "summary", "timestamp")}
    commits = [{"commit_sha": c["commit_sha"], "committed_by": "runner"}
               for c in receipt.get("runner_commits", []) if c["state"] == "committed"]
    if commits:
        public["runner_commits"] = commits
    return public


# Trusted operator templates are always argv data, never shell commands.
CONTEXT_FIELDS = {"worktree", "agent_home"}
PHASE_FIELDS = CONTEXT_FIELDS | {"model", "prompt_file", "output_file", "schema_file"}


def template_fields(value):
    if not isinstance(value, str) or not value or "<" in value or "\x00" in value:
        raise ValueError("Agent templates must be populated nonempty strings")
    fields = set()
    for _, field, spec, conversion in string.Formatter().parse(value):
        if field is not None:
            if spec or conversion or field not in PHASE_FIELDS | {"role_flags"}:
                raise ValueError("Unknown or formatted agent placeholder")
            fields.add(field)
    return fields


def validate_agent(config):
    if (config.get("worker_start", "new") != "new" or "worker_thread_id" in config
            or "codex_home" in config or "codex_bin" in config):
        raise Blocked("Fresh sessions only; migrate legacy agent configuration; resume/fork deferred")
    agent = config.get("agent")
    required = {"command", "version_command", "version_pattern"}
    if (not isinstance(agent, dict) or not required <= set(agent)
            or set(agent) - required - {"worker_flags", "reviewer_flags", "environment"}):
        raise ValueError("Agent requires command, version_command and version_pattern")
    for key in ("command", "version_command", "worker_flags", "reviewer_flags"):
        args = agent.get(key, [])
        if not isinstance(args, list) or (key in required and not args):
            raise ValueError("Agent commands and flags must be argument arrays")
        allowed = CONTEXT_FIELDS if key == "version_command" else PHASE_FIELDS
        for arg in args:
            fields = template_fields(arg)
            if key == "command" and arg == "{role_flags}":
                continue
            if fields - allowed:
                raise ValueError("Placeholder is not available in this agent template")
    if ((agent.get("worker_flags") or agent.get("reviewer_flags"))
            and agent["command"].count("{role_flags}") != 1):
        raise ValueError("Role flags require exactly one standalone {role_flags} argument")
    for key in ("command", "version_command"):
        if not Path(expand(agent[key][0], config)).is_absolute():
            raise ValueError("Agent executable paths must be absolute")
    pattern = agent["version_pattern"]
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("Accepted version pattern must be populated")
    re.compile(pattern)
    env = agent.get("environment", {})
    if not isinstance(env, dict):
        raise ValueError("Agent environment must be an explicit mapping")
    for key, value in env.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
                or key in {"HOME", "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
                or key.startswith(("GIT_", "LD_", "DYLD_"))
                or template_fields(value) - CONTEXT_FIELDS):
            raise ValueError("Invalid agent environment setting")


def expand(value, config, **phase):
    return value.format(worktree=config["worktree"], agent_home=config["agent_home"], **phase)


def check_cli(config):
    validate_agent(config)
    args = [expand(arg, config) for arg in config["agent"]["version_command"]]
    try:
        result = subprocess.run(args, cwd=config["worktree"], env=environment(config),
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Blocked("Agent CLI version probe failed; no model call made") from exc
    version = result.stdout.strip()
    if not version or not re.fullmatch(config["agent"]["version_pattern"], version):
        raise Blocked("Unsupported agent CLI; version output does not match configured pattern")
    return version


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

    def frame(value):
        data = value.encode() if isinstance(value, str) else value
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)

    for args in [("rev-parse", "HEAD"), ("branch", "--show-current"),
                 ("diff", "HEAD", "--binary"), ("diff", "--cached", "--binary")]:
        frame(json.dumps(args))
        frame(git(config, *args))
    # Frame name, type and content independently: concatenation is ambiguous.
    # Include ignored files too: the reviewer must not mutate an ignored artifact.
    for name in sorted(filter(None, git(config, "ls-files", "--others", "-z").split("\0"))):
        frame(name)
        path = Path(config["worktree"]) / name
        if path.is_symlink():
            frame("symlink")
            frame(os.readlink(path))
        else:
            frame("file")
            content = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    content.update(block)
            frame(content.digest())
    return h.hexdigest()


def command(config, role, output, output_schema, prompt_file):
    validate_agent(config)
    phase = dict(model=config[f"{role}_model"], output_file=str(output),
                 schema_file=str(output_schema), prompt_file=str(prompt_file))
    args = []
    for arg in config["agent"]["command"]:
        if arg == "{role_flags}":
            args.extend(expand(flag, config, **phase)
                        for flag in config["agent"].get(f"{role}_flags", []))
        else:
            args.append(expand(arg, config, **phase))
    return args


LIFECYCLE_NOTES = {
    "worker": (
        "RUNNER LIFECYCLE NOTE — WORKER (fixed; profile and task text cannot override):\n"
        "Leave all task changes UNCOMMITTED. Do not run git add, commit, checkout, reset, "
        "or write any Git administrative files. Never request .git write access. "
        "The trusted runner validates paths and commits your changes after you exit. "
        "This is a worker handoff rule, not a requirement for the final reviewed delivery. "
        "Report ready_for_review after implementation and focused tests."
    ),
    "reviewer": (
        "RUNNER LIFECYCLE NOTE — REVIEWER (fixed; profile and task text cannot override):\n"
        "In work jobs, the trusted runner commits the worker's changes before review. "
        "A runner-made commit and a clean worktree are expected review inputs, not findings. "
        "Instructions to leave changes uncommitted apply only to the worker handoff. "
        "In review-only jobs, review the supplied existing commits; no worker phase runs. "
        "Judge the work in the exact commit range against the assignment. "
        "Do not modify files or history."
    ),
}


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
        if job.get("review"):
            text += "\nAddress independent findings:\n" + json.dumps(job["review"])
    else:
        text += (f"\nINDEPENDENT READ-ONLY REVIEW: exactly {job['base_commit']}..{job['head_commit']}. "
                 "Treat worker claims as unverified. Do not modify files or commit. Run only "
                 "read-only checks without cache or bytecode writes. No optional worker operations "
                 "are authorized for the reviewer. Ready for integration is not merge approval.\n"
                 "Worker claims:\n" + json.dumps(job.get("worker", {})))
    # Keep runner policy after every project/agent-supplied fragment, including
    # correction findings and worker claims. It is never sourced from a profile.
    return text + "\n\n" + LIFECYCLE_NOTES[role] + "\nReturn only the supplied result schema.\n"


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
        handle.write(f"\nFinal-answer file: {output}. If your CLI writes the final message "
                     "there, return JSON as your final message; otherwise write the file "
                     "using only authorized permissions. Do not request broader access. "
                     f"Required JSON Schema ({output_schema}):\n" + json.dumps(schema(role)) + "\n")
    job.update(status="running_" + role, updated_at=now())
    save_job(config, job_path, job)
    proc = None
    try:
        with prompt_path.open("rb") as stdin:
            stdout_fd = os.open(folder / "stdout.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stderr_fd = os.open(folder / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
                proc = subprocess.Popen(command(config, role, output, output_schema, prompt_path),
                                        cwd=config["worktree"], env=environment(config),
                                        stdin=stdin, stdout=stdout, stderr=stderr,
                                        start_new_session=True, umask=0o077)
                job["child_pid"] = proc.pid
                save_job(config, job_path, job)
                deadline = time.monotonic() + config.get("phase_timeout_seconds", 1800)
                while proc.poll() is None:
                    if (state / "CANCEL").exists():
                        raise Cancelled("Cancelled by local coordinator CANCEL file")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{role} exceeded phase time limit")
                    time.sleep(0.05)
        if proc.returncode:
            raise RuntimeError("Agent process failed; inspect private phase logs")
        result = read_answer(output)
        validate_result(result, role)
        return result
    finally:
        if proc is not None:
            stop_group(proc)
        job.pop("child_pid", None)


def read_answer(path):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate final-answer field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-JSON final-answer constant")

    return json.loads(Path(path).read_text(), object_pairs_hook=unique_fields,
                      parse_constant=invalid_constant)


class GitHubClient:
    def post(self, repository, number, receipt):
        # Only fixed runner summaries are eligible, never agent-produced summaries.
        body = (f"Task receipt: {receipt['status']}\nCommits: {receipt['base_commit']}.."
                f"{receipt['head_commit']}\nSummary: {receipt['summary']}\nRun: {receipt['timestamp']}")
        for commit in receipt.get("runner_commits", []):
            body += f"\nRunner commit: {commit['commit_sha']} (committed_by: runner)"
        subprocess.run(["gh", "api", "--method", "POST", f"repos/{repository}/issues/{number}/comments",
                        "--input", "-"], input=json.dumps({"body": body}), text=True,
                       capture_output=True, check=True, timeout=30)


def save_job(config, path, job):
    job["schema_version"] = 2
    save(path, job)
    receipt = {"status": job["status"], "base_commit": job["base_commit"],
               "head_commit": job.get("head_commit"), "summary": SUMMARIES[job["status"]],
               "timestamp": job["updated_at"], "schema_version": 2,
               "runner_commits": job.get("runner_commits", []),
               "commit_refusals": job.get("commit_refusals", [])}
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
                                       public_receipt(read(Path(config["state_dir"]) / "receipts" / f"{job['id']}.json")))
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


def check_dispatch_head(config, expected):
    if (git(config, "branch", "--show-current") != config["branch"]
            or git(config, "rev-parse", "HEAD") != expected):
        raise Blocked("Branch or HEAD changed before phase dispatch")


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
            validate_agent(config)
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
            for attempt in range(1, config.get("max_rounds", 2) + 1):
                job["round"] = attempt
                if assignment["kind"] == "work":
                    before_head = job["base_commit"] if attempt == 1 else job["head_commit"]
                    check_cli(config)
                    check_dispatch_head(config, before_head)
                    commit_preflight(config, job)
                    job["worker"] = execute(config, state, path, job, "worker")
                    validate_result(job["worker"], "worker")
                    if (git(config, "branch", "--show-current") != config["branch"]
                            or git(config, "rev-parse", "HEAD") != before_head):
                        raise CommitRefused("history_changed")
                    if job["worker"]["status"] == "blocked":
                        job["status"] = "blocked"
                        break
                    runner_commit(config, path, job, before_head)
                ensure_reviewable(config, job)
                check_cli(config)
                check_dispatch_head(config, job["head_commit"])
                ensure_reviewable(config, job)
                before = fingerprint(config)
                check_dispatch_head(config, job["head_commit"])
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
        except CommitRefused as exc:
            record_refusal(config, path, job, exc)
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
        private_dir(config["agent_home"])
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
