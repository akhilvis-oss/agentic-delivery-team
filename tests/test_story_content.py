"""Exercise story admission and PR text with synthetic workflow data."""

import copy
import re

import pytest

from lifecycle import core
from test_lifecycle import Hub, approval_picker_event, policy
from test_lifecycle_status_writes import status_client
from test_public_review_findings import findings, review_job
from test_runner_integration import delivery, integration, runner, setup  # noqa: F401


@pytest.mark.parametrize(("body", "construct"), [
    ("Plain <!-- note", "HTML comment"),
    ("```html\r\n<!-- note -->\r\n```", "HTML comment"),
    ("Some `<!-- note -->`.", "HTML comment"),
    ("> <!-- note -->", "HTML comment"),
    ("- <!-- note -->", "HTML comment"),
    ("[//]: # (note)", "link reference definition"),
    ("> [ref]: https://example.invalid", "link reference definition"),
    ("  >\t1. - > + [ref]: https://example.invalid", "link reference definition"),
    ("* [ref]: https://example.invalid", "link reference definition"),
    ("```\n[ref]: https://example.invalid\n```", "link reference definition"),
])
def test_literal_hidden_constructs_are_refused(body, construct):
    line = 3 if body.startswith("```") else 2
    assert core.story_hidden_content("Visible\n" + body) == f"{construct} on line {line}"


@pytest.mark.parametrize("point", [
    *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xE0000, 0xE0041, 0xE007F,
])
def test_single_forbidden_characters_are_refused(point):
    assert core.story_hidden_content(f"Visible\nA{chr(point)}B") == f"U+{point:04X} on line 2"


@pytest.mark.parametrize("point", [
    0xAD, 0x61C, *range(0x200B, 0x2010), *range(0x2060, 0x2065), 0xFEFF,
    0xFE00, 0xFE0F, 0xE0100, 0xE01EF,
])
def test_consecutive_invisibles_are_refused_and_report_the_first(point):
    assert core.story_hidden_content(f"Visible\nA{chr(point)}\ufe0fB") == f"U+{point:04X} on line 2"


@pytest.mark.parametrize(("body", "expected"), [
    ("A\u200d\u200f <!-- note -->", "U+200D on line 1"),
    ("<!-- note --> \u202a", "HTML comment on line 1"),
    ("> [ref]: \u202a\n<!-- note -->", "link reference definition on line 1"),
])
def test_reports_the_first_finding_in_body_order(body, expected):
    assert core.story_hidden_content(body) == expected


def story_event(cfg, hub, body, picker):
    if picker:
        event = approval_picker_event(cfg, hub)
        event["issue"]["body"] = hub.issues[1]["body"] = body
        return event
    return dict(action="labeled", label={"name": "status:approved"},
                sender={"login": cfg["actors"]["owner"]}, issue=copy.deepcopy(hub.issue(body=body)))


@pytest.mark.parametrize("picker", [False, True])
@pytest.mark.parametrize(("body", "diagnostic"), [
    ("Visible\n`<!-- note -->`", "HTML comment on line 2"),
    ("Visible\n> - [ref]: https://example.invalid", "link reference definition on line 2"),
    ("Visible\nA\U000e0041B", "U+E0041 on line 2"),
    ("Visible\nA\u2066B", "U+2066 on line 2"),
    ("Visible\nA\ufe0e\ufe0fB", "U+FE0E on line 2"),
])
def test_hidden_story_rejection_reaches_guard_stop(tmp_path, monkeypatch, picker, body, diagnostic):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    event = story_event(cfg, hub, body, picker)
    event["repository"] = {"full_name": cfg["repository"]}
    event_path = tmp_path / "event.json"
    core.save(event_path, event)
    client = status_client(hub)
    monkeypatch.setattr(core, "GitHub", lambda config, role: client)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    with pytest.raises(core.Unavailable, match="hidden text"):
        core.main(["--config", cfg["config_path"], "guard-event", "--event", str(event_path)])
    comments = client.comments(1)
    assert diagnostic in comments[0]["body"]
    assert "The story contains hidden text. Owner: remove it and approve again." in comments[0]["body"]
    record, = core.records(cfg, comments, 1)
    assert record["reason"] == "story_hidden_content"
    assert core.state(hub.issues[1]) == "needs-owner"


