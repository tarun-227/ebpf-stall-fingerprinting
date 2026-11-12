#!/usr/bin/env bash
# Short end-to-end check of the tracer: loads the eBPF programs (kernel
# verifier), runs three workloads under tracing, and compares the time
# breakdown rebuilt from the trace with the workload's own reference numbers.
#
#   sudo scripts/smoke_test.sh [DATA_DIR]      (default /tmp/stall_smoke)
#
# DATA_DIR must be on a local filesystem that supports O_DIRECT (ext4, xfs).
# Build first with `make`.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/build/stallbench"
TRACE="$ROOT/build/stalltrace"
BREAKDOWN="$ROOT/src/correlation/breakdown.py"
DATA="${1:-/tmp/stall_smoke}"
[[ -x "$BENCH" && -x "$TRACE" ]] || { echo "build first: make" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DATA/runs-$STAMP"
mkdir -p "$OUT"
FILE="$DATA/data.bin"
[[ -f "$FILE" ]] || "$BENCH" --prepare --file "$FILE" --size $((64 * 1024 * 1024))

NCPU=$(nproc)
HOG_CPU=$(( NCPU > 1 ? 1 : 0 ))

run() {  # run NAME stallbench-args...
  local name="$1"; shift
  echo "== $name"
  "$TRACE" -o "$OUT/$name" -- "$BENCH" --run-id "smoke-$name" \
    --out "$OUT/$name.gt.jsonl" "$@"
}

run sleep --workload sleep --ops 20 --sleep-us 2000
run direct_read --workload direct_read --file "$FILE" --ops 20 --random
run contended --workload cpu_contended --ops 10 --hogs 3 --hog-cpu "$HOG_CPU" --cpu-ms 10

echo
fail=0
python3 - "$OUT" <<'PY' || fail=1
import json, sys, os
out = sys.argv[1]
drops = 0
for name in ("sleep", "direct_read", "contended"):
    st = json.load(open(os.path.join(out, name, "stats.json")))
    not_attached = {k: v for k, v in st["programs"].items() if v != "attached"}
    print(f"{name:<12} dropped {st['totals']['dropped']} of {st['totals']['produced']} events; "
          f"not attached: {not_attached or 'none'}")
    drops += st["totals"]["dropped"]
sys.exit(1 if drops else 0)
PY

for name in sleep direct_read contended; do
  echo
  python3 "$BREAKDOWN" "$OUT/$name" "$OUT/$name.gt.jsonl" \
    --csv "$OUT/$name.ops.csv" --summary
done

cat <<EOF

Expected: sleep mostly blocked_other, direct_read mostly blocked_io,
contended mostly runq; trace and reference within a few percent; marker skew
>= 0 and a few microseconds.
Results kept in $OUT
EOF
[[ $fail -eq 0 ]] || { echo "SMOKE TEST: events were dropped" >&2; exit 1; }
