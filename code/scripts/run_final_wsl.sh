#!/usr/bin/env bash
# All final WSL experiments in one go (one sudo prompt):
#   1. dm-delay on a loop file, formatted ext4 and mounted (skipped if present)
#   2. check that the injected delay shows up in direct-read latency
#   3. final matrix on the dm-delay mount at MAIN_DELAY_MS
#   4. injected-delay sweep (0, 5, 20 ms)
#   5. instrumentation overhead (delay 0)
#
#   sudo scripts/run_final_wsl.sh [BASE_DIR]     (default /home/$SUDO_USER/stalltest)
#
# Raw results go to BASE_DIR/raw/{final-wsl,delay-sweep-wsl,overhead-wsl}; the
# full log to BASE_DIR/final-run.log. Override MAIN_DELAY_MS [2], IMG_MB [2048].
# STEPS ["check matrix sweep overhead"] picks steps after the always-run setup;
# "smoke" adds the smoke test. OVERHEAD_ID [overhead-wsl] names the overhead run.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 [BASE_DIR]" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${1:-/home/${SUDO_USER:-root}/stalltest}"
MAIN_DELAY_MS="${MAIN_DELAY_MS:-2}"
IMG_MB="${IMG_MB:-2048}"
MNT="$BASE/mnt"
export RAW_DIR="$BASE/raw"
LOG="$BASE/final-run.log"
DM="$ROOT/scripts/dm_delay.sh"
STEPS=" ${STEPS:-check matrix sweep overhead} "
OVERHEAD_ID="${OVERHEAD_ID:-overhead-wsl}"
want() { [[ "$STEPS" == *" $1 "* ]]; }
mkdir -p "$BASE" "$RAW_DIR"
exec > >(tee -a "$LOG") 2>&1
echo "=== final WSL runs started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
t0=$(date +%s)

# 1. dm-delay test filesystem
if ! dmsetup info stall_delay >/dev/null 2>&1; then
  "$DM" create-loop "$BASE/dmdelay.img" "$IMG_MB" 0
  "$DM" format --wipe "$(cat /run/stall_delay.disk)"
fi
if ! findmnt -n "$MNT" >/dev/null 2>&1; then
  "$DM" mount "$MNT"
fi
DATA="$MNT/data"; mkdir -p "$DATA"
[[ -n "${SUDO_USER:-}" ]] && chown -R "$SUDO_USER": "$MNT"
dm_dev="$(lsblk -dno MAJ:MIN /dev/mapper/stall_delay | tr -d ' ')"
under_dev="$(lsblk -dno MAJ:MIN "/dev/$(cat /run/stall_delay.disk)" | tr -d ' ')"
export DEVS="-d $dm_dev -d $under_dev"
"$DM" status

# 2. sanity check: median direct-read latency at 0 and 10 ms delay
check() {
  "$DM" set "$1" >/dev/null
  "$ROOT/build/stallbench" --prepare --file "$DATA/check.bin" --size $((64 * 1024 * 1024)) >/dev/null
  "$ROOT/build/stallbench" --workload direct_read --file "$DATA/check.bin" --run-id check \
    --out "$BASE/check-$1.jsonl" --ops 20 --random >/dev/null
  python3 -c "import json,statistics as s; L=[json.loads(x) for x in open('$BASE/check-$1.jsonl')][1:]; \
print(f'  delay $1 ms: median direct-read latency {s.median(o[\"lat_ns\"] for o in L)/1e6:.2f} ms')"
}
if want check; then
  echo "=== delay check"
  check 0
  check 10
  rm -f "$DATA/check.bin"
fi

if want smoke; then
  echo "=== smoke test"
  "$DM" set 0
  "$ROOT/scripts/smoke_test.sh" "$BASE/smoke"
fi

# 3. final matrix
if want matrix; then
  echo "=== final matrix (delay ${MAIN_DELAY_MS} ms)"
  "$DM" set "$MAIN_DELAY_MS"
  DELAY_MS="$MAIN_DELAY_MS" "$ROOT/scripts/run_matrix.sh" "$DATA" final-wsl
fi

# 4. delay sweep
if want sweep; then
  echo "=== delay sweep"
  "$ROOT/scripts/run_delay_sweep.sh" "$DATA" delay-sweep-wsl
fi

# 5. overhead (no injected delay)
if want overhead; then
  echo "=== overhead ($OVERHEAD_ID)"
  "$DM" set 0
  "$ROOT/scripts/run_overhead.sh" "$DATA" "$OVERHEAD_ID"
fi

[[ -n "${SUDO_USER:-}" ]] && chown -R "$SUDO_USER": "$RAW_DIR" "$LOG"
echo "=== all done in $(( ($(date +%s) - t0) / 60 )) min; results under $RAW_DIR"
