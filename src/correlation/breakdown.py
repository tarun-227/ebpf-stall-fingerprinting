#!/usr/bin/env python3
"""Per-operation time breakdown from stalltrace events and stallbench ground truth.

Each traced thread's scheduler events are turned into one continuous timeline
of states:

  run            on a CPU
  runq           runnable but waiting while another task used the CPU
                 (preempted, or woken onto a busy CPU)
  wake_latency   woken onto an idle CPU and waiting for it to leave idle;
                 not contention. Kernel accounting (schedstat run_delay and
                 thread CPU time) counts this as CPU time, because the
                 scheduler skips the clock update right after such a wakeup.
  blocked_io     off the run queue with the kernel's in_iowait flag set
  blocked_other  off the run queue for any other reason (sleep, futex, ...)

Any window can then be measured against that timeline:
  - the operation window (op_begin .. op_end markers, matched by tid and
    op_id) gives the features used for classification;
  - the reference window (the workload's own kernel-accounting readings,
    ref_t0_ns .. ref_t1_ns) gives the same breakdown over exactly the
    interval the reference numbers cover, for validating the tracer.

Per operation it also counts syscalls and syscall time, bytes of block I/O the
thread queued, page faults, and block completions on the traced devices from
any process.

Standard library only, so it also runs outside the project venv.

  breakdown.py EVENTS.jsonl GT.jsonl [--csv OUT.csv] [--summary]
"""
import argparse
import bisect
import collections
import csv
import json
import sys

RUN, RUNQ, WAKE_LAT = "run", "runq", "wake_latency"
BLK_IO, BLK_OTHER = "blocked_io", "blocked_other"
STATES = (RUN, RUNQ, WAKE_LAT, BLK_IO, BLK_OTHER)
WOKEN = "woken"  # internal: woken, bucket decided when it gets the CPU

# Events the thread itself executes; seeing one means it is on a CPU.
SELF_EVENTS = ("begin", "end", "sys_enter", "sys_exit", "bio", "fault")


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_ground_truth(path):
    rows = load_jsonl(path)
    if not rows or rows[0].get("type") != "run":
        raise SystemExit(f"{path}: first line is not a run header")
    return rows[0], {(o["tid"], o["op_id"]): o for o in rows[1:] if o.get("type") == "op"}


def per_thread_timeline(events, tids):
    """Map tid -> time-sorted list of (ts, order, kind, payload)."""
    tl = collections.defaultdict(list)
    for e in events:
        t, ts = e["type"], e["ts"]
        if t == "sched_switch":
            if e["tid"] in tids:
                if e["preempt"] or e["prev_state"] == 0:
                    state = RUNQ
                elif not e["iowait_known"]:
                    state = BLK_OTHER  # cannot tell; flagged per operation
                else:
                    state = BLK_IO if e["iowait"] else BLK_OTHER
                tl[e["tid"]].append((ts, 1, "out", (state, e["iowait_known"])))
            if e["next_tid"] in tids:
                tl[e["next_tid"]].append((ts, 2, "in", bool(e.get("prev_idle", 0))))
        elif e["tid"] not in tids:
            continue
        elif t in ("sched_wakeup", "sched_wakeup_new"):
            tl[e["tid"]].append((ts, 0, "wake", None))
        elif t == "op_begin":
            tl[e["tid"]].append((ts, 3, "begin", e["op_id"]))
        elif t == "op_end":
            tl[e["tid"]].append((ts, 0, "end", e["op_id"]))
        elif t == "sys_enter":
            tl[e["tid"]].append((ts, 1, "sys_enter", e["nr"]))
        elif t == "sys_exit":
            tl[e["tid"]].append((ts, 1, "sys_exit", e["nr"]))
        elif t == "bio_queue":
            tl[e["tid"]].append((ts, 1, "bio", e["bytes"]))
        elif t == "page_fault":
            tl[e["tid"]].append((ts, 1, "fault", None))
    for v in tl.values():
        v.sort(key=lambda x: (x[0], x[1]))
    return tl


