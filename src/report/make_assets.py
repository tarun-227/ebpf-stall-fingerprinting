#!/usr/bin/env python3
"""Generate the report's LaTeX tables and pgfplots data from processed results.

    python3 src/report/make_assets.py [--results experiments/processed/wsl] [--out report/generated]

Reads results-ref/, results-design/, window-*ms/, delay.json and overhead-v2.json
and writes one .tex or .dat file per table or figure. The report \\input{}s them,
so every number in a table comes from a results file, never from hand copying.
"""
import argparse
import json
import os

LABELS = ["IO_WAIT_DOMINATED", "SCHEDULER_WAIT_DOMINATED", "SYSCALL_CPU_DOMINATED", "INDETERMINATE"]
SHORT = {"IO_WAIT_DOMINATED": "I/O", "SCHEDULER_WAIT_DOMINATED": "Sched",
         "SYSCALL_CPU_DOMINATED": "CPU", "INDETERMINATE": "Indet."}
SOURCES = [("syscall", "Syscall only"), ("block", "Block only"),
           ("scheduler", "Scheduler only"), ("scheduler_noiowait", "Scheduler, no iowait"),
           ("fused_noiowait", "Fused, no iowait"), ("fused", "Fused (all)")]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def f3(x):
    return f"{x:.3f}"


def pct(x, nd=1):
    return f"{100 * x:.{nd}f}"


def signed(x):
    return f"{x:+.3f}"


