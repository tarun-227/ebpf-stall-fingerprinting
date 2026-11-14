# eBPF-Based Fingerprinting of I/O-Induced Application Stalls

**Operating Systems, Assignment 3**
Tarun Sekar S.D · Karthick Krishna K · Rohit A · Raihan

When an application operation is slow, this tool explains **why**. It traces each operation with eBPF at three kernel layers (system calls, the scheduler and the block I/O layer), splits the operation's wall time into causes, and gives it one label:

| Label | Meaning |
|---|---|
| `IO_WAIT_DOMINATED` | the thread spent most of the operation blocked on storage |
| `SCHEDULER_WAIT_DOMINATED` | the thread was runnable but waiting for a CPU |
| `SYSCALL_CPU_DOMINATED` | the thread was mostly running (user code or kernel syscall path) |
| `INDETERMINATE` | no cause dominates, or the evidence conflicts, so the tool declines to decide |

The labels are graded against the kernel's own per-thread accounting (`schedstat` and the thread CPU clock), on 12 controlled workloads with a known disk delay injected by `dm-delay`.

---

## Deliverables

| Deliverable | Location |
|---|---|
| IEEE report (PDF) | [`report/report.pdf`](report/report.pdf); also in the [release](https://github.com/tarun-227/ebpf-stall-fingerprinting/releases/latest) |
| LaTeX source of the report | [`report/`](report/) (`main.tex`, `refs.bib`, `generated/`); also `report-latex.zip` in the release |
| Code ZIP (source, configs, processed results, raw traces) | `code.zip` in the release |
| Reproduction guide | [`REPRODUCE.md`](REPRODUCE.md) |
| Raw traces | `raw-wsl.tar.gz` and `raw-vm.tar.gz` in the release |

---

## Research questions and results

- **H1:** combining evidence from the syscall, scheduler and block layers labels operations more accurately than any single layer does.
- **H2:** letting the tool answer `INDETERMINATE` reduces confident wrong answers.

Main environment: WSL2, kernel 6.6. The evaluation uses 204 runs and 9,792 operations. Thresholds were tuned and trees trained on repetitions 1–2; the scores below are on repetitions 3–5 (5,184 operations). Brackets give 95% bootstrap confidence intervals.

| Evidence used | Macro-F1 (WSL) | Macro-F1 (VM) |
|---|---|---|
| **Fused, rules (all three layers)** | **0.989** [0.979, 0.996] | **0.971** [0.958, 0.980] |
| Fused, decision tree | 0.986 | 0.966 |
| Scheduler only (tree) | 0.986 | 0.969 |
| Block layer only (tree) | 0.852 | 0.818 |
| Syscall only (tree) | 0.647 | 0.788 |

- **H2 is supported.** With abstention, high-confidence errors fall from 18.0% to 0.7% in WSL and from 19.2% to 1.4% in the VM. The tool abstains on about 17–19% of operations.
- **H1 is supported, with a condition.** Fusion beats syscall-only and block-only evidence by a wide margin. It ties scheduler-only evidence when the kernel's `in_iowait` flag is available. Without that flag, fusion adds +0.109 [0.051, 0.176] (WSL) and +0.111 [0.056, 0.171] (VM).
- **Stall time.** The traced I/O wait per operation matches the kernel's within 125 µs (WSL) and 422 µs (VM) at 0, 5 and 20 ms of injected delay.
- **Robustness.** The same experiments were repeated in an Ubuntu 24.04 VM (kernel 6.8, QEMU/KVM). Rules tuned on WSL runs score the same there. A tree trained on WSL without `in_iowait` does not carry over (0.695).
- **Overhead.**
  - Millisecond-scale operations stay within the 10% budget in WSL.
  - Microsecond operations lose 60–90% of their throughput, mostly to the two marker uprobes (7–10 µs each in WSL, more under nested virtualisation).

Full tables are in the report and in [`experiments/processed/`](experiments/processed/).

---

## How it works

```
stallbench (workload)  --op_begin/op_end markers-->  stalltrace (eBPF + collector)
       |                                                    |
   gt.jsonl  (kernel accounting per operation)        events.bin, stats.json
       \__________________________  _______________________/
                                  \/
             breakdown.py   per-thread state timeline -> time split per operation
             features.py    one feature row + reference label per operation
             evaluate.py    rules and decision tree, metrics, H1, H2, ablations
```

1. **`stallbench`** runs one of 12 workloads and wraps every operation in two marker functions. For each operation it records the kernel's accounting, which serves as ground truth.
2. **`stalltrace`** loads the eBPF programs. These are syscall enter/exit, `sched_switch` and `sched_wakeup`, block bio and request events, user page faults, and uprobes on the markers. Events are written to `events.bin` through a ring buffer, and dropped events are counted.
3. **The Python analysis** rebuilds each thread's timeline: running, run queue, wake latency, blocked on I/O and blocked otherwise. It computes features per operation, then labels each operation and scores the labels.

---

## Repository layout

```
src/workloads/stallbench.c      12 test workloads and ground-truth recording (C)
src/ebpf/stalltrace.bpf.c       eBPF programs (libbpf CO-RE)
src/ebpf/stalltrace.h           event record shared by kernel and user space
src/collector/stalltrace.c      loader and collector
src/correlation/breakdown.py    per-thread timeline and per-operation time split
src/classifier/                 features, rules, decision tree, evaluation, delay and overhead analysis
src/report/make_assets.py       generates the report's tables and plot data from the results
scripts/                        toolchain setup, capability check, dm-delay helper, experiment runners
tests/                          unit tests (29)
experiments/configs/eval.json   evaluation settings, fixed before the final runs
experiments/processed/wsl/      final results (main environment)
experiments/processed/vm/       robustness results (Ubuntu VM)
report/                         report PDF (report.pdf) and its LaTeX source
REPRODUCE.md                    full reproduction guide
```

---

## Setup

### Requirements

| To do | You need |
|---|---|
| Inspect results, rerun the analysis | Python 3.8+ (standard library only) |
| Rebuild the report | a LaTeX distribution with `IEEEtran`, `pgfplots`, `tikz` (or Overleaf) |
| Build and run the tracer | Linux 5.15+ with BTF (`/sys/kernel/btf/vmlinux`), root, `clang`, `libbpf-dev`, `bpftool`, `gcc`, `make`; `dmsetup` for delay injection. Tested on Ubuntu 24.04 (kernel 6.8) and WSL2 (kernel 6.6). |

### Get the code

```bash
git clone https://github.com/tarun-227/ebpf-stall-fingerprinting.git
cd ebpf-stall-fingerprinting
python3 -m unittest discover -s tests        # 29 tests, under a second
```

### Build the tracer (Linux, root)

```bash
sudo scripts/setup_vm.sh            # installs clang, libbpf, bpftool, dmsetup
make                                # builds build/stallbench and build/stalltrace
sudo scripts/check_capabilities.sh  # confirms the needed tracepoints, BTF fields and dm-delay
sudo scripts/smoke_test.sh          # traces three workloads and compares with the kernel's accounting
```

---

## Usage

### 1. Trace one workload and label its operations

```bash
./build/stallbench --prepare --file $HOME/stall.bin --size $((256<<20))
sudo ./build/stalltrace -o /tmp/run1 -- \
     ./build/stallbench --workload direct_read --file $HOME/stall.bin \
     --run-id run1 --out /tmp/run1/gt.jsonl --threads 4 --ops 50
python3 src/correlation/breakdown.py /tmp/run1 /tmp/run1/gt.jsonl --summary
```

`direct_read` uses `O_DIRECT`, so the data file must be on a disk-backed file system (not `/tmp` on tmpfs). `breakdown.py` prints each operation's time split (run, run queue, I/O wait and so on) next to the kernel's numbers. `stallbench --help` lists the 12 workloads, including `cold_read`, `sync_write`, `mmap_cold`, `cpu_contended` and `mixed`.

### 2. Recompute the evaluation from raw traces (no root)

Download `raw-wsl.tar.gz` from the release, then run:

```bash
mkdir -p raw && tar xzf raw-wsl.tar.gz -C raw
python3 src/classifier/features.py raw/final-wsl --out /tmp/features.csv
python3 src/classifier/evaluate.py /tmp/features.csv \
        --config experiments/configs/eval.json --out /tmp/results
```

The printed table should match `experiments/processed/wsl/results-ref.txt`. The run takes a few minutes, mostly for the bootstrap.

### 3. Rebuild the report tables and PDF

```bash
python3 src/report/make_assets.py          # results -> report/generated/*.tex
cd report && latexmk -pdf main.tex
```

### 4. Rerun all experiments (Linux, root, about 3–4 hours)

```bash
sudo scripts/run_final_wsl.sh   # dm-delay test filesystem, final matrix, delay sweep, overhead
```

The script works on any Linux host, not only WSL. When no spare disk is available, it backs the delay device with a loop file. [`REPRODUCE.md`](REPRODUCE.md) covers:
- every option;
- the second-environment (VM) setup, using `scripts/vm_qemu.sh`;
- the expected output of each step;
- known limitations: WSL clock drift, `dm-delay` delays that are longer than nominal under WSL, and host disk caching.

---

## Limitations

- All workloads are synthetic and single-purpose. Real applications and asynchronous I/O (`io_uring`) are not evaluated.
- Both test environments are virtualised (WSL2 and a KVM guest); bare metal is untested.
- The per-operation markers cost microseconds each. The tool therefore suits diagnosing millisecond-scale stalls, not always-on tracing of very short operations.
