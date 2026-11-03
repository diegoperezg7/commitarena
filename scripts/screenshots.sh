#!/usr/bin/env bash
# Regenerate README screenshots of the recorded demo report (needs Google Chrome and npx).
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
uv run commitarena demo --no-open --out "$tmp/report.html" >/dev/null
pw() { npx -y playwright@1.56.1 screenshot --channel chrome --wait-for-timeout 1500 "$@"; }
pw --viewport-size "1280,900" --full-page "file://$tmp/report.html?theme=light&lang=en" docs/assets/report-light.png
pw --viewport-size "1280,900" --full-page "file://$tmp/report.html?theme=dark&lang=es" docs/assets/report-dark.png
pw --viewport-size "390,844" --full-page "file://$tmp/report.html?theme=light&lang=en" docs/assets/report-mobile.png
pw --viewport-size "1280,720" "file://$tmp/report.html?theme=light&lang=en" docs/assets/report-hero.png
rm -rf "$tmp"
