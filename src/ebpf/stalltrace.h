/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Event format shared by the eBPF programs (stalltrace.bpf.c) and the
 * user-space collector (src/collector/stalltrace.c).
 *
 * Every record is a fixed 48-byte struct st_event. Timestamps come from
 * bpf_ktime_get_ns(), which is CLOCK_MONOTONIC, the same clock the workload
 * uses for its ground-truth records.
 *
 * All pid/tid values are local to the collector's PID namespace (they match
 * the workload's getpid()/gettid()). A task that is not traced is reported
 * as pid/tid 0.
 *
 * Field meaning by type:
 *   SYS_ENTER         pid/tid = caller; a = syscall number
 *   SYS_EXIT          pid/tid = caller; a = syscall number; b = return value (signed)
 *   SCHED_SWITCH      pid/tid = task leaving the CPU (prev), 0 if untraced;
 *                     a = prev_state; b = next tid; c = next tgid (0 if untraced);
 *                     flags: STF_PREEMPT, STF_IOWAIT, STF_IOWAIT_KNOWN, STF_PREV_IDLE.
 *                     Emitted when prev or next belongs to a traced process.
 *   SCHED_WAKEUP(_NEW) pid/tid = task being woken
 *   BIO_QUEUE         pid/tid = submitting task (0 if untraced); dev; a = sector;
 *                     b = bytes; c = sectors; flags = STF_RW_*
 *   BLK_ISSUE         pid/tid = current task at issue (often not the app);
 *                     dev; a = sector; b = bytes; c = sectors; flags = STF_RW_*
 *   BLK_COMPLETE      pid/tid = current task (usually irq/idle context);
 *                     dev; a = sector; b = bytes; c = error; flags = STF_RW_*
 *   OP_BEGIN/OP_END   pid/tid = workload thread; a = operation id
 *   PAGE_FAULT        pid/tid = faulting task; a = page-aligned fault address;
 *                     b = x86 error code (x86 only)
 *
 * Block events are recorded for every task on the traced devices, so I/O from
 * other processes stays visible as a confounder. dev uses the kernel's
 * internal dev_t encoding: (major << 20) | minor.
 */
#ifndef STALLTRACE_H
#define STALLTRACE_H

#ifndef __VMLINUX_H__
#include <linux/types.h>
#endif

#define STALLTRACE_SCHEMA_VERSION 1
#define STALLTRACE_MAX_DEVS 8
#define STALLTRACE_MAX_TGIDS 64

enum st_event_type {
	ST_EV_SYS_ENTER = 1,
	ST_EV_SYS_EXIT = 2,
	ST_EV_SCHED_SWITCH = 3,
	ST_EV_SCHED_WAKEUP = 4,
	ST_EV_SCHED_WAKEUP_NEW = 5,
	ST_EV_BIO_QUEUE = 6,
	ST_EV_BLK_ISSUE = 7,
	ST_EV_BLK_COMPLETE = 8,
	ST_EV_OP_BEGIN = 9,
	ST_EV_OP_END = 10,
	ST_EV_PAGE_FAULT = 11,
	ST_EV_MAX = 12,
};

#define STF_PREEMPT      (1u << 0) /* switch: prev was preempted and stays runnable */
#define STF_IOWAIT       (1u << 1) /* switch: prev->in_iowait was set */
#define STF_IOWAIT_KNOWN (1u << 2) /* switch: kernel exposes in_iowait */
#define STF_RW_READ      (1u << 3)
#define STF_RW_WRITE     (1u << 4)
#define STF_RW_SYNC      (1u << 5)
#define STF_RW_FLUSH     (1u << 6)
#define STF_RW_META      (1u << 7)
#define STF_RW_DISCARD   (1u << 8)
#define STF_PREV_IDLE    (1u << 9) /* switch: prev is the idle task (CPU was idle) */

struct st_event {
	__u64 ts_ns;
	__u32 pid;
	__u32 tid;
	__u32 cpu;
	__u16 type;
	__u16 flags;
	__u64 a;
	__u64 b;
	__u32 dev;
	__u32 c;
};

#endif /* STALLTRACE_H */
