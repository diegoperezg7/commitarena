# Security policy

## Model of trust

commitarena runs code written by language models. That code runs only inside Docker containers started
with `--network none`, `--cap-drop ALL`, `no-new-privileges`, a CPU/memory/PID limit and a per-command
timeout. The agent loop itself (the part that talks to the model API) runs on the host and never executes
model output. The test image is built once per repository, with network, from the repository's own code
and install command: mine only repositories you trust to install.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab, "Report a vulnerability"). Do not open a public issue for a sandbox escape or a key leak.
You should get an answer within a week.

## Supported versions

Only the latest release gets fixes.
