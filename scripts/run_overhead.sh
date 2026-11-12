#!/usr/bin/env bash
# Instrumentation overhead: the same workloads with tracing off and on.
#
#   sudo scripts/run_overhead.sh DATA_DIR [RUN_ID]
#
# Part 1: each workload at 1 and 4 threads, tracing off vs fully on, REPS
#         repetitions; off/on alternate within a repetition so slow drift in
#         the machine cancels out.
# Part 2: variants on a syscall-heavy (warm_read) and an I/O-heavy
#         (direct_read) workload at 4 threads: marker probes off, syscall /
#         scheduler / block events off, 64 KiB and 4 MiB ring buffers, and a
#         second untraced run ("offrepeat") that measures the noise floor.
# Output: ${RAW_DIR:-experiments/raw}/RUN_ID/<run>/{gt.jsonl,stats.json,meta.json}
# Analyse with: python3 src/classifier/overhead.py <that directory>
# Overrides: REPS ["1 2 3 4 5"], RAW_DIR, DEVS, HOG_CPU [1].
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 DATA_DIR [RUN_ID]" >&2; exit 1; }
[[ $# -ge 1 ]] || { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/build/stallbench"
TRACE="$ROOT/build/stalltrace"
[[ -x "$BENCH" && -x "$TRACE" ]] || { echo "build first: make" >&2; exit 1; }

DATA="$1"
RUN_ID="${2:-overhead-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="${RAW_DIR:-$ROOT/experiments/raw}/$RUN_ID"
REPS="${REPS:-1 2 3 4 5}"
DEVS="${DEVS:-}"
HOG_CPU="${HOG_CPU:-1}"
mkdir -p "$DATA" "$OUT"
FILE="$DATA/matrix-data.bin"
[[ -f "$FILE" ]] || "$BENCH" --prepare --file "$FILE" --size $((256 * 1024 * 1024))

# Total operations and I/O size per run, sized so each run lasts about 1 s
# (shorter runs were dominated by noise: two identical 20 ms runs differed by 27%).
declare -A OPS=([direct_read]=2000 [cold_read]=2000 [sync_write]=500 [warm_read]=40000
                [buffered_write]=40000 [cpu]=100 [sleep]=400 [mixed]=1000)
declare -A IOSZ=([buffered_write]=4096)   # keep dirty data well under writeback limits
WORKLOADS="direct_read cold_read sync_write warm_read buffered_write cpu sleep mixed"

{
  echo "run_id: $RUN_ID"
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "kernel: $(uname -r); cpus: $(nproc); virtualisation: $(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "reps: $REPS; devs: ${DEVS:-all}"
  echo "stallbench_sha256: $(sha256sum "$BENCH" | cut -d' ' -f1)"
  echo "stalltrace_sha256: $(sha256sum "$TRACE" | cut -d' ' -f1)"
} > "$OUT/manifest.txt"

n=0; failed=0
one() {  # one ID REP WORKLOAD THREADS VARIANT [stalltrace options...]
  local id="$1" rep="$2" wl="$3" thr="$4" variant="$5"; shift 5
  local dir="$OUT/$id" ops=$(( ${OPS[$wl]} / thr )); (( ops >= 3 )) || ops=3
  if [[ -e "$dir/gt.jsonl" ]]; then echo "skip $id (exists)"; return; fi
  mkdir -p "$dir"
  printf '{"run_id":"%s","rep":%d,"workload":"%s","threads":%d,"variant":"%s"}\n' \
    "$id" "$rep" "$wl" "$thr" "$variant" > "$dir/meta.json"
  local bench=("$BENCH" --workload "$wl" --run-id "$id" --out "$dir/gt.jsonl" --file "$FILE"
               --threads "$thr" --ops "$ops" --seed $((rep * 1000 + n)) --hogs 3
               --hog-cpu "$HOG_CPU" --cpu-ms 10 --sleep-us 2000 --io-size "${IOSZ[$wl]:-65536}"
               --no-ref)   # no per-op reference syscalls: measure only the tracer
  n=$((n + 1))
  local rc=0
  if [[ "$variant" == off* ]]; then   # off, or offrepeat (noise floor: untraced vs untraced)
    "${bench[@]}" > "$dir/run.log" 2>&1 || rc=$?
  else
    # shellcheck disable=SC2086
    "$TRACE" -o "$dir" $DEVS "$@" -- "${bench[@]}" > "$dir/run.log" 2>&1 || rc=$?
  fi
  if (( rc != 0 )); then failed=$((failed + 1)); echo "FAILED $id (see $dir/run.log)"; fi
}

start=$(date +%s)
# Discarded warm-up: one untraced run per workload, so page cache, CPU
# frequency and lazily loaded state settle before anything is measured.
for wl in $WORKLOADS; do
  "$BENCH" --workload "$wl" --run-id warmup --out /dev/null --file "$FILE" --threads 4 \
    --ops $(( ${OPS[$wl]} / 8 + 3 )) --hogs 3 --hog-cpu "$HOG_CPU" --cpu-ms 10 \
    --sleep-us 2000 --io-size "${IOSZ[$wl]:-65536}" > /dev/null 2>&1 || true
done
for rep in $REPS; do
  for wl in $WORKLOADS; do
    for thr in 1 4; do
      # Alternate which mode goes first so order effects cancel over reps.
      if (( rep % 2 )); then modes="off on"; else modes="on off"; fi
      for mode in $modes; do
        one "$wl-t$thr-$mode-r$rep" "$rep" "$wl" "$thr" "$mode"
      done
    done
  done
  for wl in warm_read direct_read; do
    one "$wl-t4-offrepeat-r$rep" "$rep" "$wl" 4 offrepeat
    one "$wl-t4-nomarkers-r$rep" "$rep" "$wl" 4 nomarkers -M
    one "$wl-t4-nosyscalls-r$rep" "$rep" "$wl" 4 nosyscalls -S
    one "$wl-t4-nosched-r$rep" "$rep" "$wl" 4 nosched -C
    one "$wl-t4-noblock-r$rep" "$rep" "$wl" 4 noblock -K
    one "$wl-t4-rb64k-r$rep" "$rep" "$wl" 4 rb64k -b 65536
    one "$wl-t4-rb4m-r$rep" "$rep" "$wl" 4 rb4m -b 4194304
  done
  echo "  rep $rep done ($n runs, $(( $(date +%s) - start )) s)"
done
if [[ -n "${SUDO_USER:-}" ]]; then chown -R "$SUDO_USER": "$OUT"; fi
echo "done: $n runs, $failed failed, $(( $(date +%s) - start )) s"
echo "analyse: python3 src/classifier/overhead.py $OUT --out experiments/processed/$RUN_ID/overhead.json"
