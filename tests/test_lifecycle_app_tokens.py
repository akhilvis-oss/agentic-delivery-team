"""Credential plumbing uses in-memory synthetic keys and fake GitHub only."""

import base64
import datetime as dt
import io
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from lifecycle import app_tokens as auth, core, preflight
from test_lifecycle import policy, Hub

AT = 1800000000


@pytest.mark.parametrize("role,comments_only,expected", [
    ("runner", False, {"issues": "write"}),
    ("product", False, {"issues": "write", "contents": "read", "pull_requests": "read",
                         "checks": "read", "actions": "read"}),
    ("runner", True, {"issues": "write"}),
    ("product", True, {"issues": "write"}),
])
def test_existing_role_scopes_remain_exact(tmp_path, monkeypatch, role, comments_only, expected):
    cfg = configured(tmp_path)
    api = API(cfg, role)
    api.responses[2]["permissions"] = {**expected, "metadata": "read"}
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"private-fixture")
    monkeypatch.setattr(auth, "jwt", lambda *a: "jwt-fixture")
    auth.mint(cfg, role, comments_only=comments_only, at=AT, opener=api)
    assert json.loads(api.calls[2].data)["permissions"] == expected


@pytest.mark.parametrize("role,enabled", [("runner", 1), ("runner", "true"),
                                         ("runner", {}), ("product", True)])
def test_mint_rejects_invalid_integration_opt_in(tmp_path, monkeypatch, role, enabled):
    cfg = configured(tmp_path)
    monkeypatch.setattr(auth, "keychain_pem", lambda s: pytest.fail("must reject before Keychain"))
    with pytest.raises(auth.AuthenticationError, match="integration"):
        auth.mint(cfg, role, integration_enabled=enabled)


@pytest.mark.parametrize("fetch_enabled", [False, True])
def test_comments_only_still_limits_opted_in_runner(tmp_path, monkeypatch, fetch_enabled):
    cfg = configured(tmp_path)
    api = API(cfg, "runner")
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"private-fixture")
    monkeypatch.setattr(auth, "jwt", lambda *a: "jwt-fixture")
    auth.mint(cfg, "runner", integration_enabled=True, fetch_enabled=fetch_enabled,
              comments_only=True, at=AT, opener=api)
    assert json.loads(api.calls[2].data)["permissions"] == {"issues": "write"}


def configured(tmp_path):
    cfg = policy(tmp_path)
    cfg["actors"].update(product="product-fixture[bot]", runner="runner-fixture[bot]")
    cfg["app_auth"] = {
        r: dict(app_id=i, keychain_service="fixture-" + r, keychain_account="fixture")
        for r, i in [("product", 1), ("runner", 2)]
    }
    return cfg


class API:
    def __init__(self, cfg, role="product"):
        self.cfg, self.role, self.calls = cfg, role, []
        self.responses = [
            dict(id=cfg["app_auth"][role]["app_id"], slug=cfg["actors"][role][:-5]),
            dict(id=3, app_id=cfg["app_auth"][role]["app_id"], suspended_at=None),
            dict(
                token="synthetic",
                expires_at=dt.datetime.fromtimestamp(
                    AT + 3600, dt.timezone.utc
                ).isoformat(),
                repositories=[dict(full_name=cfg["repository"])],
                permissions={"issues": "write", "metadata": "read"},
            ),
            dict(total_count=1, repositories=[dict(full_name=cfg["repository"])]),
        ]

    def open(self, req, timeout):
        self.calls.append(req)
        return io.BytesIO(json.dumps(self.responses.pop(0)).encode())


def test_mint_scopes_repository_permissions_and_verifies_listing(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    api = API(cfg)
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"private-fixture")
    monkeypatch.setattr(auth, "jwt", lambda *a: "jwt-fixture")
    assert auth.mint(cfg, "product", comments_only=True, at=AT, opener=api) == (
        "synthetic",
        AT + 3600,
    )
    assert [r.full_url for r in api.calls] == [
        "https://api.github.com/app",
        "https://api.github.com/repos/example/project/installation",
        "https://api.github.com/app/installations/3/access_tokens",
        "https://api.github.com/installation/repositories?per_page=100",
    ]
    assert json.loads(api.calls[2].data) == {
        "repositories": ["project"],
        "permissions": {"issues": "write"},
    }
    assert api.calls[-1].headers["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize(
    "mutation",
    [
        "actor",
        "app",
        "installation",
        "suspended",
        "scope",
        "permissions",
        "listing",
        "expiry",
    ],
)
def test_mint_rejects_wrong_identity_scope_and_expiry(tmp_path, monkeypatch, mutation):
    cfg = configured(tmp_path)
    api = API(cfg)
    if mutation == "actor":
        api.responses[0]["slug"] = "different-fixture"
    if mutation == "app":
        api.responses[0]["id"] = 999
    if mutation == "installation":
        api.responses[1]["app_id"] = 999
    if mutation == "suspended":
        api.responses[1]["suspended_at"] = "now"
    if mutation == "scope":
        api.responses[2]["repositories"].append(dict(full_name="example/other"))
    if mutation == "permissions":
        api.responses[2]["permissions"]["contents"] = "write"
    if mutation == "listing":
        api.responses[3]["total_count"] = 2
    if mutation == "expiry":
        api.responses[2]["expires_at"] = "2000-01-01T00:00:00Z"
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"private-fixture")
    monkeypatch.setattr(auth, "jwt", lambda *a: "jwt-fixture")
    with pytest.raises(auth.AuthenticationError) as error:
        auth.mint(cfg, "product", comments_only=True, at=AT, opener=api)
    assert "synthetic" not in str(error.value) and "jwt-fixture" not in str(error.value)
    if mutation == "actor":
        assert "different-fixture[bot]" in str(error.value)
    if mutation in {"actor", "app"}:
        assert len(api.calls) == 1


