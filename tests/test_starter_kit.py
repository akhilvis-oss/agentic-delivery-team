"""Starter kit integrity: links resolve, placeholders are explicit, and key rules are present."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "starter-kit"
MARKDOWN = sorted(KIT.rglob("*.md")) + [ROOT / "CHANGELOG.md", ROOT / "SECURITY.md", ROOT / "CONTRIBUTING.md"]


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path):
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", path.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (path.parent / target).resolve().exists(), f"{path.name}: broken link {target}"


def test_kit_lists_every_file_it_ships():
    readme = (KIT / "README.md").read_text()
    for path in KIT.rglob("*"):
        if path.is_file() and path.name != "README.md":
            assert str(path.relative_to(KIT)) in readme, f"README does not list {path.relative_to(KIT)}"


def test_agents_template_keeps_core_rules():
    text = (KIT / "AGENTS.md").read_text()
    for heading in ("## Repository boundaries", "## Source of truth", "## Assumptions and escalation",
                    "## Must-ask", "## Review", "## Merge policy", "## Reporting"):
        assert heading in text
    assert "**Owner only**" in text and "verbatim" in text
    assert "leave your changes uncommitted" in text


def test_session_rules_cover_read_state_win_handoff():
    text = (KIT / "PRODUCT_AGENT.md").read_text()
    for heading in ("Read the repository first", "State what you read", "The repository wins",
                    "Nothing agreed lives only in chat", "Shape delivery, not just features",
                    "critical regression set", "weight", "test scenarios"):
        assert heading in text


def test_story_template_requires_context_and_criteria():
    text = (KIT / ".github/ISSUE_TEMPLATE/story.yml").read_text()
    for field in ("id: story", "id: context", "id: criteria", "id: tests", "id: section", "id: milestone"):
        block = text.split(field, 1)[1].split("- type:", 1)[0]
        assert "required: true" in block, field
    assert "AC-1" in text


def test_placeholders_are_explicit():
    # Values adopters must replace are written as <UPPER_CASE> or <prose>, never left blank.
    agents = (KIT / "AGENTS.md").read_text()
    for placeholder in ("<PROJECT_NAME>", "<OWNER>/<REPOSITORY>", "<BUILD_COMMAND>", "<TEST_COMMAND>"):
        assert placeholder in agents


def test_roadmap_template_defines_sections_milestone_and_regression_set():
    text = (KIT / "docs/product/roadmap.md").read_text()
    for heading in ("## Sections", "## Milestones", "### M1:", "## Critical regression set"):
        assert heading in text
    assert "add up to 100%" in text


def test_branch_ruleset_template_requires_gate_and_blocks_bypass():
    import json
    ruleset = json.loads((ROOT / "templates/branch-ruleset.json").read_text())
    assert ruleset["target"] == "branch" and ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert ruleset["bypass_actors"] == []
    rules = {r["type"]: r.get("parameters", {}) for r in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= set(rules)
    checks = {c["context"] for c in rules["required_status_checks"]["required_status_checks"]}
    assert {"checks", "review-gate"} <= checks
    assert rules["required_status_checks"]["strict_required_status_checks_policy"] is True
