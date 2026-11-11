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

# Columns written to the table, in order.
META = ["run_id", "workload", "threads", "io_size", "pattern", "rep", "seed",
        "op_id", "tid", "label", "expected", "run_drop_rate"]
FEATURES = ["wall_us", "run_f", "runq_f", "wake_f", "blocked_io_f", "blocked_other_f",
            "syscall_f", "n_syscalls", "n_switch_out", "bio_kb", "blk_any", "n_faults",
            "iowait_unknown", "uncovered_f"]


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


def run_rows(run_dir, gt_path):
    header, gt_ops = bd.load_ground_truth(gt_path)
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

    rows = []
    for r in bd.breakdown(bd.load_jsonl(os.path.join(run_dir, "events.jsonl")), gt_ops):
        if "ref_label" not in r or r["ref_label"] == "UNKNOWN" or r["wall"] <= 0:
            continue
        w = float(r["wall"])
        rows.append({
            "run_id": header["run_id"],
            "workload": header["workload"],
            "threads": header["threads"],
            "io_size": header["io_size"],
            "pattern": header["pattern"],
            "rep": meta.get("rep", ""),
            "seed": header["seed"],
            "op_id": r["op_id"],
            "tid": r["tid"],
            "label": r["ref_label"],
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
        })
    return rows


def load_table(path):
    """Read a features CSV back, converting numeric columns."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FEATURES + ["threads", "io_size", "run_drop_rate"]:
            r[k] = float(r[k])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows, nruns = [], 0
    for run_dir, gt in sorted(find_runs(args.roots)):
        rows.extend(run_rows(run_dir, gt))
        nruns += 1
    if not rows:
        raise SystemExit("no operations found")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META + FEATURES)
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
