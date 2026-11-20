"""Record validity depends on trusted data, versions and comment edit times."""

import copy
import json
from pathlib import Path

import pytest

from lifecycle import core
from test_lifecycle import WHEN, policy

LATER = "2026-01-02T10:01:00+00:00"


def record_comment(cfg, target="dispatched", **metadata):
    role = "workflow" if target in {"approved", "merged"} else "runner"
    source = "pr-open" if target == "merged" else "approved"
    if target == "needs-primary":
        source = "in-review"
    if target == "accepted":
        role = "owner"
    if target == "approved":
        metadata.update(kind="approval", event=101,
                        approved_by=cfg["actors"]["owner"], scope_sha256="b" * 64)
    record = core.public_record(cfg, 1, "record-fixture", 0, target, **metadata)
    record.update({"from": source, "actor": cfg["actors"][role], "timestamp": WHEN})
    return record, dict(user={"login": record["actor"]}, body=core.record_body(record),
                        created_at=WHEN, updated_at=WHEN)


@pytest.mark.parametrize("before,after", [
    ("A replacement notice.\n", "\nDifferent continuation instructions."),
    ("", ""),
    ("A notice with unrelated JSON: {}.\n", "\n### Continue from\nNew wording."),
])
def test_record_prose_outside_unique_block_never_counts(tmp_path, before, after):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg, "needs-owner", reason="owner_merge_required",
                                     commit="a" * 40, continuation=True)
    data = comment["body"].split(core.PREFIX)[1].split(core.SUFFIX)[0]
    comment["body"] = before + core.PREFIX + data + core.SUFFIX + after
    assert core.records(cfg, [comment], 1) == [record]


@pytest.mark.parametrize("version", [2, 9, 0, -1, True, False, 1.0, "1", None, [], {}])
def test_record_requires_supported_json_integer_version(tmp_path, version):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg)
    record["version"] = version
    comment["body"] = core.record_body(record)
    assert core.records(cfg, [comment], 1) == []


def test_record_requires_explicit_version(tmp_path):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg)
    del record["version"]
    comment["body"] = core.record_body(record)
    assert core.records(cfg, [comment], 1) == []


def test_supported_version_preserves_record_key(tmp_path):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg)
    parsed, = core.records(cfg, [comment], 1)
    assert parsed == record
    assert parsed["key"] == core.public_record(cfg, 1, "record-fixture", 0, "dispatched")["key"]


@pytest.mark.parametrize("target", ["approved", "dispatched", "blocked", "needs-owner", "needs-primary"])
def test_edited_nonfinished_record_is_ignored(tmp_path, target):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg, target)
    assert core.records(cfg, [comment], 1) == [record]
    comment["updated_at"] = LATER
    assert core.records(cfg, [comment], 1) == []


def test_edited_owner_merge_stop_cannot_authorize_merge(tmp_path):
    cfg = policy(tmp_path)
    _, comment = record_comment(cfg, "needs-owner", reason="owner_merge_required", pr=10)
    comment["updated_at"] = LATER
    evidence = core.records(cfg, [comment], 1)
    assert evidence == []
    assert not core.owner_merge_verified(cfg, 1, evidence, {"number": 10})


@pytest.mark.parametrize("target", ["merged", "accepted"])
def test_edited_finished_record_preserves_finality(tmp_path, target):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg, target)
    comment["updated_at"] = LATER
    evidence = core.records(cfg, [comment], 1)
    assert evidence == [record]
    assert not core.can_start({"state": "open", "labels": [{"name": "status:approved"}]}, evidence)


@pytest.mark.parametrize("missing", [("created_at",), ("updated_at",), ("created_at", "updated_at")])
def test_missing_comment_timestamp_counts_as_unedited(tmp_path, missing):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg)
    comment["updated_at"] = LATER
    for field in missing:
        del comment[field]
    assert core.records(cfg, [comment], 1) == [record]


@pytest.mark.parametrize("updated", [WHEN, "2026-01-02T09:59:00Z", "2026-01-02T11:00:00+01:00"])
def test_comment_timestamp_comparison_uses_instants(tmp_path, updated):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg)
    comment["updated_at"] = updated
    assert core.records(cfg, [comment], 1) == [record]


@pytest.mark.parametrize("target", ["dispatched", "merged", "accepted"])
def test_untrusted_author_cannot_forge_even_finished_record(tmp_path, target):
    cfg = policy(tmp_path)
    record, comment = record_comment(cfg, target)
    record["actor"] = comment["user"]["login"] = "untrusted-fixture"
    comment["body"] = core.record_body(record)
    comment["updated_at"] = LATER
    assert core.records(cfg, [comment], 1) == []


@pytest.mark.parametrize("extra", [core.PREFIX, core.SUFFIX])
def test_record_requires_unique_data_block(tmp_path, extra):
    cfg = policy(tmp_path)
    _, comment = record_comment(cfg)
    comment["body"] += extra
    assert core.records(cfg, [comment], 1) == []


def test_all_413_sanitised_baseline_record_shapes_still_parse(tmp_path):
    cfg = policy(tmp_path)
    comments = json.loads((Path(__file__).parent / "fixtures/records/record-shapes-413.json").read_text())
    original = copy.deepcopy(comments)
    assert len(comments) == 413
    for comment in comments:
        expected = json.loads(comment["body"].split(core.PREFIX)[1].split(core.SUFFIX)[0])
        assert core.records(cfg, [comment], expected["issue"]) == [expected]
    assert comments == original
