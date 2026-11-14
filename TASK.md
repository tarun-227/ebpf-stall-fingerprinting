# TASK.md

Tick a box only after the step has run and its output was checked. Final experiments run in WSL2 (kernel 6.6) with `dm-delay`; the Ubuntu VM is the robustness check.

## Phase 0 — Environment
- [x] Extract assignment constraints, labels, metrics, deliverables (see `PROJECT.md`, `RESEARCH_PLAN.md`).
- [x] Create Ubuntu 24.04 VM (QEMU/KVM inside WSL2, `scripts/vm_qemu.sh`; 4 vCPU, 4 GB) with a second virtual disk for tests; host I/O cache off.
- [x] Run `scripts/setup_vm.sh` to install the toolchain (in the VM).
- [x] Run `scripts/check_capabilities.sh` (VM, kernel 6.8: 0 missing items).
- [x] Confirm `sched_switch` exposes task state and `in_iowait` is readable via BTF; confirm block tracepoints fire on the test disk.
- [x] Set up `dm-delay` on the test disk and confirm the added latency with a timed direct read.
- [x] Initialise the git repository.

## Phase 1 — Workloads and ground truth
- [x] Workload binary with `op_begin` / `op_end` markers (no-inline) and per-operation ground-truth records.
- [x] Cold / `O_DIRECT` read, sync write (with `dm-delay`).
- [x] Contended CPU loop (hogs pinned to the same core) and uncontended CPU loop.
- [x] Warm cached read and buffered write.
- [x] `nanosleep` control, neighbour-I/O control, mixed operation.
- [x] Extension: mmap read, cold and warm.
- [x] Concurrency levels 1, 4, 16.
- [x] Repeatability check with tracing off.

## Phase 2 — Event schema
- [x] Versioned fixed-size event struct (under 64 bytes) and JSONL schema.
- [x] Ground-truth record schema; environment manifest schema.
- [x] Representation for dropped events and missing fields.

## Phase 3 — Tracer and collector (libbpf CO-RE)
- [x] Syscall enter/exit, filtered to the workload.
- [x] Scheduler switch/wakeup with state and iowait.
- [x] Block issue/complete.
- [x] Uprobe markers.
- [x] Ring buffer reserve/commit and per-CPU drop counter.
- [x] Capability check, clean attach/detach.
- [x] Validate: sleep shows blocked-other, `dm-delay` read shows iowait near N × delay, contended loop shows runnable wait; marker timestamps match workload clock.
  - Pre-check done in WSL2 (kernel 6.6), not yet the target VM: `scripts/smoke_test.sh` gave 0 dropped events and matched the kernel's own accounting over the same window within 0.72% (sleep), 1.61% (direct read) and 0.02% (contended). The `dm-delay` part still needs the VM.

## Phase 4 — Correlation
- [x] Per-operation split: on-CPU, runnable-wait, blocked-on-I/O, blocked-other.
- [x] Overlapping block requests and their latency.
- [x] Missing-event flags.
- [x] Marker mode and PID-only mode.

## Phase 5 — Classification
- [x] Rules with abstain path; thresholds tuned on tuning runs only, then frozen.
- [x] Depth-limited decision tree with run-level split.
- [x] Syscall-only, block-only, scheduler-only baselines.
- [x] Transfer test: different file size and access pattern.
- [x] Persist rule/model version and config.

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
- [x] Full matrix, 5 repetitions (WSL, `final-wsl`: 204 runs, 0 failed, 2 ms injected delay).
- [x] Macro-F1, per-class precision/recall, confusion matrices, abstention rate, bootstrap 95% CIs.
- [x] H1: fused versus each single-signal baseline.
- [x] H2: high-confidence error rate with and without abstain.
- [x] Stall-duration estimation error against injected delay.
- [x] Overhead: throughput, p99 latency, collector CPU, dropped events, tracing on versus off, against the budget.
- [x] Ablations: no block, no scheduler, window size, PID-only versus markers, ring buffer size.
- [x] Robustness: second environment (Ubuntu VM, kernel 6.8); results below.
- [ ] Ambiguous-case studies and failure analysis (write-up).

