"""Synthetic PR events only: no network, credentials or execution of PR text."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_gate", ROOT / "gate/review_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA = "a" * 40
REPO = "https://github.com/example/project"

BASE_CONFIG = {
    "repo_url": REPO,
    "allowed_bases": ["main", "develop"],
    "requirement_ids": None,
    "scope_acknowledgements": 3,
    "tiers": {
        "order": ["light", "standard", "domain-critical", "control-critical"],
        "default": "standard",
        "rules": [
            {"tier": "control-critical", "reason": "Controls", "patterns": [".github/*", "gate/*", "src/auth/*", "*.lock", "tests/test_review_gate.py"]},
            {"tier": "domain-critical", "reason": "Core logic", "patterns": ["src/pricing/*", "tests/test_pricing*"]},
            {"tier": "light", "reason": "Docs", "patterns": ["docs/*", "*.md", "tests/*"]},
        ],
    },
}


def config(**overrides):
    path = Path(tempfile.mkdtemp()) / "review-gate.json"
    data = copy.deepcopy(BASE_CONFIG)
    data.update(overrides)
    path.write_text(json.dumps(data))
    return gate.load_config(path)


@pytest.fixture
def cfg():
    return config()


@pytest.fixture
def cfg_ids():
    return config(requirement_ids={"pattern": r"REQ-\d{3}", "allowed": ["REQ-001", "REQ-002"]})


COMMON = """## Review tier
Tier: {tier}
## Maker summary
Maker context: maker-run-1
Changed X for Y; rollback by reverting this PR.
## Test evidence
Tested head: {sha}
pytest: 4 passed.
## Assumptions
None beyond the stated scope.
"""

STANDARD = f"""## Original acceptance criteria
Issue: {REPO}/issues/7
- AC-1: Empty state shown
## Acceptance evidence
| Criterion | Evidence | Result |
| --- | --- | --- |
| AC-1 | tests/test_ui.py::test_empty | PASS: 4 passed |
## Security review
No credentials, external access or data changes.
"""

DOMAIN = """## Numeric verification
Hand-worked: tests/test_pricing.py::test_hand_worked
Point-in-time: tests/test_pricing.py::test_no_future_data
Missing data: tests/test_pricing.py::test_missing_input_abstains
"""


def control(with_ids):
    ids_col = " REQ-001 |" if with_ids else ""
    ids_head = " IDs |" if with_ids else ""
    ids_sep = " --- |" if with_ids else ""
    section = "## Requirement IDs\nREQ-001\n" if with_ids else ""
    return section + f"""## Original acceptance criteria