class StateTimeline:
    """Continuous state intervals for one thread, queryable by time window."""

    def __init__(self, items):
        self.intervals = []  # (start, end, state), sorted, non-overlapping
        state, since, last_ts = None, None, None

        def close(ts, label):
            if since is not None and ts > since:
                self.intervals.append((since, ts, label))

        for ts, _, kind, payload in items:
            last_ts = ts
            if kind == "out":
                close(ts, RUNQ if state == WOKEN else (state or RUN))
                state, since = payload[0], ts
            elif kind == "in":
                if state == WOKEN:
                    close(ts, WAKE_LAT if payload else RUNQ)
                elif state is not None:
                    close(ts, state)
                state, since = RUN, ts
            elif kind == "wake":
                if state in (BLK_IO, BLK_OTHER):
                    close(ts, state)
                    state, since = WOKEN, ts
                elif state is None:
                    state, since = WOKEN, ts
            elif kind in SELF_EVENTS and state is None:
                state, since = RUN, ts
        if state is not None and last_ts is not None:
            close(last_ts, RUNQ if state == WOKEN else state)
        self.starts = [iv[0] for iv in self.intervals]
        self.first = self.intervals[0][0] if self.intervals else None
        self.last = self.intervals[-1][1] if self.intervals else None

    def measure(self, a, b):
        """Time in each state within [a, b], plus time the timeline does not cover."""
        out = {s: 0 for s in STATES}
        i = max(bisect.bisect_right(self.starts, a) - 1, 0)
        covered = 0
        while i < len(self.intervals):
            s, e, label = self.intervals[i]
            if s >= b:
                break
            overlap = min(e, b) - max(s, a)
            if overlap > 0:
                out[label] += overlap
                covered += overlap
            i += 1
        out["uncovered"] = (b - a) - covered
        return out


def breakdown(events, gt_ops):
    tids = {tid for tid, _ in gt_ops}
    completions = sorted(e["ts"] for e in events if e["type"] == "blk_complete")
    rows = []
    for tid, items in per_thread_timeline(events, tids).items():
        timeline = StateTimeline(items)
        op = None
        for ts, _, kind, payload in items:
            if kind == "begin":
                op = {"tid": tid, "op_id": payload, "begin_ts": ts, "sys_enter": None,
                      "iowait_unknown": False, "n_switch_out": 0, "n_syscalls": 0,
                      "syscall_ns": 0, "bio_bytes": 0, "n_faults": 0}
                continue
            if op is None:
                continue
            if kind == "out":
                op["n_switch_out"] += 1
                op["iowait_unknown"] |= not payload[1]
            elif kind == "sys_enter":
                op["sys_enter"] = ts
            elif kind == "sys_exit":
                if op["sys_enter"] is not None:
                    op["n_syscalls"] += 1
                    op["syscall_ns"] += ts - op["sys_enter"]
                    op["sys_enter"] = None
            elif kind == "bio":
                op["bio_bytes"] += payload
            elif kind == "fault":
                op["n_faults"] += 1
            elif kind == "end":
                if payload == op["op_id"]:
                    op["end_ts"] = ts
                    rows.append(finish(op, timeline, gt_ops.get((tid, op["op_id"])),
                                       completions))
                op = None
    rows.sort(key=lambda r: (r["tid"], r["op_id"]))
    return rows


