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
    cfg = policy(tmp_path)
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