def test_keychain_is_captured_and_error_redacted(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    seen = []

    def run(args, **kw):
        seen.append((args, kw))
        raise subprocess.CalledProcessError(
            1, args, output=b"PRIVATE-SECRET", stderr=b"PRIVATE-SECRET"
        )

    monkeypatch.setattr(auth.subprocess, "run", run)
    with pytest.raises(auth.AuthenticationError) as error:
        auth.keychain_pem(cfg["app_auth"]["product"])
    assert "PRIVATE-SECRET" not in str(error.value)
    assert seen[0][0][:2] == ["/usr/bin/security", "find-generic-password"]
    assert seen[0][1]["capture_output"] and seen[0][1]["timeout"] == 30
    assert not any(k in seen[0][1]["env"] for k in ["GH_TOKEN", "OPENSSL_CONF"])


def test_real_rs256_signature_uses_only_memory_and_pipes():
    pem = subprocess.run(
        ["/usr/bin/openssl", "genrsa", "2048"], capture_output=True, check=True
    ).stdout
    public = subprocess.run(
        ["/usr/bin/openssl", "rsa", "-pubout"],
        input=pem,
        capture_output=True,
        check=True,
    ).stdout
    token = auth.jwt({"app_id": 123}, pem, AT)
    header, payload, signature = token.split(".")

    def decode(v):
        return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))

    assert json.loads(decode(header))["alg"] == "RS256"
    assert json.loads(decode(payload)) == dict(iat=AT - 60, exp=AT + 540, iss="123")
    fds = []
    try:
        for content in (public, decode(signature)):
            r, w = os.pipe()
            fds.append(r)
            os.write(w, content)
            os.close(w)
        result = subprocess.run(
            [
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-verify",
                f"/dev/fd/{fds[0]}",
                "-signature",
                f"/dev/fd/{fds[1]}",
            ],
            input=(header + "." + payload).encode(),
            capture_output=True,
            pass_fds=tuple(fds),
        )
        assert result.returncode == 0
    finally:
        for fd in fds:
            os.close(fd)


def test_split_policy_rejects_private_fields_and_role_overrides(tmp_path):
    cfg = policy(tmp_path)
    local = {
        "system_retry_minutes",
        "state_dir",
        "repository_root",
        "worktree_root",
        "runner_config",
        "primary_runner_config",
        "token_env",
        "product_token_env",
    }
    public = {k: v for k, v in cfg.items() if k not in local and k != "config_path"}
    pub = tmp_path / "public.json"
    core.save(pub, public)
    settings = {k: v for k, v in cfg.items() if k in local}
    settings["policy_file"] = str(pub)
    path = tmp_path / "local.json"
    core.save(path, settings)
    assert core.load_config(path)["actors"] == cfg["actors"]
    public["app_auth"] = {}
    core.save(pub, public)
    with pytest.raises(ValueError, match="Private settings"):
        core.load_config(path)
    del public["app_auth"]
    core.save(pub, public)
    settings["actors"] = cfg["actors"]
    core.save(path, settings)
    with pytest.raises(ValueError, match="Public policy fields"):
        core.load_config(path)