def window_breakdown(events, gt_ops, width_ns, min_cover=0.5):
    """PID-only mode: ignore the markers and cut each traced thread's timeline
    into fixed windows of width_ns, as a tool without operation boundaries
    would. Each window gets the same features as an operation. For scoring
    only, it is matched to the ground-truth operation (same thread) that
    overlaps it most; windows less than min_cover inside any operation are
    dropped. Returns rows shaped like breakdown()'s, plus "gt_op_id"."""
    tids = {tid for tid, _ in gt_ops}
    completions = sorted(e["ts"] for e in events if e["type"] == "blk_complete")
    ops_by_tid = collections.defaultdict(list)
    for (tid, op_id), g in gt_ops.items():
        ops_by_tid[tid].append((g["t_begin_ns"], g["t_end_ns"], op_id, g))
    rows = []
    for tid, items in per_thread_timeline(events, tids).items():
        ops = sorted(ops_by_tid[tid])
        if not ops:
            continue
        timeline = StateTimeline(items)
        syscalls, enter = [], None   # (start, end) intervals
        bios, faults, unknown = [], [], []
        for ts, _, kind, payload in items:
            if kind == "sys_enter":
                enter = ts
            elif kind == "sys_exit" and enter is not None:
                syscalls.append((enter, ts))
                enter = None
            elif kind == "bio":
                bios.append((ts, payload))
            elif kind == "fault":
                faults.append(ts)
            elif kind == "out" and not payload[1]:
                unknown.append(ts)
        start, end = ops[0][0], ops[-1][1]
        a, k = start, 0
        while a + width_ns <= end:
            b = a + width_ns
            best, cover = None, 0
            for ob, oe, op_id, g in ops:          # few ops per thread: linear is fine
                ov = min(oe, b) - max(ob, a)
                if ov > cover:
                    best, cover = (op_id, g), ov
            if best and cover >= min_cover * width_ns:
                row = {"tid": tid, "op_id": k, "gt_op_id": best[0], "begin_ts": a, "end_ts": b,
                       "wall": width_ns, **timeline.measure(a, b)}
                inside = [(max(s, a), min(e, b)) for s, e in syscalls if e > a and s < b]
                row["n_syscalls"] = len(inside)
                row["syscall_ns"] = sum(e - s for s, e in inside)
                row["bio_bytes"] = sum(n for t, n in bios if a <= t < b)
                row["n_faults"] = sum(1 for t in faults if a <= t < b)
                row["n_switch_out"] = sum(1 for ts, _, kind, _ in items
                                          if kind == "out" and a <= ts < b)
                row["iowait_unknown"] = any(a <= t < b for t in unknown)
                row["blk_completions_any"] = (bisect.bisect_right(completions, b)
                                              - bisect.bisect_left(completions, a))
                g = best[1]
                row.update({k2: g[k2] for k2 in ("workload", "expected", "ref_label",
                                                 "ref_wall_ns", "ref_cpu_ns", "ref_runq_ns",
                                                 "ref_blocked_ns")})
                rows.append(row)
            a, k = b, k + 1
    return rows


def finish(op, timeline, gt, completions):
    lo = bisect.bisect_left(completions, op["begin_ts"])
    hi = bisect.bisect_right(completions, op["end_ts"])
    row = {k: op[k] for k in ("tid", "op_id", "begin_ts", "end_ts", "n_switch_out",
                              "n_syscalls", "syscall_ns", "bio_bytes", "n_faults",
                              "iowait_unknown")}
    row["wall"] = op["end_ts"] - op["begin_ts"]
    row.update(timeline.measure(op["begin_ts"], op["end_ts"]))
    row["blk_completions_any"] = hi - lo
    if gt:
        row.update({
            "workload": gt["workload"], "expected": gt["expected"], "ref_label": gt["ref_label"],
            "ref_wall_ns": gt["ref_wall_ns"], "ref_cpu_ns": gt["ref_cpu_ns"],
            "ref_runq_ns": gt["ref_runq_ns"], "ref_blocked_ns": gt["ref_blocked_ns"],
            "marker_skew_begin_ns": gt["t_begin_ns"] - op["begin_ts"],
            "marker_skew_end_ns": op["end_ts"] - gt["t_end_ns"],
        })
        if "ref_t0_ns" in gt:
            val = timeline.measure(gt["ref_t0_ns"], gt["ref_t1_ns"])
            row.update({f"val_{k}": v for k, v in val.items()})
    return row


