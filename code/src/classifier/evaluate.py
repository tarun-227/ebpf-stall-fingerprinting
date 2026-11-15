#!/usr/bin/env python3
"""Evaluate the stall classifiers on a feature table.

  evaluate.py FEATURES.csv --config configs/eval.json --out DIR

Split. Operations are split by whole runs, never by individual operation.
Runs whose repetition index is in `dev_reps` are used for tuning thresholds
and training trees; runs in `test_reps` are used only for the reported
numbers. Thresholds and trees are frozen before the test runs are touched.

Ground truth is the per-operation reference label (`label`, from the kernel's
own accounting); agreement with the design label (`expected`) is reported too.

Reported per classifier, on the test runs:
  macro-F1 (4 classes) with a 95% bootstrap CI over runs, accuracy,
  abstention rate (INDETERMINATE predictions), and high-confidence error rate
  (fraction of operations given a wrong cause label, i.e. not abstained).
Plus confusion matrices and per-class precision/recall for the fused models,
H1 (fused vs each single source, paired bootstrap), H2 (abstaining vs forced),
feature ablations, and a transfer test to a different I/O size and pattern.
"""
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features as ft  # noqa: E402
import models as m  # noqa: E402


# ----------------------------------------------------------------- metrics

def confusion(truth, pred):
    c = {t: {p: 0 for p in m.LABELS} for t in m.LABELS}
    for t, p in zip(truth, pred):
        c[t][p] += 1
    return c


def per_class(truth, pred):
    out = {}
    for k in m.LABELS:
        tp = sum(1 for t, p in zip(truth, pred) if t == k and p == k)
        fp = sum(1 for t, p in zip(truth, pred) if t != k and p == k)
        fn = sum(1 for t, p in zip(truth, pred) if t == k and p != k)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[k] = {"precision": prec, "recall": rec, "f1": f1, "support": tp + fn}
    return out


def macro_f1(truth, pred):
    pc = per_class(truth, pred)
    present = [k for k in m.LABELS if pc[k]["support"] > 0]
    return sum(pc[k]["f1"] for k in present) / len(present) if present else 0.0


def accuracy(truth, pred):
    return sum(t == p for t, p in zip(truth, pred)) / len(truth) if truth else 0.0


def abstention(truth, pred):
    return sum(p == m.IND for p in pred) / len(pred) if pred else 0.0


def hc_error(truth, pred):
    """High-confidence errors: committed to a cause, and wrong."""
    return sum(p != m.IND and p != t for t, p in zip(truth, pred)) / len(pred) if pred else 0.0


METRICS = {"macro_f1": macro_f1, "accuracy": accuracy, "abstention": abstention,
           "hc_error": hc_error}


def bootstrap(rows, preds, fn, n, seed, preds_b=None):
    """95% percentile CI of fn (or of fn(a) - fn(b) if preds_b), resampling whole runs."""
    by_run = {}
    for i, r in enumerate(rows):
        by_run.setdefault(r["run_id"], []).append(i)
    runs = sorted(by_run)
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        idx = [i for run in (rng.choice(runs) for _ in runs) for i in by_run[run]]
        truth = [rows[i]["label"] for i in idx]
        v = fn(truth, [preds[i] for i in idx])
        if preds_b is not None:
            v -= fn(truth, [preds_b[i] for i in idx])
        vals.append(v)
    vals.sort()
    return [vals[int(0.025 * (n - 1))], vals[int(0.975 * (n - 1))]]


# -------------------------------------------------------------------- split

def split(rows, cfg):
    reps = {str(r["rep"]) for r in rows}
    if reps == {""}:
        # No repetition index (e.g. smoke-test data): alternate whole runs.
        runs = sorted({r["run_id"] for r in rows})
        dev_runs = set(runs[0::2])
        print("warning: no repetition index; splitting alternate runs into dev/test",
              file=sys.stderr)
        return ([r for r in rows if r["run_id"] in dev_runs],
                [r for r in rows if r["run_id"] not in dev_runs])
    dev_reps = {str(x) for x in cfg["dev_reps"]}
    test_reps = {str(x) for x in cfg["test_reps"]}
    return ([r for r in rows if str(r["rep"]) in dev_reps],
            [r for r in rows if str(r["rep"]) in test_reps])


