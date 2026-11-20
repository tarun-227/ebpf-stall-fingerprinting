# Reproducibility guide

**Project:** eBPF-based fingerprinting of I/O-induced application stalls (Operating Systems, Assignment A3).
**Team:** Tarun Sekar S.D, Karthik Krishna K, Rohit A, Raihan.

The tool labels each application operation as `IO_WAIT_DOMINATED`, `SCHEDULER_WAIT_DOMINATED`, `SYSCALL_CPU_DOMINATED` or `INDETERMINATE` from syscall, scheduler and block-layer eBPF evidence, and scores those labels against the kernel's own per-thread accounting. This guide explains the code, its dependencies, how to set it up and run it, and how to regenerate every number in the report.

**All commands below are run from the `code/` folder** unless stated otherwise.

There are three levels of reproduction:

| Level | What you get | Needs root / eBPF? | Time |
|---|---|---|---|
| A. Report | Regenerates the report's tables and plot data from the shipped results, then compiles the PDF | No | about 1 minute |
| B. Analysis | Recomputes all results (features, evaluation, H1/H2, ablations, delay sweep) from the shipped raw traces | No | about 1 hour |
| C. Full rerun | Runs the workloads under the eBPF tracer again and records new raw traces | Yes | about 3–4 hours |

---

## 1. Project overview and code organisation

```
README.md                          overview of the submission
latex_report/
  main.tex, references.bib         IEEE conference report source
  report.pdf                       compiled report (6 pages)
  generated/                       tables (*.tex) and plot data (*.dat) that main.tex inputs
  figures/                         architecture.png, workflow.png, results.png (summary of all results)
  figures/source/                  LaTeX sources of the three summary figures
code/
  Makefile                         builds build/stallbench and build/stalltrace
  requirements.txt                 dependencies (Python: standard library only) and tool versions
  configs/eval.json                evaluation settings, fixed before the final runs
  src/workloads/stallbench.c       12 controlled workloads + ground-truth recording (C)
  src/ebpf/stalltrace.bpf.c        eBPF programs (libbpf CO-RE)
  src/ebpf/stalltrace.h            shared 48-byte event record (the event schema)
  src/collector/stalltrace.c       loader/collector: ring buffer -> events.bin, drop counters
  src/correlation/breakdown.py     per-thread state timeline, per-operation time split
  src/classifier/features.py       raw run -> one feature row per operation (+ reference label)
  src/classifier/models.py         rules and decision tree (pure-Python CART), feature sets
  src/classifier/evaluate.py       run-level split, metrics, CIs, H1, H2, ablations, transfer
  src/classifier/delay_analysis.py injected-delay sweep analysis
  src/classifier/overhead.py       tracing on vs off: throughput, p99, CPU per op, drops
  src/report/make_assets.py        writes ../latex_report/generated/ from the results
  src/report/make_figures.py       writes the source of the summary results figure
  src/label_trace.py               labels the operations of any traced program (Section 7)
  examples/marked_example.c        how to mark operations in your own program
  scripts/                         setup, checks, dm-delay, experiment runners, analysis, VM
  tests/                           unit tests (29)
documentation/
  reproducibility_guide.md / .pdf  this guide
supplementary/
  datasets/raw-wsl.tar.gz          raw traces, WSL: final-wsl (204 runs), delay-sweep-wsl (108)
  datasets/raw-vm.tar.gz           raw traces, VM: final-vm (204 runs), delay-sweep-vm (108)
  experimental_results/wsl/        processed results behind every number in the report
  experimental_results/vm/         processed results of the robustness VM
  additional_figures/              images of command outputs: tests, evaluation rerun, label_trace.py,
                                   breakdown.py, capability check, smoke test, live example
```

The raw traces of the overhead runs (1.6 GB for WSL) are not included because of their size. Their processed results (`experimental_results/wsl/overhead-v2.json`, `experimental_results/vm/overhead.json`) are included, and level C regenerates the raw data.

### How the pieces fit