def test_app_token_refresh_rechecks_actor(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    calls = []

    def mint(self):
        calls.append("mint")
        self.token = "synthetic"
        self.token_expiry = AT + 3600
        self.identity_verified = False

    monkeypatch.setattr(core.GitHub, "refresh_app_token", mint)
    monkeypatch.setattr(core.time, "time", lambda: AT)
    client = core.GitHub(cfg, "product")
    client.opener = SimpleNamespace(
        open=lambda *a, **kw: io.BytesIO(
            json.dumps(
                {"data": {"viewer": {"login": cfg["actors"]["product"]}}}
            ).encode()
        )
    )
    client.ensure_actor()
    assert client.identity_verified and calls == ["mint"]
    monkeypatch.setattr(core.time, "time", lambda: AT + 3600)
    client.ensure_actor()
    assert calls == ["mint", "mint"] and client.identity_verified


def test_preflight_posts_once_and_never_blind_retries(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    h = Hub(cfg)
    h.issue(number=32, state="draft")
    monkeypatch.setattr(auth, "mint", lambda c, r, **kw: ("synthetic", AT + 3600))

    class Client:
        def __init__(self, c, r, token=None):
            self.inner = h.client(r)

        def ensure_actor(self):
            pass

        def request(self, method, path, data=None):
            value = self.inner.request(method, path, data)
            value.setdefault("id", len(h.comments_by_issue[32]))
            h.comments_by_issue[32][-1]["id"] = value["id"]
            return value

        def __getattr__(self, k):
            return getattr(self.inner, k)

    monkeypatch.setattr(core, "GitHub", Client)
    result = preflight.run(cfg, 32)
    assert len(result) == 2 and len(h.comments_by_issue[32]) == 2
    assert preflight.run(cfg, 32) == result and len(h.comments_by_issue[32]) == 2
    h.comments_by_issue[32].clear()
    with pytest.raises(core.Unavailable, match="outcome unknown"):
        preflight.run(cfg, 32)
    assert not h.comments_by_issue[32]


def test_explicit_null_app_settings_never_fall_back_to_environment(
    tmp_path, monkeypatch
):
    cfg = configured(tmp_path)
    cfg["app_auth"]["product"] = None
    monkeypatch.setenv(cfg["product_token_env"], "synthetic-environment-credential")
    with pytest.raises(
        core.Unavailable, match="Invalid private App authentication settings"
    ):
        core.GitHub(cfg, "product")


def test_stateless_token_is_opaque_through_mint_and_repository_request(
    tmp_path, monkeypatch
):
    cfg = configured(tmp_path)
    api = API(cfg)
    # Synthetic token: 520 characters and two dots. Do not parse JWT claims or
    # assume the provider's token length; pass the opaque credential unchanged.
    opaque = "ghs_" + "a" * 170 + "." + "b" * 170 + "." + "c" * 174
    assert len(opaque) == 520 and opaque.count(".") == 2
    api.responses[2].update(
        token=opaque, permissions={**auth.PERMISSIONS["product"], "metadata": "read"}
    )
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"private-fixture")
    monkeypatch.setattr(auth, "jwt", lambda *a: "jwt-fixture")
    token, _ = auth.mint(cfg, "product", at=AT, opener=api)
    assert json.loads(api.calls[2].data)["permissions"] == auth.PERMISSIONS["product"]
    client = core.GitHub(cfg, "product", token=token)
    calls = []

    def opened(req, timeout):
        calls.append(req)
        assert req.headers["Authorization"] == "Bearer " + opaque
        value = (
            {"data": {"viewer": {"login": cfg["actors"]["product"]}}}
            if req.full_url.endswith("/graphql")
            else {"number": 32, "state": "open"}
        )
        return io.BytesIO(json.dumps(value).encode())

    client.opener = SimpleNamespace(open=opened)
    assert client.request("GET", "/issues/32")["number"] == 32
    assert len(calls) == 2 and client.identity_verified


@pytest.mark.parametrize("enabled", [False, True])
def test_fetch_token_exact_permissions(tmp_path, monkeypatch, enabled):
    cfg = configured(tmp_path)
    expected = (auth.INTEGRATION_PERMISSIONS if enabled else
                {"issues": "write", "contents": "read"})
    monkeypatch.setattr(auth, "keychain_pem", lambda s: b"synthetic-key")
    monkeypatch.setattr(auth, "jwt", lambda *a: "synthetic-jwt")
    api = API(cfg, "runner")
    api.responses[2]["permissions"] = {**expected, "metadata": "read"}
    auth.mint(cfg, "runner", fetch_enabled=True, integration_enabled=enabled, at=AT, opener=api)
    assert json.loads(api.calls[2].data)["permissions"] == expected
    for mutation in ("missing", "extra", "broader"):
        api = API(cfg, "runner")
        api.responses[2]["permissions"] = {**expected, "metadata": "read"}
        if mutation == "missing":
            api.responses[2]["permissions"].pop("contents")
        elif mutation == "extra":
            api.responses[2]["permissions"]["administration"] = "write"
        else:
            api.responses[2]["permissions"]["contents"] = "admin"
        with pytest.raises(auth.AuthenticationError, match="scope mismatch"):
            auth.mint(cfg, "runner", fetch_enabled=True, integration_enabled=enabled, at=AT, opener=api)


@pytest.mark.parametrize("role,enabled", [("runner", 1), ("runner", "true"),
                                         ("runner", {}), ("product", True)])
def test_mint_rejects_invalid_fetch_opt_in(tmp_path, monkeypatch, role, enabled):
    cfg = configured(tmp_path)
    monkeypatch.setattr(auth, "keychain_pem", lambda s: pytest.fail("must reject before Keychain"))
    with pytest.raises(auth.AuthenticationError, match="fetch"):
        auth.mint(cfg, role, fetch_enabled=enabled)



def test_keychain_home_comes_from_user_database(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "temporary-home"))
    account_home = str(tmp_path / "account-home")
    seen = []
    monkeypatch.setattr(auth.pwd, "getpwuid", lambda uid: (
        seen.append(uid) or SimpleNamespace(pw_dir=account_home)))
    def run(argv, **kwargs):
        assert kwargs["env"]["HOME"] == account_home
        return SimpleNamespace(stdout=base64.b64encode(
            b"-----BEGIN PRIVATE " + b"KEY-----" + b"x" * 128))
    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.keychain_pem(cfg["app_auth"]["runner"])
    assert seen == [os.getuid()]