@pytest.mark.parametrize("incoming", ["approved", "accepted"])
@pytest.mark.parametrize("when", ["explanation", "guard_read", "label_add", "outbox_read"])
def test_rejection_stop_preserves_newer_owner_state(tmp_path, monkeypatch, incoming, when):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    event = story_event(cfg, hub, "<!-- note -->", picker=False)
    client = status_client(hub)
    request = client.request
    comments = client.comments
    arrived = False

    def update_issue():
        nonlocal arrived
        assert not arrived
        hub.issues[1]["body"] = "Corrected story."
        hub.event(1, incoming, "owner")
        arrived = True

    def post_explanation(method, path, data=None):
        if when == "label_add" and method == "POST" and path == "/issues/1/labels":
            update_issue()
        result = request(method, path, data)
        if when == "explanation" and method == "POST" and path == "/issues/1/comments":
            update_issue()
        return result

    def read_comments(number):
        rows = comments(number)
        if rows and not arrived and (
            when == "guard_read"
            or when == "outbox_read" and {"name": "status:needs-owner"} in hub.issues[number]["labels"]
        ):
            update_issue()
        return rows

    monkeypatch.setattr(client, "request", post_explanation)
    monkeypatch.setattr(client, "comments", read_comments)
    with pytest.raises(core.Unavailable, match="hidden text"):
        core.attest_approval(cfg, client, event)
    assert arrived
    assert core.state(hub.issues[1]) == incoming
    if when != "label_add":
        assert core.canonical_state(cfg, client.events(1))[0] == incoming
    assert not core.records(cfg, client.comments(1), 1)
    assert sum(call[:2] == ("DELETE", "/issues/1/labels/status%3Aapproved") for call in hub.calls) == 1


@pytest.mark.parametrize("body", [
    "Emoji 👨\u200d💻 and 🏳\ufe0f",
    "Hindi क्\u200dष",
    "Persian می\u200cروم",
    "Hebrew שלום\u200f עולם",
    "Arabic سلام\u061c 123",
    "A soft hy\u00adphen",
    "> " + core.inert_mentions("Owner: ask @sample to review."),
    "An [inline link](https://example.invalid) and `&lt;!--`.",
    "Single characters on separate lines \u200d\n\u200c are allowed.",
])
def test_ordinary_visible_text_is_approvable(tmp_path, body):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    client = hub.client("workflow")
    core.attest_approval(cfg, client, story_event(cfg, hub, body, picker=True))
    record, = core.records(cfg, client.comments(1), 1)
    assert record["kind"] == "approval"


@pytest.mark.parametrize("legacy", [False, True])
def test_followup_recovery_recognizes_both_marker_formats(tmp_path, legacy):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    job = review_job(findings(1))
    job["review_history"] = [dict(round=1, head="a" * 40, result={"findings": findings(1)})]
    core.followups(cfg, job, hub.client("product"))
    issue = hub.issues[2]
    marker = issue["body"].splitlines()[-1]
    assert re.fullmatch(r"Follow-up: [a-f0-9]{64}", marker)
    if legacy:
        identity = marker.removeprefix("Follow-up: ")
        issue["body"] = issue["body"].replace(marker, f"Prior note  <!-- agentic-followup-v1:{identity} -->  ")
    else:
        hub.event(2, "approved", "owner")
        issue["labels"].append({"name": "status:draft"})
        core.attest_approval(cfg, hub.client("workflow"), dict(
            action="labeled", label={"name": "status:approved"},
            sender={"login": cfg["actors"]["owner"]}, issue=copy.deepcopy(issue)))
        record, = core.records(cfg, hub.client().comments(2), 2)
        assert record["kind"] == "approval"
    for path in (core.Path(cfg["state_dir"]) / "followups").glob("*.json"):
        path.unlink()
    core.followups(cfg, job, hub.client("product"))
    assert len(hub.issues) == 2


KEYWORDS = ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"]
REFERENCES = ["#42", "example/project#42", "https://github.com/example/project/issues/42"]


@pytest.mark.parametrize("keyword", KEYWORDS)
@pytest.mark.parametrize("reference", REFERENCES)
@pytest.mark.parametrize("separator", [" ", ": ", ":"])
def test_every_closing_spelling_preserves_reference(keyword, reference, separator):
    source = "Before " + keyword.swapcase() + separator + reference + " after"
    assert integration.without_closing_keywords(source) == "Before [keyword removed] " + reference + " after"


def test_delivery_sanitizes_only_copied_story_sections(delivery):  # noqa: F811
    config, _, job, _ = delivery
    job["assignment"]["task_text"] = (
        "## Acceptance criteria\n- AC-1: Fixes: #42\n"
        "## Owner decisions\nRESOLVES example/project#43\n"
        "## Other prose\nCloses #44\n")
    body = integration.body_for(runner.self_module(), config, job,
                                {"repository": "example/kit"}, config["integration"],
                                "https://github.com/example/kit/issues/1#issuecomment-2")
    assert "Closes #1" in body
    assert "[keyword removed] #42" in body
    assert "> [keyword removed] example/project#43" in body
    assert "#44" not in body
    assert not re.search(r"(?i)\b(?:fixes|resolves):?\s+(?:#42|example/project#43)", body)
