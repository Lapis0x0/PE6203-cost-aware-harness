#!/bin/bash
# Build charts.html from analysis/metrics.json, then export each chart to PNG with headless Chrome.
# Usage: bash report/charts/build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

python3 - "$ROOT" "$HERE" <<'EOF'
import json, sys
from pathlib import Path
root, here = Path(sys.argv[1]), Path(sys.argv[2])
m = json.loads((root / "analysis" / "metrics.json").read_text())
keep = {k: m[k] for k in ("configs", "pareto_frontier")}
html = (here / "charts.template.html").read_text().replace("__METRICS__", json.dumps(keep, ensure_ascii=False))
(here / "charts.html").write_text(html)
EOF

# id width height (same aspect ratio as the viewBox)
while read -r id w h; do
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1.5 \
    --virtual-time-budget=4000 --window-size="$((w*2)),$((h*2))" \
    --screenshot="$HERE/$id.png" "file://$HERE/charts.html?export=$id" >/dev/null 2>&1
  echo "$id.png"
done <<'LIST'
pareto 680 340
ablation 640 300
gain 640 230
cost 420 320
family 420 320
LIST
