import os

from typer.testing import CliRunner

from commitarena import cli
from commitarena.cli import app

runner = CliRunner()


def _main(args, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["commitarena", *args])
    try:
        cli.main()
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out + out.err


def test_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "0.1.1" in r.stdout


def test_demo_works_offline(tmp_path):
    out = tmp_path / "demo.html"
    r = runner.invoke(app, ["demo", "--out", str(out), "--no-open"])
    assert r.exit_code == 0, r.stdout
    assert "Recorded result" in r.stdout and "Leaderboard" in r.stdout
    html = out.read_text()
    assert "commitarena" in html and '"recorded": true' in html


def test_missing_key_error_no_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    code, out = _main(
        ["run", "--tasks", str(tmp_path / "t.json"), "--models", "mistral/codestral-latest"],
        monkeypatch,
        capsys,
    )
    assert code == 1 and "MISTRAL_API_KEY" in out and "Traceback" not in out


def test_docker_not_running_error(monkeypatch, capsys, tmp_path):
    from commitarena import tasks as tmod
    from commitarena.report import demo_dir

    monkeypatch.setenv("PATH", str(tmp_path))  # no docker binary
    (tmp_path / "docker").write_text("#!/bin/sh\nexit 1\n")
    os.chmod(tmp_path / "docker", 0o755)
    assert tmod  # module import ok
    code, out = _main(
        ["run", "--tasks", str(demo_dir() / "tasks.json"), "--models", "ollama/qwen3:4b"], monkeypatch, capsys
    )
    assert code == 1 and "Docker is not running" in out and "Traceback" not in out


def test_ollama_unreachable(monkeypatch, capsys):
    import pytest

    from commitarena import ModelSpec, ModelUnavailable
    from commitarena.llm import ChatClient

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"))
    with pytest.raises(ModelUnavailable, match="Cannot reach Ollama"):
        c.chat([{"role": "user", "content": "x"}])


def test_bad_model_format(monkeypatch, capsys, tmp_path):
    code, out = _main(["run", "--tasks", "x", "--models", "qwen3"], monkeypatch, capsys)
    assert code == 1 and "provider/name" in out