1. `stallbench` runs a workload. It calls two empty marker functions, `op_begin(id)` and `op_end(id)`, around every operation. For each operation it writes the kernel's own accounting to `gt.jsonl`: thread CPU time and run-queue delay from `/proc/thread-self/schedstat`. This is the ground truth.
2. `stalltrace` starts the workload and attaches the eBPF programs: `raw_syscalls` enter/exit, `sched_switch`/`sched_wakeup`, the block `bio_queue`/`rq_issue`/`rq_complete` events, user page faults, and uprobes on the two markers. It writes every event to `events.bin` and the drop counters to `stats.json`.
3. `breakdown.py` replays each thread's events into states (run, runqueue, wake latency, blocked-I/O, blocked-other) and splits each operation's wall time between them.
4. `features.py` turns each operation into one row of features and attaches the reference label: a cause wins if it takes more than 60% of the wall time, otherwise the label is `INDETERMINATE`.
5. `evaluate.py` tunes thresholds and trains trees on repetitions 1–2, tests on repetitions 3–5, and writes `results.json` plus a text report.

---

## 2. Requirements

### Hardware

- x86-64 machine, at least 4 CPU cores (the contended workload pins CPU hogs to one core) and 4 GB RAM.
- About 3 GB free disk for level C (a 2 GB delay-device file plus raw traces); about 1 GB for level B.
- Level C must run on a disposable machine, VM or WSL instance: it drops page caches and creates a `dm-delay` device.

### Software and versions

| Component | Needed for | Version used for the reported results |
|---|---|---|
| Python | levels A, B, C (analysis) | 3.12.3; any 3.8+ works. Standard library only, no pip packages |
| Linux kernel with BTF (`/sys/kernel/btf/vmlinux`) | level C | 6.6.87 (WSL2, Ubuntu 24.04.3) and 6.8.0-147 (Ubuntu 24.04.5 VM) |
| clang / llvm | level C (eBPF build) | 18.1.3 |
| gcc, make, pkg-config | level C | gcc 13.3.0 |
| libbpf (`libbpf-dev`), libelf, zlib | level C | libbpf 1.3.0 |
| bpftool | level C (skeleton generation) | 7.4.0 |
| dmsetup and the `dm-delay` target | level C (injected delay) | libdevmapper 1.02.185 |
| TeX Live with IEEEtran, pgfplots, booktabs; latexmk | compiling the report | TeX Live 2023, latexmk 4.83 (Overleaf also works) |

`code/requirements.txt` lists the same. The exact environments of the final runs are recorded in `supplementary/experimental_results/wsl/manifest-final.txt` (kernel, distribution, CPU, RAM, delay-device table, SHA-256 of both binaries) and `supplementary/experimental_results/vm/{toolchain_versions,capabilities}.txt`.

---

## 3. Installation and setup

```bash
cd code
python3 -m unittest discover -s tests        # 29 tests, about 1 second; checks Python works
```

That is all levels A and B need. For level C, on Ubuntu 24.04 (root):

```bash
# install clang, llvm, libbpf-dev, bpftool, dmsetup and the build tools
sudo scripts/setup_vm.sh
# build build/stallbench and build/stalltrace
make
# check BTF, tracepoints and their fields, dm-delay (report in experiments/env/)
sudo scripts/check_capabilities.sh
# load the eBPF programs, run 3 workloads, compare with the kernel's accounting
sudo scripts/smoke_test.sh
```

In WSL, `linux-tools-$(uname -r)` does not exist; install `linux-tools-generic` instead (the Makefile finds its `bpftool`). The smoke test must report 0 dropped events and a traced time split within a few percent of the kernel's for `sleep`, `direct_read` and `cpu_contended`.

For the report: `sudo apt-get install texlive-latex-extra texlive-publishers texlive-pictures texlive-science latexmk`, or upload the `latex_report/` folder to Overleaf.

---

## 4. Configuration parameters and inputs

