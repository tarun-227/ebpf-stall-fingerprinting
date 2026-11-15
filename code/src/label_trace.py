#!/usr/bin/env python3
"""Label the operations of any traced program: I/O wait, scheduler wait,
syscall/CPU work, or indeterminate.

    sudo build/stalltrace -o /tmp/myrun -- ./my_program [args]
    python3 src/label_trace.py /tmp/myrun [--csv ops.csv] [--threshold 0.6]

Operations come from the program's own op_begin(id) / op_end(id) markers
(two empty, non-inlined functions; see the reproducibility guide). Without
markers, use --window-ms N to cut each thread into fixed N-ms windows instead
(less accurate: the report measures 0.81-0.90 macro-F1 for 5-100 ms windows
against 0.99 with markers).

It uses the same timeline (src/correlation/breakdown.py) and the same fused
rule (src/classifier/models.py, threshold 0.6 as tuned in the report) as the
evaluation. No ground truth is needed. Standard library only.
"""
import argparse
import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "correlation"))
sys.path.insert(0, os.path.join(HERE, "classifier"))
import breakdown as bd  # noqa: E402
import models as m      # noqa: E402

SHORT = {m.IO: "IO_WAIT", m.SCHED: "SCHED_WAIT", m.CPU: "SYSCALL_CPU", m.IND: "INDETERMINATE"}


def features(r):
    """The fused rule's inputs, from one breakdown row (same as features.py)."""
    w = float(r["wall"])
    return {
        "wall_us": w / 1e3,
        "run_f": r["run"] / w, "runq_f": r["runq"] / w, "wake_f": r["wake_latency"] / w,
        "blocked_io_f": r["blocked_io"] / w, "blocked_other_f": r["blocked_other"] / w,
        "syscall_f": min(r["syscall_ns"] / w, 1.0), "n_syscalls": r["n_syscalls"],
        "bio_kb": r["bio_bytes"] / 1024.0, "blk_any": r["blk_completions_any"],
        "n_faults": r["n_faults"], "n_switch_out": r["n_switch_out"],
        "iowait_unknown": int(bool(r["iowait_unknown"])), "uncovered_f": r["uncovered"] / w,
    }


def marker_rows(events):
    """Operations delimited by op_begin/op_end markers, per thread."""
    ops = {(e["tid"], e["op_id"]): None for e in events if e["type"] == "op_begin"}
    return bd.breakdown(events, ops)


def window_rows(events, width_ns):
    """Fixed windows over each traced thread's lifetime (no markers needed)."""
    tids = {e["tid"] for e in events if e["type"] in ("sys_enter", "sys_exit", "page_fault",
                                                      "bio_queue") and e["tid"]}
    tids |= {e["next_tid"] for e in events if e["type"] == "sched_switch" and e.get("next_pid")}
    tids.discard(0)
    span = collections.defaultdict(lambda: [None, None])
    for e in events:
        for t in (e["tid"], e.get("next_tid")):
            if t in tids:
                s = span[t]
                s[0] = e["ts"] if s[0] is None else min(s[0], e["ts"])
                s[1] = e["ts"] if s[1] is None else max(s[1], e["ts"])
    fake = {}
    for tid, (a, b) in span.items():
        if b - a >= width_ns:   # pretend the whole lifetime is one operation, then cut it
            fake[(tid, 0)] = {"t_begin_ns": a, "t_end_ns": b, "workload": "", "expected": "",
                              "ref_label": "", "ref_wall_ns": 0, "ref_cpu_ns": 0,
                              "ref_runq_ns": 0, "ref_blocked_ns": 0}
    rows = bd.window_breakdown(events, fake, width_ns, min_cover=1.0)
    for r in rows:
        r["op_id"] = f"w{r['op_id']}"
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", help="stalltrace output directory (or events.bin)")
    ap.add_argument("--window-ms", type=float, help="no markers: label fixed windows of this length")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="dominance threshold (default 0.6, tuned in the report)")
    ap.add_argument("--csv", help="also write one row per operation with all features")
    ap.add_argument("--all", action="store_true", help="print every operation, not only the first 50")
    a = ap.parse_args()

    events = bd.load_events(a.run)
    if a.window_ms:
        rows, unit = window_rows(events, int(a.window_ms * 1e6)), f"{a.window_ms:g}-ms window"
    else:
        rows, unit = marker_rows(events), "operation"
        if not rows:
            raise SystemExit("no op_begin/op_end markers in this trace. Add the two marker "
                             "functions to the program (see the reproducibility guide), check that "
                             "stalltrace attached its uprobes (stats.json), or use --window-ms N.")
    th = {"dominance": a.threshold}
    out = []
    for r in rows:
        if r["wall"] <= 0:
            continue
        f = features(r)
        out.append({"tid": r["tid"], "op_id": r["op_id"], **f,
                    "label": m.rule_fused(f, th),
                    "forced": m.rule_fused(f, th, abstain=False)})

    drop = ""
    stats = os.path.join(a.run, "stats.json") if os.path.isdir(a.run) else None
    if stats and os.path.exists(stats):
        with open(stats) as fh:
            t = json.load(fh)["totals"]
        drop = f"; {t['dropped']} of {t['produced']} events dropped ({100 * t['drop_rate']:.2f}%)"
    print(f"{len(out)} {unit}s from {len({o['tid'] for o in out})} thread(s){drop}\n")
    hdr = (f"{'tid':>7} {'op':>12} {'wall':>10}  {'run':>5} {'runq':>5} {'wake':>5} "
           f"{'io':>5} {'other':>5} {'sysc':>5} {'bioKB':>7}  label")
    print(hdr)
    for o in (out if a.all else out[:50]):
        cpu = o["run_f"]
        print(f"{o['tid']:>7} {str(o['op_id']):>12} {o['wall_us'] / 1e3:>8.3f}ms  "
              f"{cpu:>5.0%} {o['runq_f']:>5.0%} {o['wake_f']:>5.0%} {o['blocked_io_f']:>5.0%} "
              f"{o['blocked_other_f']:>5.0%} {o['syscall_f']:>5.0%} {o['bio_kb']:>7.0f}  "
              f"{SHORT[o['label']]}" + (f"  (likeliest: {SHORT[o['forced']]})"
                                        if o["label"] == m.IND else ""))
    if len(out) > 50 and not a.all:
        print(f"... {len(out) - 50} more (--all to print them)")
    counts = collections.Counter(o["label"] for o in out)
    total_wall = sum(o["wall_us"] for o in out) or 1
    print("\nSummary")
    for lab in m.LABELS:
        sel = [o for o in out if o["label"] == lab]
        if sel:
            print(f"  {SHORT[lab]:<14} {counts[lab]:>6} {unit}s  "
                  f"{100 * sum(o['wall_us'] for o in sel) / total_wall:5.1f}% of traced time")
    print("\nColumns are shares of each operation's wall time: run (on CPU), runq (waiting for a "
          "CPU),\nwake (waking an idle CPU), io (blocked with in_iowait), other (blocked otherwise, "
          "e.g. sleep),\nsysc (inside syscalls); bioKB = disk I/O this thread submitted.")
    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0]) if out else ["tid"])
            w.writeheader()
            w.writerows(out)
        print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
