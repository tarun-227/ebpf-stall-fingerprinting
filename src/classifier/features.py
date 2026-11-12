#!/usr/bin/env python3
"""Build the per-operation feature table from traced runs.

A run directory holds events.jsonl and stats.json from stalltrace, and the
workload's ground truth either inside it (gt.jsonl) or next to it
(<dir>.gt.jsonl, as scripts/smoke_test.sh writes). Every directory under ROOT
that contains events.jsonl is treated as one run.

  features.py ROOT [ROOT...] --out features.csv

All time features are fractions of the operation's wall time, so operations
of different lengths are comparable.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "correlation"))
import breakdown as bd  # noqa: E402

LABELS = ("IO_WAIT_DOMINATED", "SCHEDULER_WAIT_DOMINATED", "SYSCALL_CPU_DOMINATED",
          "INDETERMINATE")
DOMINANCE = 0.6
# Workloads whose operation itself reads or writes the file (mirrors
# op_does_io() in stallbench.c).
IO_WORKLOADS = {"direct_read", "cold_read", "sync_write", "mmap_cold", "mixed"}

# Columns written to the table, in order.
META = ["run_id", "workload", "threads", "io_size", "pattern", "rep", "seed", "delay_ms",
        "op_id", "tid", "label", "expected", "run_drop_rate"]
FEATURES = ["wall_us", "run_f", "runq_f", "wake_f", "blocked_io_f", "blocked_other_f",
            "syscall_f", "n_syscalls", "n_switch_out", "bio_kb", "blk_any", "n_faults",
            "iowait_unknown", "uncovered_f"]
# Reference (kernel accounting) shares, how far the winning share is from the
# dominance line, and the trace measured over the same reference window.
REFERENCE = ["ref_cpu_f", "ref_runq_f", "ref_blocked_f", "ref_margin", "ref_wall_us",
             "val_cpu_f", "val_runq_f", "val_blocked_f", "val_abs_err_f"]
VAL_COLS = ["val_cpu_f", "val_runq_f", "val_blocked_f", "val_abs_err_f"]


def reference_label(workload, cpu_f, runq_f, blocked_f):
    """Dominance rule on the kernel's own accounting (same rule as stallbench.c)."""
    if cpu_f > DOMINANCE:
        return "SYSCALL_CPU_DOMINATED"
    if runq_f > DOMINANCE:
        return "SCHEDULER_WAIT_DOMINATED"
    if blocked_f > DOMINANCE:
        return "IO_WAIT_DOMINATED" if workload in IO_WORKLOADS else "INDETERMINATE"
    return "INDETERMINATE"


def find_runs(roots):
    for root in roots:
        for dirpath, _, files in os.walk(root):
            if "events.jsonl" not in files:
                continue
            gt = os.path.join(dirpath, "gt.jsonl")
            if not os.path.exists(gt):
                gt = dirpath.rstrip("/\\") + ".gt.jsonl"
            if not os.path.exists(gt):
                print(f"skipping {dirpath}: no ground truth", file=sys.stderr)
                continue
            yield dirpath, gt