| Setting | Value | Where |
|---|---|---|
| Dominance threshold | 60% of wall time | `configs/eval.json` `dominance`; `features.py`; `stallbench.c` |
| Train / test split | repetitions 1–2 / 3–5, by whole run | `configs/eval.json` `dev_reps`, `test_reps` |
| Bootstrap | 1,000 resamples over test runs, seed 20261111 | `configs/eval.json` |
| Decision tree | depth 4, minimum leaf 5, abstention grid 0–0.9 | `configs/eval.json` `tree` |
| Transfer test | train 64 KiB sequential, test 16 KiB random | `configs/eval.json` `transfer` |
| Experiment matrix | 12 workloads × {1, 4, 16} threads × 5 repetitions + 24 transfer runs; 48 ops per run | `scripts/run_matrix.sh` (`REPS`, `THREADS`, `OPS_TOTAL`, ...) |
| Injected delay | 2 ms (main), 0/5/20 ms (sweep) | `scripts/run_final_wsl.sh` `MAIN_DELAY_MS`; `run_delay_sweep.sh` `DELAYS` |
| Ring buffer | 256 KiB | `stalltrace -b` |
| Overhead budget | ≤ 10% throughput loss, < 1% dropped events | `src/classifier/overhead.py` |

**Inputs.** The workloads create their own data files (`stallbench --prepare`); no external dataset is needed. Levels A and B take the shipped results or raw traces as input. Level C needs only a writable directory on a local Linux file system.

---

## 5. Level A: regenerate the report tables and compile

```bash
# read ../supplementary/experimental_results/{wsl,vm}, write ../latex_report/generated/
python3 src/report/make_assets.py
cd ../latex_report && latexmk -pdf main.tex
```

**Expected:** `make_assets.py` rewrites the files in `generated/` with identical content; `main.pdf` has 6 pages, and Table II shows fused rules at macro-F1 0.989 [0.979, 0.996] and the fused tree at 0.986.

The summary figures in `latex_report/figures/` are built the same way. `results.tex` is generated from the result files; `architecture.tex` and `workflow.tex` are drawn by hand. Each compiles to a one-page PDF, which is then saved as a PNG at 300 dpi (with any PDF-to-image tool, for example `pdftoppm -png -r 300 -singlefile results.pdf results`):

```bash
python3 src/report/make_figures.py     # writes ../latex_report/figures/source/results.tex
cd ../latex_report/figures/source && pdflatex architecture.tex && pdflatex workflow.tex && pdflatex results.tex
```

**Expected:** `results.png` shows (a) macro-F1 by evidence source for WSL and the VM, (b) high-confidence error forced vs abstaining (18.0% → 0.7% for WSL rules), (c) traced vs kernel stall time on a log–log plot (maximum error 125 µs WSL, 422 µs VM), and (d) throughput loss per workload against the 10% budget.

---

## 6. Level B: recompute the results from the raw traces

```bash
mkdir -p ../raw && tar xzf ../supplementary/datasets/raw-wsl.tar.gz -C ../raw
scripts/analyse_results.sh ../raw wsl
```

This rewrites `supplementary/experimental_results/wsl/` and `latex_report/generated/`. The overhead step is skipped because its raw data is not shipped. The PID-only window ablation dominates the run time (its 5 ms case alone takes about 30 minutes).

To recompute only the main evaluation (a few minutes):

```bash
python3 src/classifier/features.py ../raw/final-wsl --out /tmp/features.csv
python3 src/classifier/evaluate.py /tmp/features.csv \
    --config configs/eval.json --out /tmp/results
```

**Expected output** (identical to the shipped files, because the bootstrap seed is fixed):

| File | Key numbers |
|---|---|
| `features.txt` | 9,792 operations from 204 runs: 4,027 IO / 819 SCHED / 3,440 CPU / 1,506 INDETERMINATE |
| `results-ref.txt` | rules/fused 0.989, tree/fused 0.986, tree/syscall 0.647, tree/block 0.852; H1 without iowait +0.109 (rules), +0.019 (tree); H2 18.0% → 0.7% |
| `results-design.txt` | tree/fused 0.946 against the design label; labels agree on 94.9% of operations |
| `delay.txt` | traced vs kernel I/O-blocked time within 2–125 µs; about 1.1 waits per read, 2.3 per sync write, 17 per cold mmap op |
| `window-{5,20,100}ms.txt` | tree/fused 0.809, 0.898, 0.889 |
| `run-checks.txt` | final: 766,242 events, 0.54% dropped, 26 drift-flagged runs |

