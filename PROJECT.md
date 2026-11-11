# PROJECT.md

Full research plan: `RESEARCH_PLAN.md`. Authoritative requirements: `eBPF_OS_Assignment_3_I_O_Stall_Fingerprinting.pdf`.

## Goal
Build a reproducible Linux eBPF tool that labels each controlled application operation as:
- `IO_WAIT_DOMINATED`
- `SCHEDULER_WAIT_DOMINATED`
- `SYSCALL_CPU_DOMINATED`
- `INDETERMINATE`

and evaluate the labels against injected ground truth.

## Hypotheses (from the assignment)
- **H1:** fusing syscall, block-layer and scheduler evidence classifies stalls more accurately than any single signal source.
- **H2:** an explicit abstention class reduces high-confidence errors under ambiguous correlation.

## What makes this work different
Per-operation attribution that fuses three kernel layers, an abstain class with a measured benefit, validation against delays we inject ourselves (so both label and stall duration are scored), and false-attribution controls with tracer fidelity reported as results. See `RESEARCH_PLAN.md` for the comparison with prior work.

## Fixed decisions
- Environment: VirtualBox VM, Ubuntu 24.04 (kernel 6.8), 4 vCPU, 8 GB RAM, separate ext4 test disk, `dm-delay` for known I/O latency.
- Toolchain: libbpf CO-RE in C for the tracer and collector; Python for correlation, classification and plots.
- Extension: memory-mapped file workload.
- Dominance rule: a cause must account for more than 60% of an operation's wall time; otherwise ground truth is `INDETERMINATE`.
- Ground truth is recorded two ways per operation:
  - `expected`: the label the workload was designed to produce.
  - `ref_label`: the dominance rule applied to the kernel's own per-thread accounting, measured just outside the operation and independently of eBPF. It uses `CLOCK_THREAD_CPUTIME_ID` for on-CPU time and `run_delay` from `/proc/thread-self/schedstat` for run-queue wait; blocked time is the remainder.
- The evaluation scores against `ref_label` and reports how often it agrees with `expected`. This was needed because, for CPU contention, individual operations really do deviate from the design: in WSL testing, design and measurement agreed on 80% of operations with 3 hogs, and 100% for the I/O, CPU and sleep workloads.
- Overhead budget, declared in advance: at most 10% throughput loss and under 1% dropped events.

## System components
1. **Workloads** (`src/workloads/`): one C binary with cold/direct read, sync write, contended CPU, uncontended CPU, warm read, buffered write, sleep control, neighbour-I/O control, mixed, mmap. Calls `op_begin(op_id)` / `op_end(op_id)` around each operation and writes a ground-truth record.
2. **eBPF programs** (`src/ebpf/`): `raw_syscalls` enter/exit, `sched_switch` (task state and `in_iowait`), `sched_wakeup`, `block_rq_issue` / `block_rq_complete`, uprobes on the markers. Events under 64 bytes, ring buffer reserve/commit, per-CPU drop counter, capability check at startup.
3. **Collector** (`src/collector/`): loads and attaches programs, writes immutable raw JSONL, environment manifest and drop counts.
4. **Correlation** (`src/correlation/`): splits each operation into on-CPU, runnable-wait, blocked-on-I/O and blocked-other time; attaches overlapping block requests; flags missing events. Marker mode and PID-only mode.
5. **Classifiers** (`src/classifier/`): transparent rules with abstain; depth-limited decision tree; syscall-only, block-only and scheduler-only baselines.
6. **Evaluation** (`scripts/`): experiment matrix, metrics with bootstrap 95% CIs, ablations, overhead, figures.

## Constraints
- Disposable VM and test disk only. Never drop caches or touch raw devices elsewhere.
- Never collect file contents, paths, credentials or payloads.
- Detect unsupported hooks and record them; never assume a tracepoint field exists.
- Raw data is immutable; processed data is written separately.
- Train/test split by whole run. Tuning runs and evaluation runs use different run IDs and seeds. Thresholds are frozen before evaluation. Never report only the best run.

## Layout
`src/{ebpf,collector,workloads,correlation,classifier}/`, `scripts/`, `experiments/{configs,raw,processed}/`, `tests/`, `report/` (IEEE LaTeX), `REPRODUCE.md`.

## Deliverables
1. Six-page IEEE conference report: Problem Definition, Objectives, Literature Survey, Methodology, Experimental Results, Conclusion, References.
2. ZIP of the LaTeX source, bibliography and figures.
3. ZIP of the code, configs, raw events and processed features.
4. Code explanation and reproduction document (`REPRODUCE.md`, exported to PDF).

## Definition of done
Another person can start from a clean checkout on a fresh VM, run one documented command sequence, and regenerate every table and figure in the report.
