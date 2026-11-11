# Research plan: eBPF fingerprinting of I/O-induced application stalls

## Context

OS course Assignment 3 asks for a reproducible eBPF tool that labels controlled application operations as `IO_WAIT`, `SCHED_WAIT`, `SYSCALL_CPU` or `INDETERMINATE`, evaluated against ground truth, with overhead and failure cases quantified. Deliverables: a six-page IEEE-conference report (Problem Definition, Objectives, Literature Survey, Methodology, Experimental Results, Conclusion, References), a LaTeX source ZIP, a code ZIP, and a separate reproduction document.

Decisions already made: Ubuntu VM, libbpf CO-RE in C, core plus one extension (the memory-mapped workload).

The project folder currently holds only the PDFs and ChatGPT's `AGENT.md`, `TASK.md`, `PROJECT.md`. No code exists yet.

## What the source material tells us

**Assignment sheet (authoritative).** Two hypotheses to test:
- H1: fusing syscall + block + scheduler evidence beats any single signal source.
- H2: an explicit abstention class reduces high-confidence errors.

**Gaps in ChatGPT's documents versus the assignment** (to fix in `TASK.md` / `PROJECT.md`):
- The lightweight classifier and the transfer test (different file size or I/O pattern) are required objectives, not optional.
- Three single-signal baselines are required: syscall-only, block-only, scheduler-only.
- Missing metric: error in estimated stall duration.
- Missing: confidence intervals, a predeclared overhead budget, a robustness test on a second kernel or storage setup, and at least 5 peer-reviewed papers.

