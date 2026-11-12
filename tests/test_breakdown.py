"""Unit tests for src/correlation/breakdown.py using hand-built event streams.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "correlation"))
import breakdown as bd  # noqa: E402

TID, OTHER = 100, 200


def sw(ts, prev, nxt, state=1, preempt=0, iowait=0, known=1, prev_idle=0):
    return {"ts": ts, "type": "sched_switch", "tid": prev, "next_tid": nxt,
            "prev_state": state, "preempt": preempt, "iowait": iowait, "iowait_known": known,
            "prev_idle": prev_idle}


def ev(ts, kind, tid=TID, **kw):
    return {"ts": ts, "type": kind, "tid": tid, **kw}


def gt(op_id=1, t_begin=1_010, t_end=9_990):
    return {(TID, op_id): {"workload": "w", "expected": "E", "ref_label": "R",
                           "ref_wall_ns": 10_000, "ref_cpu_ns": 0, "ref_runq_ns": 0,
                           "ref_blocked_ns": 0, "t_begin_ns": t_begin, "t_end_ns": t_end}}


class BreakdownTest(unittest.TestCase):
    def one(self, events, gt_ops=None):
        rows = bd.breakdown(events, gt_ops or gt())
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_io_block_then_runq_then_run(self):
        # run 1000-3000, blocked on I/O 3000-7000, woken 7000, on CPU at 8000, end 11000
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(3000, TID, OTHER, state=2, iowait=1),
            ev(7000, "sched_wakeup"),
            sw(8000, OTHER, TID),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["run"], r["blocked_io"], r["runq"], r["blocked_other"]),
                         (5000, 4000, 1000, 0))
        self.assertEqual(r["wall"], 10000)

    def test_wakeup_onto_idle_cpu_is_wake_latency(self):
        # blocked 2000-7000, woken 7000, idle CPU switches it in at 7040
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(2000, TID, 0, state=2, iowait=1),
            ev(7000, "sched_wakeup"),
            sw(7040, 0, TID, state=0, prev_idle=1),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["wake_latency"], r["runq"], r["blocked_io"], r["run"]),
                         (40, 0, 5000, 4960))

    def test_wakeup_onto_busy_cpu_is_runq(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(2000, TID, OTHER, state=2, iowait=1),
            ev(7000, "sched_wakeup"),
            ev(7010, "sched_wakeup"),  # a duplicate wakeup must not split the wait
            sw(7500, OTHER, TID),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["wake_latency"], r["runq"]), (0, 500))

    def test_sleep_is_blocked_other(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(1500, TID, OTHER, state=1, iowait=0),
            ev(9500, "sched_wakeup"),
            sw(9500, OTHER, TID),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual(r["blocked_other"], 8000)
        self.assertEqual(r["blocked_io"], 0)

    def test_preemption_is_runq_even_with_nonzero_state(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(2000, TID, OTHER, state=1, preempt=1),
            sw(9000, OTHER, TID),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["run"], r["runq"]), (3000, 7000))

    def test_unknown_iowait_is_flagged(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(2000, TID, OTHER, state=2, known=0),
            ev(3000, "sched_wakeup"),
            sw(3000, OTHER, TID),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertTrue(r["iowait_unknown"])
        self.assertEqual(r["blocked_other"], 1000)

    def test_syscalls_bio_and_completions(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            ev(1100, "sys_enter", nr=17),
            ev(1200, "bio_queue", bytes=65536),
            ev(2100, "sys_exit", nr=17),
            {"ts": 1900, "type": "blk_complete", "tid": 0},
            {"ts": 20000, "type": "blk_complete", "tid": 0},
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["n_syscalls"], r["syscall_ns"]), (1, 1000))
        self.assertEqual(r["bio_bytes"], 65536)
        self.assertEqual(r["blk_completions_any"], 1)

    def test_marker_skew_against_ground_truth(self):
        r = self.one([ev(1000, "op_begin", op_id=1), ev(11000, "op_end", op_id=1)],
                     gt(t_begin=1_010, t_end=10_990))
        self.assertEqual((r["marker_skew_begin_ns"], r["marker_skew_end_ns"]), (10, 10))

    def test_reference_window_includes_preemption_after_op_end(self):
        # op 1000-11000 runs throughout; preempted at 11500, back at 14500;
        # reference readings at 900 and 15000.
        g = gt()
        g[(TID, 1)].update(ref_t0_ns=900, ref_t1_ns=15000)
        r = self.one([
            ev(800, "sys_exit", nr=0),
            ev(1000, "op_begin", op_id=1),
            ev(11000, "op_end", op_id=1),
            sw(11500, TID, OTHER, state=0, preempt=1),
            sw(14500, OTHER, TID),
            ev(15100, "sys_enter", nr=0),
        ], g)
        self.assertEqual((r["run"], r["runq"]), (10000, 0))          # operation window
        self.assertEqual((r["val_run"], r["val_runq"], r["val_uncovered"]), (11100, 3000, 0))

    def test_fixed_windows_without_markers(self):
        # Thread runs 1000-3000, blocked on I/O 3000-7000, runs to 11000.
        # Ground truth: one op covering 1000-11000. Windows of 2500 ns.
        events = [
            ev(1000, "sys_exit", nr=0),
            sw(3000, TID, OTHER, state=2, iowait=1),
            ev(7000, "sched_wakeup"),
            sw(7000, OTHER, TID),
            ev(11000, "sys_enter", nr=0),
        ]
        g = gt(t_begin=1000, t_end=11000)
        rows = bd.window_breakdown(events, g, 2500)
        self.assertEqual(len(rows), 4)                    # 1000-3500 ... 8500-11000
        self.assertEqual((rows[0]["run"], rows[0]["blocked_io"]), (2000, 500))
        self.assertEqual(rows[1]["blocked_io"], 2500)
        self.assertTrue(all(r["gt_op_id"] == 1 for r in rows))

    def test_other_threads_are_ignored(self):
        r = self.one([
            ev(1000, "op_begin", op_id=1),
            sw(2000, OTHER, 300),
            ev(2500, "sys_enter", tid=OTHER, nr=0),
            ev(11000, "op_end", op_id=1),
        ])
        self.assertEqual((r["run"], r["n_syscalls"]), (10000, 0))


if __name__ == "__main__":
    unittest.main()