def test_missing_user_database_home_fails_safely(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    def missing(_):
        raise KeyError("synthetic account unavailable")
    monkeypatch.setattr(auth.pwd, "getpwuid", missing)
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **kw: pytest.fail("no Keychain lookup"))
    with pytest.raises(auth.AuthenticationError, match="Keychain credential unavailable or malformed"):
        auth.keychain_pem(cfg["app_auth"]["runner"])


@pytest.fixture
def linux_key(tmp_path, monkeypatch):
    """Mock Linux platform, trusted ancestry, ACL availability and mount facts.

    File opens, no-follow flags, fd reads, ownership and modes remain real.
    Native Linux tests below exercise the complete loader without these mocks.
    """
    import errno
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth, "_key_directory", lambda fd: None)
    monkeypatch.setattr(auth, "_key_mount", lambda fd: None)

    def no_acl(fd, name):
        assert isinstance(fd, int) and name == "system.posix_acl_access"
        raise OSError(errno.ENODATA, "synthetic")

    monkeypatch.setattr(auth.os, "getxattr", no_acl, raising=False)
    path = tmp_path / "synthetic.pem"
    path.write_bytes(b"-----BEGIN PRIVATE " + b"KEY-----\n" + b"x" * 128)
    path.chmod(0o600)
    return path, {"app_id": 1, "key_file": str(path)}


@pytest.mark.parametrize("settings", [
    {"app_id": 1},
    {"app_id": 1, "key_file": "/fixture/key", "keychain_service": "s", "keychain_account": "a"},
    {"app_id": 1, "key_file": "relative"},
    {"app_id": True, "key_file": "/fixture/key"},
    {"app_id": 1, "key_file": "/fixture/key", "openssl_path": "openssl"},
    {"app_id": 1, "key_file": "/fixture/key", "extra": True},
    {"app_id": 1, "keychain_service": "s"},
])
def test_linux_settings_reject_invalid_source_sets(monkeypatch, settings):
    # Platform fact mocked; all prior invalid-setting assertions stay intact.
    monkeypatch.setattr(auth.sys, "platform", "linux")
    with pytest.raises(auth.AuthenticationError, match="^Invalid private App authentication settings$"):
        auth.validate(settings, "runner")


def test_key_file_mac_message_and_optional_openssl(linux_key, monkeypatch):
    _, settings = linux_key
    auth.validate({**settings, "openssl_path": "/usr/bin/openssl"}, "runner")
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    with pytest.raises(auth.AuthenticationError, match="^key_file is supported on Linux; use the Keychain on macOS$"):
        auth.validate(settings, "runner")
    auth.validate(dict(app_id=1, keychain_service="s", keychain_account="a",
                       openssl_path="/usr/bin/openssl"), "runner")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o601, 0o4600, 0o2600, 0o1600])
def test_linux_key_rejects_mode_and_special_bits(linux_key, monkeypatch, mode):
    path, settings = linux_key
    path.chmod(mode)
    # macOS scratch filesystems may strip setuid/setgid. Mock those Linux
    # mode facts explicitly; the native test exercises kernel-enforced modes.
    original = os.fstat
    def mode_facts(fd):
        import stat
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=info.st_uid,
                                   st_size=info.st_size)
        return info
    monkeypatch.setattr(auth.os, "fstat", mode_facts)
    with pytest.raises(auth.AuthenticationError, match="^Key file must have owner-only permissions and no special bits$"):
        auth.file_pem(settings)


