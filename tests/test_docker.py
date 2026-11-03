"""End-to-end with Docker: mine a toy repo, run a scripted model, score. Linux CI only."""

import shutil
import subprocess

import pytest
from conftest import fake_response

from commitarena import Budget, ContextFile, run_task
from commitarena.llm import ChatClient
from commitarena.mine import mine


def _docker_ok():
    return shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker not available")]


@pytest.fixture
def mined(toy_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMITARENA_CACHE", str(tmp_path / "cache"))
    tasks, meta = mine(str(toy_repo), out=tmp_path / "tasks.json")
    return tasks, meta


def test_mine_validates_fail_to_pass(mined):
    tasks, meta = mined
    assert meta["validated"] == 1
    t = tasks[0]
    assert t.fail_to_pass == ["tests.test_calc::test_clamp_upper"]
    assert t.pass_to_pass == ["tests.test_calc::test_add"]
    assert "SECRET-COMMIT-MESSAGE" not in t.prompt and "SECRET-COMMIT-NOTE" not in t.prompt
    assert "min(hi" in t.gold_patch and "CHANGELOG" not in t.gold_patch


FIX = "sed -i 's/return max(lo, x)/return min(hi, max(lo, x))/' toy/calc.py"
CHEAT = "printf 'def test_clamp_upper():\\n    pass\\n' > tests/test_calc.py"


def _patch_client(monkeypatch, scripted, commands, seen):
    sdk = scripted([fake_response(tool_calls=commands), fake_response("done")])
    orig = ChatClient._sdk

    def fake(self):
        self._client = sdk
        return sdk

    monkeypatch.setattr(ChatClient, "_sdk", fake)
    seen.append(sdk)
    return orig


def test_run_task_fix_passes_and_network_is_off(mined, monkeypatch, scripted):
    task = mined[0][0]
    seen = []
    _patch_client(
        monkeypatch,
        scripted,
        [FIX, "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"", "cat AGENTS.md"],
        seen,
    )
    traj = []
    ctx = ContextFile("Always run the tests.", "AGENTS.md")
    r = run_task(task, "ollama/qwen3:4b", budget=Budget(), context=ctx, keep_trajectory=traj)
    assert r.passed, r
    assert r.fail_to_pass_passed == 1 and r.pass_to_pass_failed == 0
    assert "min(hi" in r.patch and "AGENTS.md" not in r.patch
    tool_out = [m["content"] for m in traj if m["role"] == "tool"]
    assert "Network is unreachable" in tool_out[1] or "exit code 1" in tool_out[1]
    assert "Always run the tests." in tool_out[2]
    assert "Always run the tests." in seen[0].calls[0]["messages"][0]["content"]


def test_run_task_test_edits_are_discarded(mined, monkeypatch, scripted):
    task = mined[0][0]
    _patch_client(monkeypatch, scripted, [CHEAT], [])
    r = run_task(task, "ollama/qwen3:4b")
    assert not r.passed
    assert r.ignored_test_edits == ["tests/test_calc.py"] and r.patch == ""