The robustness VM results are recomputed the same way from `raw-vm.tar.gz`:

```bash
mkdir -p ../raw && tar xzf ../supplementary/datasets/raw-vm.tar.gz -C ../raw
OUT=../supplementary/experimental_results/vm
python3 src/classifier/features.py ../raw/final-vm --out $OUT/features.csv
python3 src/classifier/evaluate.py $OUT/features.csv --config configs/eval.json \
    --out $OUT/results-ref --train-from \
    ../supplementary/experimental_results/wsl/features.csv > $OUT/results-ref.txt
python3 src/classifier/features.py ../raw/delay-sweep-vm --out /tmp/vm-delay.csv
python3 src/classifier/delay_analysis.py /tmp/vm-delay.csv --out $OUT/delay.json
```

Expected: rules/fused 0.971 [0.958, 0.980]; trained on WSL, tree/fused without iowait 0.695; traced vs kernel I/O-blocked time within 422 µs. `--train-from` trains the classifiers on another environment's development runs and scores them here (`cross_environment` in `results.json`).

Useful single commands:

```bash
# score against the design label instead of the reference label
python3 src/classifier/evaluate.py /tmp/features.csv --config configs/eval.json \
    --out /tmp/res-d --truth expected
# PID-only mode: fixed 20 ms windows instead of markers
python3 src/classifier/features.py ../raw/final-wsl --out /tmp/w20.csv --window-ms 20
# time split of every operation in one run
R=../raw/final-wsl/direct_read-t4-io64k-seq-d2ms-r3
python3 src/correlation/breakdown.py $R $R/gt.jsonl --summary
```

---

## 7. Level C: full rerun (root)

After the setup in Section 3:

```bash
# base directory defaults to /home/$SUDO_USER/stalltest
sudo scripts/run_final_wsl.sh
```

Despite its name, the script works on any Linux host. It runs, in order:

1. **Delay device:** creates a 2 GB loop file with a `dm-delay` device on it, formats it ext4 and mounts it at `<base>/mnt` (skipped if the device already exists).
2. **Delay check:** confirms that the delay shows up as direct-read latency.
3. **Main matrix** (`raw/final-wsl`): 2 ms delay; 12 workloads × {1, 4, 16} threads × 5 repetitions, plus 24 transfer runs. About 1.5 hours.
4. **Delay sweep** (`raw/delay-sweep-wsl`): 0, 5 and 20 ms delay; 6 workloads × {1, 4} threads × 3 repetitions.
5. **Overhead** (`raw/overhead-wsl`): tracing off vs on, interleaved, 5 repetitions, plus variants (no markers, no syscall/scheduler/block events, 64 KiB and 4 MiB ring buffers, a second untraced run). The report's run id is reproduced with `sudo env STEPS=overhead OVERHEAD_ID=overhead-wsl-v2 scripts/run_final_wsl.sh`.

`STEPS="check matrix sweep overhead smoke"` selects steps. The log goes to `<base>/final-run.log`. Results are never overwritten: a rerun with an existing id stops. Then analyse:

```bash
OVERHEAD_ID=overhead-wsl-v2 OVERHEAD_OUT=overhead-v2 \
    scripts/analyse_results.sh /home/$USER/stalltest/raw wsl
```

### Robustness VM

The VM results come from an Ubuntu 24.04 cloud image (kernel 6.8) under QEMU/KVM inside WSL2. `scripts/vm_qemu.sh` builds and boots it; it needs `qemu-system-x86 qemu-utils cloud-image-utils` and membership of group `kvm`:

```bash
mkdir -p ~/vm && curl -fLo ~/vm/noble.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
scripts/vm_qemu.sh create && scripts/vm_qemu.sh start     # SSH on localhost:2222, user stall
```

Copy `code/` into the VM, then inside it (`vdb` is the 4 GB test disk, host caching off):

```bash
sudo scripts/setup_vm.sh && make && sudo scripts/check_capabilities.sh
sudo scripts/dm_delay.sh create vdb 0
sudo scripts/dm_delay.sh format --wipe vdb         # DESTROYS vdb
sudo env STEPS="check matrix sweep overhead" OVERHEAD_ID=overhead-vm \
    scripts/run_final_wsl.sh ~/stalltest
```

