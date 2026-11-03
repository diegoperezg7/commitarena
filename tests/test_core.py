import json

import pytest
from conftest import fake_response

from commitarena import Budget, BudgetExceeded, CommitArenaError, MissingAPIKey, ModelSpec
from commitarena.agent import AgentConfig, run_agent, truncate
from commitarena.llm import ChatClient
from commitarena.mine import build_prompt, find_candidates
from commitarena.report import leaderboard, render
from commitarena.sandbox import ExecResult
from commitarena.tasks import (
    Task,
    changed_files,
    is_doc_path,
    is_test_path,
    load_tasks,
    repo_slug,
    save_tasks,
)
from commitarena.testrun import parse_junit, pytest_command


def test_test_path_classification():
    assert is_test_path("tests/test_x.py")
    assert is_test_path("tests/files/data.sql")
    assert is_test_path("pkg/test_thing.py")
    assert is_test_path("pkg/thing_test.py")
    assert is_test_path("conftest.py")
    assert not is_test_path("pkg/testing_utils.py")
    assert not is_test_path("pkg/core.py")
    assert is_doc_path("docs/index.rst") and is_doc_path("CHANGELOG") and is_doc_path("benchmarks/b.py")
    assert not is_doc_path("pkg/core.py")


def test_repo_slug():
    assert repo_slug("https://github.com/andialbrecht/sqlparse") == "andialbrecht-sqlparse"
    assert repo_slug("git@github.com:a/B.git") == "a-b"
    assert repo_slug("/tmp/x/toy") == "x-toy"


def test_parse_junit_statuses():
    xml = """<testsuites><testsuite>
    <testcase classname="tests.test_a" name="test_ok"/>
    <testcase classname="tests.test_a" name="test_bad"><failure message="boom">trace</failure></testcase>
    <testcase classname="tests.test_a" name="test_skip"><skipped/></testcase>
    <testcase classname="" name="tests.test_b"><error message="collection failed"/></testcase>
    </testsuite></testsuites>"""
    out = parse_junit(xml)
    assert out["tests.test_a::test_ok"][0] == "passed"
    assert out["tests.test_a::test_bad"] == ("failed", "boom\ntrace")
    assert out["tests.test_a::test_skip"][0] == "skipped"
    assert out["tests.test_b"][0] == "failed"


def test_pytest_command_neutralises_addopts():
    cmd = pytest_command()
    assert "--continue-on-collection-errors" in cmd and "-o addopts=''" in cmd and "no:cacheprovider" in cmd


def test_prompt_has_tests_not_commit_message():
    p = build_prompt(
        ["tests.t::test_x"], "+def test_x(): ...", {"tests.t::test_x": "AssertionError"}, ["tests/t.py"]
    )
    assert "tests.t::test_x" in p and "AssertionError" in p and "python -m pytest -q tests/t.py" in p
    assert "restored before scoring" in p


def test_find_candidates_and_changed_files(toy_repo, tmp_path):
    cands, skipped = find_candidates(toy_repo)
    assert len(cands) == 1
    assert cands[0].source_paths == ["toy/calc.py"]
    assert cands[0].test_paths == ["tests/test_calc.py"]
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "x").mkdir(parents=True)
    (b / "x").mkdir(parents=True)
    (a / "x" / "same.py").write_text("1")
    (b / "x" / "same.py").write_text("1")
    (a / "gone.py").write_text("1")
    (b / "new.py").write_text("2")
    (b / "__pycache__").mkdir()
    (b / "__pycache__" / "c.pyc").write_bytes(b"x")
    assert changed_files(a, b) == {"gone.py": None, "new.py": b"2"}