@pytest.mark.parametrize("rule", ["owner", "size", "fifo", "directory", "acl", "acl_error"])
def test_linux_key_metadata_refusals(linux_key, monkeypatch, rule):
    import errno
    path, settings = linux_key
    expected = {
        "owner": "Key file must be owned by the effective user",
        "size": "Key file exceeds 16 KiB",
        "fifo": "Key file must be a regular file",
        "directory": "Key file must be a regular file",
        "acl": "Key file must not have a POSIX ACL",
        "acl_error": "Key file ACL could not be checked",
    }[rule]
    if rule == "owner":
        # Effective-user fact mocked; no ownership changes on the host.
        uid = os.geteuid()
        monkeypatch.setattr(auth.os, "geteuid", lambda: uid + 1)
    elif rule == "size":
        path.write_bytes(b"x" * 16385)
    elif rule in {"fifo", "directory"}:
        path.unlink()
        os.mkfifo(path, 0o600) if rule == "fifo" else path.mkdir(mode=0o700)
    elif rule == "acl":
        monkeypatch.setattr(auth.os, "getxattr", lambda *a: b"synthetic-acl")
    else:
        def denied(*args):
            raise OSError(errno.EACCES, str(path))
        monkeypatch.setattr(auth.os, "getxattr", denied)
    with pytest.raises(auth.AuthenticationError) as error:
        auth.file_pem(settings)
    assert str(error.value) == expected


@pytest.mark.parametrize("ancestor", [False, True])
def test_linux_key_no_symlinks_in_walk(linux_key, ancestor):
    path, settings = linux_key
    link = path.parent / "link"
    link.symlink_to(path.parent if ancestor else path, target_is_directory=ancestor)
    settings["key_file"] = str(link / path.name if ancestor else link)
    with pytest.raises(auth.AuthenticationError, match="^Key file path must not contain symlinks$"):
        auth.file_pem(settings)


@pytest.mark.parametrize("uid,mode,git,expected", [
    (0, 0o755, False, None), (123, 0o700, False, None),
    (456, 0o700, False, "Key file ancestor must have trusted ownership and permissions"),
    (0, 0o1777, False, "Key file ancestor must have trusted ownership and permissions"),
    (123, 0o720, False, "Key file ancestor must have trusted ownership and permissions"),
    (123, 0o700, True, "Key file must be outside git working trees"),
])
def test_key_ancestor_rules_with_mock_os_facts(monkeypatch, uid, mode, git, expected):
    monkeypatch.setattr(auth.os, "geteuid", lambda: 123)
    monkeypatch.setattr(auth.os, "fstat", lambda fd: SimpleNamespace(st_uid=uid, st_mode=mode))
    def entry(path, **kwargs):
        assert path == ".git" and kwargs == {"dir_fd": 99, "follow_symlinks": False}
        if not git:
            raise FileNotFoundError()
        return SimpleNamespace()
    monkeypatch.setattr(auth.os, "stat", entry)
    if expected:
        with pytest.raises(auth.AuthenticationError) as error:
            auth._key_directory(99)
        assert str(error.value) == expected
    else:
        auth._key_directory(99)


@pytest.mark.parametrize("filesystem", ["ext4", "drvfs", "9p", "missing"])
def test_key_mount_uses_open_fd_mount_id(monkeypatch, filesystem):
    import builtins
    def opened(path, **kwargs):
        if path == "/proc/self/fdinfo/99":
            return io.StringIO("mnt_id:\t42\n")
        assert path == "/proc/self/mountinfo"
        return io.BytesIO(("42 1 0:1 / /fixture rw - " + filesystem + " source rw\n").encode()
                          if filesystem != "missing" else b"")
    monkeypatch.setattr(builtins, "open", opened)
    if filesystem == "ext4":
        auth._key_mount(99)
    else:
        with pytest.raises(auth.AuthenticationError) as error:
            auth._key_mount(99)
        assert str(error.value) == ("Key file mount could not be checked" if filesystem == "missing"
                                    else "Key file must be on a Linux filesystem, not a Windows drive")


def test_key_reads_exact_size_and_rechecks_same_fd(linux_key, monkeypatch):
    path, settings = linux_key
    read, opened = os.read, os.open
    reads, opens = [], []
    def bounded(fd, count):
        reads.append((fd, count))
        return read(fd, min(count, 37))
    def tracked(part, flags, **kwargs):
        opens.append((part, flags, kwargs))
        return opened(part, flags, **kwargs)
    monkeypatch.setattr(auth.os, "read", bounded)
    monkeypatch.setattr(auth.os, "open", tracked)
    assert auth.file_pem(settings) == path.read_bytes()
    assert len({fd for fd, _ in reads}) == 1
    assert reads[0][1] == path.stat().st_size and reads[-1][1] == 1
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for _, flags, _ in opens)
    assert opens[0][0] == "/" and all("dir_fd" in kw for _, _, kw in opens[1:])


