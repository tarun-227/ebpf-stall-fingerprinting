# Reproducing the results

This document explains the code, its dependencies, how to run it, and how to regenerate every number in the report.

Project: **eBPF-based fingerprinting of I/O-induced application stalls** (OS Assignment 3).
The tool labels each application operation as `IO_WAIT_DOMINATED`, `SCHEDULER_WAIT_DOMINATED`, `SYSCALL_CPU_DOMINATED` or `INDETERMINATE` from syscall, scheduler and block-layer eBPF evidence. It then scores those labels against the kernel's own accounting.

There are three levels of reproduction. Pick the one you need.

| Level | What you get | Needs root / eBPF? | Time |
|---|---|---|---|
| A. Report tables only | Rebuilds `report/generated/` from the processed results shipped in the ZIP | No | seconds |
| B. Analysis from raw events | Rebuilds `experiments/processed/wsl/` (features, evaluation, ablations, delay sweep) from the raw traces shipped in the ZIP | No | about 1 hour |
| C. Full rerun | Runs the workloads under the tracer again and produces new raw traces | Yes | about 3–4 hours |

---

## 1. What is in the code ZIP

```
Makefile                      builds build/stallbench and build/stalltrace
src/workloads/stallbench.c    12 controlled workloads + ground truth (C)
src/ebpf/stalltrace.bpf.c     eBPF programs (libbpf CO-RE)
src/ebpf/stalltrace.h         shared 48-byte event record
src/collector/stalltrace.c    loader/collector: ring buffer -> events.bin, drop counters
src/correlation/breakdown.py  per-thread state timeline, per-operation time split
src/classifier/features.py    raw run -> one feature row per operation (+ reference label)
src/classifier/models.py      rules, decision tree (pure-Python CART), feature sets
src/classifier/evaluate.py    split, metrics, bootstrap CIs, H1, H2, ablations, transfer
src/classifier/delay_analysis.py   injected-delay sweep analysis
src/classifier/overhead.py    tracing on vs off: throughput, p99, CPU/op, drops
src/report/make_assets.py     LaTeX tables and pgfplots data for the report
scripts/                      setup, capability check, dm-delay, experiment runners, analysis
tests/                        unit tests for correlation and classifiers (29 tests)
experiments/configs/eval.json evaluation settings, frozen before the final runs
experiments/processed/wsl/    final processed results (the numbers in the report)
experiments/processed/pilot-wsl/   pilot results (tuning only, not reported)
report/                       LaTeX source of the report (main.tex, refs.bib, generated/)
data/raw-wsl.tar.gz           raw traces: final-wsl (204 runs), delay-sweep-wsl (108 runs)
RESEARCH_PLAN.md, PROJECT.md, TASK.md   plan, fixed decisions, checklist with results
```

The raw traces of the overhead runs (1.6 GB) are not in the ZIP. Their processed results (`experiments/processed/wsl/overhead-v2.json`) are included, and level C regenerates the raw traces.

### How the pieces fit

1. `stallbench` runs a workload. It calls two empty marker functions, `op_begin(id)` and `op_end(id)`, around every operation. For each operation it writes the kernel's own accounting to `gt.jsonl`: thread CPU time and run-queue delay from `/proc/thread-self/schedstat`. This is the ground truth.
2. `stalltrace` starts the workload. It attaches the eBPF programs: `raw_syscalls` enter/exit, `sched_switch`/`sched_wakeup`, the block `bio_queue`/`rq_issue`/`rq_complete` events, user page faults, and uprobes on the two markers. It writes every event to `events.bin` and the drop counters to `stats.json`.
3. `breakdown.py` replays each thread's events into states: run, runqueue, wake-latency, blocked-I/O and blocked-other. It then splits each operation's wall time between those states.
4. `features.py` turns each operation into one row of features. It also attaches the reference label: a cause wins if it takes more than 60% of the wall time, otherwise the label is `INDETERMINATE`.
5. `evaluate.py` tunes thresholds and trains trees on repetitions 1–2. It tests on repetitions 3–5 and writes `results.json` plus a text report.

### Output files of one run (`<raw>/<matrix>/<run_id>/`)

| File | Contents |
|---|---|
| `events.bin` | 16-byte header (`STEVENT`, schema, record size), then 48-byte records `<QIIIHHQQII>`: ts, pid, tid, cpu, type, flags, a, b, dev, c |
| `gt.jsonl` | first line: run header (workload, threads, clock ratio, drift flag); then one line per operation (times, reference CPU/runq/blocked, labels) |
| `stats.json` | programs attached, devices, events produced/dropped/received per type, collector CPU |
| `meta.json` | run parameters, injected delay |

---

## 2. Dependencies

**Analysis (levels A and B):** Python 3.8 or newer, standard library only. No pip packages are needed.