def test_model_spec_and_prices():
    s = ModelSpec.parse("openrouter/anthropic/claude-x")
    assert (s.provider, s.name) == ("openrouter", "anthropic/claude-x")
    assert ModelSpec.parse("ollama/qwen3:4b").unit_price() == (0.0, 0.0)
    assert ModelSpec.parse("mistral/codestral-latest").unit_price() == (0.30, 0.90)
    with pytest.raises(CommitArenaError):
        ModelSpec.parse("qwen3")
    with pytest.raises(CommitArenaError):
        ModelSpec.parse("nope/x")
    with pytest.raises(CommitArenaError, match="--price"):
        ChatClient(ModelSpec.parse("openai/unknown-model"))


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    c = ChatClient(ModelSpec.parse("mistral/codestral-latest"))
    with pytest.raises(MissingAPIKey, match="MISTRAL_API_KEY"):
        c._sdk()


def test_budget_checked_before_call(scripted):
    budget = Budget(max_cost=0.001)
    c = ChatClient(ModelSpec.parse("mistral/codestral-latest"), budget)
    c._client = scripted([fake_response("hi", tin=10_000, tout=1_000)])
    _, usage = c.chat([{"role": "user", "content": "x"}])
    assert usage.cost_usd == pytest.approx((10_000 * 0.30 + 1_000 * 0.90) / 1e6)
    with pytest.raises(BudgetExceeded, match="max-cost"):
        c.chat([{"role": "user", "content": "x"}])


class FakeSandbox:
    def __init__(self):
        self.commands = []

    def exec(self, command, timeout=None):
        self.commands.append(command)
        return ExecResult(0, "x" * 50)


def test_agent_loop_runs_tools_and_finishes(scripted):
    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c._client = scripted([fake_response(tool_calls=["ls", "cat a.py"]), fake_response("done")])
    sb = FakeSandbox()
    out = run_agent(c, sb, "fix it", AgentConfig(max_steps=5), system_extra="# Project instructions\nrule")
    assert sb.commands == ["ls", "cat a.py"]
    assert out.stop_reason == "finished" and out.steps == 2 and out.tool_calls == 2
    assert out.usage.tokens_in == 200 and out.usage.cost_usd == 0
    sent = c._client.calls[0]
    assert "rule" in sent["messages"][0]["content"]
    assert sent["tools"][0]["function"]["name"] == "bash"
    assert [m["role"] for m in out.trajectory] == ["system", "user", "assistant", "tool", "tool", "assistant"]


def test_agent_loop_stops_at_max_steps_and_budget(scripted):
    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c._client = scripted([fake_response(tool_calls=["ls"])] * 3)
    out = run_agent(c, FakeSandbox(), "x", AgentConfig(max_steps=2))
    assert out.stop_reason == "max_steps" and out.steps == 2
    c2 = ChatClient(ModelSpec.parse("mistral/codestral-latest"), Budget(max_cost=0.0))
    out2 = run_agent(c2, FakeSandbox(), "x")
    assert out2.stop_reason == "budget_exceeded" and out2.steps == 0


def test_agent_handles_bad_tool_arguments(scripted):
    from types import SimpleNamespace

    bad = fake_response(tool_calls=["ls"])
    bad.choices[0].message.tool_calls[0].function.arguments = "not json"
    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c._client = scripted([bad, fake_response("ok")])
    sb = FakeSandbox()
    out = run_agent(c, sb, "x")
    assert sb.commands == [] and "error" in out.trajectory[3]["content"]
    assert SimpleNamespace  # keep import used


def test_truncate():
    assert truncate("abc", 10) == "abc"
    s = truncate("a" * 100, 20)
    assert s.startswith("a" * 10) and "truncated" in s


def _result(model, task, passed, date, cost=0.0):
    return {
        "model": model,
        "task_id": task,
        "passed": passed,
        "task_date": date,
        "cost_usd": cost,
        "tokens_in": 10,
        "tokens_out": 5,
        "wall_time_s": 2.0,
        "steps": 3,
    }


def test_leaderboard_cutoff_split():
    rs = [
        _result("m/a", "t1", True, "2024-01-01T00:00:00"),
        _result("m/a", "t2", False, "2026-01-01T00:00:00"),
        _result("m/a", "t3", True, "2026-02-01T00:00:00", 0.5),
        _result("m/b", "t1", False, "2024-01-01T00:00:00"),
    ]
    rows = leaderboard(rs, {"m/a": "2025-01-01"})
    a = rows[0]
    assert a["model"] == "m/a" and a["passed"] == 2 and a["total"] == 3
    assert a["before"] == {"passed": 1, "total": 1} and a["after"] == {"passed": 1, "total": 2}
    assert rows[1]["before"]["total"] == 0  # no cutoff known
    assert a["tokens"] == 45 and a["cost"] == 0.5


