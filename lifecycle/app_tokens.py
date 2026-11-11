"""macOS Keychain -> RS256 App JWT -> one-repository installation token.

Credentials exist only in process memory/pipes. Never print this module's return
values, HTTP bodies, subprocess output or exception details.
"""

import base64
import datetime as dt
import json
import os
import re
import ssl
import subprocess
import time
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
        or set(settings) != {"app_id", "keychain_service", "keychain_account"}
        or type(settings["app_id"]) is not int
        or settings["app_id"] < 1
        or any(
            not isinstance(settings[k], str)
            or not settings[k]
            or len(settings[k]) > 200
            or any(ord(c) < 32 for c in settings[k])
            for k in ("keychain_service", "keychain_account")
        )
    ):
        raise AuthenticationError("Invalid private App authentication settings")


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
            env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")},
        )
        pem = base64.b64decode(result.stdout.strip(), validate=True)
        if not 128 <= len(pem) <= 16384 or b"PRIVATE " + b"KEY-----" not in pem:
            raise ValueError()
        return pem
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AuthenticationError(
            "Keychain credential unavailable or malformed"
        ) from None


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def jwt(settings, pem, at):
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
                "/usr/bin/openssl",
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


def mint(cfg, role, *, comments_only=False, integration_enabled=False, at=None, opener=None):
    """Return a token and expiry; never a printable credential receipt/cache."""
    if type(integration_enabled) is not bool or (integration_enabled and role != "runner"):
        raise AuthenticationError("Invalid integration authentication opt-in")
    at = time.time() if at is None else at
    settings = cfg["app_auth"][role]
    validate(settings, role)
    repository = cfg["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AuthenticationError("Invalid target repository")
    opener = opener or urllib.request.build_opener(NoRedirect)
    credential = jwt(settings, keychain_pem(settings), at)
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
            {"issues": "write"} if comments_only else
            dict(INTEGRATION_PERMISSIONS) if integration_enabled else dict(PERMISSIONS[role])
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
