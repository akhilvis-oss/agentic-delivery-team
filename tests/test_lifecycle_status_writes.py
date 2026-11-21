"""Status writers preserve concurrent labels and retain guard restoration semantics."""

import ast
import copy
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
import urllib.error
from urllib.parse import unquote

import pytest

from lifecycle import core
from test_lifecycle import Hub, WHEN, policy

ROOT = Path(__file__).resolve().parents[1]


def literal(node):
    """Read a literal route, preserving dynamic slots without evaluating code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [literal(part) if isinstance(part, ast.Constant) else "{value}"
                 for part in node.values]
        return "".join(parts) if all(part is not None for part in parts) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left), literal(node.right)
        if left is None and right is None:
            return None
        return (left or "{value}") + (right or "{value}")
    return None


def status_write_violations(source, filename):
    """Fail closed on unreadable writes; exceptions name exact owners and routes."""
    violations = []

    class Scan(ast.NodeVisitor):
        def __init__(self):
            self.context = []

        def visit_FunctionDef(self, node):
            self.context.append(node.name)
            self.generic_visit(node)
            self.context.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Call(self, node):
            self.generic_visit(node)
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "request":
                return
            args = dict(zip(("method", "path", "data"), node.args))
            args.update({kw.arg: kw.value for kw in node.keywords})
            method, path = literal(args.get("method")), literal(args.get("path"))
            owner = ".".join(self.context)
            # The smoke client's inherited transport is the sole dynamic pass-through.
            if (filename == "lifecycle/smoke.py" and owner == "SmokeGitHub.request"
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "super"):
                return
            if method == "GET":
                return
            if method is None or "{value}" in method or path is None or not path.startswith("/"):
                violations.append((node.lineno, "unreadable method or path"))
                return
            if (path.startswith("/issues/") and "{value}" in path.split("/", 3)[-1]
                    and not re.match(r"/issues/(?:[0-9]+|\{value\})/(?:labels|comments|assignees)(?:/|$)", path)
                    and not re.fullmatch(r"/issues/\{value\}", path)
                    and not re.fullmatch(r"/issues/comments/(?:[0-9]+|\{value\})(?:/pin)?", path)):
                violations.append((node.lineno, "unreadable issue endpoint"))
                return
            labels_route = re.fullmatch(r"/issues/(?:[0-9]+|\{value\})/labels(?:/.*)?", path)
            issue_route = re.fullmatch(r"/issues(?:/(?:[0-9]+|\{value\}))?", path)
            data = args.get("data")
            keys = [literal(key) for key in data.keys] if isinstance(data, ast.Dict) else None
            writes = method in {"PUT", "POST", "DELETE"} and labels_route
            if method in {"POST", "PATCH"} and issue_route:
                # Unknown bodies can hide a labels key, including dict expansion.
                writes = keys is None or None in keys or "labels" in keys
            if not writes:
                return
            if filename == "lifecycle/core.py" and owner in {"set_status", "remove_status"}:
                return
            if (filename == "lifecycle/core.py" and owner == "followups"
                    and method == "POST" and path == "/issues"):
                return
            if filename == "lifecycle/smoke.py" and (
                owner == "approve_once" and method == "POST" and labels_route
                or owner == "run" and (
                    method == "POST" and path == "/issues"
                    or method == "DELETE" and path == "/issues/{value}/labels/status%3Aapproved"
                )
            ):
                return
            violations.append((node.lineno, "status label write outside its owners"))

    Scan().visit(ast.parse(source))
    return violations


def test_only_named_status_writers_in_lifecycle_and_runner():
    violations = {}
    for folder in ("lifecycle", "runner"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            name = path.relative_to(ROOT).as_posix()
            findings = status_write_violations(path.read_text(), name)
            if findings:
                violations[name] = findings
    assert not violations


@pytest.mark.parametrize("call", [
    'client.request("PUT", f"/issues/{number}/labels", {"labels": names})',
    'client.request("POST", "/issues/" + str(number) + "/labels", data)',
    'client.request("DELETE", f"/issues/{number}/labels/status%3Aapproved")',
    'client.request("POST", "/issues", {"labels": ["status:draft"]})',
    'client.request("PATCH", f"/issues/{number}", {"labels": labels})',
    'client.request("PATCH", f"/issues/{number}", {**payload})',
    'client.request(method, "/issues/1/labels", data)',
    'client.request(f"{method}", "/issues/1/labels", data)',
    'client.request("PO" + suffix, "/issues/1/labels", data)',
    'client.request("POST", path, data)',
    'client.request(path=path, method="DELETE")',
    'client.request("POST", f"{path}", data)',
    'client.request("POST", f"/issues/{number}/{endpoint}", data)',
])
def test_scan_rejects_new_or_unreadable_writes(call):
    assert status_write_violations(call, "lifecycle/new_writer.py")


@pytest.mark.parametrize("call", [
    'client.request("GET", path)',
    'client.request("POST", f"/issues/{number}/comments", {"body": text})',
    'client.request("PATCH", "/issues/1", {"state": "closed"})',
    'client.request("POST", "/issues", {"title": "Report"})',
])
def test_scan_allows_reads_and_writes_without_labels(call):
    assert not status_write_violations(call, "lifecycle/new_writer.py")


def status_client(hub, role="workflow", after_add=None, fail_remove=False, before_write=None):
    """Use the real scoped transport and writer with a synthetic GitHub server."""
    standin = hub.client(role)
    client = core.GitHub(hub.cfg, role, token="fake")
    client.identity_verified = True
    client.issue, client.events = standin.issue, standin.events
    client.comments, client.notify = standin.comments, standin.notify

    def opened(req, timeout):
        path = req.full_url.split("/repos/" + hub.cfg["repository"], 1)[1]
        data = json.loads(req.data) if req.data else None
        if req.method in {"POST", "PUT", "DELETE"} and "/labels" in path:
            hub.calls.append((req.method, path, copy.deepcopy(data)))
            number = int(path.split("/")[2])
            labels = hub.issues[number]["labels"]
            if req.method in {"POST", "PUT"}:
                if before_write:
                    before_write()
                if req.method == "PUT":
                    hub.issues[number]["labels"] = labels = []
                for name in data["labels"]:
                    if name not in {label["name"] for label in labels}:
                        labels.append({"name": name})
                        event("labeled", name)
                if after_add:
                    after_add()
            else:
                if fail_remove:
                    raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, None)
                name = unquote(path.rsplit("/", 1)[1])
                hub.issues[number]["labels"] = [x for x in labels if x["name"] != name]
                event("unlabeled", name)
            value = hub.issues[number]["labels"]
        else:
            value = standin.request(req.method, path, data)
        return io.BytesIO(json.dumps(value).encode())

    def event(kind, name):
        hub.counter += 1
        hub.events_by_issue[1].append(dict(
            id=hub.counter, event=kind, label={"name": name},
            actor={"login": hub.cfg["actors"][role]}, created_at=WHEN,
        ))

    client.opener = SimpleNamespace(open=opened)
    return client


def names(hub):
    return {label["name"] for label in hub.issues[1]["labels"]}


def test_set_state_adds_first_and_preserves_concurrent_non_status_label(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.issues[1]["labels"].append({"name": "priority:normal"})
    client = status_client(hub, before_write=lambda: hub.issues[1]["labels"].append(
        {"name": "priority:high"}))
    client.set_state(1, "blocked")
    assert names(hub) == {"status:blocked", "priority:normal", "priority:high"}
    assert [call[0] for call in hub.calls] == ["POST", "DELETE"]
    assert hub.calls[0][2] == {"labels": ["status:blocked"]}


def test_status_added_after_snapshot_is_left_for_guard(tmp_path):
    hub = Hub(policy(tmp_path))
    hub.issue()
    client = status_client(hub, after_add=lambda: hub.issues[1]["labels"].append(
        {"name": "status:needs-owner"}))
    core.set_status(client, 1, "blocked")
    assert names(hub) == {"status:blocked", "status:needs-owner"}


def test_target_already_present_only_removes_other_statuses(tmp_path):
    hub = Hub(policy(tmp_path))
    hub.issue()
    hub.issues[1]["labels"] += [{"name": "status:blocked"}, {"name": "priority:high"}]
    core.set_status(status_client(hub), 1, "approved")
    assert names(hub) == {"status:approved", "priority:high"}
    assert [call[:2] for call in hub.calls] == [("DELETE", "/issues/1/labels/status%3Ablocked")]


def test_remove_status_encodes_one_label_and_keeps_every_other_label(tmp_path):
    hub = Hub(policy(tmp_path))
    hub.issue()
    hub.issues[1]["labels"] += [{"name": "status:unknown name/#"}, {"name": "priority:high"}]
    core.remove_status(status_client(hub), 1, "unknown name/#")
    assert names(hub) == {"status:approved", "priority:high"}
    assert hub.calls == [("DELETE", "/issues/1/labels/status%3Aunknown%20name%2F%23", None)]


@pytest.mark.parametrize("name", ["..", "unsafe..name"])
def test_parent_sequence_stays_refused_before_transport(tmp_path, name):
    hub = Hub(policy(tmp_path))
    hub.issue()
    with pytest.raises(ValueError, match="Invalid repository API path"):
        core.remove_status(status_client(hub), 1, name)
    assert hub.calls == []


@pytest.mark.parametrize("path", [
    "/issues/1", "/labels", "/issues/1/labels/status%3A", "/issues/1/labels/priority%3Ahigh",
    "/issues/1/labels/status%3Aname%2fother", "/issues/1/labels/status%3Aname%XX",
])
def test_delete_allowlist_stays_scoped_to_encoded_status_labels(tmp_path, path):
    hub = Hub(policy(tmp_path))
    hub.issue()
    with pytest.raises(ValueError, match="Only repository issue/label writes"):
        status_client(hub).request("DELETE", path)
    assert hub.calls == []


def test_guard_restores_unknown_encoded_status_through_real_writer(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.approve()
    hub.issues[1]["labels"].append({"name": "status:unknown name"})
    client = status_client(hub)
    assert core.guard_issue(cfg, client, 1)
    assert names(hub) == {"status:approved"}
    assert ("DELETE", "/issues/1/labels/status%3Aunknown%20name", None) in hub.calls
    assert sum("The guard restored" in c["body"] for c in client.comments(1)) == 1


@pytest.mark.parametrize("mode", ["guard_during_add", "failed_removal"])
def test_two_label_moment_recovers_same_target_with_one_restoration(tmp_path, mode):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    guard = status_client(hub)
    observed = []

    def interleave():
        observed.append(names(hub))
        if mode == "guard_during_add":
            assert core.guard_issue(cfg, guard, 1)

    runner = status_client(hub, "runner", after_add=interleave,
                           fail_remove=mode == "failed_removal")
    if mode == "failed_removal":
        with pytest.raises(core.Unavailable):
            runner.set_state(1, "blocked")
        assert core.guard_issue(cfg, guard, 1)
    else:
        runner.set_state(1, "blocked")
    assert observed == [{"status:in-review", "status:blocked"}]
    assert names(hub) == {"status:blocked"}
    assert sum("The guard restored" in c["body"] for c in guard.comments(1)) == 1
    assert not core.guard_issue(cfg, guard, 1)


def test_restoration_readback_reuses_newest_matching_workflow_event(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    older = hub.event(1, "in-review", "workflow")
    newest = hub.event(1, "in-review", "workflow")
    hub.issues[1]["labels"].append({"name": "status:unknown name"})
    client = status_client(hub)
    assert core.guard_issue(cfg, client, 1)
    records = [json.loads(c["body"].split(core.PREFIX)[1].split(core.SUFFIX)[0])
               for c in client.comments(1) if "The guard restored" in c["body"]]
    assert len(records) == 1 and records[0]["event"] == newest["id"] != older["id"]
    assert not any(call[0] == "POST" and "/labels" in call[1] for call in hub.calls)
    assert hub.events_by_issue[1][-1]["event"] == "unlabeled"
    assert names(hub) == {"status:in-review"}


@pytest.mark.parametrize("readback", ["distractors", "outage"])
def test_restoration_readback_keeps_safe_none_without_a_match(tmp_path, readback):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    hub.issues[1]["labels"].append({"name": "status:unknown name"})
    client = status_client(hub)
    events = client.events
    reads = 0

    def read_events(number):
        nonlocal reads
        reads += 1
        history = events(number)
        if reads == 2:
            if readback == "outage":
                raise core.Unavailable("readback unavailable")
            return history + [
                dict(id=900, event="labeled", label={"name": "status:in-review"},
                     actor={"login": cfg["actors"]["runner"]}, created_at=WHEN),
                dict(id=901, event="labeled", label={"name": "status:blocked"},
                     actor={"login": cfg["actors"]["workflow"]}, created_at=WHEN),
                dict(id=902, event="unlabeled", label={"name": "status:in-review"},
                     actor={"login": cfg["actors"]["workflow"]}, created_at=WHEN),
            ]
        return history

    client.events = read_events
    assert core.guard_issue(cfg, client, 1)
    notice, = [c for c in client.comments(1) if "The guard restored" in c["body"]]
    record = json.loads(notice["body"].split(core.PREFIX)[1].split(core.SUFFIX)[0])
    assert record["event"] is None
    assert names(hub) == {"status:in-review"}