def test_tasks_roundtrip_and_report(tmp_path):
    t = Task(
        id="r-abc12345",
        repo="https://github.com/o/r",
        commit="abc12345" * 5,
        parent="p" * 40,
        date="2026-01-01T00:00:00+00:00",
        prompt="p",
        fail_to_pass=["a::b"],
        pass_to_pass=[],
        test_files={"tests/test_a.py": "x"},
        gold_patch="diff --git a/x b/x\n+fix </script>\n",
        image="img",
    )
    run = tmp_path / "run"
    save_tasks(run / "tasks.json", [t])
    assert load_tasks(run)[0] == t
    (run / "results.jsonl").write_text(
        json.dumps({**_result("m/a", t.id, True, t.date), "patch": "+x"}) + "\n"
    )
    (run / "run.json").write_text(json.dumps({"version": "0.1.0", "cutoffs": {"m/a": "2025-06-01"}}))
    from commitarena.report import build_payload

    payload = build_payload(run)
    html = render(payload)
    assert "</script>\\n" not in html and "<\\/script>" in html
    assert payload["tasks"][0]["url"] == "https://github.com/o/r/commit/" + t.commit
    assert payload["board"][0]["after"] == {"passed": 1, "total": 1}
    assert 'lang="en"' in html and "prefers-color-scheme" in html


def test_load_tasks_errors(tmp_path):
    from commitarena import TaskError

    with pytest.raises(TaskError, match="commitarena mine"):
        load_tasks(tmp_path / "missing.json")


def test_agent_nudges_after_empty_or_truncated_reply(scripted):
    cut = fake_response("")
    cut.choices[0].finish_reason = "length"
    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c._client = scripted([cut, fake_response(tool_calls=["ls"]), fake_response("done")])
    sb = FakeSandbox()
    out = run_agent(c, sb, "x")
    assert out.stop_reason == "finished" and sb.commands == ["ls"] and out.steps == 3
    c2 = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c2._client = scripted([fake_response("")] * 3)
    assert run_agent(c2, FakeSandbox(), "x").stop_reason == "no_action"


def test_message_text_handles_block_lists():
    from commitarena.agent import message_text

    assert message_text(None) == "" and message_text("hi") == "hi"
    blocks = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
        {"type": "text", "text": "done"},
    ]
    assert message_text(blocks) == "done"


def test_null_byte_command_is_a_failed_step_not_a_crash():
    from commitarena.sandbox import Sandbox

    sb = Sandbox("unused-image")
    r = sb.exec("echo a\x00b")
    assert r.exit_code == 126 and "null byte" in r.output


def test_repeated_command_gets_a_note_and_context_is_trimmed(scripted):
    from commitarena.agent import ELIDED, REPEAT_NOTE, fit_context

    c = ChatClient(ModelSpec.parse("ollama/qwen3:4b"), Budget())
    c._client = scripted(
        [fake_response(tool_calls=["ls"]), fake_response(tool_calls=["ls"]), fake_response("ok")]
    )
    out = run_agent(c, FakeSandbox(), "x")
    tools = [m["content"] for m in out.trajectory if m["role"] == "tool"]
    assert REPEAT_NOTE not in tools[0] and REPEAT_NOTE in tools[1]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    msgs += [{"role": "tool", "content": "x" * 1000} for _ in range(8)]
    fitted = fit_context(msgs, 5000)
    assert sum(len(m["content"]) for m in fitted) <= 5000
    assert fitted[1]["content"] == "task" and fitted[-1]["content"] == "x" * 1000
    assert ELIDED in fitted[2]["content"] and msgs[2]["content"] == "x" * 1000  # original untouched