@pytest.mark.parametrize("change", ["shrink", "grow", "mode", "read_error", "malformed"])
def test_key_read_failure_is_redacted(linux_key, monkeypatch, capsys, change):
    path, settings = linux_key
    original = os.read
    if change == "malformed":
        path.write_bytes(b"synthetic-not-a-pem")
    else:
        def read(fd, count):
            if change == "read_error":
                raise OSError(str(path) + " synthetic-secret")
            if change == "shrink":
                return b""
            if change == "grow" and count == 1:
                return b"x"
            if change == "mode":
                path.chmod(0o644)
            return original(fd, count)
        monkeypatch.setattr(auth.os, "read", read)
    with pytest.raises(auth.AuthenticationError) as error:
        auth.file_pem(settings)
    output = str(error.value) + str(capsys.readouterr())
    assert str(path) not in output and "synthetic-secret" not in output
    assert path.read_bytes().decode() not in output


@pytest.mark.parametrize("mutation", ["relative", "missing", "owner", "writable", "directory",
                                      "not_executable", "symlink_owner", "ancestor", "target_ancestor"])
def test_signing_executable_rules_mock_root_facts(monkeypatch, mutation):
    import stat
    # All executable filesystem facts mocked; no host executable is modified.
    def info(path):
        mode, uid = (stat.S_IFREG | 0o755, 0) if path.endswith("openssl") else (stat.S_IFDIR | 0o755, 0)
        if path == "/opt/link":
            mode = stat.S_IFLNK | 0o777
            if mutation == "symlink_owner":
                uid = 123
        if path == "/opt" and mutation == "ancestor":
            mode |= 0o020
        if path == "/target" and mutation == "target_ancestor":
            uid = 123
        if path.endswith("openssl"):
            if mutation == "missing":
                raise FileNotFoundError("synthetic-private-path")
            if mutation == "owner":
                uid = 123
            if mutation == "writable":
                mode |= 0o002
            if mutation == "directory":
                mode = stat.S_IFDIR | 0o755
        return SimpleNamespace(st_mode=mode, st_uid=uid)
    monkeypatch.setattr(auth.os, "lstat", info)
    monkeypatch.setattr(auth.os, "stat", lambda path, **kw: info(path))
    monkeypatch.setattr(auth.os, "readlink", lambda path: "/target/openssl")
    monkeypatch.setattr(auth.os, "access", lambda *a: mutation != "not_executable")
    path = "relative" if mutation == "relative" else "/opt/link"
    with pytest.raises(auth.AuthenticationError, match="^App JWT signing executable unavailable or unsafe$"):
        auth.signing_executable({"openssl_path": path})


def test_signer_runs_resolved_target_with_minimal_environment(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(auth, "signing_executable", lambda s: "/trusted/resolved/openssl")
    def run(argv, **kwargs):
        seen.append(argv)
        assert argv[0] == "/trusted/resolved/openssl"
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        assert kwargs["input"] == b"synthetic-secret"
        assert "synthetic-secret" not in str(argv)
        if len(seen) == 2:
            raise subprocess.CalledProcessError(1, argv, stderr=b"synthetic-secret /private/key")
        return SimpleNamespace(stdout=b"synthetic-signature")
    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.jwt({"app_id": 1}, b"synthetic-secret", AT).count(".") == 2
    with pytest.raises(auth.AuthenticationError) as error:
        auth.jwt({"app_id": 1}, b"synthetic-secret", AT)
    assert str(error.value) == "App JWT signing failed"
    assert len(seen) == 2 and not any(capsys.readouterr())


def test_key_file_mint_cache_and_next_mint_mode_check(linux_key, tmp_path, monkeypatch):
    path, settings = linux_key
    cfg = configured(tmp_path)
    cfg["app_auth"]["runner"] = {**settings, "app_id": 2}
    monkeypatch.setattr(auth, "jwt", lambda *a: "synthetic-jwt")
    monkeypatch.setattr(auth, "keychain_pem", lambda *a: pytest.fail("no Keychain for file source"))
    auth.clear_cache()
    first = auth.get(cfg, "runner", at=AT, opener=API(cfg, "runner"))
    path.chmod(0o644)
    assert auth.get(cfg, "runner", at=AT + 1) == first
    with pytest.raises(auth.AuthenticationError, match="owner-only permissions"):
        auth.get(cfg, "runner", at=AT + 3300, opener=API(cfg, "runner"))
    path.chmod(0o600)
    api = API(cfg, "runner")
    api.responses[0]["id"] = 999
    with pytest.raises(auth.AuthenticationError, match="App identity mismatch"):
        auth.get(cfg, "runner", at=AT + 3300, opener=api)
    assert len(api.calls) == 1


@pytest.fixture
def native_linux_key():
    """Disposable protected scratch; no service, network or real credentials."""
    import sys
    import tempfile
    from pathlib import Path
    if sys.platform != "linux":
        pytest.skip("requires native Linux filesystem and distribution OpenSSL")
    with tempfile.TemporaryDirectory(prefix="adt-synthetic-key-", dir=Path.home()) as directory:
        path = Path(directory) / "synthetic.pem"
        pem = subprocess.run(["/usr/bin/openssl", "genrsa", "2048"],
                             capture_output=True, check=True, env={"PATH": "/usr/bin:/bin"}).stdout
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(pem)
        yield path, {"app_id": 123, "key_file": str(path)}


def assert_independent_key_signature(path, settings, capsys):
    pem = auth.file_pem(settings)
    token = auth.jwt(settings, pem, AT)
    header, payload, signature = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))) == {
        "iat": AT - 60, "exp": AT + 540, "iss": str(settings["app_id"])}
    public = subprocess.run(["/usr/bin/openssl", "rsa", "-pubout"], input=pem,
                            capture_output=True, check=True).stdout
    fds = []
    try:
        for data in (public, base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))):
            r, w = os.pipe()
            fds.append(r)
            os.write(w, data)
            os.close(w)
        verified = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify",
                                   f"/dev/fd/{fds[0]}", "-signature", f"/dev/fd/{fds[1]}"],
                                  input=(header + "." + payload).encode(), capture_output=True,
                                  pass_fds=tuple(fds), check=True)
        assert verified.stdout.strip() == b"Verified OK"
        assert str(path) not in token and pem.decode() not in token
        assert not any(capsys.readouterr())
    finally:
        for fd in fds:
            os.close(fd)


