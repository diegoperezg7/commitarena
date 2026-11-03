"""Run pytest inside a sandbox and parse JUnit XML into per-test outcomes."""

from __future__ import annotations

import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .sandbox import Sandbox

JUNIT_PATH = "/tmp/commitarena-junit.xml"

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


@dataclass
class TestOutcomes:
    status: dict[str, str] = field(default_factory=dict)  # test id -> passed/failed/skipped
    messages: dict[str, str] = field(default_factory=dict)  # failure text (truncated)
    output: str = ""
    ok: bool = True  # False if pytest crashed or produced no XML

    def passed(self) -> set[str]:
        return {t for t, s in self.status.items() if s == PASSED}

    def failed(self) -> set[str]:
        return {t for t, s in self.status.items() if s == FAILED}


def pytest_command(paths: list[str] | None = None, extra: str = "") -> str:
    target = " ".join(shlex.quote(p) for p in paths) if paths else ""
    return (
        f"rm -f {JUNIT_PATH}; python -m pytest -p no:cacheprovider -o addopts='' -q "
        f"--continue-on-collection-errors --junitxml={JUNIT_PATH} {extra} {target}"
    ).strip()


def parse_junit(xml_text: str, max_msg: int = 1500) -> dict[str, tuple[str, str]]:
    """Return {test_id: (status, message)}. Test id is ``classname::name``."""
    out: dict[str, tuple[str, str]] = {}
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        cls = case.get("classname", "")
        name = case.get("name", "")
        tid = f"{cls}::{name}" if cls else name
        status, msg = PASSED, ""
        for child in case:
            if child.tag in ("failure", "error"):
                status = FAILED
                msg = ((child.get("message") or "") + "\n" + (child.text or "")).strip()[:max_msg]
                break
            if child.tag == "skipped":
                status = SKIPPED
        # an error in teardown after a pass also counts as failed (handled by break above)
        prev = out.get(tid)
        if prev is None or status == FAILED:
            out[tid] = (status, msg)
    return out


def run_tests(sb: Sandbox, timeout: int = 600, paths: list[str] | None = None) -> TestOutcomes:
    res = sb.exec(pytest_command(paths), timeout=timeout)
    xml = sb.exec(f"cat {JUNIT_PATH}", timeout=60)
    to = TestOutcomes(output=res.output[-6000:])
    if xml.exit_code != 0 or "<testsuite" not in xml.output:
        to.ok = False
        return to
    try:
        parsed = parse_junit(xml.output[xml.output.index("<") :])
    except ET.ParseError:
        to.ok = False
        return to
    for tid, (status, msg) in parsed.items():
        to.status[tid] = status
        if msg:
            to.messages[tid] = msg
    return to