**Tracing (level C):**
- Linux 5.15 or newer with BTF (`/sys/kernel/btf/vmlinux`). The results were produced on WSL2 kernel 6.6.87 with Ubuntu 24.04.
- `clang`, `llvm`, `libbpf-dev`, `libelf-dev`, `zlib1g-dev`, `bpftool` (from `linux-tools-$(uname -r)` or `linux-tools-generic`), `make`, `gcc`, `pkg-config`.
- `dmsetup` and the `dm-delay` kernel module, for injected disk delay.
- Root, to load eBPF programs and set up the delay device.
- At least 4 CPUs (the contended workload pins CPU hogs), and about 3 GB of free disk.

On Ubuntu 24.04 you can install everything with:

```bash
sudo scripts/setup_vm.sh
```

The script also creates a Python venv with numpy and pandas for convenience; the analysis does not need them. In WSL, `linux-tools-$(uname -r)` does not exist. Install `linux-tools-generic` instead; the Makefile finds its `bpftool`.

**Report:** a LaTeX distribution with IEEEtran, pgfplots and booktabs (Overleaf has them all). On Ubuntu:

```bash
sudo apt-get install texlive-latex-extra texlive-publishers texlive-pictures texlive-science latexmk
```

---

## 3. Level A: regenerate the report tables and compile

```bash
python3 src/report/make_assets.py          # reads experiments/processed/wsl, writes report/generated/
cd report && latexmk -pdf main.tex         # or upload main.tex, refs.bib and generated/ to Overleaf
```

**Expected:** `main.pdf`, 5 pages. Table II shows fused rules at macro-F1 0.989 [0.979, 0.996] and the fused tree at 0.986.

---

## 4. Level B: rerun the analysis from the raw traces

```bash
python3 -m unittest discover -s tests      # 29 tests, all OK
mkdir -p raw && tar xzf data/raw-wsl.tar.gz -C raw
scripts/analyse_results.sh raw wsl         # rewrites experiments/processed/wsl/ and report/generated/
```

The overhead step is skipped because its raw data is not shipped. The window ablation dominates the run time; its 5 ms case alone takes about 30 minutes.

**Expected output**, identical to the shipped files because the bootstrap seed is fixed in `eval.json`:

| File | Key numbers |
|---|---|
| `features.txt` | 9,792 operations from 204 runs: 4,027 IO / 819 SCHED / 3,440 CPU / 1,506 INDETERMINATE |
| `results-ref.txt` | rules/fused 0.989, tree/fused 0.986, tree/syscall 0.647, tree/block 0.852; H1 without iowait +0.109 (rules), +0.019 (tree); H2 18.0% → 0.7% |
| `results-design.txt` | tree/fused 0.946 against the design label; labels agree on 94.9% of operations |
| `delay.txt` | traced vs kernel I/O-blocked time within 2–125 µs; ≈1.1 waits per read, ≈2.3 per sync write, ≈17 per cold mmap op |
| `window-{5,20,100}ms.txt` | tree/fused 0.809, 0.898, 0.889 |
| `run-checks.txt` | final-wsl: 766,242 events, 0.54% dropped, 26 drift-flagged runs |

`git diff --stat experiments/processed report/generated` should then show no changes.

Useful single commands:

```bash
python3 src/classifier/features.py raw/final-wsl --out /tmp/f.csv                 # feature table
python3 src/classifier/evaluate.py /tmp/f.csv --config experiments/configs/eval.json --out /tmp/res
python3 src/classifier/evaluate.py /tmp/f.csv --config experiments/configs/eval.json --out /tmp/res-d --truth expected
python3 src/classifier/features.py raw/final-wsl --out /tmp/w20.csv --window-ms 20  # PID-only windows
python3 src/correlation/breakdown.py raw/final-wsl/<run_id>                        # per-op time split of one run
```

---

## 5. Level C: full rerun (root)

### 5.1 Build and check

```bash
make                                   # build/stallbench, build/stalltrace
sudo scripts/check_capabilities.sh     # kernel, BTF, tracepoints, dm-delay
sudo scripts/smoke_test.sh             # loads the eBPF programs, runs 3 workloads
```

The smoke test must report 0 dropped events. It must also show the traced time split within a few percent of the kernel's for `sleep`, `direct_read` and `cpu_contended`.

### 5.2 All final experiments in one command (WSL or a VM without a spare disk)

```bash
sudo scripts/run_final_wsl.sh                  # base directory defaults to /home/$SUDO_USER/stalltest
```

The script runs these steps in order:

1. **Delay device:** creates a 2 GB loop file and a `dm-delay` device on it, then formats and mounts it at `<base>/mnt`.
2. **Delay check:** confirms that the delay is visible as direct-read latency.
3. **Main matrix** (`raw/final-wsl`): 2 ms delay; 12 workloads × {1, 4, 16} threads × 5 repetitions, plus 24 transfer runs (16 KiB random I/O). About 1.5 hours.
4. **Delay sweep** (`raw/delay-sweep-wsl`): 0, 5 and 20 ms delay; 6 workloads × {1, 4} threads × 3 repetitions.
5. **Overhead** (`raw/overhead-wsl`): tracing off vs on, interleaved, 5 repetitions, plus variants (no markers, no syscall/scheduler/block events, 64 KiB and 4 MiB ring buffers, a second untraced run). To reproduce the report's run id:

   ```bash
   sudo env STEPS=overhead OVERHEAD_ID=overhead-wsl-v2 scripts/run_final_wsl.sh
   ```

