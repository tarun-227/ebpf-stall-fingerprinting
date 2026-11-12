# eBPF Fingerprinting of I/O-Induced Application Stalls

OS course, Assignment 3. When an application operation is slow, this tool tells you **why**:

| Label | Meaning |
|---|---|
| `IO_WAIT_DOMINATED` | the thread was mostly blocked waiting for storage |
| `SCHEDULER_WAIT_DOMINATED` | the thread was runnable but waiting for a CPU |
| `SYSCALL_CPU_DOMINATED` | the thread was mostly running (user code or kernel syscall path) |
| `INDETERMINATE` | no single cause dominates, or the evidence conflicts: the tool says "I can't tell" |

It traces each operation with eBPF at three kernel layers (syscalls, scheduler, block I/O), splits the operation's wall time into causes, labels it, and is graded against the kernel's own per-thread accounting on workloads with **injected disk delay** (`dm-delay`).

## Main results (WSL2, kernel 6.6, 5,184 held-out operations)

| | Macro-F1 [95% CI] |
|---|---|
| **Fused rules (all three layers)** | **0.989** [0.979, 0.996] |
| Fused decision tree | 0.986 [0.973, 0.994] |
| Scheduler only | 0.986 |
| Block layer only | 0.852 |
| Syscall only | 0.647 |

- **H2 (abstention helps): supported.** High-confidence errors fall from 18.0% to 0.7% when the tool may answer `INDETERMINATE`. It does so for 17% of operations.
- **H1 (fusion helps): supported, with a condition.** Fused evidence beats syscall-only and block-only by a wide margin. It ties scheduler-only evidence while the kernel's `in_iowait` flag is available. Without that flag, fusion adds +0.109 (rules) and +0.019 (tree), and both confidence intervals are above zero.
- **Stall time:** traced I/O wait matches the kernel within 125 µs at 0, 5 and 20 ms injected delay.
- **Overhead:** within the 10% budget for millisecond operations. Microsecond operations lose 60–90% of their throughput, mostly from the marker probes (about 7–10 µs each).

Full numbers are in [`report/`](report/) and [`experiments/processed/wsl/`](experiments/processed/wsl/).

## Repository layout

```
src/workloads/stallbench.c     12 test workloads + ground truth (C)
src/ebpf/stalltrace.bpf.c      eBPF programs (libbpf CO-RE)
src/collector/stalltrace.c     loads the eBPF programs, writes events.bin, counts drops
src/correlation/breakdown.py   rebuilds each thread's timeline, splits each operation's time
src/classifier/                features, rules, decision tree, evaluation, delay/overhead analysis
src/report/make_assets.py      turns results into the report's LaTeX tables and plot data
scripts/                       setup, dm-delay helper, experiment runners, analysis
tests/                         unit tests (python3 -m unittest discover -s tests)
experiments/configs/eval.json  evaluation settings (frozen before the final runs)
experiments/processed/wsl/     final results used in the report
report/                        IEEE report source (main.tex, refs.bib, generated/)
REPRODUCE.md                   detailed reproduction guide (the "separate document" deliverable)
RESEARCH_PLAN.md               research questions, hypotheses, design
TASK.md                        checklist with status and final results
```

## Quick start

**1. Look at the results (no setup).** Open `experiments/processed/wsl/results-ref.txt`, or download the report PDF from the [latest release](../../releases/latest).

**2. Rebuild the report.** Upload `report/main.tex`, `report/refs.bib` and the `report/generated/` folder to Overleaf, or run:

```bash
python3 src/report/make_assets.py
cd report && latexmk -pdf main.tex
```

**3. Rerun the analysis from raw traces** (Python 3 standard library only, no root, about 1 hour). Download `raw-wsl.tar.gz` from the release, then:

```bash
python3 -m unittest discover -s tests
mkdir -p raw && tar xzf raw-wsl.tar.gz -C raw
scripts/analyse_results.sh raw wsl
git diff --stat            # should show no changes
```

**4. Run the tracer yourself** (Linux with BTF, root; Ubuntu 24.04 or WSL2):

```bash
sudo scripts/setup_vm.sh          # toolchain: clang, libbpf, bpftool, dmsetup
make                              # builds build/stallbench and build/stalltrace
sudo scripts/smoke_test.sh        # loads the eBPF programs, checks against the kernel's accounting
sudo scripts/run_final_wsl.sh     # all final experiments (about 3-4 h)
```

See [REPRODUCE.md](REPRODUCE.md) for every option, the expected output of each step, and known problems (WSL clock drift, `dm-delay` rounding, disk caching).

## Deliverables

| Deliverable | Where |
|---|---|
| Six-page IEEE report (currently 5 pages) | `report/` (PDF in the release) |
| LaTeX source ZIP | release: `report-latex.zip` |
| Code ZIP | release: `code.zip` |
| Reproduction document | [`REPRODUCE.md`](REPRODUCE.md) |

## Still to do

- [ ] Robustness check on an Ubuntu 24.04 VM (VirtualBox, second disk with host cache off); add the results to the report.
- [ ] Read wPerf (OSDI '18) and Esteves et al. (DSN-W '23), and check the sentences that cite them.
- [ ] Fill in author names and affiliation in `report/main.tex`.
- [ ] Final proofread, then submit.

## Team

Tarun, [teammate], [teammate]