def summarize(rows, header, gt_ops, out=sys.stdout):
    def pct(num, den):
        return 100.0 * num / den if den else 0.0

    matched = [r for r in rows if "ref_wall_ns" in r]
    print(f"run {header['run_id']}: workload {header['workload']}, "
          f"{len(gt_ops)} ops in ground truth, {len(rows)} reconstructed from markers, "
          f"{len(matched)} matched", file=out)
    if not matched:
        return
    wall = sum(r["wall"] for r in matched)
    print("  operation window (features):  " + "  ".join(
        f"{s} {pct(sum(r[s] for r in matched), wall):5.1f}%" for s in STATES), file=out)

    validated = [r for r in matched if "val_run" in r]
    if validated:
        # Same window as the reference, compared in the kernel's convention:
        # wake latency counts as CPU time.
        ref_wall = sum(r["ref_wall_ns"] for r in validated)
        cpu = sum(r["val_run"] + r["val_wake_latency"] for r in validated)
        runq = sum(r["val_runq"] for r in validated)
        blk = sum(r["val_blocked_io"] + r["val_blocked_other"] for r in validated)
        print(f"  reference window, trace:      cpu {pct(cpu, ref_wall):5.1f}%  "
              f"runq {pct(runq, ref_wall):5.1f}%  blocked {pct(blk, ref_wall):5.1f}%", file=out)
        print(f"  reference window, kernel:     "
              f"cpu {pct(sum(r['ref_cpu_ns'] for r in validated), ref_wall):5.1f}%  "
              f"runq {pct(sum(r['ref_runq_ns'] for r in validated), ref_wall):5.1f}%  "
              f"blocked {pct(sum(r['ref_blocked_ns'] for r in validated), ref_wall):5.1f}%",
              file=out)
        err = [abs(r["val_run"] + r["val_wake_latency"] - r["ref_cpu_ns"])
               + abs(r["val_runq"] - r["ref_runq_ns"])
               + abs(r["val_blocked_io"] + r["val_blocked_other"] - r["ref_blocked_ns"])
               for r in validated]
        err.sort()
        print(f"  per-op absolute error, same window: mean {sum(err) / len(err) / 1e3:.1f} us "
              f"({pct(sum(err), ref_wall):.2f}% of wall), median {err[len(err) // 2] / 1e3:.1f} us, "
              f"max {err[-1] / 1e3:.1f} us", file=out)
        uncovered = sum(r["val_uncovered"] for r in validated)
        if uncovered:
            print(f"  note: {pct(uncovered, ref_wall):.2f}% of the reference window is outside "
                  f"the traced timeline", file=out)
    else:
        print("  (ground truth has no ref_t0_ns/ref_t1_ns; rebuild stallbench to validate)",
              file=out)

    skew = [r["marker_skew_begin_ns"] for r in matched] + [r["marker_skew_end_ns"] for r in matched]
    print(f"  marker skew vs workload clock: min {min(skew) / 1e3:.1f} us, "
          f"max {max(skew) / 1e3:.1f} us (should be >= 0 and small)", file=out)
    if any(r["iowait_unknown"] for r in matched):
        print("  WARNING: kernel did not expose in_iowait for some switches", file=out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("events")
    ap.add_argument("ground_truth")
    ap.add_argument("--csv", help="write one row per operation")
    ap.add_argument("--summary", action="store_true", help="print trace vs reference summary")
    args = ap.parse_args()

    header, gt_ops = load_ground_truth(args.ground_truth)
    rows = breakdown(load_jsonl(args.events), gt_ops)
    if args.csv:
        fields = sorted({k for r in rows for k in r})
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    if args.summary or not args.csv:
        summarize(rows, header, gt_ops)


if __name__ == "__main__":
    main()
