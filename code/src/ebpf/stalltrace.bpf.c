// SPDX-License-Identifier: GPL-2.0
/*
 * eBPF programs for per-operation stall fingerprinting.
 *
 * Process identity. The kernel helpers report host-wide (init namespace)
 * PIDs, but the collector and workload may live in a PID namespace (WSL,
 * containers), where fork() and gettid() return different numbers. So:
 *   - The collector puts the target's namespace PID in pending_tgids and sets
 *     registration_open.
 *   - The first time a hook runs in that process, try_register() checks its
 *     namespace PID and namespace inode, and records host tgid -> namespace
 *     tgid in traced_tgids. Filtering after that is one hash lookup.
 *   - Every emitted pid/tid is the namespace-local id, matching what the
 *     workload writes in its ground-truth records. Tasks that are not traced
 *     are reported as pid/tid 0.
 *
 * Block events are recorded for every task on the traced devices, so I/O from
 * other processes stays visible as a confounder. Each event is reserved
 * directly in the ring buffer; failed reservations are counted in `dropped`.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include "stalltrace.h"

char LICENSE[] SEC("license") = "GPL";

/* Set by the collector before load. */
const volatile bool filter_devs = false;
const volatile __u32 pidns_inum = 0;	/* inode of the collector's PID namespace */

/* Set by the collector after load; cleared here once the target registers. */
volatile __u32 registration_open = 0;

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024); /* overridden by the collector */
} events SEC(".maps");

/* host tgid -> namespace tgid */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, STALLTRACE_MAX_TGIDS);
	__type(key, __u32);
	__type(value, __u32);
} traced_tgids SEC(".maps");

/* namespace tgids waiting to be registered */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, STALLTRACE_MAX_TGIDS);
	__type(key, __u32);
	__type(value, __u8);
} pending_tgids SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, STALLTRACE_MAX_DEVS);
	__type(key, __u32);
	__type(value, __u8);
} traced_devs SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, ST_EV_MAX);
	__type(key, __u32);
	__type(value, __u64);
} produced SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, ST_EV_MAX);
	__type(key, __u32);
	__type(value, __u64);
} dropped SEC(".maps");

/* ------------------------------------------------------- process identity */

/* PID of task t in its innermost PID namespace, and that namespace's inode. */
static __always_inline int task_ns_pid(struct task_struct *t, __u32 *nr, __u32 *inum)
{
	struct pid *p = BPF_CORE_READ(t, thread_pid);
	struct upid up = {};
	unsigned int lvl;

	if (!p)
		return -1;
	lvl = BPF_CORE_READ(p, level);
	if (lvl > 32)
		return -1;
	if (bpf_probe_read_kernel(&up, sizeof(up),
				  (void *)p + bpf_core_field_offset(struct pid, numbers) +
				  (__u64)lvl * sizeof(struct upid)))
		return -1;
	*nr = (__u32)up.nr;
	if (inum)
		*inum = BPF_CORE_READ(up.ns, ns.inum);
	return 0;
}

static __always_inline __u32 ns_tid(struct task_struct *t)
{
	__u32 nr = 0;

	task_ns_pid(t, &nr, NULL);
	return nr;
}

/* If t belongs to a pending target process, start tracing it. */
static __always_inline __u32 *try_register(struct task_struct *t, __u32 host_tgid)
{
	struct task_struct *leader;
	__u32 ns_tgid = 0, inum = 0;

	if (!registration_open)
		return NULL;
	leader = BPF_CORE_READ(t, group_leader);
	if (task_ns_pid(leader, &ns_tgid, &inum) || inum != pidns_inum)
		return NULL;
	if (!bpf_map_lookup_elem(&pending_tgids, &ns_tgid))
		return NULL;
	bpf_map_update_elem(&traced_tgids, &host_tgid, &ns_tgid, BPF_ANY);
	bpf_map_delete_elem(&pending_tgids, &ns_tgid);
	registration_open = 0;
	return bpf_map_lookup_elem(&traced_tgids, &host_tgid);
}

/* Namespace tgid if task t is traced (registering it if pending), else NULL. */
static __always_inline __u32 *task_traced(struct task_struct *t)
{
	__u32 host_tgid = BPF_CORE_READ(t, tgid);
	__u32 *ns_tgid = bpf_map_lookup_elem(&traced_tgids, &host_tgid);

	if (!ns_tgid && registration_open)
		ns_tgid = try_register(t, host_tgid);
	return ns_tgid;
}

/* Same for the current task, with a fast path that avoids reading the task. */
static __always_inline bool current_ids(__u32 *ns_tgid, __u32 *ns_tid_out)
{
	__u32 host_tgid = bpf_get_current_pid_tgid() >> 32;
	struct task_struct *t = (struct task_struct *)bpf_get_current_task();
	__u32 *v = bpf_map_lookup_elem(&traced_tgids, &host_tgid);

	if (!v) {
		if (!registration_open)
			return false;
		v = try_register(t, host_tgid);
		if (!v)
			return false;
	}
	*ns_tgid = *v;
	*ns_tid_out = ns_tid(t);
	return true;
}