`STEPS="check matrix sweep overhead smoke"` picks steps. The log goes to `<base>/final-run.log`. Results are never overwritten: a rerun with an existing id stops instead.

Then analyse:

```bash
OVERHEAD_ID=overhead-wsl-v2 OVERHEAD_OUT=overhead-v2 scripts/analyse_results.sh /home/$USER/stalltest/raw wsl
```

### 5.3 On a VM with a spare test disk (robustness setup)

```bash
sudo scripts/dm_delay.sh create sdb 2              # sdb = disposable disk, host I/O cache off
sudo scripts/dm_delay.sh format --wipe sdb         # DESTROYS sdb
sudo scripts/dm_delay.sh mount /mnt/stall
sudo env RAW_DIR=$HOME/raw DELAY_MS=2 THREADS="1 4" REPS="1 2 3" \
     DEVS="$(sudo scripts/dm_delay.sh status | grep -o '\-d [0-9:]*' | tr '\n' ' ')" \
     scripts/run_matrix.sh /mnt/stall/data final-vm
python3 src/classifier/features.py $HOME/raw/final-vm --out experiments/processed/vm/features.csv
```

The third `REPS` value doubles as the test split, so set `test_reps` in a copy of `eval.json` to `[3]`.

### 5.4 Running the tool on its own

```bash
./build/stallbench --prepare --file /mnt/stall/data/f.bin --size $((256<<20))
sudo ./build/stalltrace -o /tmp/run1 -- ./build/stallbench --workload direct_read \
     --file /mnt/stall/data/f.bin --run-id run1 --out /tmp/run1.gt.jsonl --threads 4 --ops 50
python3 src/correlation/breakdown.py /tmp/run1          # per-operation time split
```

`stalltrace -h` and `stallbench --help` list all options. These include `-b` for the ring-buffer size, `-S/-C/-K/-F/-M` to disable event families, `-J` for JSON output, and `--no-ref` to skip ground-truth readings during overhead runs.

---

## 6. Configuration that defines the results

| Setting | Value | Where |
|---|---|---|
| Dominance threshold | 60% of wall time | `eval.json` `dominance`, `features.py`, `stallbench.c` |
| Train / test split | repetitions 1–2 / 3–5 (by run) | `eval.json` |
| Bootstrap | 1,000 resamples over test runs, seed 20261111 | `eval.json` |
| Decision tree | depth 4, min leaf 5, abstain grid 0–0.9 | `eval.json` |
| Transfer | train 64 KiB sequential, test 16 KiB random | `eval.json` |
| Injected delay | 2 ms (main), 0/5/20 ms (sweep) | `run_final_wsl.sh` |
| Ring buffer | 256 KiB (default) | `stalltrace -b` |
| Overhead budget | ≤ 10% throughput loss, < 1% drops | `overhead.py` |

Each matrix directory has a `manifest.txt` with the kernel, CPU, `dm` table and SHA-256 of both binaries. Copies for the final runs are `experiments/processed/wsl/manifest-*.txt`.

---

## 7. Known limitations and pitfalls

- **WSL clock:** the kernel's `sched_clock` runs 0.92–1.09× the speed of `CLOCK_MONOTONIC` and drifts over minutes. `stallbench` measures the ratio at the start and end of each run, rescales, and flags drifting runs (`clock_drift_flag`). Results with flagged runs removed are in `results-ref.txt` (`sensitivity_clean_runs`).
- **`dm-delay` quantisation:** in WSL a nominal 5 ms became about 11 ms and 20 ms about 23 ms. Use the measured per-operation blocked time, not the nominal value.
- **Disk cache:** WSL's virtual disk is cached by Windows, so `cold_read` without `dm-delay` is often served from memory. Always run on the delay mount.
- **Raw output location:** put `RAW_DIR` on the Linux file system (for example `/home/...`), not `/mnt/c`. Writing to the Windows drive is slow enough to cause dropped events.
- **Overhead in VMs is noisy:** two identical untraced runs differed by 15–41% for disk reads. Compare CPU per operation as well as throughput.
- **Marker cost:** each uprobe costs about 7–10 µs in WSL. For workloads with microsecond operations, tracing costs 60–90% throughput; use PID-only windows (`--window-ms`) or remove markers (`-M`) there, at a cost in accuracy.
- **Scope:** synchronous I/O by the thread that waits. Asynchronous I/O (`io_uring`, AIO) is not attributed.
