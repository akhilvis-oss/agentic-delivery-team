"""Fail-closed Markdown boundaries for the evidence-only PR body editor."""

from types import SimpleNamespace

import pytest

import test_runner as fixtures


BASE = "## Test evidence\nold\n\n## Acceptance evidence\nold\n\n## Security review\nfixed\n"


@pytest.mark.parametrize("heading", [
    "Security review\n---------------",
    "Security review\n===============",
    *[" " * indent + "#" * level + " Security review ###"
      for level in range(1, 7) for indent in (0, 3)],
])
def test_evidence_edit_refuses_inserted_rendered_heading(heading):
    api = fixtures.runner.integration_api()
    sent = []
    new = BASE.replace("## Security review", heading + "\nChanged authority.\n\n## Security review")
    with pytest.raises(ValueError):
        api.edit_pr_body(SimpleNamespace(request=lambda *args: sent.append(args)),
                         {"integration": {"pr": 9, "body": BASE}}, 9, {"body": new}, "standard")
    assert sent == []


@pytest.mark.parametrize("ambiguous", [
    "```\nSecurity review\n---",
    "~~~text\n# Security review\n````",
    "```bad`info\nSecurity review\n---\n```",
    "    # Security review",
    "\t# Security review",
    "> Security review\n> ---",
    "- ## Security review",
    "1. # Security review",
    "- - ## Security review",
    "- ```\n  ## Test evidence\n  hidden\n  ```",
    "- - ~~~\n    # Security review\n    ~~~",
    "- Security review\n  ---",
    "First paragraph line\nSecurity review\n---",
    "<!-- hidden boundary\n## Security review\n-->",
    "<h2>Security review</h2>",
    "<div>\nSecurity review\n---\n</div>",
    "Security review\\\n---",
    "Security review\v---",
    "Security review\x85---",
    "Security review\u2028---",
    "Security review\u2029---",
])
def test_evidence_edit_refuses_ambiguous_markdown_context(ambiguous):
    api = fixtures.runner.integration_api()
    sent = []
    new = BASE.replace("## Acceptance evidence\nold", "## Acceptance evidence\nold\n\n" + ambiguous)
    with pytest.raises(ValueError):
        api.edit_pr_body(SimpleNamespace(request=lambda *args: sent.append(args)),
                         {"integration": {"pr": 9, "body": BASE}}, 9, {"body": new}, "standard")
    assert sent == []


@pytest.mark.parametrize("line_end", ["\n", "\r\n", "\r"])
def test_setext_boundaries_preserve_protected_sections_and_allow_evidence(line_end):
    api = fixtures.runner.integration_api()
    old = "Test evidence\n---\nold\n\nSecurity review\n===\nfixed\n".replace("\n", line_end)
    new = old.replace("old", "new")
    sent = []
    api.edit_pr_body(SimpleNamespace(request=lambda *args: sent.append(args)),
                     {"integration": {"pr": 9, "body": old}}, 9, {"body": new}, "standard")
    assert sent == [("PATCH", "/pulls/9", {"body": new})]
    with pytest.raises(ValueError):
        api.edit_pr_body(SimpleNamespace(request=lambda *args: None),
                         {"integration": {"pr": 9, "body": old}}, 9,
                         {"body": old.replace("fixed", "changed")}, "standard")


@pytest.mark.parametrize("opening,closing", [("```json", "```"), ("~~~~text", "~~~~~")])
def test_evidence_fenced_literals_are_not_rendered_heading_boundaries(opening, closing):
    api = fixtures.runner.integration_api()
    old = BASE.replace("## Test evidence\nold", "## Test evidence\n" + opening +
                       "\nSecurity review\n---\n# Code heading\nold\n" + closing)
    new = old.replace("old", "new", 1)
    sent = []
    api.edit_pr_body(SimpleNamespace(request=lambda *args: sent.append(args)),
                     {"integration": {"pr": 9, "body": old}}, 9, {"body": new}, "standard")
    assert sent == [("PATCH", "/pulls/9", {"body": new})]


@pytest.mark.parametrize("space", ["\u00a0", "\u2003"])
def test_unicode_space_is_not_a_markdown_blank_paragraph(space):
    api = fixtures.runner.integration_api()
    with pytest.raises(ValueError, match="Setext"):
        api.evidence_sections(space + "\nTest evidence\n---\nold\n")