/* ------------------------------------------------------------ event helpers */

static __always_inline bool dev_traced(__u32 dev)
{
	if (!filter_devs)
		return true;
	return bpf_map_lookup_elem(&traced_devs, &dev) != NULL;
}

static __always_inline void bump(void *map, __u32 type)
{
	__u64 *cnt = bpf_map_lookup_elem(map, &type);

	if (cnt)
		(*cnt)++;
}

static __always_inline struct st_event *reserve(__u32 type)
{
	struct st_event *e;

	bump(&produced, type);
	e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e) {
		bump(&dropped, type);
		return NULL;
	}
	e->ts_ns = bpf_ktime_get_ns();
	e->cpu = bpf_get_smp_processor_id();
	e->type = type;
	e->flags = 0;
	e->pid = 0;
	e->tid = 0;
	e->a = 0;
	e->b = 0;
	e->dev = 0;
	e->c = 0;
	return e;
}

/* Emit an event about the current task if it is traced. */
static __always_inline struct st_event *reserve_current(__u32 type)
{
	__u32 pid, tid;
	struct st_event *e;

	if (!current_ids(&pid, &tid))
		return NULL;
	e = reserve(type);
	if (!e)
		return NULL;
	e->pid = pid;
	e->tid = tid;
	return e;
}

/*
 * Turn the tracepoint's rwbs string (e.g. "WS", "RA", "FWS") into flags.
 * The string is copied to the stack first: indexing it in place makes clang
 * derive a moved ctx pointer, and the verifier rejects loads through that
 * ("dereference of modified ctx ptr").
 */
static __always_inline __u16 rwbs_flags(const char *rwbs_field)
{
	char rwbs[8] = {};
	__u16 f = 0;

	bpf_probe_read_kernel(rwbs, sizeof(rwbs), rwbs_field);
#pragma unroll
	for (int i = 0; i < 8; i++) {
		char ch = rwbs[i];

		if (ch == 0)
			break;
		if (ch == 'R')
			f |= STF_RW_READ;
		else if (ch == 'W')
			f |= STF_RW_WRITE;
		else if (ch == 'S')
			f |= STF_RW_SYNC;
		else if (ch == 'F')
			f |= STF_RW_FLUSH;
		else if (ch == 'M')
			f |= STF_RW_META;
		else if (ch == 'D')
			f |= STF_RW_DISCARD;
	}
	return f;
}

/* Block events: issuer ids only if the issuing task is traced. */
static __always_inline void set_block_issuer(struct st_event *e)
{
	__u32 host_tgid = bpf_get_current_pid_tgid() >> 32;
	__u32 *ns_tgid = bpf_map_lookup_elem(&traced_tgids, &host_tgid);

	if (ns_tgid) {
		e->pid = *ns_tgid;
		e->tid = ns_tid((struct task_struct *)bpf_get_current_task());
	}
}

/* ---------------------------------------------------------- registration */

/* The target always execs right after the collector opens the gate. */
SEC("tp_btf/sched_process_exec")
int BPF_PROG(handle_process_exec, struct task_struct *p, pid_t old_pid,
	     struct linux_binprm *bprm)
{
	if (registration_open)
		task_traced(p);
	return 0;
}

/* ---------------------------------------------------------------- syscalls */

SEC("tracepoint/raw_syscalls/sys_enter")
int handle_sys_enter(struct trace_event_raw_sys_enter *ctx)
{
	struct st_event *e = reserve_current(ST_EV_SYS_ENTER);

	if (!e)
		return 0;
	e->a = ctx->id;
	bpf_ringbuf_submit(e, 0);
	return 0;
}

SEC("tracepoint/raw_syscalls/sys_exit")
int handle_sys_exit(struct trace_event_raw_sys_exit *ctx)
{
	struct st_event *e = reserve_current(ST_EV_SYS_EXIT);

	if (!e)
		return 0;
	e->a = ctx->id;
	e->b = (__u64)ctx->ret;
	bpf_ringbuf_submit(e, 0);
	return 0;
}

/* --------------------------------------------------------------- scheduler */

/* Kernel 5.18+ signature (prev_state passed explicitly). */
SEC("tp_btf/sched_switch")
int BPF_PROG(handle_sched_switch, bool preempt, struct task_struct *prev,
	     struct task_struct *next, unsigned int prev_state)
{
	/*
	 * Copy out of the map values right away: testing two map-value pointers
	 * together lets clang OR the pointers, which the verifier rejects.
	 */
	__u32 prev_tgid = 0, next_tgid = 0;
	bool have_prev = false, have_next = false;
	struct st_event *e;
	__u32 *v;