**Papers read in full, and what we take from each:**
| Paper | Use in our work |
|---|---|
| EZIOTracer (CHEOPS '21) | Closest prior work. Links user-level and kernel I/O events by thread ID and a shared monotonic clock (`clock_gettime` and `bpf_ktime_get_ns`). We reuse that idea for operation markers. It reports 2–26% overhead and has no scheduler evidence and no classification; that is our gap. Its buffered-write finding (block layer sees ~0 events because the page cache absorbs writes) is one of our negative controls. |
| No Two Snowflakes (eBPF '25) | Justifies libbpf: 99–100% event capture with small events versus 13–53% for bpftrace. Tells us to keep events small, use ring buffer reserve/commit, count drops in-kernel, and report loss. Their setup (Ubuntu 24.04, kernel 6.8, 256 KiB ring buffer) is our reference configuration. |
| Rezvani et al. (ISPASS '24) | Syscall-only observability. Shows syscall timing carries real signal but cannot see request boundaries or causes; this motivates our syscall-only baseline and H1. |

**Two more peer-reviewed papers needed** (cited inside the papers above; to be obtained and read before citing): wPerf (OSDI '18, off-CPU analysis), Esteves et al. (DSN-W '23, I/O diagnosis via syscalls). Backups: Craun et al. (eBPF '24), Treadmill (ISCA '16).

## What sets our paper apart, and how we get there

**One-sentence claim:** we attribute each individual application operation's delay to a cause by fusing three kernel layers, we let the tool say "I don't know", and we score it against delays we injected ourselves.

| Prior work | What it does | What it does not do |
|---|---|---|
| EZIOTracer | Links user-level I/O calls to VFS, page cache, file system and block events per thread | No scheduler evidence, so it cannot tell storage wait from CPU starvation. No classification. Bottlenecks are found by a person reading the trace, scored with a theoretical contention estimate, not ground truth. |
| ISPASS '24 | Infers throughput and saturation from aggregate syscall statistics | Syscall layer only. The authors state eBPF cannot see request boundaries, so there is no per-operation view and no cause attribution. |
| No Two Snowflakes | Measures overhead and event loss of eBPF libraries | Studies the tracer, not the application. No diagnosis at all. |
| Standard tools (off-CPU profilers, block latency histograms) | Show each signal on its own | No fusion, no label, no statement of uncertainty. |

**Our four differentiators and the mechanism behind each:**

1. **Per-operation, three-layer fusion.**
   - How: uprobe markers give exact operation boundaries (the thing ISPASS '24 lacked). Inside each window we split wall time into on-CPU, runnable-but-waiting, blocked-on-I/O and blocked-other, using scheduler switch/wakeup events plus the kernel's own `in_iowait` flag, then attach block requests that overlap.
   - Evidence in the paper: Table IV, fused versus each single-signal baseline (H1).

2. **Explicit abstention with a measured benefit.**
   - How: the rules and the decision tree both output INDETERMINATE when no cause passes the dominance threshold, when signals conflict, or when events are missing. We report high-confidence error rate with and without that option.
   - Evidence: Table V (H2), plus the ambiguous case studies.

3. **Validation against injected ground truth, including stall duration.**
   - How: `dm-delay` adds a known latency per block request, CPU hogs add a known contention level, `nanosleep` adds a known non-I/O block. We therefore know both the correct label and the correct stall length, and can report estimation error. Neither EZIOTracer nor ISPASS '24 has this.
   - Evidence: Fig. 3 and Fig. 5.

4. **False-attribution controls and tracer fidelity reported as results.**
   - How: workloads where block activity is present but is not the cause (buffered writes, a neighbour process doing heavy I/O), and the PID-only versus marker ablation. Dropped events and overhead are reported for every configuration against a budget declared in advance, following the Snowflakes paper's argument that fidelity must be measured.
   - Evidence: Fig. 4 and Fig. 6.

**Limits on the novelty claim:**
- The claim is narrow: a validated per-operation stall taxonomy on controlled workloads on one VM, not a general production diagnosis tool.
- wPerf (OSDI '18) does off-CPU bottleneck analysis and may overlap with differentiator 1. It and the DSN-W '23 paper must be read before the claim is finalised; the wording in the Literature Survey is adjusted to whatever they actually cover.
- If H1 or H2 fails on our data, the paper reports that as the finding.

## Research design

### Environment
- VirtualBox VM, Ubuntu 24.04 (kernel 6.8), 4 vCPU, 8 GB RAM.
- A second virtual disk formatted ext4 as the disposable test disk, with host I/O caching turned off.
- A `dm-delay` device-mapper target on that disk to inject a known, fixed latency per block request. This gives deterministic I/O-wait ground truth and a known stall duration to score estimates against.
- Robustness setup: same pipeline on the raw virtual disk without `dm-delay` (second storage setup). Portability boundary stated for anything not tested.
- Code lives in a git repository and is built and run inside the VM. eBPF cannot run from Windows.

### Workloads and ground truth (C, one binary, `src/workloads/`)
| Workload | Intervention | Expected label |
|---|---|---|
| Cold read / `O_DIRECT` read | `dm-delay`, cache dropped | IO_WAIT |
| Synchronous write (`O_SYNC` / `fsync`) | `dm-delay` | IO_WAIT |
| CPU loop under contention | N CPU hogs pinned to the same core | SCHED_WAIT |
| CPU loop, no contention | none | SYSCALL_CPU |
| Warm cached read, buffered write | page cache absorbs I/O | SYSCALL_CPU |
| `nanosleep` negative control | blocked, but not on I/O | INDETERMINATE |
| CPU-bound op while another process does heavy I/O | block events nearby, not ours | SYSCALL_CPU (false-attribution test) |
| Mixed op (about half I/O, half contention) | both | INDETERMINATE |
| **Extension:** mmap file read | page faults instead of syscalls | IO_WAIT (cold) / SYSCALL_CPU (warm) |

- A label is "dominated" when one cause accounts for more than 60% of the operation's wall time; otherwise the ground truth is INDETERMINATE. The threshold is declared before evaluation.
- Each operation writes a ground-truth record: run ID, operation ID, TID, monotonic start/end, expected label, injected delay.

### Instrumentation (libbpf CO-RE, `src/ebpf/`)
- Syscalls: `raw_syscalls:sys_enter` / `sys_exit`, filtered to the workload's process.
- Scheduler: `sched_switch` (BTF variant, reading the outgoing task's state and `in_iowait` flag), `sched_wakeup`, `sched_wakeup_new`.
- Block layer: `block_rq_issue`, `block_rq_complete`.
- Operation markers: uprobes on two no-inline functions `op_begin(op_id)` / `op_end(op_id)` in the workload.
- mmap extension: page-fault entry/exit hook, chosen after checking what the target kernel exposes.
- Fixed-size events under 64 bytes, ring buffer with reserve/commit, per-CPU drop counter.
- Startup capability check that records which hooks attached; no file contents or paths collected.

### Collector, correlation, classification
- Collector (C, `src/collector/`): writes immutable raw JSONL plus an environment manifest and drop counts.
- Correlation (Python, `src/correlation/`): per operation, split wall time into on-CPU, runnable-but-waiting, blocked-in-iowait, and blocked-other; attach overlapping block requests and their latency; flag missing events. Two modes: marker-based windows and PID-only windows (required ablation).
- Classifiers (Python, `src/classifier/`):
  - Transparent rules with an abstain path.
  - A depth-limited decision tree (scikit-learn), trained with run-level train/test separation.
  - Three single-signal baselines built by restricting features to syscall, block, or scheduler evidence.
- Transfer test: train on one file size and sequential access; test on a different size and random access.

### Experiment matrix
- 8 core workloads + mmap, 3 concurrency levels (1, 4, 16 threads), warm and cold cache where meaningful, 5 repetitions.
- Tuning runs and final evaluation runs use separate run IDs and seeds; thresholds are frozen before evaluation.
- Overhead: every configuration with tracing off and on.
- Predeclared budget: at most 10% throughput loss and under 1% dropped events.

### Metrics and ablations
- Macro-F1, per-class precision/recall, confusion matrix, abstention rate, all with bootstrap 95% confidence intervals.
- Error of estimated stall duration against the injected delay.
- Throughput, p99 latency, collector CPU usage, dropped-event rate.
- H1 test: fused versus each single-signal baseline.
- H2 test: high-confidence error rate with and without the abstain class.
- Ablations: no block events, no scheduler events, correlation-window size, PID-only versus markers, ring buffer 256 KiB versus 4 MiB.

## What the final research output looks like

Numbers below are placeholders; every value comes from the evaluation runs, including negative results.

### 1. The paper (six pages, IEEE conference format)
Working title: *Fingerprinting I/O-Induced Application Stalls by Fusing Syscall, Scheduler and Block-Layer Evidence with eBPF*.

| Section | Content | Figures and tables |
|---|---|---|
| Problem Definition | One latency number cannot tell storage wait from CPU starvation from kernel work. States H1 and H2. | Fig. 1: one operation's timeline split into on-CPU, runnable-wait, I/O-blocked, other-blocked |
| Objectives | The six assignment objectives, the 60% dominance rule, the overhead budget declared up front | — |
| Literature Survey | Five or more papers, grouped: user/kernel I/O tracing (EZIOTracer), syscall-only observability (ISPASS '24, DSN-W '23), off-CPU analysis (wPerf), tracer fidelity and cost (Snowflakes). Ends with the gap: none fuses three layers per operation with abstention. | Table I: prior work versus ours by signals used, per-operation attribution, abstention, overhead reported |
| Methodology | Workloads and ground truth, tracepoints, event schema, correlation model, rules, decision tree, baselines, run-level split | Fig. 2: architecture. Table II: workload → intervention → expected label. Table III: test machine and kernel |
| Experimental Results | H1 and H2 answered, ablations, transfer, overhead, ambiguous cases | See list below |
| Conclusion | What held, what did not, portability boundary, threats to validity, privacy safeguards | — |
| References | Kernel/eBPF docs plus the peer-reviewed papers | — |

Results figures and tables:
- **Table IV (headline, H1):** macro-F1 with 95% CI for syscall-only, block-only, scheduler-only, fused rules, fused decision tree.
- **Fig. 3:** confusion matrices for the fused rules and the decision tree.
- **Table V (H2):** high-confidence error rate and abstention rate, with and without the abstain class.
- **Fig. 4:** ablations as a bar chart: no block events, no scheduler events, PID-only versus markers, correlation-window size.
- **Fig. 5:** estimated versus injected stall duration, with the error distribution.
- **Table VI:** transfer test, trained on one file size and pattern, tested on another.
- **Fig. 6:** overhead with tracing off versus on: throughput, p99 latency, collector CPU, dropped-event rate at each concurrency level, against the declared budget.
- **Short case studies:** two or three ambiguous operations, such as the buffered write where the block layer shows nothing, and the CPU-bound operation next to another process's heavy I/O.

The paper's claims take this form: "fused evidence reaches macro-F1 X versus Y for the best single signal", "abstention cuts high-confidence errors from A% to B% at an abstention rate of C%", "tracing costs D% throughput and drops E% of events", "results hold on the second storage setup / fail under condition F".

### 2. Report source ZIP
`report/`: `main.tex`, `IEEEtran.cls`, `refs.bib`, `figures/` (PDFs generated by the analysis scripts), and a one-line build command. Compiles to the submitted PDF on Overleaf.

### 3. Code ZIP
The repository in the layout below, plus the frozen configs, raw event files, processed feature tables and the environment manifest from the reported runs, so every table can be regenerated without rerunning the tracer.

### 4. Code explanation document (`REPRODUCE.md`, also exported to PDF)
- What each component does and how the pieces connect.
- Dependencies with versions (kernel, clang, libbpf, bpftool, Python packages).
- VM and test-disk setup, including `dm-delay`.
- Exact commands: build, run one workload, run the full matrix, evaluate, regenerate figures.
- Configuration options and the frozen thresholds.
- Expected runtime and expected outputs, plus known limitations and unsupported hooks.

## Repository layout
`src/{ebpf,collector,workloads,correlation,classifier}/`, `scripts/` (setup, run matrix, evaluate), `experiments/{configs,raw,processed}/`, `tests/`, `report/` (IEEE LaTeX, figures, bibliography), `REPRODUCE.md`.

## Verification
- Tracer correctness: a 1-second `nanosleep` shows about 1 s blocked-other and near-zero iowait; a `dm-delay` read of N blocks shows iowait close to N × delay; a contended CPU loop shows runnable wait close to the expected share.
- Marker timestamps from uprobes agree with the workload's own `clock_gettime` records within a small tolerance.
- Dropped-event counter is zero on pilot runs, and non-zero when the ring buffer is deliberately shrunk.
- Labels regenerate from saved feature files without rerunning the tracer.
- One script reruns the pipeline from a clean checkout on a fresh VM snapshot and reproduces the reported tables.
- The report compiles from the LaTeX ZIP alone and is six pages.

## Open risks
- VirtualBox virtual disks may hide real device behaviour; `dm-delay` controls for this, and the report states it as a limit.
- `in_iowait` and block tracepoint fields must be confirmed on the target kernel before anything else is built; if unavailable, fall back to block-request overlap only and record the weaker evidence.
- Uprobe markers add a few microseconds per operation; operations are sized so this stays negligible, and the overhead runs measure it.
