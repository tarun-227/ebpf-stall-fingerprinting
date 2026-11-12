#!/usr/bin/env python3
"""Injected-delay analysis: does the traced I/O-blocked time track the delay?

  delay_analysis.py FEATURES.csv [--out results.json]

Input is the feature table of a delay sweep (scripts/run_delay_sweep.sh), where
every run records the dm-delay setting in delay_ms. Per workload and thread
count it reports, for each delay:
  - median traced I/O-blocked time per operation (operation window, us)
  - median kernel-accounted blocked time (reference window, us)
  - median |trace - kernel| over the same window (stall-duration error, us)
  - share of operations whose reference label is I/O-dominated
and the slope of traced blocked time against the delay, i.e. how many
delay-length waits an operation contains. One delayed round of requests gives
a slope near 1 (a direct or cold read); dependent rounds give more (fdatasync:
data then journal; mmap with MADV_RANDOM: one 4 KiB fault after another).
"""
import argparse
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features as ft  # noqa: E402


def slope(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else float("nan")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("features")
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = [r for r in ft.load_table(args.features) if str(r.get("delay_ms", "")) != ""]
    if not rows:
        raise SystemExit("no rows with delay_ms; is this a delay-sweep table?")

    groups = {}
    for r in rows:
        groups.setdefault((r["workload"], int(r["threads"])), {}).setdefault(
            float(r["delay_ms"]), []).append(r)

    out = {}
    print(f"{'workload':<13}{'thr':>4}{'delay':>7}{'n':>6}{'trace io-blocked':>18}"
          f"{'kernel blocked':>16}{'|trace-kernel|':>16}{'IO label':>10}")
    for (wl, thr), by_delay in sorted(groups.items()):
        entry, xs, ys = {}, [], []
        for d, rs in sorted(by_delay.items()):
            trace_us = [r["blocked_io_f"] * r["wall_us"] for r in rs]
            kern_us = [r["ref_blocked_f"] * r["ref_wall_us"] for r in rs]
            err_us = [r["val_abs_err_f"] * r["ref_wall_us"] for r in rs
                      if isinstance(r.get("val_abs_err_f"), float)]
            io_share = sum(r["label"] == "IO_WAIT_DOMINATED" for r in rs) / len(rs)
            entry[str(d)] = {"n": len(rs), "trace_io_blocked_us": st.median(trace_us),
                             "kernel_blocked_us": st.median(kern_us),
                             "abs_err_us": st.median(err_us) if err_us else None,
                             "io_label_share": io_share}
            xs += [d * 1000.0] * len(rs)
            ys += trace_us
            e = entry[str(d)]
            err = f"{e['abs_err_us']:.0f}" if e["abs_err_us"] is not None else "n/a"
            print(f"{wl:<13}{thr:>4}{d:>6.0f}ms{len(rs):>6}{e['trace_io_blocked_us']:>16.0f}us"
                  f"{e['kernel_blocked_us']:>14.0f}us{err:>14}us{io_share:>10.0%}")
        if len(by_delay) > 1:
            entry["slope_waits_per_delay"] = slope(xs, ys)
            print(f"{'':<17}slope: {entry['slope_waits_per_delay']:.2f} delay-length waits per operation")
        out[f"{wl}/t{thr}"] = entry
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