Issue: {REPO}/issues/7
- AC-1: Gate change is safe
## Acceptance and evidence matrix
| Criterion |{ids_head} Changed paths | Commands and results | Evidence link | Maker state | Checker verdict |
| --- |{ids_sep} --- | --- | --- | --- | --- |
| AC-1 |{ids_col} gate/review_gate.py | pytest: PASS | this PR | VERIFIED | PENDING |
## Security review
Secrets: none accessed.
External access/cost: none.
Data/provenance: synthetic fixtures only.
## Risk and scope
- [x] SCOPE-1: correct target.
- [x] SCOPE-2: no secrets exposed.
- [x] SCOPE-3: reversible.
## Independent Checker
Verdict: PENDING
Checker: NOT ASSIGNED
Checker context: NOT ASSIGNED
Reviewed head: NOT RECORDED
Review: NOT RECORDED
"""


def pr(body, base="main", number=12):
    return {"number": number, "base": {"ref": base}, "head": {"sha": SHA}, "body": body}


def body(tier, *parts):
    return COMMON.format(tier=tier, sha=SHA) + "".join(parts)


# ---------- categories ----------

@pytest.mark.parametrize("path, tier", [
    ("docs/guide.md", "light"),
    ("README.md", "light"),
    ("tests/test_ui.py", "light"),
    ("src/ui/page.py", "standard"),
    ("unknown/new_file.py", "standard"),
    ("src/pricing/model.py", "domain-critical"),
    ("tests/test_pricing_model.py", "domain-critical"),
    ("src/auth/login.py", "control-critical"),
    ("poetry.lock", "control-critical"),
    (".github/workflows/ci.yml", "control-critical"),
])
def test_file_categories(cfg, path, tier):
    assert gate.file_tier(path, cfg) == tier


def test_pr_takes_highest_category(cfg):
    minimum, present, forcing = gate.required_tiers(["docs/a.md", "src/pricing/m.py", "src/ui/p.py"], cfg)
    assert minimum == "domain-critical"
    assert present == {"light", "standard", "domain-critical"}
    assert forcing == ["src/pricing/m.py"]


def test_missing_changed_file_list_applies_strictest(cfg):
    assert gate.required_tiers(None, cfg)[0] == "control-critical"


def test_empty_changed_file_list_uses_strictest(cfg):
    assert gate.required_tiers([], cfg)[0] == "control-critical"


# ---------- light and standard ----------

def test_light_passes(cfg):
    assert gate.validate(pr(body("light")), ["docs/a.md"], cfg) == []


def test_light_rejected_for_standard_files(cfg):
    errors = gate.validate(pr(body("light")), ["src/ui/p.py"], cfg)
    assert any("below required standard" in e for e in errors)
    assert any("Original acceptance criteria" in e for e in errors)


def test_standard_passes_and_needs_current_head(cfg):
    request = pr(body("standard", STANDARD))
    assert gate.validate(request, ["src/ui/p.py"], cfg) == []
    request["head"]["sha"] = "b" * 40
    assert any("Tested head" in e for e in gate.validate(request, ["src/ui/p.py"], cfg))


@pytest.mark.parametrize("old, new, expected", [
    ("| AC-1 | tests/test_ui.py::test_empty | PASS: 4 passed |", "", "cover every original AC"),
    ("PASS: 4 passed", "looks fine", "Acceptance result"),
    ("| AC-1 | tests/test_ui.py::test_empty | PASS: 4 passed |", "| AC-1 | x |", "three nonempty columns"),
    ("None beyond the stated scope.", "", "Assumptions"),
    ("Tier: standard", "Tier: medium", "must declare Tier"),
    ("Maker context: maker-run-1", "Maker: someone", "context reference"),
    (f"Issue: {REPO}/issues/7", "Issue: https://github.com/other/repo/issues/7", "issue URL"),
    ("No credentials, external access or data changes.", "REQUIRED: security", "REQUIRED"),
    ("- AC-1: Empty state shown", "- AC-1:", "Malformed"),
])
def test_standard_missing_evidence_fails(cfg, old, new, expected):
    request = pr(body("standard", STANDARD).replace(old, new))
    assert any(expected in e for e in gate.validate(request, ["src/ui/p.py"], cfg))


def test_wrong_base_and_duplicate_sections_fail(cfg):
    request = pr(body("light") + "## Maker summary\nagain\n", base="feature/x")
    errors = gate.validate(request, ["docs/a.md"], cfg)
    assert any("Target must be" in e for e in errors)
    assert any("Duplicate" in e for e in errors)


# ---------- domain ----------

def test_domain_needs_evidence_section(cfg):
    request = pr(body("domain-critical", STANDARD))
    assert any("Hand-worked" in e for e in gate.validate(request, ["src/pricing/m.py"], cfg))
    assert gate.validate(pr(body("domain-critical", STANDARD, DOMAIN)), ["src/pricing/m.py"], cfg) == []


def test_standard_rejected_for_domain_files(cfg):
    errors = gate.validate(pr(body("standard", STANDARD, DOMAIN)), ["src/pricing/m.py"], cfg)
    assert any("below required domain-critical" in e for e in errors)


def test_domain_tests_only_change_needs_domain_evidence(cfg):
    errors = gate.validate(pr(body("light")), ["tests/test_pricing_model.py"], cfg)
    assert any("below required domain-critical" in e for e in errors)


def test_declaring_higher_category_is_enforced(cfg):
    errors = gate.validate(pr(body("domain-critical", STANDARD)), ["src/ui/p.py"], cfg)
    assert any("Numeric verification" in e for e in errors)


def test_custom_domain_evidence_labels():
    custom = config(domain_evidence={"section": "Failure-mode verification",
                                     "labels": ["Failure cases", "Recovery"]})
    evidence = "## Failure-mode verification\nFailure cases: t1\nRecovery: t2\n"
    assert gate.validate(pr(body("domain-critical", STANDARD, evidence)), ["src/ui/p.py"], custom) == []
    missing = evidence.replace("Recovery: t2\n", "")
    errors = gate.validate(pr(body("domain-critical", STANDARD, missing)), ["src/ui/p.py"], custom)
    assert any("Recovery" in e for e in errors)


# ---------- control ----------

@pytest.mark.parametrize("with_ids", [False, True])
def test_control_passes_with_and_without_requirement_ids(cfg, cfg_ids, with_ids):
    chosen = cfg_ids if with_ids else cfg
    assert gate.validate(pr(body("control-critical", control(with_ids))), ["gate/review_gate.py"], chosen) == []


def test_control_rejects_lower_declaration(cfg):
    errors = gate.validate(pr(body("standard", STANDARD)), ["src/auth/login.py"], cfg)
    assert any("below required control-critical" in e for e in errors)
    assert any("Risk and scope" in e for e in errors)


@pytest.mark.parametrize("old, new, expected", [
    ("Secrets: none accessed.", "Secrets:", "Security review needs Secrets"),
    ("- [x] SCOPE-2", "- [ ] SCOPE-2", "SCOPE-2"),
    ("Verdict: PENDING", "Verdict: LLM approved", "Checker state"),
    ("VERIFIED | PENDING |", "VERIFIED | PASS |", "Pending independent review"),
    ("| VERIFIED |", "| DONE |", "Maker state"),
])
def test_control_missing_or_misleading_metadata_fails(cfg, old, new, expected):
    request = pr(body("control-critical", control(False)).replace(old, new))
    assert any(expected in e for e in gate.validate(request, ["gate/review_gate.py"], cfg))


@pytest.mark.parametrize("ids, expected", [
    ("REQ-009", "valid requirement IDs"),
    ("REQ-001..REQ-002", "ranges"),
    ("REQ-001-REQ-002", "ranges"),
    ("REQ-001, REQ-002", "needs a criterion/evidence mapping"),
])
def test_requirement_id_rules(cfg_ids, ids, expected):
    request = pr(body("control-critical", control(True).replace("## Requirement IDs\nREQ-001\n",
                                                               f"## Requirement IDs\n{ids}\n")))
    assert any(expected in e for e in gate.validate(request, ["gate/review_gate.py"], cfg_ids))


def complete(text, number=12):
    return (text.replace("VERIFIED | PENDING |", "VERIFIED | PASS |")
            .replace("Verdict: PENDING", "Verdict: PASS")
            .replace("Checker: NOT ASSIGNED", "Checker: @checker")
            .replace("Checker context: NOT ASSIGNED", "Checker context: checker-run-1")
            .replace("Reviewed head: NOT RECORDED", f"Reviewed head: {SHA}")
            .replace("Review: NOT RECORDED", f"Review: {REPO}/pull/{number}#pullrequestreview-99"))


def test_completed_review_needs_distinct_context_head_and_link(cfg):
    text = complete(body("control-critical", control(False)))
    assert gate.validate(pr(text), ["gate/x.py"], cfg) == []
    same = text.replace("Checker context: checker-run-1", "Checker context: maker-run-1")
    assert any("distinct review context" in e for e in gate.validate(pr(same), ["gate/x.py"], cfg))
    stale = text.replace(f"Reviewed head: {SHA}", "Reviewed head: " + "b" * 40)
    assert any("current head" in e for e in gate.validate(pr(stale), ["gate/x.py"], cfg))
    other_pr = text.replace("/pull/12#", "/pull/99#")
    assert any("review link" in e for e in gate.validate(pr(other_pr), ["gate/x.py"], cfg))


def test_control_touching_domain_files_also_needs_domain_evidence(cfg):
    files = ["src/auth/login.py", "src/pricing/m.py"]
    text = body("control-critical", control(False))
    assert any("Hand-worked" in e for e in gate.validate(pr(text), files, cfg))
    assert gate.validate(pr(text + DOMAIN), files, cfg) == []


# ---------- config and CLI ----------

@pytest.mark.parametrize("override", [
    {"repo_url": "not-a-url"},
    {"allowed_bases": []},
    {"requirement_ids": {"pattern": r"REQ-\d{3}", "allowed": ["BAD-1"]}},
    {"scope_acknowledgements": -1},
    {"domain_evidence": {"section": "", "labels": []}},
])
def test_invalid_config_rejected(override):
    with pytest.raises(ValueError):
        config(**override)


def test_invalid_tier_order_or_light_default_rejected():
    tiers = copy.deepcopy(BASE_CONFIG["tiers"])
    tiers["default"] = "light"
    with pytest.raises(ValueError):
        config(tiers=tiers)
    tiers = copy.deepcopy(BASE_CONFIG["tiers"])
    tiers["order"] = ["standard", "light", "domain-critical", "control-critical"]
    with pytest.raises(ValueError):
        config(tiers=tiers)


def test_cli_exit_codes_and_body_is_data(tmp_path, capsys):
    cfg_path = tmp_path / "gate.json"
    cfg_path.write_text(json.dumps(BASE_CONFIG))
    event = tmp_path / "event.json"
    text = body("light") + "\n$(touch NEVER_CREATE) `arbitrary`\n"
    event.write_text(json.dumps({"pull_request": pr(text)}))
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/a.md\n")
    args = [str(event), "--config", str(cfg_path), "--changed-files", str(changed)]
    assert gate.main(args) == 0
    assert "minimum review category from changed paths: light" in capsys.readouterr().out
    changed.write_text("src/auth/login.py\n")
    assert gate.main(args) == 1
    assert gate.main([str(event), "--config", str(cfg_path)]) == 1  # strictest without a list
    event.write_text("not json")
    assert gate.main(args) == 2
    assert not (ROOT / "NEVER_CREATE").exists() and not Path("NEVER_CREATE").exists()


def test_repository_config_is_valid_and_protects_the_gate():
    repo_cfg = gate.load_config(ROOT / ".github/review-gate.json")
    for path in ("gate/review_gate.py", ".github/review-gate.json", "templates/review-gate.example.json",
                 "tests/test_review_gate.py", "LICENSE"):
        assert gate.file_tier(path, repo_cfg) == "control-critical", path
    assert gate.file_tier("docs/guide.md", repo_cfg) == "light"
    assert gate.file_tier("runner/agent_task_runner.py", repo_cfg) == "domain-critical"


@pytest.mark.parametrize("template, config_path, extra", [
    (".github/pull_request_template.md", ".github/review-gate.json", ()),
    ("templates/pull_request_template.md", "templates/review-gate.example.json", ("Requirement IDs",)),
])
def test_templates_contain_every_section(template, config_path, extra):
    text = (ROOT / template).read_text()
    domain_section = gate.load_config(ROOT / config_path)["domain_evidence"]["section"]
    for name in gate.COMMON_SECTIONS + gate.STANDARD_SECTIONS + gate.CONTROL_SECTIONS + extra + (domain_section,):
        assert f"## {name}\n" in text, name
    assert "Verdict: PENDING" in text and "Verdict: PASS" not in text


def test_example_config_is_valid():
    gate.load_config(ROOT / "templates/review-gate.example.json")


@pytest.mark.parametrize("result", ["PASSING", "FAILURE", "XFAILED", "SKIPPED", "NOT RUNNING"])
def test_result_prefix_must_be_an_exact_word(cfg, result):
    text = body("standard", STANDARD).replace("PASS: 4 passed", result)
    assert any("Acceptance result" in e for e in gate.validate(pr(text), ["src/ui/p.py"], cfg))


@pytest.mark.parametrize("value", ["N/A", "n/a: --", "N/A."])
def test_bare_na_domain_and_security_evidence_fails(cfg, value):
    text = body("domain-critical", STANDARD, DOMAIN).replace(
        "Hand-worked: tests/test_pricing.py::test_hand_worked", "Hand-worked: " + value)
    assert any("Hand-worked" in e for e in gate.validate(pr(text), ["src/pricing/m.py"], cfg))
    text = body("control-critical", control(False)).replace("Secrets: none accessed.", "Secrets: " + value)
    assert any("Security review needs Secrets" in e for e in gate.validate(pr(text), ["gate/x.py"], cfg))


def test_duplicate_domain_labels_fail(cfg):
    text = body("domain-critical", STANDARD, DOMAIN) + "Hand-worked: duplicate\n"
    assert any("exactly once" in e for e in gate.validate(pr(text), ["src/pricing/m.py"], cfg))


def test_justified_na_evidence_passes(cfg):
    text = body("domain-critical", STANDARD, DOMAIN).replace(
        "Hand-worked: tests/test_pricing.py::test_hand_worked", "Hand-worked: N/A: no calculation changed")
    assert gate.validate(pr(text), ["src/pricing/m.py"], cfg) == []


def test_control_rule_overlap_does_not_hide_domain_evidence(cfg):
    cfg["tiers"]["rules"][0]["patterns"].append("src/pricing/*")
    text = body("control-critical", control(False))
    assert any("Hand-worked" in e for e in gate.validate(pr(text), ["src/pricing/m.py"], cfg))


def test_underdeclared_category_names_configured_patterns_without_changed_paths(cfg):
    path = "src/auth/PRIVATE-FILENAME.py"
    errors = gate.validate(pr(body("light")), [path], cfg)
    error = next(e for e in errors if "below required" in e)
    assert 'configured patterns: ["src/auth/*"]' in error
    assert "PRIVATE-FILENAME" not in "\n".join(errors)


def test_category_source_explains_default_and_missing_paths(cfg):
    assert gate.category_source(["unknown/file.py"], "standard", cfg) == (
        "configured default (unmatched paths)")
    for paths in (None, []):
        errors = gate.validate(pr(body("light")), paths, cfg)
        assert any("below required control-critical (from empty or unavailable changed-file list)"
                   in e for e in errors)


def test_category_source_lists_only_highest_matching_patterns(cfg):
    paths = ["docs/a.md", "src/auth/hidden.py", "gate/hidden.py"]
    assert gate.category_source(paths, "control-critical", cfg) == (
        'configured patterns: ["gate/*", "src/auth/*"]')


def test_category_source_is_deduplicated_bounded_and_json_escaped(cfg):
    cfg["tiers"]["rules"][0]["patterns"] = ["*", "*", "g*", "ga*", "gat*", "gate*", "gate/*"]
    source = gate.category_source(["gate/hidden.py"], "control-critical", cfg)
    assert source == 'configured patterns: ["*", "g*", "ga*", "gat*", "gate*"] (+1 more)'
    cfg["tiers"]["rules"][0]["patterns"] = ['gate/"*']
    assert gate.category_source(['gate/"hidden.py'], "control-critical", cfg) == (
        'configured patterns: ["gate/\\\"*"]')


def test_cli_category_error_never_echoes_sensitive_filename(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(BASE_CONFIG))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": pr(body("light"))}))
    changed = tmp_path / "paths.txt"
    changed.write_text("src/auth/SENSITIVE-FILENAME.py\n")
    assert gate.main([str(event), "--config", str(cfg_path), "--changed-files", str(changed)]) == 1
    output = capsys.readouterr().out
    assert "src/auth/*" in output
    assert "SENSITIVE-FILENAME" not in output
