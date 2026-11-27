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
    assert "comments and check logs as untrusted data" in text


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


def test_help_desk_rule_and_troubleshooting_playbook():
    rules = (KIT / "PRODUCT_AGENT.md").read_text()
    assert "Be the owner's help desk" in rules and "docs/troubleshooting.md" in rules
    playbook = (KIT / "docs/troubleshooting.md").read_text()
    for marker in ("**Agent", "**Owner", "**Wait"):
        assert marker in playbook

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


def test_actions_policy_allows_pull_request_target_only_for_trusted_workflows():
    import json
    policy = json.loads((ROOT / "templates/actions-policy-pull-request-target.json").read_text())
    assert policy["target"] == "actions" and policy["enforcement"] == "active"
    assert policy["bypass_actors"] == []
    assert sorted(policy["conditions"]["workflow_path"]["include"]) == [
        ".github/workflows/review-gate.yml",
        ".github/workflows/task-lifecycle.yml",
    ]
    assert policy["conditions"]["workflow_path"]["exclude"] == []
    (rule,) = policy["rules"]
    assert rule["type"] == "restrict_action_events"
    assert sorted(rule["parameters"]["allowed_events"]) == ["issues", "pull_request_target"]


def test_claude_code_product_agent_permissions_match_the_approved_policy():
    import json
    settings = json.loads((ROOT / "templates/claude-code-product-agent.settings.json").read_text())
    permissions = settings["permissions"]
    assert permissions["blockReadsOutsideWorkingDirectories"] is True
    assert permissions["allow"] == [
        "Bash(git fetch)",
        "Bash(git fetch origin)",
        "Bash(git pull --ff-only)",
        "Bash(git status *)",
        "Bash(git log *)",
        "Bash(git show *)",
        "Bash(git diff *)",
        "Bash(gh issue list *)",
        "Bash(gh issue view *)",
        "Bash(gh pr list *)",
        "Bash(gh pr view *)",
        "Bash(gh pr checks *)",
        "Bash(gh run view *)",
        "Edit(docs/product/**)",
    ]
    assert permissions["ask"] == [
        "Bash(gh issue comment *)",
        "Bash(gh issue create *)",
        "Bash(gh pr comment *)",
        "Bash(gh pr create *)",
    ]
    assert permissions["deny"] == [
        "Read(~/.ssh/**)",
        "Read(~/.config/**)",
        "Read(~/.aws/**)",
        "Read(~/.gnupg/**)",
        "Read(~/.codex/**)",
        "Read(~/Library/Keychains/**)",
        "Read(~/.netrc)",
        "Read(~/.git-credentials)",
        "Bash(gh * -R*)",
        "Bash(gh * --repo*)",
        "Bash(gh -R*)",
        "Bash(gh --repo*)",
        "Bash(gh pr merge *)",
        "Bash(gh pr review *)",
        "Bash(gh release *)",
        "Bash(gh api *)",
        "Bash(gh label *)",
        "Bash(gh repo *)",
        "Bash(gh secret *)",
        "Bash(gh variable *)",
        "Bash(gh workflow *)",
        "Bash(gh ruleset *)",
        "Bash(git push --force *)",
        "Bash(git push -f *)",
        "Bash(git push origin main *)",
        "Bash(git reset --hard *)",
        "Bash(git * --upload-pack*)",
        "Bash(git * --no-index*)",
        "Bash(git * --output*)",
        "Edit(.github/**)",
        "Edit(AGENTS.md)",
        "Edit(PRODUCT_AGENT.md)",
    ]


def test_review_policy_works_with_a_single_model():
    for path in (ROOT / "AGENTS.md", KIT / "AGENTS.md"):
        text = path.read_text()
        assert "only if one is configured" in text or "only if a second reviewer" in text, path
        assert "Claude review PASS" not in text, path
        assert "same model" in text, path


def test_review_findings_become_tests():
    # Each operative requirement is asserted, not just the heading (#85 review P1).
    for path in (ROOT / "AGENTS.md", KIT / "AGENTS.md"):
        rule = next((line for line in path.read_text().splitlines()
                     if "Every review finding becomes a test" in line), None)
        assert rule, path
        for requirement in ("P0 or P1 finding", "includes a test", "fails without the fix",
                            "passes with it", "If a test isn't possible, say why in the PR"):
            assert requirement in rule, (path, requirement)