The script names its outputs `final-wsl` and `delay-sweep-wsl`; rename them to `final-vm` and `delay-sweep-vm`, copy them out, and analyse as in Section 6. The runs take about 25 minutes.

### Running the tool on its own

```bash
./build/stallbench --prepare --file $HOME/stall.bin --size $((256<<20))
sudo ./build/stalltrace -o /tmp/run1 -- ./build/stallbench --workload direct_read \
     --file $HOME/stall.bin --run-id run1 --out /tmp/run1/gt.jsonl --threads 4 --ops 50
python3 src/correlation/breakdown.py /tmp/run1 /tmp/run1/gt.jsonl --summary
```

`direct_read` uses `O_DIRECT`, so the data file must be on a disk-backed file system, not tmpfs. `stalltrace -h` and `stallbench --help` list all options, including `-b` (ring-buffer size), `-S/-C/-K/-F/-M` (disable event families), `-J` (JSON output) and `--no-ref` (skip ground-truth reads during overhead runs).

### Using the tool on your own program

The tracer works on any program; `src/label_trace.py` turns its trace into a label per operation. It uses the same timeline and the same fused rule (threshold 0.6, as tuned in the report) as the evaluation, and needs no ground truth. On the shipped traces it gives the same labels as the evaluation pipeline.

**1. Mark the operations.** Add these two functions to the program, unchanged, and call them around each operation you want labelled. The tracer finds them by name and attaches uprobes; `noinline, used` and the empty `asm` stop the compiler from removing them.

```c
#include <stdint.h>
__attribute__((noinline, used)) void op_begin(uint64_t id) { __asm__ volatile("" :: "r"(id)); }
__attribute__((noinline, used)) void op_end(uint64_t id)   { __asm__ volatile("" :: "r"(id)); }

op_begin(42);
/* the operation: a read, a request handler, a query, ... */
op_end(42);
```

Use any id, the same for both calls. Several threads may run operations at the same time; they are matched per thread. `examples/marked_example.c` is a complete program with three kinds of operation.

**2. Build, trace and label** (from `code/`, as root for the tracer):

```bash
gcc -O0 -g examples/marked_example.c -o build/marked_example
sudo ./build/stalltrace -o /tmp/example_run -- ./build/marked_example $HOME/example.dat
python3 src/label_trace.py /tmp/example_run
```

For a program in another language or a shared library, the markers must be in the binary that `stalltrace` hooks: by default the traced command, otherwise name it with `-u PATH`. To trace a program that is already running, use `-p PID` instead of `-- COMMAND`.

**3. Read the output.** One line per operation (the first 50; `--all` prints every one), then a summary:

```
    tid           op       wall    run  runq  wake    io other  sysc   bioKB  label
   1951            0   16.345ms    51%    0%    0%   49%    0%   50%      64  INDETERMINATE  (likeliest: SYSCALL_CPU)
```

| Column | Meaning |
|---|---|
| `wall` | operation duration |
| `run`, `runq`, `wake` | share of the duration on a CPU, waiting for a CPU, and waking an idle CPU |
| `io`, `other` | share blocked with the kernel's `in_iowait` flag set (storage), and blocked otherwise (sleep, locks) |
| `sysc` | share inside system calls |
| `bioKB` | disk I/O the thread itself submitted during the operation |
| `label` | a cause that takes more than 60% of the time and is supported by the evidence; otherwise `INDETERMINATE`, with the likeliest cause in brackets |

The summary gives the number of operations per label and their share of the traced time; `--csv FILE` writes every operation with all its features.

**Without markers.** If the program cannot be changed, `python3 src/label_trace.py /tmp/myrun --window-ms 20` labels fixed 20 ms windows of each thread instead. Windows mix operations, so this is less accurate (the report measures macro-F1 0.81–0.90 for 5–100 ms windows, against 0.99 with markers).