def test_native_linux_modes_sticky_and_git(native_linux_key):
    path, settings = native_linux_key
    path.chmod(0o644)
    with pytest.raises(auth.AuthenticationError, match="owner-only permissions"):
        auth.file_pem(settings)
    path.chmod(0o600)
    path.parent.chmod(0o1777)
    with pytest.raises(auth.AuthenticationError, match="ancestor"):
        auth.file_pem(settings)
    path.parent.chmod(0o700)
    (path.parent / ".git").write_text("synthetic git marker")
    with pytest.raises(auth.AuthenticationError, match="outside git"):
        auth.file_pem(settings)


def test_native_linux_actual_posix_acl(native_linux_key):
    import struct
    path, settings = native_linux_key
    # Linux POSIX ACL xattr v2: owner rw, named user read (masked to none),
    # group none, mask none, other none. File mode stays 0600 so this tests ACLs.
    uid = os.geteuid() + 1
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, perms, entry_uid)
        for tag, perms, entry_uid in [(1, 6, 0xffffffff), (2, 4, uid),
                                     (4, 0, 0xffffffff), (16, 0, 0xffffffff),
                                     (32, 0, 0xffffffff)])
    os.setxattr(path, "system.posix_acl_access", acl)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(auth.AuthenticationError, match="must not have a POSIX ACL"):
        auth.file_pem(settings)



def test_native_linux_key_signs_independently(native_linux_key, capsys):
    assert_independent_key_signature(*native_linux_key, capsys)


def test_mock_linux_file_signs_independently(linux_key, capsys):
    path, settings = linux_key
    path.write_bytes(subprocess.run(["/usr/bin/openssl", "genrsa", "2048"],
                                   capture_output=True, check=True).stdout)
    assert_independent_key_signature(path, settings, capsys)


@pytest.mark.parametrize("error_number", ["ENOTSUP", "EOPNOTSUPP"])
def test_linux_filesystem_without_xattrs_passes(linux_key, monkeypatch, error_number):
    import errno
    path, settings = linux_key
    def unsupported(*args):
        raise OSError(getattr(errno, error_number), "synthetic")
    monkeypatch.setattr(auth.os, "getxattr", unsupported)
    assert auth.file_pem(settings) == path.read_bytes()


def test_root_owned_signing_symlink_resolves_both_ancestries(monkeypatch):
    import stat
    seen = []
    def info(path):
        seen.append(path)
        mode = stat.S_IFDIR | 0o755
        if path == "/opt/link":
            mode = stat.S_IFLNK | 0o777
        if path == "/target/bin/openssl":
            mode = stat.S_IFREG | 0o755
        return SimpleNamespace(st_uid=0, st_mode=mode)
    monkeypatch.setattr(auth.os, "lstat", info)
    monkeypatch.setattr(auth.os, "stat", lambda path, **kw: info(path))
    monkeypatch.setattr(auth.os, "readlink", lambda path: "/target/bin/openssl")
    monkeypatch.setattr(auth.os, "access", lambda *a: True)
    assert auth.signing_executable({"openssl_path": "/opt/link"}) == "/target/bin/openssl"
    assert seen == ["/", "/opt", "/opt/link", "/target", "/target/bin",
                    "/target/bin/openssl", "/target/bin/openssl"]


