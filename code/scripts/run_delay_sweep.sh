#!/usr/bin/env bash
# Injected-delay sweep: run the I/O workloads (plus controls) with dm-delay set
# to each level in turn. Gives ground truth for stall duration: every block
# request takes at least the injected delay longer.
#
#   sudo scripts/run_delay_sweep.sh DATA_DIR [SWEEP_ID]
#
# DATA_DIR must be on the dm-delay mount (scripts/dm_delay.sh create/create-loop,
# format, mount). Overrides: DELAYS ["0 5 20"], REPS ["1 2 3"], THREADS ["1 4"],
# RAW_DIR, plus anything run_matrix.sh accepts.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 DATA_DIR [SWEEP_ID]" >&2; exit 1; }
[[ $# -ge 1 ]] || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$1"
SWEEP_ID="${2:-delay-sweep-$(date -u +%Y%m%dT%H%M%SZ)}"
DELAYS="${DELAYS:-0 5 20}"

dmsetup info stall_delay >/dev/null 2>&1 || { echo "no dm-delay device; see scripts/dm_delay.sh" >&2; exit 1; }
src="$(findmnt -no SOURCE -T "$DATA")"
[[ "$src" == /dev/mapper/stall_delay ]] || { echo "$DATA is on $src, not the dm-delay device" >&2; exit 1; }

# Record block events only for the delay device and the device under it.
dm_dev="$(lsblk -dno MAJ:MIN /dev/mapper/stall_delay | tr -d ' ')"
under="$(cat /run/stall_delay.disk)"
under_dev="$(lsblk -dno MAJ:MIN "/dev/$under" | tr -d ' ')"
export DEVS="-d $dm_dev -d $under_dev"
export REPS="${REPS:-1 2 3}" THREADS="${THREADS:-1 4}" TRANSFER=0
export IO_WORKLOADS="direct_read cold_read sync_write mmap_cold"
export OTHER_WORKLOADS="mixed sleep"

for d in $DELAYS; do
  "$ROOT/scripts/dm_delay.sh" set "$d"
  sync; echo 3 > /proc/sys/vm/drop_caches   # disposable test environment only
  DELAY_MS="$d" "$ROOT/scripts/run_matrix.sh" "$DATA" "$SWEEP_ID"
done
"$ROOT/scripts/dm_delay.sh" set 0
echo "sweep $SWEEP_ID done; analyse with:"
echo "  python3 src/classifier/features.py \${RAW_DIR:-experiments/raw}/$SWEEP_ID --out ../supplementary/experimental_results/$SWEEP_ID/features.csv"
echo "  python3 src/classifier/delay_analysis.py ../supplementary/experimental_results/$SWEEP_ID/features.csv"
