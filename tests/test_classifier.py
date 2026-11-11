"""Unit tests for src/classifier (rules, decision tree, metrics).

Run: python3 -m unittest discover -s tests
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "classifier"))
import evaluate as ev  # noqa: E402
import models as m  # noqa: E402

TH = {"dominance": 0.6}


def row(label=m.CPU, run="r1", rep=1, **kw):
    base = {"run_id": run, "rep": rep, "label": label, "wall_us": 1000.0, "run_f": 0.0,
            "runq_f": 0.0, "wake_f": 0.0, "blocked_io_f": 0.0, "blocked_other_f": 0.0,
            "syscall_f": 0.0, "n_syscalls": 0.0, "n_switch_out": 0.0, "bio_kb": 0.0,
            "blk_any": 0.0, "n_faults": 0.0, "iowait_unknown": 0.0}
    base.update(kw)
    return base


DIRECT_READ = row(m.IO, run_f=0.1, wake_f=0.03, blocked_io_f=0.87, syscall_f=0.95,
                  n_syscalls=1, bio_kb=64, blk_any=1)
SLEEP = row(m.IND, run_f=0.02, wake_f=0.02, blocked_other_f=0.96, syscall_f=0.98, n_syscalls=1)
CONTENDED = row(m.SCHED, run_f=0.25, runq_f=0.75, n_switch_out=3)
NEIGHBOR = row(m.CPU, run_f=1.0, blk_any=12)            # busy loop beside another process's I/O
IOWAIT_NO_BLOCK = row(m.IND, run_f=0.1, blocked_io_f=0.9, syscall_f=0.9)


class RuleTest(unittest.TestCase):
    def test_fused_rules_on_typical_operations(self):
        self.assertEqual(m.rule_fused(DIRECT_READ, TH), m.IO)
        self.assertEqual(m.rule_fused(SLEEP, TH), m.IND)
        self.assertEqual(m.rule_fused(CONTENDED, TH), m.SCHED)
        self.assertEqual(m.rule_fused(NEIGHBOR, TH), m.CPU)

    def test_fused_abstains_on_iowait_without_block_evidence(self):
        self.assertEqual(m.rule_fused(IOWAIT_NO_BLOCK, TH), m.IND)
        self.assertEqual(m.rule_scheduler(IOWAIT_NO_BLOCK, TH), m.IO)

    def test_fused_abstains_when_iowait_flag_unknown(self):
        r = dict(DIRECT_READ, iowait_unknown=1, blocked_io_f=0.0, blocked_other_f=0.87)
        self.assertEqual(m.rule_fused(r, TH), m.IND)

    def test_forced_variants_always_commit(self):
        for r in (DIRECT_READ, SLEEP, CONTENDED, NEIGHBOR, IOWAIT_NO_BLOCK):
            for rule in m.RULES.values():
                self.assertIn(rule(r, TH, abstain=False), m.CAUSES)

    def test_single_source_baselines_make_their_known_mistakes(self):
        self.assertEqual(m.rule_syscall(SLEEP, TH), m.IO)        # long syscall != I/O
        self.assertEqual(m.rule_syscall(CONTENDED, TH), m.CPU)   # cannot see run-queue wait
        self.assertEqual(m.rule_block(NEIGHBOR, TH), m.IO)       # someone else's I/O

    def test_wake_latency_counts_toward_cpu_not_scheduler(self):
        r = row(m.CPU, run_f=0.5, wake_f=0.3, runq_f=0.0)
        self.assertEqual(m.rule_fused(r, TH), m.CPU)

    def test_tune_rule_picks_a_threshold_from_the_grid(self):
        rows = [DIRECT_READ, SLEEP, CONTENDED, NEIGHBOR]
        th = m.tune_rule(m.rule_fused, rows, ev.macro_f1)
        self.assertTrue(0.4 <= th["dominance"] <= 0.8)


class TreeTest(unittest.TestCase):
    def data(self, n=60, seed=1):
        rng = random.Random(seed)
        rows = []
        for i in range(n):
            k = i % 3
            noise = rng.uniform(-0.05, 0.05)
            if k == 0:
                rows.append(row(m.IO, blocked_io_f=0.85 + noise, run_f=0.15 - noise, bio_kb=64))
            elif k == 1:
                rows.append(row(m.SCHED, runq_f=0.7 + noise, run_f=0.3 - noise))
            else:
                rows.append(row(m.CPU, run_f=0.95 + noise))
        return rows

    def test_tree_separates_clean_classes(self):
        rows = self.data()
        t = m.DecisionTree(m.FEATURE_SETS["fused"], max_depth=3, min_leaf=3).fit(rows)
        self.assertEqual(sum(t.predict(r) == r["label"] for r in rows), len(rows))
        self.assertIn("if ", t.describe())

    def test_tree_abstains_below_threshold_and_forced_never_does(self):
        rows = self.data() + [row(m.IND, run_f=0.95)] * 4   # ambiguous leaf mixes labels
        t = m.DecisionTree(m.FEATURE_SETS["fused"], max_depth=3, min_leaf=3).fit(rows)
        probe = row(run_f=0.95)
        self.assertEqual(t.predict(probe, abstain_below=0.99), m.IND)
        self.assertIn(t.predict(probe, abstain=False), m.CAUSES)

    def test_tree_respects_feature_set(self):
        rows = self.data()
        t = m.DecisionTree(m.FEATURE_SETS["syscall"], max_depth=3, min_leaf=3).fit(rows)
        used = set()

        def walk(n):
            if not n["leaf"]:
                used.add(n["feature"])
                walk(n["left"])
                walk(n["right"])
        walk(t.root)
        self.assertTrue(used <= set(m.FEATURE_SETS["syscall"]))


class MetricTest(unittest.TestCase):
    def test_macro_f1_and_rates(self):
        truth = [m.IO, m.IO, m.CPU, m.IND]
        pred = [m.IO, m.CPU, m.CPU, m.IND]
        self.assertAlmostEqual(ev.accuracy(truth, pred), 0.75)
        self.assertAlmostEqual(ev.abstention(truth, pred), 0.25)
        self.assertAlmostEqual(ev.hc_error(truth, pred), 0.25)
        # IO f1 = 2/3, CPU f1 = 2/3, IND f1 = 1 -> mean over present classes
        self.assertAlmostEqual(ev.macro_f1(truth, pred), (2 / 3 + 2 / 3 + 1) / 3)

    def test_bootstrap_is_deterministic_and_brackets_the_value(self):
        rows = [row(m.IO, run=f"r{i % 6}") for i in range(30)]
        preds = [m.IO if i % 5 else m.CPU for i in range(30)]
        a = ev.bootstrap(rows, preds, ev.accuracy, 200, seed=7)
        b = ev.bootstrap(rows, preds, ev.accuracy, 200, seed=7)
        self.assertEqual(a, b)
        self.assertLessEqual(a[0], ev.accuracy([r["label"] for r in rows], preds) + 1e-9)

    def test_split_is_by_whole_run(self):
        rows = [row(run=f"run{r}-{i}", rep=r) for r in (1, 2, 3, 4, 5) for i in range(3)]
        dev, test = ev.split(rows, {"dev_reps": [1, 2], "test_reps": [3, 4, 5]})
        self.assertFalse({r["run_id"] for r in dev} & {r["run_id"] for r in test})
        self.assertEqual(len(dev), 6)


if __name__ == "__main__":
    unittest.main()
