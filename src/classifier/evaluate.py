#!/usr/bin/env python3
"""Evaluate the stall classifiers on a feature table.

  evaluate.py FEATURES.csv --config experiments/configs/eval.json --out DIR

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
        th = m.tune_rule(rule, dev, macro_f1) if src != "block" else dict(m.DEFAULT_THRESHOLDS)
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
    print(f"  {'classifier':<18}{'macro-F1':>9}  {'95% CI':<15}{'accuracy':>9}"
          f"{'abstain':>9}{'hc-error':>9}{'hc-error forced':>17}")
    for name, e in res.items():
        print(f"  {name:<18}{e['macro_f1']:>9.3f}  [{e['macro_f1_ci'][0]:.3f}, "
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

    # Confusion matrices and per-class scores for the fused models.
    results["fused_detail"] = {
        name: {"confusion": confusion(truth, preds[name]), "per_class": per_class(truth, preds[name])}
        for name in ("rules/fused", "tree/fused")}

    # H1: fused vs each single source (paired bootstrap over runs).
    h1 = {}
    for fam in ("rules", "tree"):
        for src in ("syscall", "block", "scheduler"):
            a, b = preds[f"{fam}/fused"], preds[f"{fam}/{src}"]
            h1[f"{fam}: fused - {src}"] = {
                "macro_f1_diff": macro_f1(truth, a) - macro_f1(truth, b),
                "ci": bootstrap(test, a, macro_f1, cfg["bootstrap"], cfg["seed"], b)}
    results["H1"] = h1
    print("\nH1 (fused minus single source, macro-F1, 95% CI):")
    for k, v in h1.items():
        print(f"  {k:<26}{v['macro_f1_diff']:+.3f}  [{v['ci'][0]:+.3f}, {v['ci'][1]:+.3f}]")

    # H2: abstaining vs forced, high-confidence error rate.
    h2 = {}
    for fam in ("rules", "tree"):
        a, f = preds[f"{fam}/fused"], preds[f"{fam}/fused/forced"]
        h2[fam] = {"hc_error_abstain": hc_error(truth, a), "hc_error_forced": hc_error(truth, f),
                   "abstention": abstention(truth, a),
                   "diff_ci": bootstrap(test, f, hc_error, cfg["bootstrap"], cfg["seed"], a)}
    results["H2"] = h2
    print("\nH2 (high-confidence error, forced minus abstaining, 95% CI):")
    for k, v in h2.items():
        print(f"  {k:<6} forced {v['hc_error_forced']:.3f}  abstaining {v['hc_error_abstain']:.3f}"
              f"  (abstains on {v['abstention']:.1%})  diff CI [{v['diff_ci'][0]:+.3f}, "
              f"{v['diff_ci'][1]:+.3f}]")

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
    if cfg.get("transfer"):
        tr = [r for r in dev if matches(r, cfg["transfer"]["train_filter"])
              or r["workload"] in cfg["transfer"]["always_include"]]
        te_all = [r for r in rows if matches(r, cfg["transfer"]["test_filter"])]
        te = [r for r in te_all if str(r["rep"]) in {str(x) for x in cfg["test_reps"]}] or te_all
        if tr and te:
            results["transfer"], _ = score_all(te, build_classifiers(tr, cfg), cfg,
                                               "Transfer (different I/O size and pattern)")

    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    with open(os.path.join(args.out, "fused_tree.txt"), "w") as f:
        f.write(main_res["tree/fused"]["description"]["rules"])
    print(f"\nwrote {args.out}/results.json and fused_tree.txt")


if __name__ == "__main__":
    main()
