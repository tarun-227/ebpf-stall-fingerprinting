#!/usr/bin/env bash
# Turn raw runs into the processed results used by the report (no root needed).
#
#   scripts/analyse_results.sh RAW_DIR [TAG]
#
# Expects RAW_DIR/{final-<TAG>,delay-sweep-<TAG>,overhead-<TAG>} from
# scripts/run_final_wsl.sh (TAG defaults to wsl). Writes everything to
# experiments/processed/<TAG>/ inside the repository:
#   features.csv, results-ref/ and results-design/ (main evaluation),
#   window-<N>ms/ (PID-only ablation), delay.json, overhead.json, *.txt logs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW="${1:?usage: $0 RAW_DIR [TAG]}"
TAG="${2:-wsl}"
OUT="$ROOT/experiments/processed/$TAG"
CFG="$ROOT/experiments/configs/eval.json"
PY="python3 $ROOT/src/classifier"
mkdir -p "$OUT"
cd "$ROOT"

echo "== main evaluation"
$PY/features.py "$RAW/final-$TAG" --out "$OUT/features.csv" | tee "$OUT/features.txt"
$PY/evaluate.py "$OUT/features.csv" --config "$CFG" --out "$OUT/results-ref" > "$OUT/results-ref.txt"
$PY/evaluate.py "$OUT/features.csv" --config "$CFG" --out "$OUT/results-design" --truth expected \
  > "$OUT/results-design.txt"
grep -E 'agrees|^  (rules|tree)/(fused|scheduler|syscall|block)' "$OUT/results-ref.txt" | head -20

echo "== injected-delay sweep"
$PY/features.py "$RAW/delay-sweep-$TAG" --out "$OUT/delay-features.csv" > /dev/null
$PY/delay_analysis.py "$OUT/delay-features.csv" --out "$OUT/delay.json" | tee "$OUT/delay.txt"

echo "== overhead"
$PY/overhead.py "$RAW/overhead-$TAG" --out "$OUT/overhead.json" | tee "$OUT/overhead.txt"

echo "== PID-only window ablation (5, 20, 100 ms)"
for ms in 5 20 100; do
  $PY/features.py "$RAW/final-$TAG" --out "$OUT/window-${ms}ms.csv" --window-ms "$ms" > /dev/null
  $PY/evaluate.py "$OUT/window-${ms}ms.csv" --config "$CFG" --out "$OUT/window-${ms}ms" \
    > "$OUT/window-${ms}ms.txt"
  grep -E '^  (rules|tree)/fused ' "$OUT/window-${ms}ms.txt" | head -2 | sed "s/^/  [${ms} ms] /"
done

# Run-level checks for the report: drops and clock drift.
python3 - "$RAW" "$TAG" <<'PY' | tee "$OUT/run-checks.txt"
import glob, json, os, sys
raw, tag = sys.argv[1], sys.argv[2]
for part in ("final", "delay-sweep", "overhead"):
    runs = glob.glob(os.path.join(raw, f"{part}-{tag}", "*"))
    prod = drop = drift = n = 0
    ratios = []
    for d in runs:
        s, g = os.path.join(d, "stats.json"), os.path.join(d, "gt.jsonl")
        if os.path.exists(s):
            t = json.load(open(s))["totals"]; prod += t["produced"]; drop += t["dropped"]
        if os.path.exists(g):
            h = json.loads(open(g).readline()); n += 1
            ratios.append(h.get("clock_ratio", 1.0)); drift += bool(h.get("clock_drift_flag"))
    if n:
        print(f"{part:<12} runs {n:>4}  events {prod:>10}  dropped {drop} "
              f"({100 * drop / max(prod, 1):.4f}%)  clock ratio {min(ratios):.4f}-{max(ratios):.4f}  "
              f"drift-flagged runs {drift}")
PY
echo "processed results in $OUT"
