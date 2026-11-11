"""Classifiers for per-operation stall fingerprinting.

Two families, each with an abstaining and a forced variant:

  rules   transparent threshold rules, one per evidence source
  tree    a depth-limited CART decision tree (standard library only)

Evidence sources (the feature sets each classifier may use):

  syscall    what a syscall-latency tool sees: time inside syscalls, count
  block      what a block-layer tool sees: this thread's queued bio bytes and
             block completions on the device during the operation
  scheduler  what a scheduler tracer sees: run / run-queue / wake-latency /
             blocked-on-I/O / blocked-other fractions, context switches
  fused      all of the above

All sources also get the operation's duration, which any of those tools would
know once operation boundaries are marked.

"Abstain" means predicting INDETERMINATE when evidence is weak or conflicting.
The forced variants always commit to one of the three causes; comparing them
tests H2 (abstention reduces high-confidence errors).
"""
IO, SCHED, CPU, IND = ("IO_WAIT_DOMINATED", "SCHEDULER_WAIT_DOMINATED",
                       "SYSCALL_CPU_DOMINATED", "INDETERMINATE")
LABELS = (IO, SCHED, CPU, IND)
CAUSES = (IO, SCHED, CPU)

FEATURE_SETS = {
    "syscall": ["wall_us", "syscall_f", "n_syscalls"],
    "block": ["wall_us", "bio_kb", "blk_any"],
    "scheduler": ["wall_us", "run_f", "runq_f", "wake_f", "blocked_io_f", "blocked_other_f",
                  "n_switch_out"],
}
FEATURE_SETS["fused"] = (FEATURE_SETS["scheduler"]
                         + ["syscall_f", "n_syscalls", "bio_kb", "blk_any", "n_faults"])

DEFAULT_THRESHOLDS = {"dominance": 0.6}


# ------------------------------------------------------------------- rules

def _block_evidence(r):
    return r["bio_kb"] > 0 or r["blk_any"] > 0


def rule_fused(r, th, abstain=True):
    t = th["dominance"]
    cpu = r["run_f"] + r["wake_f"]
    if not abstain:
        return max(((r["blocked_io_f"], IO), (r["runq_f"], SCHED), (cpu, CPU)))[1]
    if r["iowait_unknown"] and r["blocked_io_f"] + r["blocked_other_f"] > t:
        return IND                          # cannot tell I/O from other blocking
    if r["runq_f"] > t:
        return SCHED
    if r["blocked_io_f"] > t:
        return IO if _block_evidence(r) else IND   # iowait with no block activity: conflict
    if cpu > t:
        return CPU
    return IND


def rule_scheduler(r, th, abstain=True):
    t = th["dominance"]
    cpu = r["run_f"] + r["wake_f"]
    scores = ((r["blocked_io_f"], IO), (r["runq_f"], SCHED), (cpu, CPU))
    if not abstain:
        return max(scores)[1]
    for score, label in scores:
        if score > t:
            return label
    return IND


def rule_syscall(r, th, abstain=True):
    """A syscall-latency tool's natural reading: long syscalls mean waiting on I/O."""
    t = th["dominance"]
    inside, outside = r["syscall_f"], 1.0 - r["syscall_f"]
    if not abstain:
        return IO if inside >= outside else CPU
    if inside > t:
        return IO
    if outside > t:
        return CPU
    return IND


def rule_block(r, th, abstain=True):
    """A block-layer tool's natural reading: block activity during the op means I/O."""
    del th, abstain  # block evidence alone gives no basis for abstaining
    return IO if _block_evidence(r) else CPU


RULES = {"fused": rule_fused, "scheduler": rule_scheduler,
         "syscall": rule_syscall, "block": rule_block}


def tune_rule(rule, rows, metric, grid=None):
    """Pick the dominance threshold that maximises metric(truth, pred) on rows."""
    grid = grid or [x / 100 for x in range(40, 85, 5)]
    truth = [r["label"] for r in rows]
    best = None
    for t in grid:
        th = {"dominance": t}
        score = metric(truth, [rule(r, th) for r in rows])
        if best is None or score > best[0] + 1e-12:
            best = (score, th)
    return best[1]


# -------------------------------------------------------------------- tree

class DecisionTree:
    """CART with Gini impurity on numeric features; leaves hold class probabilities."""

    def __init__(self, features, max_depth=4, min_leaf=5):
        self.features = list(features)
        self.max_depth = max_depth
        self.min_leaf = min_leaf
        self.root = None

    def fit(self, rows):
        X = [[float(r[f]) for f in self.features] for r in rows]
        y = [r["label"] for r in rows]
        self.root = self._build(X, y, list(range(len(y))), 0)
        return self

    @staticmethod
    def _gini(counts, n):
        return 1.0 - sum((c / n) ** 2 for c in counts.values()) if n else 0.0

    def _leaf(self, y, idx):
        counts = {}
        for i in idx:
            counts[y[i]] = counts.get(y[i], 0) + 1
        return {"leaf": True, "n": len(idx),
                "proba": {k: v / len(idx) for k, v in counts.items()}}

    def _build(self, X, y, idx, depth):
        node = self._leaf(y, idx)
        if depth >= self.max_depth or len(idx) < 2 * self.min_leaf or len(node["proba"]) == 1:
            return node
        parent = self._gini({k: v * len(idx) for k, v in node["proba"].items()}, len(idx))
        best = None
        for j in range(len(self.features)):
            order = sorted(idx, key=lambda i: X[i][j])
            left, right = {}, {}
            for i in order:
                right[y[i]] = right.get(y[i], 0) + 1
            n = len(order)
            for k in range(1, n):
                lab = y[order[k - 1]]
                left[lab] = left.get(lab, 0) + 1
                right[lab] -= 1
                if k < self.min_leaf or n - k < self.min_leaf:
                    continue
                a, b = X[order[k - 1]][j], X[order[k]][j]
                if a == b:
                    continue
                score = (k * self._gini(left, k) + (n - k) * self._gini(right, n - k)) / n
                if best is None or score < best[0] - 1e-12:
                    best = (score, j, (a + b) / 2)
        if best is None or best[0] >= parent - 1e-12:
            return node
        _, j, thr = best
        li = [i for i in idx if X[i][j] <= thr]
        ri = [i for i in idx if X[i][j] > thr]
        return {"leaf": False, "feature": self.features[j], "threshold": thr,
                "left": self._build(X, y, li, depth + 1),
                "right": self._build(X, y, ri, depth + 1), "n": len(idx)}

    def proba(self, r):
        node = self.root
        while not node["leaf"]:
            node = node["left"] if float(r[node["feature"]]) <= node["threshold"] else node["right"]
        return node["proba"]

    def predict(self, r, abstain_below=0.0, abstain=True):
        p = self.proba(r)
        if not abstain:
            causes = {k: v for k, v in p.items() if k in CAUSES}
            return max(causes, key=causes.get) if causes else CPU
        label = max(p, key=p.get)
        return label if p[label] >= abstain_below else IND

    def describe(self, node=None, indent=""):
        """Readable rules, for the report and for checking the tree makes sense."""
        node = node or self.root
        if node["leaf"]:
            top = sorted(node["proba"].items(), key=lambda kv: -kv[1])
            return indent + f"-> {top[0][0]} ({top[0][1]:.2f}, n={node['n']})\n"
        return (indent + f"if {node['feature']} <= {node['threshold']:.4g}:\n"
                + self.describe(node["left"], indent + "    ")
                + indent + "else:\n"
                + self.describe(node["right"], indent + "    "))
