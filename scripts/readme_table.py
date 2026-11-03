"""Replace the example-output block in README.md / README.es.md with the current recorded demo output."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
out = subprocess.run(
    ["uv", "run", "commitarena", "demo", "--no-open", "--out", "/dev/null"],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
).stdout
lines = [line.rstrip() for line in out.splitlines() if not line.startswith(("Report:", "Run it yourself"))]
block = "<!-- demo:start -->\n```text\n$ commitarena demo\n" + "\n".join(lines) + "\n```\n<!-- demo:end -->"
for name, img in (("README.md", "report-light.png"), ("README.es.md", "report-dark.png")):
    p = ROOT / name
    s = p.read_text()
    full = (
        block
        + f'\n\n<p align="center"><a href="https://diegoperezg7.github.io/commitarena/"><img src="docs/assets/{img}" alt="report" width="760"></a></p>'
    )
    if "__DEMO_TABLE__" in s:
        s = s.replace("__DEMO_TABLE__", full)
    else:
        s = re.sub(r"<!-- demo:start -->.*?<!-- demo:end -->", block, s, flags=re.S)
    p.write_text(s)
print(block)