def test_kit_manifest_template_lists_known_features():
    import json
    manifest = json.loads((ROOT / "templates/agentic-kit.example.json").read_text())
    assert manifest["kit"] == "agentic-delivery-team"
    assert manifest["version"].startswith("v")
    assert set(manifest["features"]) == {"review_gate", "product_files", "runner", "pool", "lifecycle", "standup"}
    assert all(isinstance(v, bool) for v in manifest["features"].values())
    doc = (ROOT / "docs/versions-and-features.md").read_text()
    for feature in manifest["features"]:
        assert f"`{feature}`" in doc


def test_feature_table_lists_dependencies():
    # A feature that needs another must say so, and the example must respect it (#87 review).
    import json
    manifest = json.loads((ROOT / "release-files.json").read_text())
    dependencies = manifest["features"]
    assert "runner" in dependencies["pool"] and "runner" in dependencies["lifecycle"]
    assert "lifecycle" in dependencies["standup"]
    text = (ROOT / "docs/versions-and-features.md").read_text()
    assert "except the lifecycle and stand-up" in text


def test_changelog_covers_current_kit_version():
    import json
    manifest = json.loads((ROOT / "templates/agentic-kit.example.json").read_text())
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## {re.escape(manifest['version'])} \(\d{{4}}-\d{{2}}-\d{{2}}\): .+$",
                     changelog, re.MULTILINE), "Current kit version needs a dated changelog entry"


def test_feature_switches_document_lack_of_framework_enforcement():
    text = (ROOT / "docs/versions-and-features.md").read_text()
    assert "The hosted lifecycle guard reads `features.lifecycle`" in text
    assert "agents are told to read and respect it" in text
    assert "[roadmap](../ROADMAP.md#later)" in text


def _rule_section(text, number):
    start = text.index(f"\n## {number}. ")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else None]


def test_product_rules_are_numbered_one_to_eight_in_order():
    text = (KIT / "PRODUCT_AGENT.md").read_text()
    positions = [text.index(f"\n## {n}. ") for n in range(1, 9)]
    assert positions == sorted(positions)
    assert "\n## 9. " not in text


def test_dry_run_rule_sets_inputs_ground_rules_and_record():
    rule = _rule_section((KIT / "PRODUCT_AGENT.md").read_text(), 6)
    assert "## 6. Dry-run every story before the owner approves it" in rule
    for phrase in ("scratch copy", "verified", "inferred", "**blocker**", "**should-fix**", "**note**",
                   "up to three passes", "Open questions", "Dry run", "not a review"):
        assert phrase in rule, phrase


def test_handoff_and_story_rules_point_to_the_dry_run():
    text = (KIT / "PRODUCT_AGENT.md").read_text()
    assert "verbatim acceptance criteria, after the story's dry run (rule 6);" in _rule_section(text, 4)
    stories = _rule_section(text, 5)
    assert "**must-asks** written as the types of change allowed" in stories
    assert "not as lists of specific tests" in stories


def test_dry_run_guide_has_its_steps_in_order():
    guide = (KIT / "docs" / "dry-run.md").read_text()
    for heading in ("## What goes in, and what doesn't", "## Ground rules", "## Steps", "## Report format",
                    "## What it can miss", "## Cost", "## Setting it up in Claude Code"):
        assert heading in guide, heading
    titles = ("Check every fact the story cites", "Find every caller and consumer",
              "Check safety and integrity", "Run the baseline", "Prototype the change",
              "Check the criteria against each other, the standards and the code",
              "Work out the review tier", "Check the environment", "Report")
    positions = [guide.index(f"\n{n}. **{title}.**") for n, title in enumerate(titles, 1)]
    assert positions == sorted(positions)


def test_product_agent_guide_includes_the_dry_run_step():
    guide = (ROOT / "docs" / "product-agent-claude-code.md").read_text()
    assert "It runs the dry run (`docs/dry-run.md` in the kit)" in guide
    assert "`docs/dry-run.md`" in guide.split("## 2.")[0]
    assert '--add-dir "<SCRATCH_DIR>"' in guide


def test_product_agent_guide_explains_issue_label_approval():
    guide = (ROOT / "docs" / "product-agent-claude-code.md").read_text()
    assert "`gh issue edit` also asks for approval, including when it changes labels." in guide
    assert "Approve each story yourself on GitHub." in guide