def write(out, name, text):
    with open(os.path.join(out, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def table_main(ref, design):
    rows = []
    for key, name in SOURCES:
        for kind in ("rules", "tree"):
            m = ref["main"][f"{kind}/{key}"]
            d = design["main"][f"{kind}/{key}"]
            lo, hi = m["macro_f1_ci"]
            bold = key == "fused"
            cell = lambda s: f"\\textbf{{{s}}}" if bold else s  # noqa: E731
            rows.append(" & ".join([
                name if kind == "rules" else "", kind,
                cell(f3(m["macro_f1"])), f"[{f3(lo)}, {f3(hi)}]", pct(m["accuracy"]),
                pct(m["abstention"]), pct(m["hc_error"]), pct(m["forced"]["hc_error"]),
                f3(d["macro_f1"])]) + r" \\")
        rows.append(r"\midrule" if key != SOURCES[-1][0] else "")
    return "\n".join(r for r in rows if r) + "\n"


def table_confusion(ref, model="tree/fused"):
    conf = ref["fused_detail"][model]["confusion"]
    per = ref["fused_detail"][model]["per_class"]
    lines = []
    for t in LABELS:
        cells = [str(conf[t][p]) for p in LABELS]
        cells = [f"\\textbf{{{c}}}" if LABELS[i] == t else c for i, c in enumerate(cells)]
        lines.append(" & ".join([SHORT[t]] + cells + [f3(per[t]["precision"]), f3(per[t]["recall"])])
                     + r" \\")
    return "\n".join(lines) + "\n"


def table_hypotheses(ref, design):
    rows = []
    pairs = [("fused - syscall", "vs.\\ syscall"), ("fused - block", "vs.\\ block"),
             ("fused - scheduler", "vs.\\ scheduler"),
             ("fused_noiowait - scheduler_noiowait", "vs.\\ sched., no iowait$^*$")]
    for key, name in pairs:
        cells = [name]
        for kind in ("rules", "tree"):
            h = ref["H1"][f"{kind}: {key}"]
            lo, hi = h["ci"]
            cells += [signed(h["macro_f1_diff"]), f"[{lo:.3f}, {hi:.3f}]"]
        rows.append(" & ".join(cells) + r" \\")
    return "\n".join(rows) + "\n"


def table_h2(ref):
    rows = []
    for model, name in (("rules/fused", "Rules, fused"), ("tree/fused", "Tree, fused"),
                        ("rules/fused_noiowait", "Rules, fused no iowait"),
                        ("tree/fused_noiowait", "Tree, fused no iowait")):
        h = ref["H2"][model]
        lo, hi = h["diff_ci"]
        rows.append(" & ".join([name, pct(h["hc_error_forced"]), pct(h["hc_error_abstain"]),
                                pct(h["abstention"]), f"[{pct(lo)}, {pct(hi)}]"]) + r" \\")
    return "\n".join(rows) + "\n"


def table_ablation(ref, windows):
    rows = []
    base = ref["main"]["tree/fused"]
    rows.append(f"Full fused tree (markers) & {f3(base['macro_f1'])} & "
                f"[{f3(base['macro_f1_ci'][0])}, {f3(base['macro_f1_ci'][1])}] \\\\")
    for key, name in (("no block events", "No block events"),
                      ("no syscall events", "No syscall events"),
                      ("no scheduler events", "No scheduler events")):
        a = ref["ablations"][key]
        rows.append(f"{name} & {f3(a['macro_f1'])} & [{f3(a['ci'][0])}, {f3(a['ci'][1])}] \\\\")
    m = ref["main"]["tree/fused_noiowait"]
    rows.append(f"No \\texttt{{in\\_iowait}} flag & {f3(m['macro_f1'])} & "
                f"[{f3(m['macro_f1_ci'][0])}, {f3(m['macro_f1_ci'][1])}] \\\\")
    rows.append(r"\midrule")
    for ms, res in windows:
        w = res["main"]["tree/fused"]
        n = res.get("_n_ops")
        rows.append(f"PID-only, {ms}\\,ms windows & {f3(w['macro_f1'])} & "
                    f"[{f3(w['macro_f1_ci'][0])}, {f3(w['macro_f1_ci'][1])}] \\\\")
    rows.append(r"\midrule")
    c = ref["sensitivity_clean_runs"]
    rows.append(f"Clean runs only ({pct(c['kept_fraction'], 0)}\\% of ops) & "
                f"{f3(c['tree/fused']['macro_f1'])} & -- \\\\")
    s = ref["sensitivity_margin_0.05"]
    rows.append(f"Drop ops within 5\\,pp of the 60\\% rule & {f3(s['tree/fused']['macro_f1'])} & -- \\\\")
    t = ref["transfer_comparison"]["tree/fused_noiowait"]
    rows.append(f"Transfer 64K seq $\\to$ 16K rand (no iowait, acc.) & "
                f"{f3(t['accuracy_transfer'])} & -- \\\\")
    return "\n".join(rows) + "\n"


OVERHEAD_ROWS = [("sleep", "sleep"), ("sync_write", "sync\\_write"), ("cold_read", "cold\\_read"),
                 ("direct_read", "direct\\_read"), ("mixed", "mixed"), ("cpu", "cpu"),
                 ("warm_read", "warm\\_read"), ("buffered_write", "buffered\\_write")]


def table_overhead(ov):
    rows = []
    for key, name in OVERHEAD_ROWS:
        for thr in (1, 4):
            g = ov["groups"].get(f"{key}/{thr}/on")
            if not g:
                continue
            over = f"{key}/{thr}/on" in ov["over_budget"]
            loss = pct(g["throughput_loss"]["median"])
            rows.append(" & ".join([
                name if thr == 1 else "", str(thr),
                f"\\textbf{{{loss}}}" if over else loss,
                pct(g["p99_increase"]["median"], 0),
                f"{g['cpu_per_op_added_us']['median']:.0f}",
                pct(g["collector_cpu"]["median"], 0),
                pct(g["drop_rate"]["max"], 2)]) + r" \\")
    for variant, name in (("rb4m", "warm\\_read, 4\\,MiB ring buffer"),
                          ("nomarkers", "warm\\_read, no marker probes")):
        g = ov["groups"].get(f"warm_read/4/{variant}")
        if g:
            rows.append(r"\midrule" if variant == "rb4m" else "")
            rows.append(" & ".join([name, "4", pct(g["throughput_loss"]["median"]),
                                    pct(g["p99_increase"]["median"], 0),
                                    f"{g['cpu_per_op_added_us']['median']:.0f}",
                                    pct(g["collector_cpu"]["median"], 0),
                                    pct(g["drop_rate"]["max"], 2)]) + r" \\")
    return "\n".join(r for r in rows if r) + "\n"


def data_f1bars(ref):
    lines = ["idx source rules rules_lo rules_hi tree tree_lo tree_hi"]
    for i, (key, _) in enumerate(SOURCES):
        cells = [str(i), key]
        for kind in ("rules", "tree"):
            m = ref["main"][f"{kind}/{key}"]
            lo, hi = m["macro_f1_ci"]
            cells += [f"{m['macro_f1']:.4f}", f"{m['macro_f1'] - lo:.4f}", f"{hi - m['macro_f1']:.4f}"]
        lines.append(" ".join(cells))
    return "\n".join(lines) + "\n"


def data_delay(delay):
    """One file per workload/thread: nominal delay, traced and kernel I/O-blocked ms per op."""
    out = {}
    for key, per in delay.items():
        lines = ["delay trace kernel"]
        for d in ("0.0", "5.0", "20.0"):
            if d in per:
                lines.append(f"{float(d):g} {per[d]['trace_io_blocked_us'] / 1000:.4f} "
                             f"{per[d]['kernel_blocked_us'] / 1000:.4f}")
        out[key.replace("/", "_") + ".dat"] = "\n".join(lines) + "\n"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default="experiments/processed/wsl")
    ap.add_argument("--out", default="report/generated")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    r = lambda *p: os.path.join(a.results, *p)  # noqa: E731
    ref = load(r("results-ref", "results.json"))
    design = load(r("results-design", "results.json"))
    windows = [(ms, load(r(f"window-{ms}ms", "results.json"))) for ms in (5, 20, 100)
               if os.path.exists(r(f"window-{ms}ms", "results.json"))]
    write(a.out, "tab_main.tex", table_main(ref, design))
    write(a.out, "tab_confusion.tex", table_confusion(ref))
    write(a.out, "tab_h1.tex", table_hypotheses(ref, design))
    write(a.out, "tab_h2.tex", table_h2(ref))
    write(a.out, "tab_ablation.tex", table_ablation(ref, windows))
    write(a.out, "f1bars.dat", data_f1bars(ref))
    for name in ("overhead-v2.json", "overhead.json"):      # v2 is the final overhead run
        if os.path.exists(r(name)):
            write(a.out, "tab_overhead.tex", table_overhead(load(r(name))))
            break
    if os.path.exists(r("delay.json")):
        for name, text in data_delay(load(r("delay.json"))).items():
            write(a.out, "delay_" + name, text)
    print(f"wrote assets to {a.out}: " + ", ".join(sorted(os.listdir(a.out))))


if __name__ == "__main__":
    main()
