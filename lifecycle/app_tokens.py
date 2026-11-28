"""Keychain or protected Linux key -> RS256 JWT -> repository installation token.

Credentials exist only in process memory/pipes. Never print this module's return
values, HTTP bodies, subprocess output or exception details.
"""

import base64
import datetime as dt
import errno
import json
import os
import pwd
import re
import ssl
import stat
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request


class AuthenticationError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PERMISSIONS = {
    "runner": {"issues": "write"},
    "product": {
        "issues": "write",
        "contents": "read",
        "pull_requests": "read",
        "checks": "read",
        "actions": "read",
    },
}

INTEGRATION_PERMISSIONS = {
    "issues": "write",
    "contents": "write",
    "pull_requests": "write",
    "checks": "read",
    "actions": "read",
}


def validate(settings, role):
    if (
        role not in PERMISSIONS
        or not isinstance(settings, dict)
        or set(settings) - {"openssl_path"} not in (
            {"app_id", "keychain_service", "keychain_account"}, {"app_id", "key_file"})
        or type(settings["app_id"]) is not int
        or settings["app_id"] < 1
        or any(
            not isinstance(settings[k], str)
            or not settings[k]
            or len(settings[k]) > 200
            or any(ord(c) < 32 for c in settings[k])
            for k in ("keychain_service", "keychain_account") if k in settings
        )
        or any(not isinstance(settings[k], str) or not settings[k].startswith("/")
               or "\x00" in settings[k] or any(ord(c) < 32 for c in settings[k])
               for k in ("key_file", "openssl_path") if k in settings)
    ):
        raise AuthenticationError("Invalid private App authentication settings")
    if "key_file" in settings and sys.platform != "linux":
        raise AuthenticationError(
            "key_file is supported on Linux; use the Keychain on macOS")


