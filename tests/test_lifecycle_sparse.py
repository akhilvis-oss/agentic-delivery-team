"""Exercise the hosted guard using only its declared checkout, without network."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from lifecycle import core
from test_lifecycle import policy

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("templates/task-lifecycle.yml", ".github/workflows/task-lifecycle.yml")


def sparse_paths(workflow):
    text = (ROOT / workflow).read_text()
    block = re.search(r"(?m)^( +)sparse-checkout: \|\n((?:\1 +[^\n]+\n)+)", text)
    assert block, "Workflow must declare a literal sparse checkout"
    return [line.strip().strip("/") for line in block[2].splitlines()]


def checkout(tmp_path, workflow):
    target = tmp_path / "checkout"
    target.mkdir()
    for name in sparse_paths(workflow):
        source, dest = ROOT / name, target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(source, dest)
    return target


def payload(tmp_path):
    cfg = policy(tmp_path)
    comments = []
    for index, details in enumerate([
        {"stop_details": {"summary": "Builder stopped for clarification.", "findings": [
            {"severity": "P1", "criterion": "AC-1", "detail": "Expected behavior is unclear."}]}},
        {"owner_question": "May I add the optional dependency?"},
    ]):
        record = core.public_record(cfg, 1, f"sparse-{index}", 1, "needs-owner", **details)
        record.update(actor=cfg["actors"]["runner"], **{"from": "in-progress"})
        comments.append(dict(user={"login": record["actor"]}, body=core.record_body(record)))
    return dict(cfg=cfg, comments=comments)


PARSE = '''
namespace = runpy.run_path("lifecycle/core.py", run_name="sparse_guard")
data = json.loads(sys.stdin.read())
records = namespace["records"](data["cfg"], data["comments"], 1)
assert len(records) == 2
assert records[0]["stop_details"]["summary"] == "Builder stopped for clarification."
assert records[0]["stop_details"]["findings"][0]["detail"] == "Expected behavior is unclear."
assert records[1]["owner_question"] == "May I add the optional dependency?"
'''


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_sparse_checkout_parses_stop_details_and_owner_question(tmp_path, workflow):
    target = checkout(tmp_path, workflow)
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import runpy, json, sys\n" + PARSE],
        cwd=target, input=json.dumps(payload(tmp_path)), text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    direct = subprocess.run([sys.executable, "-I", "lifecycle/core.py", "--help"],
                            cwd=target, text=True, capture_output=True)
    assert direct.returncode == 0, direct.stderr


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_guard_imports_cannot_open_files_outside_sparse_checkout(tmp_path, workflow):
    target = checkout(tmp_path, workflow)
    # Monitor all transitive imports and path-based loads, allowing only the
    # interpreter's standard library and the exact checkout. Failed opens count.
    audit = '''
import runpy, json, sys
from pathlib import Path
root = Path.cwd().resolve()
stdlib = Path(sys.base_prefix).resolve()
allowed = [(root / name, recursive) for name, recursive in json.loads(sys.argv[1])]
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0]).resolve()
        assert path.is_relative_to(stdlib) or any(
            path == base or recursive and path.is_relative_to(base)
            for base, recursive in allowed
        ), "Outside sparse checkout"
sys.addaudithook(audit)
'''
    allowed = [(name, (ROOT / name).is_dir()) for name in sparse_paths(workflow)]
    result = subprocess.run([sys.executable, "-I", "-c", audit + PARSE, json.dumps(allowed)], cwd=target,
                            input=json.dumps(payload(tmp_path)), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_missing_rules_stops_guard_with_plain_message(tmp_path, workflow):
    target = checkout(tmp_path, workflow)
    (target / "lifecycle/private_content.py").unlink()
    result = subprocess.run(
        [sys.executable, "-I", "lifecycle/core.py", "--config",
         ".github/task-lifecycle.json", "guard-event", "--event", "absent.json"],
        cwd=target, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        "Lifecycle guard unavailable: cannot load lifecycle/private_content.py; "
        "restore the lifecycle package from the same kit release.\n"
    )


def test_upgrade_inventory_exists_and_matches_guard_checkout():
    inventory = json.loads((ROOT / "release-files.json").read_text())
    destinations = {entry["destination"] for entry in inventory["entries"]
                    if entry["kind"] in {"framework", "adopter"}}
    for entry in inventory["entries"]:
        assert (ROOT / entry["source"]).exists()
    for workflow in WORKFLOWS:
        for path in sparse_paths(workflow):
            assert path in destinations or any(name.startswith(path + "/") for name in destinations)
    assert "lifecycle/private_content.py" in destinations
    manifest = json.loads((ROOT / "templates/agentic-kit.example.json").read_text())
    example = (ROOT / "docs/versions-and-features.md").read_text().split("```json\n")[1].split("```")[0]
    assert json.loads(example) == manifest
    assert manifest["version"] == "v0.5.6"
