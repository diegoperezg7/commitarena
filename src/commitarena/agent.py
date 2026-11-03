"""The minimal agent harness: one `bash` tool, same loop and prompt for every model."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import BudgetExceeded
from .llm import ChatClient, Usage
from .sandbox import Sandbox

SYSTEM_PROMPT = """\
You are a software engineer fixing a bug in a Python repository. The repository is at /work inside a \
Linux container with no network access (pip install will not work; the dependencies are already \
installed). Use the `bash` tool to inspect files, edit them (for example with python, sed or a heredoc) \
and run tests. Each call runs in a fresh shell starting in /work; long outputs are truncated.

Work in small steps: read the failing tests, find the cause in the source code, make a minimal fix, \
and run the tests. Do not modify test files: they are restored before scoring. When the tests pass, \
reply with a one-line summary and no tool call to finish."""

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a bash command in /work and return its combined stdout/stderr and exit code.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run."}},
            "required": ["command"],
        },
    },
}


@dataclass
class AgentConfig:
    max_steps: int = 30  # model calls
    max_wall_time: int = 1200  # seconds for the whole attempt
    max_output_chars: int = 6000  # per tool result, head + tail kept
    max_tokens: int = 4096  # per model reply
    temperature: float = 0.0
    system_prompt: str = SYSTEM_PROMPT


@dataclass
class AgentOutcome:
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""  # finished | max_steps | time_limit | budget_exceeded | model_error
    error: str = ""
    final_message: str = ""
    trajectory: list[dict] = field(default_factory=list)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{text[-half:]}"


def _assistant_dict(msg) -> dict:
    d: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return d


def run_agent(
    client: ChatClient,
    sandbox: Sandbox,
    prompt: str,
    config: AgentConfig | None = None,
    system_extra: str = "",
    on_step: Callable[[int, str], None] | None = None,
) -> AgentOutcome:
    cfg = config or AgentConfig()
    system = cfg.system_prompt + (f"\n\n{system_extra}" if system_extra else "")
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    out = AgentOutcome()
    out.trajectory = messages
    start = time.monotonic()
    while True:
        if out.steps >= cfg.max_steps:
            out.stop_reason = "max_steps"
            break
        if time.monotonic() - start > cfg.max_wall_time:
            out.stop_reason = "time_limit"
            break
        try:
            resp, usage = client.chat(
                messages, tools=[BASH_TOOL], temperature=cfg.temperature, max_tokens=cfg.max_tokens
            )
        except BudgetExceeded as e:
            out.stop_reason, out.error = "budget_exceeded", str(e)
            break
        out.steps += 1
        out.usage.add(usage)
        if not resp.choices:
            out.stop_reason, out.error = "model_error", "empty response"
            break
        msg = resp.choices[0].message
        messages.append(_assistant_dict(msg))
        if not msg.tool_calls:
            out.stop_reason = "finished"
            out.final_message = msg.content or ""
            break
        for tc in msg.tool_calls:
            out.tool_calls += 1
            try:
                args = json.loads(tc.function.arguments or "{}")
                command = args["command"] if tc.function.name == "bash" else None
            except (json.JSONDecodeError, KeyError, TypeError):
                command = None
            if not isinstance(command, str):
                result = 'error: call the bash tool with a JSON object {"command": "..."}'
            else:
                if on_step:
                    on_step(out.steps, command)
                r = sandbox.exec(command)
                result = truncate(r.output, cfg.max_output_chars) + f"\n[exit code {r.exit_code}]"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return out
