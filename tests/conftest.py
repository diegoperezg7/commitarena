import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def fake_response(content="", tool_calls=None, tin=100, tout=20):
    calls = [
        SimpleNamespace(
            id=f"call_{i}", function=SimpleNamespace(name="bash", arguments=json.dumps({"command": c}))
        )
        for i, c in enumerate(tool_calls or [])
    ] or None
    msg = SimpleNamespace(content=content, tool_calls=calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=tin, completion_tokens=tout, model_extra={}),
    )


class ScriptedSDK:
    """Stands in for the openai client: returns a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


@pytest.fixture
def scripted():
    return ScriptedSDK


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def toy_repo(tmp_path) -> Path:
    """A tiny pytest repo with one bug-fix commit that adds a failing test."""
    r = tmp_path / "toy"
    (r / "toy").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "toy" / "__init__.py").write_text("")
    (r / "toy" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef clamp(x, lo, hi):\n    return max(lo, x)\n"
    )
    (r / "tests" / "test_calc.py").write_text(
        "from toy.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (r / "pyproject.toml").write_text('[project]\nname = "toy"\nversion = "0"\n')
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "initial")
    (r / "toy" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef clamp(x, lo, hi):\n    return min(hi, max(lo, x))\n"
    )
    (r / "tests" / "test_calc.py").write_text(
        "from toy.calc import add, clamp\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_clamp_upper():\n    assert clamp(10, 0, 5) == 5\n"
    )
    (r / "CHANGELOG.md").write_text("SECRET-COMMIT-NOTE\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "Fix clamp ignoring the upper bound SECRET-COMMIT-MESSAGE")
    return r