def _key_directory(fd):
    info = os.fstat(fd)
    if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
        raise AuthenticationError("Key file ancestor must have trusted ownership and permissions")
    try:
        os.stat(".git", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise AuthenticationError("Key file must be outside git working trees")


def _key_metadata(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise AuthenticationError("Key file must be a regular file")
    if info.st_size > 16384:
        raise AuthenticationError("Key file exceeds 16 KiB")
    if info.st_uid != os.geteuid():
        raise AuthenticationError("Key file must be owned by the effective user")
    if info.st_mode & 0o7077:
        raise AuthenticationError("Key file must have owner-only permissions and no special bits")
    try:
        os.getxattr(fd, "system.posix_acl_access")
    except OSError as error:
        if error.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise AuthenticationError("Key file ACL could not be checked") from None
    else:
        raise AuthenticationError("Key file must not have a POSIX ACL")
    return info


def _key_mount(fd):
    # Bind the mount check to the opened inode, including bind mounts. WSL's
    # Windows-backed mounts are refused regardless of their chosen mount point.
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as source:
            mount_id = next(line.split()[1] for line in source if line.startswith("mnt_id:"))
        # Mount paths and sources are uninterpreted bytes, not ASCII text.
        with open("/proc/self/mountinfo", mode="rb") as source:
            filesystem = next(line.split(b" - ", 1)[1].split()[0]
                              for line in source if line.split()[0] == mount_id.encode("ascii"))
    except (OSError, ValueError, IndexError, StopIteration, UnicodeError):
        raise AuthenticationError("Key file mount could not be checked") from None
    if filesystem in {b"drvfs", b"9p"}:
        raise AuthenticationError("Key file must be on a Linux filesystem, not a Windows drive")


def file_pem(settings):
    """Walk from / without symlinks; validate and read the same nonblocking fd."""
    fd = None
    try:
        parts = settings["key_file"].split("/")[1:]
        if not parts or any(p in {"", ".", ".."} for p in parts):
            raise AuthenticationError("Invalid private App authentication settings")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open("/", flags | os.O_DIRECTORY)
        for index, part in enumerate(parts):
            _key_directory(fd)
            try:
                child = os.open(part, flags | (os.O_DIRECTORY if index < len(parts) - 1 else 0),
                                dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise AuthenticationError("Key file path must not contain symlinks") from None
                raise
            os.close(fd)
            fd = child
        before = _key_metadata(fd)
        _key_mount(fd)
        # Read exactly the observed length, then one bounded EOF probe. Short
        # reads are retried; growth, shrinkage or metadata changes fail closed.
        chunks, remaining = [], before.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise AuthenticationError("Key file changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise AuthenticationError("Key file changed while reading")
        after = _key_metadata(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AuthenticationError("Key file changed while reading")
        pem = b"".join(chunks)
        if not 128 <= len(pem) <= 16384 or b"PRIVATE " + b"KEY-----" not in pem:
            raise AuthenticationError("Key file unavailable or malformed")
        return pem
    except (OSError, UnicodeError):
        raise AuthenticationError("Key file unavailable or unreadable") from None
    finally:
        if fd is not None:
            os.close(fd)


def signing_executable(settings):
    """Resolve only through root-controlled components; never search PATH."""
    path = settings.get("openssl_path", "/usr/bin/openssl")
    try:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError()
        pending, resolved, links = path.split("/")[1:], [], 0
        root = os.lstat("/")
        if root.st_uid != 0 or root.st_mode & 0o022:
            raise ValueError()
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                resolved = resolved[:-1]
                continue
            candidate = "/" + "/".join([*resolved, part])
            info = os.lstat(candidate)
            if info.st_uid != 0:
                raise ValueError()
            if stat.S_ISLNK(info.st_mode):
                # Symlink mode bits are not access controls on Linux/macOS.
                links += 1
                if links > 40:
                    raise ValueError()
                target = os.readlink(candidate)
                if target.startswith("/"):
                    resolved = []
                pending = target.split("/") + pending
                continue
            if info.st_mode & 0o022 or (pending and not stat.S_ISDIR(info.st_mode)):
                raise ValueError()
            resolved.append(part)
        result = "/" + "/".join(resolved)
        info = os.stat(result, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or not os.access(result, os.X_OK)):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise AuthenticationError("App JWT signing executable unavailable or unsafe") from None


def keychain_pem(settings):
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                settings["keychain_service"],
                "-a",
                settings["keychain_account"],
                "-w",
            ],
            capture_output=True,
            timeout=30,
            check=True,
            env={"PATH": "/usr/bin:/bin", "HOME": pwd.getpwuid(os.getuid()).pw_dir},
        )
        pem = base64.b64decode(result.stdout.strip(), validate=True)
        if not 128 <= len(pem) <= 16384 or b"PRIVATE " + b"KEY-----" not in pem:
            raise ValueError()
        return pem
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        raise AuthenticationError(
            "Keychain credential unavailable or malformed"
        ) from None


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def jwt(settings, pem, at):
    executable = signing_executable(settings)
    message = b".".join(
        (
            b64(b'{"alg":"RS256","typ":"JWT"}'),
            b64(
                json.dumps(
                    dict(
                        iat=int(at) - 60, exp=int(at) + 540, iss=str(settings["app_id"])
                    ),
                    separators=(",", ":"),
                ).encode()
            ),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        # Only the small, non-secret JWT header/claims enter this pipe. PEM uses
        # stdin, and never appears in argv, environment, files or stderr logs.
        os.write(write_fd, message)
        os.close(write_fd)
        write_fd = None
        result = subprocess.run(
            [
                executable,
                "dgst",
                "-sha256",
                "-sign",
                "/dev/stdin",
                "-passin",
                "pass:",
                f"/dev/fd/{read_fd}",
            ],
            input=pem,
            capture_output=True,
            pass_fds=(read_fd,),
            timeout=30,
            check=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        if not result.stdout:
            raise ValueError()
        return (message + b"." + b64(result.stdout)).decode("ascii")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AuthenticationError("App JWT signing failed") from None
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def request(opener, credential, method, path, data=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=None if data is None else json.dumps(data).encode(),
        headers={
            "Authorization": "Bearer " + credential,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener.open(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        stage = (
            "App metadata"
            if path == "/app"
            else "repository installation"
            if path.endswith("/installation")
            else "token issuance"
            if path.endswith("/access_tokens")
            else "token scope"
        )
        if (error.code == 422 and path.endswith("/access_tokens")
                and data and data.get("permissions") == {"statuses": "write"}):
            raise AuthenticationError(
                "App installation lacks Commit statuses permission on the framework repository (HTTP 422)"
            ) from None
        raise AuthenticationError(
            f"App authentication {stage} failed (HTTP {error.code})"
        ) from None
    except urllib.error.URLError as error:
        reason = (
            "TLS verification"
            if isinstance(error.reason, ssl.SSLCertVerificationError)
            else "transport"
        )
        raise AuthenticationError("App authentication " + reason + " failed") from None
    except (OSError, ValueError):
        raise AuthenticationError(
            "App authentication transport/response failed"
        ) from None


def mint(cfg, role, *, comments_only=False, integration_enabled=False,
         fetch_enabled=False, status_only=False, at=None, opener=None):
    """Return a token and expiry; never a printable credential receipt/cache."""
    if type(integration_enabled) is not bool or (integration_enabled and role != "runner"):
        raise AuthenticationError("Invalid integration authentication opt-in")
    if type(fetch_enabled) is not bool or (fetch_enabled and role != "runner"):
        raise AuthenticationError("Invalid fetch authentication opt-in")
    if (type(status_only) is not bool or (status_only and role != "runner")
            or (status_only and (comments_only or integration_enabled or fetch_enabled))):
        raise AuthenticationError("Invalid status authentication opt-in")
    at = time.time() if at is None else at
    settings = cfg["app_auth"][role]
    validate(settings, role)
    repository = cfg["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AuthenticationError("Invalid target repository")
    opener = opener or urllib.request.build_opener(NoRedirect)
    credential = jwt(settings, file_pem(settings) if "key_file" in settings
                     else keychain_pem(settings), at)
    try:
        app = request(opener, credential, "GET", "/app")
        actor = app["slug"] + "[bot]"
        if app["id"] != settings["app_id"] or actor != cfg["actors"][role]:
            # Logins are public policy; report the actual identity, never keys.
            raise AuthenticationError("App identity mismatch: " + actor)
        installation = request(
            opener, credential, "GET", f"/repos/{repository}/installation"
        )
        if (
            type(installation["id"]) is not int
            or installation["id"] < 1
            or installation["app_id"] != settings["app_id"]
            or installation.get("suspended_at") is not None
        ):
            raise AuthenticationError("Wrong or suspended repository installation")
        permissions = (
            {"statuses": "write"} if status_only else
            {"issues": "write"} if comments_only else
            dict(INTEGRATION_PERMISSIONS) if integration_enabled else
            {"issues": "write", "contents": "read"} if fetch_enabled else
            dict(PERMISSIONS[role])
        )
        value = request(
            opener,
            credential,
            "POST",
            f"/app/installations/{installation['id']}/access_tokens",
            {"repositories": [repository.split("/")[1]], "permissions": permissions},
        )
        repos = value["repositories"]
        actual = {k: v for k, v in value["permissions"].items() if v != "none"}
        if (
            len(repos) != 1
            or repos[0]["full_name"] != repository
            or actual != {**permissions, "metadata": "read"}
        ):
            raise AuthenticationError("Installation token scope mismatch")
        expiry = dt.datetime.fromisoformat(
            value["expires_at"].replace("Z", "+00:00")
        ).timestamp()
        if not at + 60 < expiry <= at + 3660:
            raise AuthenticationError("Invalid installation token expiry")
        token = value["token"]
        if not isinstance(token, str) or not token or any(ord(c) < 33 for c in token):
            raise AuthenticationError("Invalid installation token")
        # Independently verify the token's accessible repository list. No paging
        # ambiguity is allowed: a token for exactly one repository needs one row.
        accessible = request(
            opener, token, "GET", "/installation/repositories?per_page=100"
        )
        if (
            accessible["total_count"] != 1
            or len(accessible["repositories"]) != 1
            or accessible["repositories"][0]["full_name"] != repository
        ):
            raise AuthenticationError(
                "Installation token repository verification failed"
            )
        return token, expiry
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AuthenticationError("Malformed App authentication response") from None


# Only the canonical module owns process memory, including when callers load a
# fresh copy of a runner or this file. Never serialize these values.
_cache = {}
_cache_lock = threading.RLock()


def permissions(role, *, comments_only=False, integration_enabled=False,
                fetch_enabled=False, status_only=False):
    return ({"statuses": "write"} if status_only else
            {"issues": "write"} if comments_only else
            dict(INTEGRATION_PERMISSIONS) if integration_enabled else
            {"issues": "write", "contents": "read"} if fetch_enabled else
            dict(PERMISSIONS[role]))


def get(cfg, role, **options):
    """Reuse a verified token until five minutes before expiry, under one lock."""
    from lifecycle import app_tokens
    if app_tokens.get is not get:
        return app_tokens.get(cfg, role, **options)
    settings = cfg["app_auth"][role]
    validate(settings, role)
    for name in ("comments_only", "integration_enabled", "fetch_enabled", "status_only"):
        if type(options.get(name, False)) is not bool:
            raise AuthenticationError("Invalid authentication opt-in")
    if role != "runner" and any(options.get(n) for n in
                                ("integration_enabled", "fetch_enabled", "status_only")):
        raise AuthenticationError("Invalid authentication opt-in")
    if options.get("status_only") and any(options.get(n) for n in
                                         ("comments_only", "integration_enabled", "fetch_enabled")):
        raise AuthenticationError("Invalid status authentication opt-in")
    scope = permissions(role, **{k: options[k] for k in
                        ("comments_only", "integration_enabled", "fetch_enabled", "status_only")
                        if k in options})
    identity = (cfg["repository"], settings["app_id"], role,
                cfg["actors"][role], tuple(sorted(scope.items())))
    with _cache_lock:
        at = options.get("at")
        at = time.time() if at is None else at
        value = _cache.get(identity)
        if value is None or at >= value[1] - 300:
            value = mint(cfg, role, **options)
            _cache[identity] = value
        return value


def invalidate(token):
    """Drop only the rejected credential; don't evict a newer concurrent mint."""
    from lifecycle import app_tokens
    if app_tokens.invalidate is not invalidate:
        return app_tokens.invalidate(token)
    with _cache_lock:
        for identity, value in list(_cache.items()):
            if value[0] == token:
                del _cache[identity]


def clear_cache():
    from lifecycle import app_tokens
    with app_tokens._cache_lock:
        app_tokens._cache.clear()