def run_rows(run_dir, gt_path, window_ns=None, legacy_clock_ratio=1.0):
    header, gt_ops = bd.load_ground_truth(gt_path)
    # Runs from stallbench with a clock_ratio header already report the
    # kernel's CPU/run-queue times on the wall-clock scale. Older runs (the
    # first WSL pilot) report raw sched_clock values; scale those here.
    scale = 1.0 if "clock_ratio" in header else 1.0 / legacy_clock_ratio
    if scale != 1.0:
        for g in gt_ops.values():
            g["ref_cpu_ns"] = g["ref_cpu_ns"] * scale
            if g["ref_runq_ns"] >= 0:
                g["ref_runq_ns"] = g["ref_runq_ns"] * scale
                g["ref_blocked_ns"] = g["ref_wall_ns"] - g["ref_cpu_ns"] - g["ref_runq_ns"]
    stats_path = os.path.join(run_dir, "stats.json")
    drop_rate = 0.0
    if os.path.exists(stats_path):
        with open(stats_path) as f:
            drop_rate = json.load(f)["totals"]["drop_rate"]
    meta_path = os.path.join(run_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    events = bd.load_jsonl(os.path.join(run_dir, "events.jsonl"))
    source = (bd.window_breakdown(events, gt_ops, window_ns) if window_ns
              else bd.breakdown(events, gt_ops))
    rows = []
    for r in source:
        if "ref_label" not in r or r["ref_label"] == "UNKNOWN" or r["wall"] <= 0:
            continue
        w = float(r["wall"])
        rw = float(r["ref_wall_ns"])
        if rw <= 0 or r["ref_runq_ns"] < 0:
            continue
        ref = (r["ref_cpu_ns"] / rw, r["ref_runq_ns"] / rw, max(r["ref_blocked_ns"], 0) / rw)
        val = {}
        if "val_run" in r:
            # Trace over exactly the reference window, kernel convention
            # (wake latency counts as CPU).
            vcpu = (r["val_run"] + r["val_wake_latency"]) / rw
            vrunq = r["val_runq"] / rw
            vblk = (r["val_blocked_io"] + r["val_blocked_other"]) / rw
            val = {"val_cpu_f": vcpu, "val_runq_f": vrunq, "val_blocked_f": vblk,
                   "val_abs_err_f": abs(vcpu - ref[0]) + abs(vrunq - ref[1]) + abs(vblk - ref[2])}
        rows.append({
            "run_id": header["run_id"],
            "workload": header["workload"],
            "threads": header["threads"],
            "io_size": header["io_size"],
            "pattern": header["pattern"],
            "rep": meta.get("rep", ""),
            "seed": header["seed"],
            "delay_ms": meta.get("delay_ms", ""),
            "op_id": r["op_id"],
            "tid": r["tid"],
            "label": reference_label(header["workload"], *ref),
            "expected": r["expected"],
            "run_drop_rate": drop_rate,
            "wall_us": w / 1e3,
            "run_f": r["run"] / w,
            "runq_f": r["runq"] / w,
            "wake_f": r["wake_latency"] / w,
            "blocked_io_f": r["blocked_io"] / w,
            "blocked_other_f": r["blocked_other"] / w,
            "syscall_f": min(r["syscall_ns"] / w, 1.0),
            "n_syscalls": r["n_syscalls"],
            "n_switch_out": r["n_switch_out"],
            "bio_kb": r["bio_bytes"] / 1024.0,
            "blk_any": r["blk_completions_any"],
            "n_faults": r["n_faults"],
            "iowait_unknown": int(bool(r["iowait_unknown"])),
            "uncovered_f": r["uncovered"] / w,
            "ref_cpu_f": ref[0], "ref_runq_f": ref[1], "ref_blocked_f": ref[2],
            "ref_margin": abs(max(ref) - DOMINANCE), "ref_wall_us": rw / 1e3,
            **{k: val.get(k, "") for k in VAL_COLS},
        })
    return rows


def load_table(path):
    """Read a features CSV back, converting numeric columns."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FEATURES + ["threads", "io_size", "run_drop_rate"]:
            r[k] = float(r[k])
        for k in REFERENCE:
            if r.get(k, "") != "":
                r[k] = float(r[k])
        # Derived: total blocked time, for models that cannot see in_iowait.
        r["blocked_f"] = r["blocked_io_f"] + r["blocked_other_f"]
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window-ms", type=float,
                    help="PID-only ablation: ignore operation markers and use fixed windows "
                         "of this width, each labelled by the operation covering most of it")
    ap.add_argument("--clock-ratio", type=float, default=1.0,
                    help="for runs recorded before stallbench measured it: rate of the "
                         "kernel's thread clocks relative to CLOCK_MONOTONIC (1.0909 on the "
                         "WSL2 machine used for the first pilot)")
    args = ap.parse_args()
    window_ns = int(args.window_ms * 1e6) if args.window_ms else None

    rows, nruns = [], 0
    for run_dir, gt in sorted(find_runs(args.roots)):
        rows.extend(run_rows(run_dir, gt, window_ns, args.clock_ratio))
        nruns += 1
    if not rows:
        raise SystemExit("no operations found")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META + FEATURES + REFERENCE)
        w.writeheader()
        w.writerows(rows)
    counts = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"{len(rows)} operations from {nruns} runs -> {args.out}")
    for k in LABELS:
        print(f"  {k:<26} {counts.get(k, 0)}")


if __name__ == "__main__":
    main()