	v = task_traced(prev);
	if (v) {
		prev_tgid = *v;
		have_prev = true;
	}
	v = task_traced(next);
	if (v) {
		next_tgid = *v;
		have_next = true;
	}
	if (!have_prev && !have_next)
		return 0;
	e = reserve(ST_EV_SCHED_SWITCH);
	if (!e)
		return 0;
	if (have_prev) {
		e->pid = prev_tgid;
		e->tid = ns_tid(prev);
	}
	if (have_next) {
		e->c = next_tgid;
		e->b = ns_tid(next);
	}
	e->a = prev_state;
	if (preempt)
		e->flags |= STF_PREEMPT;
	/* Host pid 0 is the per-CPU idle task: a switch-in from it means the
	 * woken task waited for the CPU to leave idle, not for another task. */
	if (BPF_CORE_READ(prev, pid) == 0)
		e->flags |= STF_PREV_IDLE;
	if (bpf_core_field_exists(prev->in_iowait)) {
		e->flags |= STF_IOWAIT_KNOWN;
		if (BPF_CORE_READ_BITFIELD_PROBED(prev, in_iowait))
			e->flags |= STF_IOWAIT;
	}
	bpf_ringbuf_submit(e, 0);
	return 0;
}

static __always_inline int emit_wakeup(struct task_struct *p, __u32 type)
{
	__u32 *ns_tgid = task_traced(p);
	struct st_event *e;

	if (!ns_tgid)
		return 0;
	e = reserve(type);
	if (!e)
		return 0;
	e->pid = *ns_tgid;
	e->tid = ns_tid(p);
	bpf_ringbuf_submit(e, 0);
	return 0;
}

SEC("tp_btf/sched_wakeup")
int BPF_PROG(handle_sched_wakeup, struct task_struct *p)
{
	return emit_wakeup(p, ST_EV_SCHED_WAKEUP);
}

SEC("tp_btf/sched_wakeup_new")
int BPF_PROG(handle_sched_wakeup_new, struct task_struct *p)
{
	return emit_wakeup(p, ST_EV_SCHED_WAKEUP_NEW);
}

/* ------------------------------------------------------------- block layer */

SEC("tracepoint/block/block_bio_queue")
int handle_bio_queue(struct trace_event_raw_block_bio *ctx)
{
	struct st_event *e;

	if (!dev_traced(ctx->dev))
		return 0;
	e = reserve(ST_EV_BIO_QUEUE);
	if (!e)
		return 0;
	set_block_issuer(e);
	e->dev = ctx->dev;
	e->a = ctx->sector;
	e->b = (__u64)ctx->nr_sector * 512;
	e->c = ctx->nr_sector;
	e->flags = rwbs_flags(ctx->rwbs);
	bpf_ringbuf_submit(e, 0);
	return 0;
}

SEC("tracepoint/block/block_rq_issue")
int handle_rq_issue(struct trace_event_raw_block_rq *ctx)
{
	struct st_event *e;

	if (!dev_traced(ctx->dev))
		return 0;
	e = reserve(ST_EV_BLK_ISSUE);
	if (!e)
		return 0;
	set_block_issuer(e);
	e->dev = ctx->dev;
	e->a = ctx->sector;
	e->b = ctx->bytes;
	e->c = ctx->nr_sector;
	e->flags = rwbs_flags(ctx->rwbs);
	bpf_ringbuf_submit(e, 0);
	return 0;
}

SEC("tracepoint/block/block_rq_complete")
int handle_rq_complete(struct trace_event_raw_block_rq_completion *ctx)
{
	struct st_event *e;

	if (!dev_traced(ctx->dev))
		return 0;
	e = reserve(ST_EV_BLK_COMPLETE);
	if (!e)
		return 0;
	set_block_issuer(e);
	e->dev = ctx->dev;
	e->a = ctx->sector;
	e->b = (__u64)ctx->nr_sector * 512;
	e->c = (__u32)ctx->error;
	e->flags = rwbs_flags(ctx->rwbs);
	bpf_ringbuf_submit(e, 0);
	return 0;
}

/* --------------------------------------------------------- operation markers */

static __always_inline int emit_marker(__u32 type, __u64 op_id)
{
	struct st_event *e = reserve_current(type);

	if (!e)
		return 0;
	e->a = op_id;
	bpf_ringbuf_submit(e, 0);
	return 0;
}

/* Attached by the collector to op_begin()/op_end() in the workload binary. */
SEC("uprobe")
int BPF_KPROBE(handle_op_begin, __u64 op_id)
{
	return emit_marker(ST_EV_OP_BEGIN, op_id);
}

SEC("uprobe")
int BPF_KPROBE(handle_op_end, __u64 op_id)
{
	return emit_marker(ST_EV_OP_END, op_id);
}

/* -------------------------------------------- page faults (mmap extension) */

#if defined(__TARGET_ARCH_x86)
SEC("tracepoint/exceptions/page_fault_user")
int handle_page_fault(struct trace_event_raw_x86_exceptions *ctx)
{
	struct st_event *e = reserve_current(ST_EV_PAGE_FAULT);

	if (!e)
		return 0;
	e->a = ctx->address & ~0xfffULL;
	e->b = ctx->error_code;
	bpf_ringbuf_submit(e, 0);
	return 0;
}
#endif
