"""Offline incremental polling and public status request-budget regressions."""

import copy
import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import urllib.error

import pytest

from lifecycle import core, service
from recorded_github import FIXTURES, RecordedGitHub
from test_lifecycle import Client, Hub, configured_service, policy


class SinceClient(Client):
    """A separate fake that models GitHub's updated-at since filter."""

    def __init__(self, hub):
        super().__init__(hub, "runner")
        self.listings = []
        self.event_reads = []
        self.issue_reads = []

    def pages(self, path):
        self.listings.append(path)
        rows = super().pages(path)
        since = parse_qs(urlsplit(path).query).get("since")
        return [i for i in rows if not since or i["updated_at"] >= since[0]]

    def events(self, number):
        self.event_reads.append(number)
        return super().events(number)

    def issue(self, number):
        self.issue_reads.append(number)
        return super().issue(number)


def idle(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    client = SinceClient(hub)
    service.poll(cfg, client, clock=lambda: 1000)
    hub.calls.clear()
    hub.issues[1]["updated_at"] = "1970-01-01T00:16:40Z"
    return cfg, hub, client


def test_changed_open_admission_only_and_overlap_anchor(tmp_path):
    cfg, hub, client = idle(tmp_path)
    for number, state in [(2, "approved"), (3, "needs-primary"), (4, "draft"),
                          (5, "approved"), (6, "needs-owner")]:
        item = hub.issue(number, state)
        item["updated_at"] = service.timestamp(1050).replace("+00:00", "Z")
    hub.issues[5]["state"] = "closed"
    # First full listing marks all eligible issues changed.
    service.poll(cfg, client, clock=lambda: 4600)
    assert client.event_reads == [2, 3]
    client.event_reads.clear()
    hub.issues[2]["updated_at"] = service.timestamp(5600).replace("+00:00", "Z")
    # Exclude unchanged rows despite the five-minute overlap.
    moments = iter([5500, 5500, 5500, 5500, 6100])
    service.poll(cfg, client, clock=lambda: next(moments))
    assert client.event_reads == [2]
    assert client.listings[-1] == "/issues?state=all&since=1970-01-01T01:11:40Z"
    # A build completing at 6100 must not move the anchor past its start at 5500.
    service.poll(cfg, client, clock=lambda: 6200)
    assert client.listings[-1] == "/issues?state=all&since=1970-01-01T01:26:40Z"


def test_hourly_full_replaces_cache_and_rechecks_admission(tmp_path):
    cfg, hub, client = idle(tmp_path)
    hub.issue(2, "approved")["updated_at"] = "1970-01-01T00:00:00Z"
    service.poll(cfg, client, clock=lambda: 1200)
    assert client.event_reads == []
    service.poll(cfg, client, clock=lambda: 4600)
    assert client.listings == ["/issues?state=all",
                               "/issues?state=all&since=1970-01-01T00:11:40Z",
                               "/issues?state=all"]
    assert client.event_reads == [2]
    del hub.issues[2]
    service.poll(cfg, client, clock=lambda: 8200)
    assert 2 not in cfg["_poll_cache"]["issues"]


def test_unchanged_claim_still_processed(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = SinceClient(hub)
    assert service.poll(cfg, client, run_jobs=False, clock=lambda: 2000000000) == [
        "issue-1-event-101"]
    client.event_reads.clear()
    client.issue_reads.clear()
    assert service.poll(cfg, client, run_jobs=False, clock=lambda: 2000000100) == [
        "issue-1-event-101"]
    assert client.event_reads == []
    assert client.issue_reads and set(client.issue_reads) == {1}


def test_idle_status_refresh_and_content_change(tmp_path):
    cfg, hub, client = idle(tmp_path)
    before = copy.deepcopy(hub.comments_by_issue)
    service.poll(cfg, client, clock=lambda: 1100)
    assert not [c for c in hub.calls if c[0] == "PATCH"]
    assert hub.comments_by_issue == before
    assert service.heartbeat(cfg) == 1100
    service.poll(cfg, client, clock=lambda: 1600)
    assert len([c for c in hub.calls if c[0] == "PATCH"]) == 1
    cfg["_upgrade_notice"] = "Synthetic upgrade notice"
    service.poll(cfg, client, clock=lambda: 1700)
    assert len([c for c in hub.calls if c[0] == "PATCH"]) == 2
    assert "last update" in hub.comments_by_issue[1][0]["body"]


@pytest.mark.parametrize("delay", [700, 2000])
def test_status_refresh_next_poll_after_long_interval(tmp_path, delay):
    cfg, hub, client = idle(tmp_path)
    cfg["poll_seconds"] = delay
    service.poll(cfg, client, clock=lambda: 1000 + delay)
    assert len([c for c in hub.calls if c[0] == "PATCH"]) == 1


def test_missing_status_and_edit_404_force_full_list(tmp_path, monkeypatch):
    cfg, hub, client = idle(tmp_path)
    cfg["_poll_cache"]["issues"].clear()
    service.poll(cfg, client, clock=lambda: 1100)
    assert client.listings[-1] == "/issues?state=all"
    request = client.request

    def deleted(method, path, data=None):
        if method == "PATCH":
            del hub.issues[1]
            hub.comments_by_issue[1].clear()
            try:
                raise urllib.error.HTTPError("https://example.invalid", 404, "gone", {}, None)
            except urllib.error.HTTPError:
                raise core.Unavailable("GitHub request rejected") from None
        return request(method, path, data)

    monkeypatch.setattr(client, "request", deleted)
    service.poll(cfg, client, clock=lambda: 1700)
    assert client.listings[-2:] == ["/issues?state=all&since=1970-01-01T00:13:20Z",
                                  "/issues?state=all"]
    assert not hub.issues
    monkeypatch.setattr(client, "request", request)
    service.poll(cfg, client, clock=lambda: 1800)
    assert len(hub.issues) == 1
    assert hub.comments_by_issue[1][0]["pin"]


def test_recorded_idle_poll_budget(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    cfg["repository"] = "akhilvis-oss/adt-smoke"
    comments = json.loads((FIXTURES / "smoke-status7-comments.json").read_text())
    cfg["actors"]["runner"] = comments[0]["user"]["login"]
    hub = Hub(cfg)
    status = hub.issue(7, "draft", "<!-- agentic-service-status-v1 -->")
    status["user"] = {"login": cfg["actors"]["runner"]}
    hub.comments_by_issue[7] = copy.deepcopy(comments)
    start = dt.datetime(2026, 11, 20, 7, 30, tzinfo=dt.timezone.utc).timestamp()
    service.poll(cfg, hub.client(), clock=lambda: start)
    cfg["token_env"] = "SYNTHETIC_POLL_TOKEN"
    monkeypatch.setenv("SYNTHETIC_POLL_TOKEN", "synthetic")
    client = core.GitHub(cfg, "runner")
    transport = RecordedGitHub(client, {
        ("GET", "/repos/akhilvis-oss/adt-smoke/issues?state=all&since=2026-11-20T07:25:00Z&per_page=100&page=1"):
            "smoke-idle-since-issues.json",
        ("GET", "/repos/akhilvis-oss/adt-smoke/issues/7/comments?per_page=100&page=1"):
            "smoke-status7-comments.json",
    })
    service.poll(cfg, client, clock=lambda: start + 60)
    assert len(transport.calls) <= 3
    assert len(transport.calls) == 2
    assert all(method == "GET" for method, _ in transport.calls)


def test_run_once_bypasses_cursor_and_lists_full(tmp_path):
    cfg, hub, client = idle(tmp_path)
    core.save(Path(cfg["state_dir"]) / "poll.json", {"next_poll": 9999999999})
    service.run_loop(cfg, client, "run-once")
    assert client.listings[-1] == "/issues?state=all"


def test_failed_poll_retains_last_completed_listing_anchor(tmp_path, monkeypatch):
    cfg, hub, client = idle(tmp_path)
    runner_module = service.runner_module

    def failed():
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(service, "runner_module", failed)
    with pytest.raises(RuntimeError, match="synthetic"):
        service.poll(cfg, client, clock=lambda: 2000)
    monkeypatch.setattr(service, "runner_module", runner_module)
    service.poll(cfg, client, clock=lambda: 2100)
    assert client.listings[-1] == "/issues?state=all&since=1970-01-01T00:11:40Z"


def test_owner_backfill_reads_only_changed_issues(tmp_path):
    cfg, hub, client = idle(tmp_path)
    for number in [2, 3]:
        issue = hub.issue(number, "needs-owner")
        issue.update(state="closed", updated_at="1970-01-01T00:00:00Z")
    service.poll(cfg, client, clock=lambda: 2000, full_list=True)
    reads = []
    original = client.comments

    def comments(number):
        reads.append(number)
        return original(number)

    client.comments = comments
    hub.issues[2]["updated_at"] = "1970-01-01T00:50:00Z"
    service.poll(cfg, client, clock=lambda: 3000)
    assert 2 in reads and 3 not in reads
