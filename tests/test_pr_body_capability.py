"""Real transport reentrancy cannot reuse a validated PR-body edit capability."""

import io
import json
from types import SimpleNamespace

import pytest

from lifecycle import core
from test_lifecycle import policy
import test_runner as fixtures


@pytest.fixture
def transport(tmp_path):
    cfg = policy(tmp_path)
    api = fixtures.runner.integration_api()

    class TokenOnly(core.GitHub):
        def __init__(self, settings):
            super().__init__(settings, token="synthetic")

    client = api.client_for(SimpleNamespace(GitHub=TokenOnly), cfg)
    sent, identity_calls = [], []
    callback = {"identity": None, "transport": None}
    fired = set()

    def run_callback(stage):
        if stage not in fired and callback[stage]:
            fired.add(stage)
            callback[stage]()

    def identity():
        identity_calls.append(True)
        run_callback("identity")

    def opened(req, timeout):
        sent.append((req.method, req.full_url.split('/repos/' + cfg['repository'], 1)[1],
                     json.loads(req.data) if req.data else None))
        run_callback("transport")
        return io.BytesIO(json.dumps({"body": json.loads(req.data)["body"]}).encode())

    client.ensure_actor = identity
    client.opener = SimpleNamespace(open=opened)
    old = "## Test evidence\nold\n\n## Acceptance evidence\nold\n\n## Security review\nfixed\n"
    new = old.replace("old", "new")
    job = {"integration": {"pr": 9, "body": old}}
    return SimpleNamespace(client=client, api=api, callback=callback, sent=sent,
                           identity_calls=identity_calls, job=job, body=new)


@pytest.mark.parametrize("stage", ["identity", "transport"])
@pytest.mark.parametrize("attack", ["title", "extra_field", "identical", "other_body",
                                    "other_pr", "issue_route", "nested_helper"])
def test_real_transport_denies_reentrant_unvalidated_or_reused_payload(transport, stage, attack):
    t = transport
    def reenter():
        payload, route = {"body": t.body}, "/pulls/9"
        if attack == "title":
            payload = {"title": "changed"}
        elif attack == "extra_field":
            payload["title"] = "changed"
        elif attack == "other_body":
            payload["body"] = t.body.replace("fixed", "changed security")
        elif attack == "other_pr":
            route = "/pulls/10"
        elif attack == "issue_route":
            route = "/issues/9"
        with pytest.raises(ValueError):
            if attack == "nested_helper":
                t.api.edit_pr_body(t.client, t.job, 9, payload, "standard")
            else:
                t.client.request("PATCH", route, payload)
    t.callback[stage] = reenter
    t.api.edit_pr_body(t.client, t.job, 9, {"body": t.body}, "standard")
    assert t.sent == [("PATCH", "/pulls/9", {"body": t.body})]
    assert not t.client.write_allowed("PATCH", "/pulls/9")
    assert not hasattr(t.client, "_pr_body_write")


def test_real_transport_intended_edit_succeeds_once_and_direct_reuse_is_denied(transport):
    t = transport
    for payload in [{"title": "changed"}, {"body": t.body}]:
        with pytest.raises(ValueError):
            t.client.request("PATCH", "/pulls/9", payload)
    t.api.edit_pr_body(t.client, t.job, 9, {"body": t.body}, "standard")
    with pytest.raises(ValueError):
        t.client.request("PATCH", "/pulls/9", {"body": t.body})
    assert t.sent == [("PATCH", "/pulls/9", {"body": t.body})]


def test_identity_failure_consumes_capability_without_publication_or_reuse(transport):
    t = transport
    def fail_identity():
        raise core.Unavailable("Synthetic identity failure")
    t.client.ensure_actor = fail_identity
    with pytest.raises(core.Unavailable):
        t.api.edit_pr_body(t.client, t.job, 9, {"body": t.body}, "standard")
    assert not hasattr(t.client, "_pr_body_write")
    assert not hasattr(t.client, "_pr_body_edit_active")
    t.client.ensure_actor = lambda: None
    with pytest.raises(ValueError):
        t.client.request("PATCH", "/pulls/9", {"body": t.body})
    assert t.sent == []


def test_transmitted_payload_is_frozen_before_identity_callback(transport):
    t = transport
    captured = []
    consume = t.client.consume_write_payload
    def capture(method, path, data):
        captured.append(data)
        return consume(method, path, data)
    t.client.consume_write_payload = capture
    def mutate():
        captured[0].clear()
        captured[0]["title"] = "changed"
    t.callback["identity"] = mutate
    t.api.edit_pr_body(t.client, t.job, 9, {"body": t.body}, "standard")
    assert captured == [{"title": "changed"}]
    assert t.sent == [("PATCH", "/pulls/9", {"body": t.body})]


@pytest.mark.parametrize("role", ["runner", "product", "workflow"])
@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/issues/9", True),
    ("POST", "/issues/9/comments", True),
    ("PATCH", "/pulls/9", False),
])
def test_default_payload_hook_preserves_ordinary_role_permission_and_identity_order(
        tmp_path, role, method, path, allowed):
    cfg = policy(tmp_path)
    client = core.GitHub(cfg, role=role, token="synthetic")
    calls = []
    original_allowed = client.write_allowed
    def permission(verb, route):
        calls.append("permission")
        return original_allowed(verb, route)
    client.write_allowed = permission
    client.ensure_actor = lambda: calls.append("identity")
    def opened(req, timeout):
        calls.append("transport")
        assert json.loads(req.data) == {"body": "ordinary"}
        return io.BytesIO(b"{}")
    client.opener = SimpleNamespace(open=opened)
    assert client.consume_write_payload(method, path, {"body": "ordinary"}) is None
    if allowed:
        assert client.request(method, path, {"body": "ordinary"}) == {}
        assert calls == (["identity", "transport"] if method == "GET" else
                         ["identity", "permission", "identity", "transport"])
    else:
        with pytest.raises(ValueError):
            client.request(method, path, {"body": "ordinary"})
        assert calls == ["identity", "permission"]
