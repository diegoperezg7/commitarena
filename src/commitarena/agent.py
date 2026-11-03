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

NUDGE = (
    "Your last reply was empty or cut off at the token limit. Keep your reasoning short and call the bash "
    "tool, or reply with a one-line summary if you are done."
)
MAX_NUDGES = 2  # consecutive empty or truncated replies before giving up

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
    # Older tool outputs are elided once the conversation passes this size, so a 16K-token local
    # model keeps the task text and the latest outputs (about 3.5 characters per token).
    max_context_chars: int = 40000


@dataclass
class AgentOutcome:
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""  # finished | max_steps | time_limit | no_action | budget_exceeded | model_error
    error: str = ""
    final_message: str = ""
    trajectory: list[dict] = field(default_factory=list)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        f"{text[:half]}\n... [{len(text) - limit} characters truncated. Use grep -n or sed -n 'START,ENDp' "
        f"FILE to see specific lines] ...\n{text[-half:]}"
    )


ELIDED = "[older output elided to fit the context window]"
REPEAT_NOTE = "[note: you already ran this exact command and got the same output. Try a different approach.]"


def fit_context(messages: list[dict], max_chars: int, keep_last: int = 4) -> list[dict]:
    """Copy of `messages` with the oldest tool outputs elided until the total fits in `max_chars`.

    The system prompt, the task and the last `keep_last` messages are never elided."""
    size = sum(len(m.get("content") or "") for m in messages)
    if size <= max_chars:
        return messages
    out = [dict(m) for m in messages]
    for m in out[2 : max(2, len(out) - keep_last)]:
        if size <= max_chars:
            break
        if m["role"] == "tool" and m["content"] != ELIDED:
            size -= len(m["content"]) - len(ELIDED)
            m["content"] = ELIDED
    return out


def message_text(content) -> str:
    """Reply text. Some providers return a list of blocks (e.g. thinking + text); keep the text ones."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if kind in (None, "text") and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _assistant_dict(msg) -> dict:
    d: dict = {"role": "assistant", "content": message_text(msg.content)}
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
    nudges = 0
    seen: dict[str, str] = {}  # command -> last output, to flag exact repeats
    while True:
        if out.steps >= cfg.max_steps:
            out.stop_reason = "max_steps"
            break
        if time.monotonic() - start > cfg.max_wall_time:
            out.stop_reason = "time_limit"
            break
        try:
            resp, usage = client.chat(
                fit_context(messages, cfg.max_context_chars),
                tools=[BASH_TOOL],
                temperature=cfg.temperature,
                top_p=1.0,
                max_tokens=cfg.max_tokens,
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
        finish = getattr(resp.choices[0], "finish_reason", None)
        text = message_text(msg.content)
        if not msg.tool_calls and (finish == "length" or not text.strip()):
            # Thinking models can spend the whole reply budget reasoning. Ask once more instead of
            # treating an empty or truncated reply as "done". Same rule for every model.
            if nudges < MAX_NUDGES:
                nudges += 1
                messages.append({"role": "assistant", "content": text.strip() or "(no reply)"})
                messages.append({"role": "user", "content": NUDGE})
                continue
            out.stop_reason = "no_action"
            break
        messages.append(_assistant_dict(msg))
        if not msg.tool_calls:
            out.stop_reason = "finished"
            out.final_message = text
            break
        nudges = 0
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
                if seen.get(command) == result:
                    result += "\n" + REPEAT_NOTE
                else:
                    seen[command] = result
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return out