**Limits.** The labels are as reliable as in the report only for synchronous work by the waiting thread; asynchronous I/O (`io_uring`, AIO) and work handed to other threads are not attributed. The two markers add about 14 µs of CPU per operation, so marking microsecond operations slows them down noticeably.

---

## 8. Output files and evaluation metrics

### Raw output of one run (`<raw>/<matrix>/<run_id>/`)

| File | Contents |
|---|---|
| `events.bin` | 16-byte header (`STEVENT`, schema version, record size), then 48-byte records `<QIIIHHQQII>`: ts, pid, tid, cpu, type, flags, a, b, dev, c (meaning per type in `src/ebpf/stalltrace.h`) |
| `gt.jsonl` | first line: run header (workload, threads, clock ratio, drift flag); then one line per operation (times, reference CPU/runq/blocked, design and reference labels) |
| `stats.json` | programs attached, devices, events produced/dropped/received per type, collector CPU |
| `meta.json` | run parameters, injected delay |
| `../manifest.txt` | per matrix: kernel, distribution, CPU, RAM, `dm` table, SHA-256 of both binaries |

### Processed results (`supplementary/experimental_results/<env>/`)

| File | Contents |
|---|---|
| `features.csv` | one row per operation: run parameters, time shares, syscall/block/fault features, reference and design labels |
| `results-ref/results.json`, `results-ref.txt` | main evaluation against the reference label: every classifier, H1, H2, confusion matrices, ablations, transfer, sensitivity |
| `results-ref/fused_tree.txt` | the trained decision tree |
| `results-design*` | the same against the design label |
| `window-{5,20,100}ms*` | PID-only window ablation |
| `delay.json`, `delay.txt` | injected-delay sweep: traced vs kernel blocked time |
| `overhead-v2.json`/`.txt` (WSL), `overhead.json`/`.txt` (VM) | tracing overhead per workload and variant |
| `run-checks.txt` | events, drops and clock-drift flags per experiment |

### Metrics

- **Macro-F1:** mean of per-class F1 over the four labels, with a 95% bootstrap confidence interval over test runs.
- **Accuracy, per-class precision and recall, confusion matrix.**
- **Abstention rate:** share of operations labelled `INDETERMINATE` by a classifier.
- **High-confidence error rate:** share of operations given a wrong non-`INDETERMINATE` label; "forced" is the same for the variant that may not abstain.
- **H1:** macro-F1 of fused minus single-source evidence, paired bootstrap CI. **H2:** forced minus abstaining high-confidence error, paired bootstrap CI.
- **Stall-duration error:** absolute difference between traced and kernel blocked time per operation.
- **Overhead:** throughput loss, p50/p99 latency change, added CPU per operation, collector CPU, dropped-event rate, against the budget above.

---

## 9. Known limitations and pitfalls

- **WSL clock:** the kernel's `sched_clock` ran 0.92–1.09× the speed of `CLOCK_MONOTONIC` and drifts over minutes. `stallbench` measures the ratio at the start and end of each run, rescales, and flags drifting runs (`clock_drift_flag`); results without flagged runs are in `results-ref.txt` (`sensitivity_clean_runs`).
- **Injected delay under WSL:** a nominal 5 ms delay added about 11 ms per read and 20 ms about 23 ms, while the VM added close to the nominal value. Use the measured per-operation blocked time, not the nominal delay.
- **Disk cache:** WSL's virtual disk is cached by Windows, so `cold_read` without `dm-delay` is often served from memory. Always run on the delay mount.
- **Raw output location:** keep `RAW_DIR` on the Linux file system (`/home/...`), not `/mnt/c`; writing to the Windows drive is slow enough to cause dropped events.
- **Overhead is noisy in virtual machines:** a second untraced `direct_read` run differed from the first by 41%. Compare CPU per operation as well as throughput.
- **Marker cost:** the two marker uprobes add about 14 µs of CPU per operation in WSL (more under nested virtualisation). For microsecond operations tracing costs 60–90% throughput; use PID-only windows (`--window-ms`) or disable markers (`-M`) there, at a cost in accuracy.
- **Scope:** synchronous I/O by the waiting thread. Asynchronous I/O (`io_uring`, AIO) is not attributed.
