#!/usr/bin/env python3
"""Summarise instrumentation overhead from scripts/run_overhead.sh output.

  overhead.py RAW_DIR [--out overhead.json] [--budget-throughput 0.10] [--budget-drops 0.01]

Per workload, thread count and variant (off = no tracing, on = full tracing,
plus the single-feature variants): throughput (operations per second of the
workload's own run time), p50 and p99 operation latency, collector CPU as a
share of one CPU, and dropped-event rate. Each repetition is compared with
the tracing-off run of the same repetition; the table shows the median over
repetitions and the min-max range. "offrepeat" is a second untraced run: its
"loss" is the noise floor, which any overhead must exceed to be meaningful.
The budget (declared before
any measurement) is at most 10% throughput loss and under 1% dropped events.
"""
import argparse
import glob
import json
import os
import statistics as st


def run_metrics(d):
    with open(os.path.join(d, "meta.json")) as f:
        meta = json.load(f)
    with open(os.path.join(d, "gt.jsonl")) as f:
        lines = [json.loads(x) for x in f if x.strip()]
    head, ops = lines[0], [o for o in lines[1:] if o.get("type") == "op" and not o["err"]]
    secs = (head["t_end_ns"] - head["t_start_ns"]) / 1e9
    lat = sorted(o["lat_ns"] for o in ops)
    # Per-operation thread CPU time (kernel accounting on the wall-clock
    # scale): what the tracer adds to the workload itself, and much less
    # sensitive to frequency scaling than wall-clock throughput.
    cpu = sorted(o["ref_cpu_ns"] for o in ops if o.get("ref_runq_ns", -1) >= 0)
    if cpu:
        cpu_per_op = cpu[len(cpu) // 2] / 1e3
    elif head.get("worker_cpu_ns") and ops:
        # --no-ref runs: workers' total CPU time over all operations, on the
        # wall-clock scale (the kernel clock can run fast or slow in a VM).
        cpu_per_op = head["worker_cpu_ns"] / head.get("clock_ratio", 1.0) / len(ops) / 1e3
    else:
        cpu_per_op = None
    m = {"meta": meta, "throughput": len(ops) / secs if secs > 0 else 0.0,
         "p50_us": lat[len(lat) // 2] / 1e3 if lat else 0.0,
         "p99_us": lat[int(0.99 * (len(lat) - 1))] / 1e3 if lat else 0.0,
         "cpu_per_op_us": cpu_per_op,
         "collector_cpu": None, "drop_rate": None}
    stats = os.path.join(d, "stats.json")
    if os.path.exists(stats):
        with open(stats) as f:
            s = json.load(f)
        wall = (s["t_end_ns"] - s["t_start_ns"]) / 1e9
        if wall > 0:
            m["collector_cpu"] = (s["collector_cpu_user_s"] + s["collector_cpu_sys_s"]) / wall
        m["drop_rate"] = s["totals"]["drop_rate"]
    return m


def summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"median": st.median(vals), "min": min(vals), "max": max(vals)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("raw")
    ap.add_argument("--out")
    ap.add_argument("--budget-throughput", type=float, default=0.10)
    ap.add_argument("--budget-drops", type=float, default=0.01)
    args = ap.parse_args()

    runs = [run_metrics(d) for d in sorted(glob.glob(os.path.join(args.raw, "*")))
            if os.path.exists(os.path.join(d, "gt.jsonl"))]
    off = {(r["meta"]["workload"], r["meta"]["threads"], r["meta"]["rep"]): r
           for r in runs if r["meta"]["variant"] == "off"}
    groups = {}
    for r in runs:
        mt = r["meta"]
        if mt["variant"] == "off":
            continue
        base = off.get((mt["workload"], mt["threads"], mt["rep"]))
        if not base:
            continue
        g = groups.setdefault((mt["workload"], mt["threads"], mt["variant"]), [])
        g.append({
            "throughput_loss": 1 - r["throughput"] / base["throughput"] if base["throughput"] else None,
            "p50_increase": r["p50_us"] / base["p50_us"] - 1 if base["p50_us"] else None,
            "p99_increase": r["p99_us"] / base["p99_us"] - 1 if base["p99_us"] else None,
            "cpu_per_op_increase": (r["cpu_per_op_us"] / base["cpu_per_op_us"] - 1
                                    if r["cpu_per_op_us"] and base["cpu_per_op_us"] else None),
            "cpu_per_op_added_us": (r["cpu_per_op_us"] - base["cpu_per_op_us"]
                                    if r["cpu_per_op_us"] is not None
                                    and base["cpu_per_op_us"] is not None else None),
            "collector_cpu": r["collector_cpu"], "drop_rate": r["drop_rate"],
            "throughput_off": base["throughput"], "throughput_on": r["throughput"]})

    out, violations = {}, []
    print(f"{'workload':<15}{'thr':>4} {'variant':<11}{'reps':>5}{'thr. loss':>11}{'(range)':>17}"
          f"{'p50 +':>9}{'p99 +':>9}{'CPU/op +':>10}{'(us)':>8}{'collector CPU':>15}{'drops':>9}")
    for key in sorted(groups, key=lambda k: (k[2] != "on", k[0], k[1], k[2])):
        g = groups[key]
        e = {k: summary([x[k] for x in g]) for k in g[0]}
        out["/".join(map(str, key))] = {"reps": len(g), **e}
        tl, dr = e["throughput_loss"], e["drop_rate"]
        within = (tl is None or tl["median"] <= args.budget_throughput) and \
                 (dr is None or dr["max"] < args.budget_drops)
        if key[2] == "on" and not within:
            violations.append("/".join(map(str, key)))
        pct = lambda s: f"{100 * s['median']:+.1f}%" if s else "n/a"
        rng = f"[{100 * tl['min']:+.1f}, {100 * tl['max']:+.1f}]" if tl else ""
        cpu = f"{100 * e['collector_cpu']['median']:.1f}%" if e["collector_cpu"] else "n/a"
        drops = f"{100 * dr['max']:.3f}%" if dr else "n/a"
        added = e["cpu_per_op_added_us"]
        added_s = f"{added['median']:+.1f}" if added else "n/a"
        print(f"{key[0]:<15}{key[1]:>4} {key[2]:<11}{len(g):>5}{pct(tl):>11}{rng:>17}"
              f"{pct(e['p50_increase']):>9}{pct(e['p99_increase']):>9}"
              f"{pct(e['cpu_per_op_increase']):>10}{added_s:>8}{cpu:>15}{drops:>9}"
              + ("" if within or key[2] != "on" else "  OVER BUDGET"))
    print(f"\nbudget: throughput loss <= {args.budget_throughput:.0%} (median), drops < "
          f"{args.budget_drops:.0%} (worst run); full tracing over budget in: "
          f"{', '.join(violations) or 'none'}")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({"groups": out, "over_budget": violations,
                       "budget": {"throughput_loss": args.budget_throughput,
                                  "drop_rate": args.budget_drops}}, f, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