def test_key_file_core_and_smoke_consumers(linux_key, tmp_path, monkeypatch):
    from lifecycle import smoke, smoke_watch
    from test_smoke import config
    _, settings = linux_key
    cfg = configured(tmp_path)
    cfg["app_auth"] = {r: {**settings, "app_id": i} for r, i in [("runner", 2), ("product", 1)]}
    calls = []
    def mint(config, role, **kwargs):
        auth.validate(config["app_auth"][role], role)
        assert "key_file" in config["app_auth"][role]
        calls.append((role, kwargs))
        return "synthetic", AT + 3600
    monkeypatch.setattr(auth, "mint", mint)
    auth.clear_cache()
    client = core.GitHub(cfg, "product")
    client.refresh_app_token()
    assert core.app_client(cfg, comments_only=True).token == "synthetic"
    smoke_cfg = config(tmp_path)
    for role in smoke_cfg["app_auth"]:
        smoke_cfg["app_auth"][role] = {**settings, "app_id": smoke_cfg["app_auth"][role]["app_id"]}
    path = tmp_path / "linux-smoke.json"
    core.save(path, smoke_cfg)
    smoke_cfg = smoke.load_config(path)
    runner, owner = smoke.clients(smoke_cfg, cfg)
    assert runner.token == owner.token == "synthetic"
    assert smoke.status_client(smoke_cfg).token == "synthetic"
    assert smoke_watch.framework_client(smoke_cfg).token == "synthetic"
    assert calls


@pytest.mark.parametrize("filesystem", [b"ext4", b"drvfs", b"9p"])
@pytest.mark.parametrize("metadata", ["unrelated", "checked"])
def test_key_mount_non_ascii_metadata_is_uninterpreted(monkeypatch, filesystem, metadata):
    import builtins
    # Synthetic proc records only: mount paths/sources need not decode as ASCII
    # or UTF-8. An unrelated Windows mount must not determine the checked type.
    unrelated = b"142 1 0:2 / /fixture rw - drvfs source rw\n"
    checked = b"42 1 0:1 / /fixture rw - " + filesystem + b" source rw\n"
    if metadata == "unrelated":
        unrelated = unrelated.replace(b"/fixture", b"/fixture-\xff").replace(b"source", b"source-\xc3\xa9")
    else:
        checked = checked.replace(b"/fixture", b"/fixture-\xc3\xa9").replace(b"source", b"source-\xff")
    def opened(path, **kwargs):
        if path == "/proc/self/fdinfo/99":
            return io.StringIO("mnt_id:\t42\n")
        assert path == "/proc/self/mountinfo"
        stream = io.BytesIO(unrelated + checked)
        if kwargs.get("mode") == "rb":
            return stream
        return io.TextIOWrapper(stream, encoding=kwargs["encoding"])
    monkeypatch.setattr(builtins, "open", opened)
    if filesystem == b"ext4":
        auth._key_mount(99)
    else:
        with pytest.raises(auth.AuthenticationError) as error:
            auth._key_mount(99)
        assert str(error.value) == "Key file must be on a Linux filesystem, not a Windows drive"


@pytest.mark.parametrize("record", [b"", b"42", b"42 1 0:1 / /fixture rw - \n"])
def test_key_mount_missing_or_malformed_is_redacted(monkeypatch, record, capsys):
    import builtins
    def opened(path, **kwargs):
        if path == "/proc/self/fdinfo/99":
            return io.StringIO("mnt_id:\t42\n")
        return io.BytesIO(record)
    monkeypatch.setattr(builtins, "open", opened)
    with pytest.raises(auth.AuthenticationError) as error:
        auth._key_mount(99)
    assert str(error.value) == "Key file mount could not be checked"
    assert not any(capsys.readouterr())


@pytest.mark.parametrize("source", ["key_file", "openssl_path"])
def test_unencodable_filesystem_path_is_redacted(monkeypatch, capsys, source):
    # A benign synthetic name with a lone high surrogate cannot be encoded by
    # Python's filesystem codec. No key material or private path is involved.
    path = "/synthetic-name-\ud800"
    monkeypatch.setattr(auth.sys, "platform", "linux")
    settings = {"app_id": 1, "key_file": path}
    auth.validate(settings, "runner")
    if source == "key_file":
        monkeypatch.setattr(auth, "_key_directory", lambda fd: None)
        def load():
            return auth.file_pem(settings)
        expected = "Key file unavailable or unreadable"
    else:
        def load():
            return auth.signing_executable({"openssl_path": path})
        expected = "App JWT signing executable unavailable or unsafe"
    with pytest.raises(auth.AuthenticationError) as error:
        load()
    assert str(error.value) == expected
    assert error.value.__suppress_context__
    output = str(error.value) + str(capsys.readouterr())
    assert "synthetic-name" not in output and path not in output
