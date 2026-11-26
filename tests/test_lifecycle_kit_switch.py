"""The trusted pre-policy switch is token-free, compatible and fail-closed."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from lifecycle import kit_switch

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('value', 'expected'), [(True, 'on'), (False, 'off')])
def test_switch_returns_exact_boolean_state(tmp_path, value, expected):
    path = tmp_path / 'kit.json'
    path.write_text(json.dumps({'features': {'lifecycle': value}}))
    assert kit_switch.lifecycle_switch(path) == expected


def test_missing_kit_keeps_existing_guard_on(tmp_path):
    assert kit_switch.lifecycle_switch(tmp_path / 'missing') == 'on'


@pytest.mark.parametrize('value', [None, [], {}, {'features': {}}, {'features': {'lifecycle': 1}},
                                 {'features': {'lifecycle': 'false'}}])
def test_invalid_switch_refuses_without_echoing_data(tmp_path, value, capsys):
    path, output = tmp_path / 'kit.json', tmp_path / 'output'
    path.write_text(json.dumps(value))
    assert kit_switch.main(['--kit', str(path), '--output', str(output)]) == 1
    assert not output.exists()
    assert capsys.readouterr().err == kit_switch.MESSAGE + '\n'


def test_invalid_json_and_symlink_never_echo_or_enable(tmp_path, capsys):
    target, path, output = tmp_path / 'secret', tmp_path / 'kit', tmp_path / 'output'
    target.write_text('CANARY-PRIVATE-SETTING')
    for make_link in [False, True]:
        if path.exists():
            path.unlink()
        if make_link:
            path.symlink_to(target)
        else:
            path.write_bytes(target.read_bytes())
        assert kit_switch.main(['--kit', str(path), '--output', str(output)]) == 1
        captured = capsys.readouterr()
        assert captured.out == '' and captured.err == kit_switch.MESSAGE + '\n'
        assert not output.exists()


def test_switch_cli_outputs_only_fixed_github_output(tmp_path):
    output = tmp_path / 'output'
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'lifecycle/kit_switch.py'),
                             '--kit', str(tmp_path / 'missing'), '--output', str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == result.stderr == ''
    assert output.read_text() == 'lifecycle=on\n'


def test_both_workflows_gate_setup_and_guard_without_switch_credentials():
    first = (ROOT / 'templates/task-lifecycle.yml').read_bytes()
    assert first == (ROOT / '.github/workflows/task-lifecycle.yml').read_bytes()
    text = first.decode()
    assert '/.github/agentic-kit.json' in text
    switch = text.split('      - name: Read the lifecycle feature switch without credentials\n')[1].split('      - if:')[0]
    assert 'id: kit' in switch and 'python3 -I lifecycle/kit_switch.py' in switch
    assert 'token' not in switch.lower() and 'env:' not in switch
    assert "      - if: steps.kit.outputs.lifecycle == 'on'\n        uses: actions/setup-python@" in text
    guard = text.split('      - name: Validate the label event or synchronize the PR\n')[1]
    assert "if: steps.kit.outputs.lifecycle == 'on'" in guard
