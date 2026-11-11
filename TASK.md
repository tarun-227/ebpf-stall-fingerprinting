# TASK.md

Tick a box only after the step has run on the target VM and its output was checked.

## Phase 0 — Environment
- [x] Extract assignment constraints, labels, metrics, deliverables (see `PROJECT.md`, `RESEARCH_PLAN.md`).
- [ ] Create Ubuntu 24.04 VM (4 vCPU, 8 GB) with a second virtual disk for tests; host I/O cache off.
- [ ] Run `scripts/setup_vm.sh` to install the toolchain.
- [ ] Run `scripts/check_capabilities.sh`; save its report to `experiments/env/`.
- [ ] Confirm `sched_switch` exposes task state and `in_iowait` is readable via BTF; confirm block tracepoints fire on the test disk.
- [ ] Set up `dm-delay` on the test disk and confirm the added latency with a timed direct read.
- [ ] Initialise the git repository.

## Phase 1 — Workloads and ground truth
- [ ] Workload binary with `op_begin` / `op_end` markers (no-inline) and per-operation ground-truth records.
- [ ] Cold / `O_DIRECT` read, sync write (with `dm-delay`).
- [ ] Contended CPU loop (hogs pinned to the same core) and uncontended CPU loop.
- [ ] Warm cached read and buffered write.
- [ ] `nanosleep` control, neighbour-I/O control, mixed operation.
- [ ] Extension: mmap read, cold and warm.
- [ ] Concurrency levels 1, 4, 16.
- [ ] Repeatability check with tracing off.

## Phase 2 — Event schema
- [ ] Versioned fixed-size event struct (under 64 bytes) and JSONL schema.
- [ ] Ground-truth record schema; environment manifest schema.
- [ ] Representation for dropped events and missing fields.

## Phase 3 — Tracer and collector (libbpf CO-RE)
- [ ] Syscall enter/exit, filtered to the workload.
- [ ] Scheduler switch/wakeup with state and iowait.
- [ ] Block issue/complete.
- [ ] Uprobe markers.
- [ ] Ring buffer reserve/commit and per-CPU drop counter.
- [ ] Capability check, clean attach/detach.
- [ ] Validate: sleep shows blocked-other, `dm-delay` read shows iowait near N × delay, contended loop shows runnable wait; marker timestamps match workload clock.
  - Pre-check done in WSL2 (kernel 6.6), not yet the target VM: `scripts/smoke_test.sh` gave 0 dropped events and matched the kernel's own accounting over the same window within 0.72% (sleep), 1.61% (direct read) and 0.02% (contended). The `dm-delay` part still needs the VM.

## Phase 4 — Correlation
- [ ] Per-operation split: on-CPU, runnable-wait, blocked-on-I/O, blocked-other.
- [ ] Overlapping block requests and their latency.
- [ ] Missing-event flags.
- [ ] Marker mode and PID-only mode.

## Phase 5 — Classification
- [ ] Rules with abstain path; thresholds tuned on tuning runs only, then frozen.
- [ ] Depth-limited decision tree with run-level split.
- [ ] Syscall-only, block-only, scheduler-only baselines.
- [ ] Transfer test: different file size and access pattern.
- [ ] Persist rule/model version and config.

### Status (2026-11-12)
- Implemented: `src/classifier/` (features, rules, decision tree, baselines, evaluation) and `scripts/run_matrix.sh`. 23 unit tests pass.
- WSL pilot (`experiments/processed/pilot-wsl`): 204 runs, 9,792 operations, 0 of 807,052 events dropped.
  - Test runs, fused tree: macro-F1 0.955 against the reference label and 0.922 against the design label.
  - H2 supported: high-confidence errors fall from 15.7% to 1.9% (tree) with 15% abstention.
  - H1 partly supported: fused beats syscall-only and block-only, but not scheduler-only (in_iowait carries the signal; block corroboration never changes a decision).
- Fixed after the pilot: transfer-filter bug (numbers vs text); `mixed` now spins for as long as its own read took (was 82% CPU-dominated, now 83–88% indeterminate).
- Known limits from the pilot:
  - In WSL, the host caches the virtual disk, so `cold_read` at 1 thread is I/O-dominated only about 50% of the time. The VM with host caching off and dm-delay should fix this.
  - The design label for oversubscribed CPU loops (16 threads) rarely matches the measurement; the reference label is used as ground truth.
  - The reference label comes from scheduler accounting, so also report results against the design label.

## Phase 6 — Evaluation
- [ ] Full matrix, 5 repetitions, warm and cold cache.
- [ ] Macro-F1, per-class precision/recall, confusion matrices, abstention rate, bootstrap 95% CIs.
- [ ] H1: fused versus each single-signal baseline.
- [ ] H2: high-confidence error rate with and without abstain.
- [ ] Stall-duration estimation error against injected delay.
- [ ] Overhead: throughput, p99 latency, collector CPU, dropped events, tracing on versus off, against the budget.
- [ ] Ablations: no block, no scheduler, window size, PID-only versus markers, ring buffer size.
- [ ] Robustness: raw test disk without `dm-delay`; state the portability boundary.
- [ ] Ambiguous-case studies and failure analysis.

## Phase 7 — Literature
- [ ] Read wPerf (OSDI '18) and Esteves et al. (DSN-W '23); adjust the novelty claim.
- [ ] Reach at least 5 peer-reviewed papers in `report/refs.bib`.

## Phase 8 — Deliverables
- [ ] Six-page IEEE report with all required sections.
- [ ] LaTeX source ZIP; compiles alone.
- [ ] Code ZIP with configs, raw events, processed features, manifest.
- [ ] `REPRODUCE.md` (dependencies, setup, exact commands, configs, expected outputs, limitations), exported to PDF.
- [ ] Clean-checkout rerun on a fresh VM snapshot reproduces every table and figure.
