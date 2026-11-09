"""Product roles never imply owner authority; answers need exact repository cites."""

import base64
import copy
import hashlib

import pytest

from lifecycle import core, product
from test_lifecycle import Hub, policy


def source_client(
    cfg, hub, text="The runner uses fresh sessions.\nThe owner approves stories.\n"
):
    client = hub.client("product")
    original = client.request
    raw = text.encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    source = dict(
        type="file", encoding="base64", sha=blob, content=base64.b64encode(raw).decode()
    )

    def request(method, path, data=None):
        if method == "GET" and path.startswith("/contents/"):
            return copy.deepcopy(source)
        return original(method, path, data)

    client.request = request
    return client, source


def request(kind="clarification"):
    return dict(
        id="question-fixture",
        issue=1,
        kind=kind,
        citations=[
            dict(
                path="README.md",
                ref="a" * 40,
                start=1,
                end=1,
                quote="The runner uses fresh sessions.",
            )
        ],
    )


def test_owner_and_app_share_product_role_without_sharing_owner_authority(tmp_path):
    cfg = policy(tmp_path)
    assert core.has_role(cfg, cfg["actors"]["owner"], "product")
    assert core.has_role(cfg, cfg["actors"]["product"], "product")
    assert core.permitted(cfg, "draft", "approved", cfg["actors"]["owner"])
    assert not core.permitted(cfg, "draft", "approved", cfg["actors"]["product"])
    assert not core.permitted(cfg, "approved", "approved", cfg["actors"]["product"])
    assert not core.permitted(cfg, "pr-open", "accepted", cfg["actors"]["product"])
    cfg["identity_roles"][cfg["actors"]["product"]].append("owner")
    core.save(cfg["config_path"], cfg)
    with pytest.raises(ValueError, match="designated owner"):
        core.load_config(cfg["config_path"])


def test_repository_grounded_clarification_cites_commit_and_lines_without_state_change(
    tmp_path,
):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client, _ = source_client(cfg, h)
    before = copy.deepcopy(h.issues[1])
    result = product.answer(cfg, request(), client)
    assert (
        result["route"] == "repository_clarification" and result["status"] == "synced"
    )
    assert h.issues[1] == before
    body = h.comments_by_issue[1][0]["body"]
    assert "> The runner uses fresh sessions." in body
    assert (
        "https://github.com/example/project/blob/" + "a" * 40 + "/README.md#L1-L1"
        in body
    )
    assert "no scope, criteria or approval decision" in body
    records = core.records(cfg, h.comments_by_issue[1], 1)
    assert (
        records[0]["kind"] == "clarification"
        and records[0]["actor"] == cfg["actors"]["product"]
    )
    product.answer(cfg, request(), client)
    assert len(h.comments_by_issue[1]) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "scope_change",
        "acceptance_criteria_change",
        "story_approval",
        "ambiguous_request",
    ],
)
def test_product_escalates_scope_ac_and_approval_without_answering(tmp_path, kind):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client, _ = source_client(cfg, h)
    result = product.answer(cfg, request(kind), client)
    assert result["route"] == "owner" and result["status"] == "synced"
    assert core.state(h.issues[1]) == "needs-owner"
    assert h.issues[1]["assignees"] == [{"login": cfg["actors"]["owner"]}]
    assert not any(c[0] == "GET" and "/contents/" in c[1] for c in h.calls)
    assert "The runner uses fresh sessions." not in h.comments_by_issue[1][0]["body"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "invented",
        "outside_repo",
        "mutable_ref",
        "outside_allowlist",
        "bad_hash",
    ],
)
def test_missing_or_unsupported_citations_go_to_owner(tmp_path, mutation):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client, source = source_client(cfg, h)
    value = request()
    if mutation == "missing":
        value["citations"] = []
    elif mutation == "invented":
        value["citations"][0]["quote"] = "The App approves stories."
    elif mutation == "outside_repo":
        value["citations"][0]["path"] = "../private.md"
    elif mutation == "mutable_ref":
        value["citations"][0]["ref"] = "main"
    elif mutation == "outside_allowlist":
        value["citations"][0]["path"] = "private.py"
    else:
        source["sha"] = "b" * 40
    result = product.answer(cfg, value, client)
    assert result["route"] == "owner" and core.state(h.issues[1]) == "needs-owner"
    assert "The App approves stories." not in h.comments_by_issue[1][0]["body"]


def test_owner_product_role_cannot_forge_guard_attestation_or_runner_review(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    actor = cfg["actors"]["owner"]
    approval = core.public_record(
        cfg,
        1,
        "approval",
        0,
        "approved",
        event=101,
        kind="approval",
        scope_sha256="a" * 64,
        approved_by=actor,
    )
    approval.update({"from": "approved", "actor": actor})
    review = core.public_record(
        cfg,
        1,
        "job",
        1,
        "in-review",
        commit="a" * 40,
        kind="review",
        outcome="ready_for_integration",
    )
    review.update({"from": "in-review", "actor": actor})
    comments = [
        dict(user={"login": actor}, body=core.record_body(r))
        for r in [approval, review]
    ]
    assert core.records(cfg, comments, 1) == []
