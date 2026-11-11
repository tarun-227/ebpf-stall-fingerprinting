# AGENT.md

## Mission
Implement the eBPF-based I/O-stall fingerprinting project described in `PROJECT.md`, following the sequence and checklists in `TASK.md`.

## First actions
1. Read `PROJECT.md`, `TASK.md`, and the original assignment.
2. Inspect the repository and target Linux environment before editing.
3. Summarize constraints, available kernel hooks, and proposed implementation choices.
4. Do not assume a hook or field exists; verify it on the target kernel.

## Engineering rules
- Work incrementally; keep each stage runnable.
- Build workload ground truth before building the classifier.
- Add tracing in this order: syscall, scheduler, block layer.
- Keep a versioned event schema and immutable raw event files.
- Track dropped events and missing signals.
- Use monotonic timestamps for duration calculations.
- Handle PID/TID lifecycle and concurrent operations.
- Preserve `INDETERMINATE`; do not manufacture certainty.
- Never collect file contents, credentials, or application payloads.
- Never run destructive cache/device experiments on production systems.
- Keep tuning and final test runs separate to prevent leakage.
- Avoid unrelated refactors and unnecessary dependencies.

## Validation gates
Before declaring a phase complete:
- run its scripts on the target environment;
- inspect representative raw outputs;
- test error paths and missing capabilities;
- record exact commands and versions;
- update `TASK.md` checkboxes only after verification.

## Completion report
For each task, state:
- files changed;
- implementation decisions and assumptions;
- commands executed and verification results;
- limitations, unsupported signals, and remaining work.

## Tool compatibility
Keep instructions and project conventions tool-agnostic. This file must work with both Claude Code and Codex. Follow repository-specific instructions if present, provided they do not conflict with the project constraints above.
