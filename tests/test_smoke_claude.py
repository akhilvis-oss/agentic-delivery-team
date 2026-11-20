"""Claude smoke contract: documented argv and offline runner stand-in only."""

import contextlib
import copy
import json
import sys

import pytest

from lifecycle import core, smoke
from test_runner import REAL_CHECK_CLI, runner, setup  # noqa: F401
from test_smoke import config


def documented_agent():
    text = (smoke.ROOT / "docs/smoke-test.md").read_text()
    section = text.split("<!-- claude-smoke-agent -->", 1)[1]
    return json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])


def assert_reviewer_allowlist(agent):
    assert agent["command"] == [
        "<ABSOLUTE_CLAUDE_EXECUTABLE>", "--bare", "--settings", "{agent_home}/settings.json",
        "--print", "--no-session-persistence", "--model", "{model}", "{role_flags}"]
    assert agent["reviewer_flags"] == [
        "--tools", "Read,Edit", "--permission-mode", "dontAsk",
        "--allowedTools", "Edit(/{output_file})"]
    assert agent["environment"] == {"CLAUDE_CONFIG_DIR": "{agent_home}"}


def test_claude_environment_is_accepted(tmp_path):
    cfg = config(tmp_path)
    cfg["runner"]["agent"]["environment"] = {"CLAUDE_CONFIG_DIR": "{agent_home}"}
    core.save(tmp_path / "claude.json", cfg)
    assert smoke.load_config(tmp_path / "claude.json") == cfg


@pytest.mark.parametrize("environment", [
    {"CLAUDE_CONFIG_DIR": "{agent_home}", "EXTRA": "fixture"},
    {"CLAUDE_CONFIG_DIR": "{agent_home}", "CODEX_HOME": "{agent_home}"},
    {"CLAUDE_CONFIG_DIR": "somewhere-else"},
    {"ANTHROPIC_API_KEY": "fixture-only"},
])
def test_claude_other_environments_stay_rejected(tmp_path, environment):
    cfg = config(tmp_path)
    cfg["runner"]["agent"]["environment"] = environment
    core.save(tmp_path / "invalid.json", cfg)
    with pytest.raises(ValueError):
        smoke.load_config(tmp_path / "invalid.json")


def test_documented_reviewer_allowlist():
    assert_reviewer_allowlist(documented_agent())


@pytest.mark.parametrize("flags", [
    ["--tools", "Read,Edit,Bash", "--permission-mode", "dontAsk", "--allowedTools", "Edit(/{output_file})"],
    ["--tools", "Read,Edit", "--permission-mode", "dontAsk", "--allowedTools", "Edit(//**)"],
    ["--tools", "Read,Edit", "--permission-mode", "acceptEdits", "--allowedTools", "Edit(/{output_file})"],
])
def test_reviewer_allowlist_rejects_broader_permissions(flags):
    agent = copy.deepcopy(documented_agent())
    agent["reviewer_flags"] = flags
    with pytest.raises(AssertionError):
        assert_reviewer_allowlist(agent)


def test_workspace_resolves_symlinked_temporary_parent(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    @contextlib.contextmanager
    def temporary_directory(**kwargs):
        yield str(alias)

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    with smoke.workspace({}) as (root, result):
        result["passed"] = True
        assert root == actual.resolve()
        # Candidate and runner output paths inherit this canonical root.
        assert (root / "runner-state/result.json").parent.parent == actual.resolve()


@pytest.mark.parametrize("mutate_review", [False, True])
def test_claude_standin_completes_runner_smoke_story(setup, monkeypatch, mutate_review):  # noqa: F811
    cfg, state, path, _ = setup
    agent = documented_agent()
    assert_reviewer_allowlist(agent)
    script = state / "claude-standin.py"
    script.write_text('''import json, os, pathlib, re, sys
if '--version' in sys.argv:
    print('fixture-claude'); sys.exit(0)
assert '--bare' in sys.argv and '--print' in sys.argv
assert sys.argv[sys.argv.index('--settings') + 1] == os.environ['CLAUDE_CONFIG_DIR'] + '/settings.json'
assert not any(k in os.environ for k in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'GH_TOKEN'))
prompt = sys.stdin.read()
output = pathlib.Path(re.search(r'Final-answer file: (.+?)\\. If your CLI', prompt)[1])
review = 'INDEPENDENT READ-ONLY REVIEW' in prompt
if review:
    assert sys.argv[sys.argv.index('--tools') + 1] == 'Read,Edit'
    assert sys.argv[sys.argv.index('--permission-mode') + 1] == 'dontAsk'
    assert sys.argv[sys.argv.index('--allowedTools') + 1] == 'Edit(/' + str(output) + ')'
    if MUTATE:
        pathlib.Path('smoke_add.py').write_text('broken = True\\n')
else:
    pathlib.Path('smoke_add.py').write_text('def add(a, b):\\n    return a + b\\n')
    pathlib.Path('test_smoke_add.py').write_text('import unittest\\nfrom smoke_add import add\\nclass Smoke(unittest.TestCase):\\n    def test_add(self):\\n        self.assertEqual(add(2, 3), 5)\\n        self.assertEqual(add(0, 0), 0)\\n        self.assertEqual(add(-2, -3), -5)\\n')
value = dict(status='ready_for_integration' if review else 'ready_for_review', summary='fixture', tests=[], limitations=[])
if review:
    value['findings'] = []
output.write_text(json.dumps(value))
print('stdout is deliberately not a JSON answer')
'''.replace('MUTATE', repr(mutate_review)))
    agent["command"][:1] = [sys.executable, str(script)]
    agent["version_command"] = [sys.executable, str(script), "--version"]
    agent["version_pattern"] = "fixture-claude"
    cfg["agent"] = agent
    cfg["verification"] = {"commands": [[sys.executable, "-B", "-m", "unittest", "-q"]]}
    monkeypatch.setattr(runner, "check_cli", REAL_CHECK_CLI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-only")
    monkeypatch.setenv("GH_TOKEN", "fixture-only")
    runner.run_once(cfg)
    final = runner.read(path)
    if mutate_review:
        assert final["status"] == "failed"
    else:
        assert final["status"] == "ready_for_integration"
        assert final["reviewed_head"] == runner.git(cfg, "rev-parse", "HEAD")
        assert final["cli_version"] == "fixture-claude"
        assert final["verification_runs"][-1]["status"] == "passed"
        assert runner.git(cfg, "status", "--porcelain") == ""