### Final WSL results (processed output in `experiments/processed/wsl/`)
- Events: 766,242 in the final matrix, 0.54% dropped; clock drift flagged in 26 of 204 runs.
- Reference label, test runs (reps 3-5, 5,184 operations): fused rules macro-F1 0.989 [0.979, 0.996], fused tree 0.986, scheduler-only tree 0.986, block-only tree 0.852, syscall-only tree 0.647.
- H2 supported: high-confidence errors 18.0% -> 0.7% (rules) and 18.2% -> 0.9% (tree) at 17.5% abstention.
- H1: with `in_iowait`, fused equals scheduler-only. Without it, fusion helps: rules +0.109 [0.051, 0.176], tree +0.019 [0.005, 0.038]. Same direction against the design label (rules +0.097, tree +0.021).
- Design label: fused tree 0.946; design and reference labels agree on 94.9% of operations.
- Clean runs only (no drops, no drift; 95 runs, 88% of operations): fused rules 0.989, fused tree 0.984. Results do not depend on the affected runs.
- Tracer vs kernel accounting: median error 0.02-1.1% per workload.
- Delay sweep (0/5/20 ms): traced I/O-blocked time within 2-125 us of the kernel's; delay-length waits per operation about 1.1 (reads), 2.3 (sync_write), 17 (mmap_cold), 0 (sleep). dm-delay quantises 5 ms to ~11 ms and 20 ms to ~23 ms.
- PID-only fixed windows, fused tree macro-F1: 5 ms 0.809, 20 ms 0.898, 100 ms 0.889 (markers: 0.986).
- Overhead (`overhead-v2`, binary output, `--no-ref`): within budget for sleep (+4%), sync_write (<=1%), contended CPU (+1.5%). Over budget for microsecond operations (warm_read, buffered_write: 61-92% throughput loss) and disk reads (13-27%, noisy: an untraced repeat differs by 15-41%). Cost is a fixed 21-45 us per operation, mostly the two marker uprobes (~7-10 us each in WSL). Drops at 4 threads: warm_read 17.6% with the default buffer, 0% with 4 MiB.

### VM robustness results (Ubuntu 24.04, kernel 6.8, QEMU/KVM; `experiments/processed/vm/`)
- Same matrix as WSL (204 runs, all completed), same `eval.json`. Events 793,933, 0 dropped; drift flagged in 2 runs.
- Fused rules 0.971 [0.958, 0.980], fused tree 0.966, scheduler-only tree 0.969, block-only tree 0.818, syscall-only tree 0.788.
- H1 without `in_iowait`: rules +0.111 [0.056, 0.171]. H2: high-confidence errors 19.2% -> 1.4% (rules) at 18.9% abstention.
- Trained on WSL dev runs, tested in the VM: rules unchanged (0.971); fused tree 0.964; fused tree without `in_iowait` 0.695 (timing thresholds do not transfer).
- Delay sweep: traced I/O-blocked time within 422 us of the kernel's; 5 ms injected adds ~5.7 ms per read (WSL's ~11 ms was its coarse timer).
- Overhead: noisier and higher under nested virtualisation; markers add ~100 us per operation, so sleep and sync_write lose 10-30%.

## Phase 7 — Literature
- [ ] Read wPerf (OSDI '18) and Esteves et al. (DSN-W '23); adjust the novelty claim.
- [ ] Reach at least 5 peer-reviewed papers in `report/refs.bib`.

## Phase 8 — Deliverables
- [ ] Six-page IEEE report with all required sections.
- [ ] LaTeX source ZIP; compiles alone.
- [ ] Code ZIP with configs, raw events, processed features, manifest.
- [ ] `REPRODUCE.md` (dependencies, setup, exact commands, configs, expected outputs, limitations), exported to PDF.
- [ ] Clean-checkout rerun on a fresh VM snapshot reproduces every table and figure.
