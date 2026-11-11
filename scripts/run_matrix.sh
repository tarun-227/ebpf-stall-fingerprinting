#!/usr/bin/env bash
# Run the full experiment matrix under the tracer.
#
#   sudo scripts/run_matrix.sh DATA_DIR [MATRIX_ID]
#
# DATA_DIR must be on the test filesystem (local, supports O_DIRECT; on the VM
# this is the dm-delay mount). Results go to
#   experiments/raw/MATRIX_ID/<run_id>/{events.jsonl,stats.json,gt.jsonl,meta.json}
# and are never overwritten.
#
# Environment overrides (defaults in brackets):
#   REPS ["1 2 3 4 5"]  THREADS ["1 4 16"]  TRANSFER_REPS ["3 4 5"]
#   DEVS [""]  e.g. "-d 252:0 -d 8:16" to record only the test disk
#   HOG_CPU [1]  OPS_TOTAL [48]  CPU_MS [10]  SLEEP_US [2000]
#   RAW_DIR [experiments/raw]  put raw output on a fast local disk; under WSL,
#                              the Windows drive (/mnt/c) is slow and can cause drops
# Pass overrides through sudo with env, e.g.
#   sudo env RAW_DIR=/home/me/raw THREADS="1 4" scripts/run_matrix.sh /home/me/data
#
# Main runs use 64 KiB sequential I/O; transfer runs repeat the I/O workloads
# with 16 KiB random I/O (see experiments/configs/eval.json).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 DATA_DIR [MATRIX_ID]" >&2; exit 1; }
[[ $# -ge 1 ]] || { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/build/stallbench"
TRACE="$ROOT/build/stalltrace"
[[ -x "$BENCH" && -x "$TRACE" ]] || { echo "build first: make" >&2; exit 1; }

DATA="$1"
MATRIX_ID="${2:-matrix-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="${RAW_DIR:-$ROOT/experiments/raw}/$MATRIX_ID"
REPS="${REPS:-1 2 3 4 5}"
THREADS="${THREADS:-1 4 16}"
TRANSFER_REPS="${TRANSFER_REPS:-3 4 5}"
DEVS="${DEVS:-}"
HOG_CPU="${HOG_CPU:-1}"
OPS_TOTAL="${OPS_TOTAL:-48}"
CPU_MS="${CPU_MS:-10}"
SLEEP_US="${SLEEP_US:-2000}"

IO_WORKLOADS="direct_read cold_read sync_write mmap_cold"
OTHER_WORKLOADS="warm_read buffered_write mmap_warm cpu cpu_contended sleep neighbor_io mixed"

mkdir -p "$DATA" "$OUT"
FILE="$DATA/matrix-data.bin"
[[ -f "$FILE" ]] || "$BENCH" --prepare --file "$FILE" --size $((256 * 1024 * 1024))

# Environment manifest for the report and the reproducibility checklist.
{
  echo "matrix_id: $MATRIX_ID"
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "kernel: $(uname -r) ($(uname -m))"
  echo "distro: $(. /etc/os-release && echo "$PRETTY_NAME")"
  echo "cpu: $(lscpu | sed -n 's/^Model name:[[:space:]]*//p') x $(nproc)"
  echo "ram: $(free -h | awk '/^Mem:/{print $2}')"
  echo "virtualisation: $(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "data_dir: $DATA ($(findmnt -no SOURCE,FSTYPE -T "$DATA" | tr -s ' '))"
  echo "reps: $REPS; threads: $THREADS; transfer_reps: $TRANSFER_REPS"
  echo "ops_total: $OPS_TOTAL; cpu_ms: $CPU_MS; sleep_us: $SLEEP_US; hog_cpu: $HOG_CPU; devs: ${DEVS:-all}"
  echo "stallbench_sha256: $(sha256sum "$BENCH" | cut -d' ' -f1)"
  echo "stalltrace_sha256: $(sha256sum "$TRACE" | cut -d' ' -f1)"
} > "$OUT/manifest.txt"

n=0; failed=0
run() {  # run RUN_ID REP WORKLOAD THREADS IO_SIZE PATTERN
  local id="$1" rep="$2" wl="$3" thr="$4" io="$5" pat="$6"
  local dir="$OUT/$id" ops=$(( OPS_TOTAL / thr )); (( ops >= 3 )) || ops=3
  local seed=$(( rep * 100000 + n ))
  local extra=()
  [[ "$pat" == random ]] && extra+=(--random)
  if [[ -e "$dir/events.jsonl" ]]; then echo "skip $id (exists)"; return; fi
  mkdir -p "$dir"
  printf '{"run_id":"%s","rep":%d,"workload":"%s","threads":%d,"io_size":%d,"pattern":"%s","seed":%d}\n' \
    "$id" "$rep" "$wl" "$thr" "$io" "$pat" "$seed" > "$dir/meta.json"
  n=$((n + 1))
  # shellcheck disable=SC2086
  if ! "$TRACE" -o "$dir" $DEVS -- "$BENCH" --workload "$wl" --run-id "$id" --out "$dir/gt.jsonl" \
        --file "$FILE" --threads "$thr" --ops "$ops" --io-size "$io" --seed "$seed" \
        --hogs 3 --hog-cpu "$HOG_CPU" --cpu-ms "$CPU_MS" --sleep-us "$SLEEP_US" "${extra[@]}" \
        > "$dir/run.log" 2>&1; then
    failed=$((failed + 1))
    echo "FAILED $id (see $dir/run.log)"
  fi
}

total=$(( $(wc -w <<<"$REPS") * $(wc -w <<<"$THREADS") * $(wc -w <<<"$IO_WORKLOADS $OTHER_WORKLOADS")
          + $(wc -w <<<"$TRANSFER_REPS") * 2 * $(wc -w <<<"$IO_WORKLOADS") ))
echo "matrix $MATRIX_ID: $total runs -> $OUT"
start=$(date +%s)
for rep in $REPS; do
  for wl in $IO_WORKLOADS $OTHER_WORKLOADS; do
    for thr in $THREADS; do
      run "$wl-t$thr-io64k-seq-r$rep" "$rep" "$wl" "$thr" 65536 sequential
    done
  done
  echo "  rep $rep done ($n runs, $(( $(date +%s) - start )) s)"
done
for rep in $TRANSFER_REPS; do
  for wl in $IO_WORKLOADS; do
    for thr in 1 4; do
      run "$wl-t$thr-io16k-rand-r$rep" "$rep" "$wl" "$thr" 16384 random
    done
  done
done

if [[ -n "${SUDO_USER:-}" ]]; then chown -R "$SUDO_USER": "$OUT"; fi
echo "done: $n runs, $failed failed, $(( $(date +%s) - start )) s"
cat <<EOF

Next (as a normal user):
  python3 src/classifier/features.py $OUT --out experiments/processed/$MATRIX_ID/features.csv
  python3 src/classifier/evaluate.py experiments/processed/$MATRIX_ID/features.csv \\
      --config experiments/configs/eval.json --out experiments/processed/$MATRIX_ID/results
EOF