def _same(a, b):
    """Compare a table value with a config value; numbers compare numerically."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def matches(r, flt):
    return all(_same(r[k], v) for k, v in flt.items())


# -------------------------------------------------------------- classifiers

def fit_tree(dev, feats, cfg):
    """Fit a tree; pick its abstention threshold by leave-one-run-group-out on dev."""
    groups = sorted({str(r["rep"]) or r["run_id"] for r in dev})
    taus = cfg["tree"]["abstain_grid"]
    best_tau, best_score = taus[0], -1.0
    if len(groups) > 1:
        for tau in taus:
            truth, pred = [], []
            for g in groups:
                tr = [r for r in dev if (str(r["rep"]) or r["run_id"]) != g]
                va = [r for r in dev if (str(r["rep"]) or r["run_id"]) == g]
                if not tr or not va:
                    continue
                t = m.DecisionTree(feats, cfg["tree"]["max_depth"], cfg["tree"]["min_leaf"]).fit(tr)
                truth += [r["label"] for r in va]
                pred += [t.predict(r, tau) for r in va]
            score = macro_f1(truth, pred)
            if score > best_score + 1e-12:
                best_tau, best_score = tau, score
    tree = m.DecisionTree(feats, cfg["tree"]["max_depth"], cfg["tree"]["min_leaf"]).fit(dev)
    return tree, best_tau


def build_classifiers(dev, cfg):
    """name -> (predict(row, abstain), description). All tuning uses dev only."""
    out = {}
    for src, rule in m.RULES.items():
        th = (dict(m.DEFAULT_THRESHOLDS) if src in m.UNTUNED
              else m.tune_rule(rule, dev, macro_f1))
        out[f"rules/{src}"] = (lambda r, a, rule=rule, th=th: rule(r, th, a),
                               {"type": "rules", "source": src, "thresholds": th})
    for src, feats in m.FEATURE_SETS.items():
        tree, tau = fit_tree(dev, feats, cfg)
        out[f"tree/{src}"] = (lambda r, a, tree=tree, tau=tau: tree.predict(r, tau, a),
                              {"type": "tree", "source": src, "abstain_below": tau,
                               "rules": tree.describe()})
    return out


def score_all(test, classifiers, cfg, label):
    truth = [r["label"] for r in test]
    res, preds = {}, {}
    for name, (predict, desc) in classifiers.items():
        p = [predict(r, True) for r in test]
        pf = [predict(r, False) for r in test]
        preds[name], preds[name + "/forced"] = p, pf
        entry = {k: fn(truth, p) for k, fn in METRICS.items()}
        entry["macro_f1_ci"] = bootstrap(test, p, macro_f1, cfg["bootstrap"], cfg["seed"])
        entry["forced"] = {k: fn(truth, pf) for k, fn in METRICS.items()}
        entry["description"] = desc
        res[name] = entry
    print(f"\n{label}: {len(test)} operations in {len({r['run_id'] for r in test})} runs")
    print(f"  {'classifier':<26}{'macro-F1':>9}  {'95% CI':<15}{'accuracy':>9}"
          f"{'abstain':>9}{'hc-error':>9}{'hc-error forced':>17}")
    for name, e in res.items():
        print(f"  {name:<26}{e['macro_f1']:>9.3f}  [{e['macro_f1_ci'][0]:.3f}, "
              f"{e['macro_f1_ci'][1]:.3f}]{e['accuracy']:>9.3f}{e['abstention']:>9.3f}"
              f"{e['hc_error']:>9.3f}{e['forced']['hc_error']:>17.3f}")
    return res, preds


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("features")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--truth", choices=("reference", "expected"), default="reference",
                    help="score against the measured reference label (default) or the "
                         "design label; the reference comes from scheduler accounting, so "
                         "also check scheduler-based models against the design label")
    ap.add_argument("--train-from", metavar="FEATURES_CSV",
                    help="robustness: also train and tune on the dev runs of another "
                         "environment's feature table and score those frozen classifiers "
                         "on this table's test runs (no retuning)")
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    rows = ft.load_table(args.features)
    os.makedirs(args.out, exist_ok=True)
    results = {"config": cfg, "truth": args.truth}

    base = [r for r in rows if not matches(r, cfg["transfer"]["test_filter"])] \
        if cfg.get("transfer") else rows
    dev, test = split(base, cfg)
    if not dev or not test:
        raise SystemExit("empty dev or test split; check dev_reps/test_reps")

    agree = sum(r["label"] == r["expected"] for r in rows) / len(rows)
    results["design_vs_reference_agreement"] = agree
    print(f"design label agrees with reference label on {agree:.1%} of {len(rows)} operations")
    if args.truth == "expected":
        for r in rows:
            r["reference_label"], r["label"] = r["label"], r["expected"]
        print("scoring against the DESIGN label")

    classifiers = build_classifiers(dev, cfg)
    main_res, preds = score_all(test, classifiers, cfg, "Main evaluation (test runs)")
    results["main"] = main_res
    truth = [r["label"] for r in test]

    if args.train_from:
        other = ft.load_table(args.train_from)
        if args.truth == "expected":
            for r in other:
                r["reference_label"], r["label"] = r["label"], r["expected"]
        other_base = [r for r in other if not matches(r, cfg["transfer"]["test_filter"])] \
            if cfg.get("transfer") else other
        other_dev, _ = split(other_base, cfg)
        cross, _ = score_all(test, build_classifiers(other_dev, cfg), cfg,
                             f"Cross-environment: trained on {os.path.basename(args.train_from)}"
                             f" ({len(other_dev)} dev ops), tested here")
        results["cross_environment"] = {"trained_on": args.train_from, "results": cross}

    # Confusion matrices and per-class scores for the fused models.
    results["fused_detail"] = {
        name: {"confusion": confusion(truth, preds[name]), "per_class": per_class(truth, preds[name])}
        for name in ("rules/fused", "tree/fused", "rules/fused_noiowait", "tree/fused_noiowait")}

    # H1: each fused variant vs the single sources it combines (paired bootstrap).
    # With in_iowait the scheduler layer alone can tell I/O blocking from other
    # blocking; without it, that distinction has to come from block evidence.
    h1 = {}
    comparisons = (("fused", ("syscall", "block", "block_thread", "block_device", "scheduler")),
                   ("fused_noiowait", ("syscall", "block", "scheduler_noiowait")))
    for fam in ("rules", "tree"):
        for fused, sources in comparisons:
            for src in sources:
                a, b = preds[f"{fam}/{fused}"], preds[f"{fam}/{src}"]
                h1[f"{fam}: {fused} - {src}"] = {
                    "macro_f1_diff": macro_f1(truth, a) - macro_f1(truth, b),
                    "ci": bootstrap(test, a, macro_f1, cfg["bootstrap"], cfg["seed"], b)}
    results["H1"] = h1
    print("\nH1 (fused minus single source, macro-F1, 95% CI):")
    for k, v in h1.items():
        print(f"  {k:<42}{v['macro_f1_diff']:+.3f}  [{v['ci'][0]:+.3f}, {v['ci'][1]:+.3f}]")

    # H2: abstaining vs forced, high-confidence error rate.
    h2 = {}
    for fam in ("rules", "tree"):
        for fused in ("fused", "fused_noiowait"):
            a, f = preds[f"{fam}/{fused}"], preds[f"{fam}/{fused}/forced"]
            h2[f"{fam}/{fused}"] = {
                "hc_error_abstain": hc_error(truth, a), "hc_error_forced": hc_error(truth, f),
                "abstention": abstention(truth, a),
                "diff_ci": bootstrap(test, f, hc_error, cfg["bootstrap"], cfg["seed"], a)}
    results["H2"] = h2
    print("\nH2 (high-confidence error, forced minus abstaining, 95% CI):")
    for k, v in h2.items():
        print(f"  {k:<21} forced {v['hc_error_forced']:.3f}  abstaining {v['hc_error_abstain']:.3f}"
              f"  (abstains on {v['abstention']:.1%})  diff CI [{v['diff_ci'][0]:+.3f}, "
              f"{v['diff_ci'][1]:+.3f}]")

    # Sensitivity: drop operations whose reference share is within 5 points of
    # the dominance line, where the label itself is uncertain.
    if args.truth == "reference" and test and "ref_margin" in test[0]:
        keep = [i for i, r in enumerate(test) if r["ref_margin"] >= 0.05]
        kt = [truth[i] for i in keep]
        sens = {"kept_fraction": len(keep) / len(test)}
        for name in ("rules/fused", "tree/fused", "rules/fused_noiowait", "tree/fused_noiowait",
                     "tree/scheduler", "tree/syscall", "tree/block"):
            kp = [preds[name][i] for i in keep]
            sens[name] = {"macro_f1": macro_f1(kt, kp), "accuracy": accuracy(kt, kp),
                          "hc_error": hc_error(kt, kp)}
        results["sensitivity_margin_0.05"] = sens
        print(f"\nSensitivity: excluding operations within 0.05 of the dominance line "
              f"(keeps {sens['kept_fraction']:.1%})")
        for k, v in sens.items():
            if k != "kept_fraction":
                print(f"  {k:<26} macro-F1 {v['macro_f1']:.3f}  accuracy {v['accuracy']:.3f}"
                      f"  hc-error {v['hc_error']:.3f}")

    # Sensitivity: drop whole runs that lost events (missing events can corrupt
    # a timeline) or whose kernel clock drifted by more than 1% during the run
    # (reference shares were scaled with the start-of-run ratio).
    clean = [i for i, r in enumerate(test)
             if float(r.get("run_drop_rate", 0) or 0) == 0
             and str(r.get("clock_drift", "0")) in ("0", "0.0", "", "False")]
    if len(clean) < len(test):
        ct = [truth[i] for i in clean]
        sens = {"kept_fraction": len(clean) / len(test),
                "kept_runs": len({test[i]["run_id"] for i in clean})}
        for name in ("rules/fused", "tree/fused", "rules/fused_noiowait", "tree/fused_noiowait",
                     "tree/scheduler", "tree/syscall", "tree/block"):
            cp = [preds[name][i] for i in clean]
            sens[name] = {"macro_f1": macro_f1(ct, cp), "accuracy": accuracy(ct, cp),
                          "hc_error": hc_error(ct, cp)}
        results["sensitivity_clean_runs"] = sens
        print(f"\nSensitivity: only runs with no dropped events and no clock drift "
              f"(keeps {sens['kept_fraction']:.1%} of operations, {sens['kept_runs']} runs)")
        for k, v in sens.items():
            if isinstance(v, dict):
                print(f"  {k:<26} macro-F1 {v['macro_f1']:.3f}  accuracy {v['accuracy']:.3f}"
                      f"  hc-error {v['hc_error']:.3f}")

    # Feature ablations (tree, so the model can re-weight what is left).
    abl = {}
    full = m.FEATURE_SETS["fused"]
    for name, drop in (("no block events", m.FEATURE_SETS["block"][1:]),
                       ("no scheduler events", m.FEATURE_SETS["scheduler"][1:]),
                       ("no syscall events", m.FEATURE_SETS["syscall"][1:])):
        feats = [f for f in full if f not in drop]
        tree, tau = fit_tree(dev, feats, cfg)
        p = [tree.predict(r, tau) for r in test]
        abl[name] = {"features": feats, "macro_f1": macro_f1(truth, p),
                     "ci": bootstrap(test, p, macro_f1, cfg["bootstrap"], cfg["seed"])}
    results["ablations"] = abl
    print("\nAblations (fused tree with a layer removed, macro-F1):")
    for k, v in abl.items():
        print(f"  {k:<22}{v['macro_f1']:.3f}  [{v['ci'][0]:.3f}, {v['ci'][1]:.3f}]")

    # Transfer: tuned/trained on one I/O size and pattern, tested on another.
    # The transfer set holds only I/O workloads, so macro-F1 there is dominated
    # by a few rare-class operations; the fair comparison is accuracy and
    # per-class recall against the SAME workloads at the training size/pattern.
    if cfg.get("transfer"):
        tr = [r for r in dev if matches(r, cfg["transfer"]["train_filter"])
              or r["workload"] in cfg["transfer"]["always_include"]]
        te_all = [r for r in rows if matches(r, cfg["transfer"]["test_filter"])]
        test_reps = {str(x) for x in cfg["test_reps"]}
        te = [r for r in te_all if str(r["rep"]) in test_reps] or te_all
        xfer_wl = {r["workload"] for r in te}
        same = [r for r in test if r["workload"] in xfer_wl
                and matches(r, cfg["transfer"]["train_filter"])]
        if tr and te:
            clf = build_classifiers(tr, cfg)
            results["transfer"], tpreds = score_all(
                te, clf, cfg, "Transfer (different I/O size and pattern)")
            te_truth = [r["label"] for r in te]
            same_truth = [r["label"] for r in same]
            cmp = {}
            print(f"\n  accuracy on the same workloads: trained size/pattern ({len(same)} ops)"
                  f" vs transfer size/pattern ({len(te)} ops)")
            for name in ("rules/fused", "tree/fused", "rules/fused_noiowait",
                         "tree/fused_noiowait", "tree/scheduler"):
                predict = clf[name][0]
                acc_same = accuracy(same_truth, [predict(r, True) for r in same]) if same else None
                acc_xfer = accuracy(te_truth, tpreds[name])
                rec = {k: v["recall"] for k, v in per_class(te_truth, tpreds[name]).items()
                       if v["support"]}
                cmp[name] = {"accuracy_trained_setting": acc_same, "accuracy_transfer": acc_xfer,
                             "recall_transfer": rec}
                same_s = f"{acc_same:.3f}" if acc_same is not None else "  n/a"
                print(f"  {name:<26} {same_s} -> {acc_xfer:.3f}   recall: "
                      + ", ".join(f"{k.split('_')[0]} {v:.2f}" for k, v in rec.items()))
            results["transfer_comparison"] = cmp

    # Tracer validation over the whole matrix: trace vs kernel accounting over
    # the same window, per workload.
    vals = [r for r in rows if isinstance(r.get("val_abs_err_f"), float)]
    if vals:
        byw = {}
        for r in vals:
            byw.setdefault(r["workload"], []).append(r["val_abs_err_f"])
        tv = {}
        print("\nTracer vs kernel accounting, same window (sum of |error| over cpu/runq/blocked,"
              " share of wall):")
        for wl in sorted(byw):
            e = sorted(byw[wl])
            tv[wl] = {"n": len(e), "mean": sum(e) / len(e), "median": e[len(e) // 2],
                      "p95": e[int(0.95 * (len(e) - 1))]}
            print(f"  {wl:<16} n={len(e):<5} mean {tv[wl]['mean']:.4f}  median "
                  f"{tv[wl]['median']:.4f}  p95 {tv[wl]['p95']:.4f}")
        results["tracer_validation"] = tv

    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    with open(os.path.join(args.out, "fused_tree.txt"), "w") as f:
        f.write(main_res["tree/fused"]["description"]["rules"])
    print(f"\nwrote {args.out}/results.json and fused_tree.txt")


if __name__ == "__main__":
    main()
