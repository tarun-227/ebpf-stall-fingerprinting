// SPDX-License-Identifier: GPL-2.0
/*
 * stalltrace: load the eBPF programs, start a workload under tracing, and
 * record raw events.
 *
 *   sudo stalltrace -o OUTDIR [-d MAJ:MIN]... [options] -- COMMAND [ARGS...]
 *   sudo stalltrace -o OUTDIR -p PID [options]
 *
 * In command mode the workload is forked but held at a gate until every
 * program is attached and its PID is in the traced set, so no early events
 * are missed. Output (never overwritten):
 *   OUTDIR/events.bin    raw 48-byte event records (default), or
 *   OUTDIR/events.jsonl  one JSON object per kernel event (-J)
 *   OUTDIR/stats.json    attach status, per-type produced/dropped/received
 *                        counts, collector CPU time, exit status
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "stalltrace.h"
#include "stalltrace.skel.h"

#define MAX_LINKS 16

struct options {
	const char *out_dir;
	const char *uprobe_bin;
	__u32 devs[STALLTRACE_MAX_DEVS];
	char dev_str[STALLTRACE_MAX_DEVS][24];
	int ndevs;
	unsigned long rb_bytes;
	bool fam_syscalls, fam_sched, fam_block, fam_faults, fam_markers;
	int linger_ms;
	pid_t attach_pid;
	bool verbose;
	bool json;
	char **cmd;
};

struct prog_spec {
	const char *name;
	const char *family;
	bool required;
};

/* Programs attached through libbpf's automatic attach. */
static const struct prog_spec specs[] = {
	{ "handle_process_exec", "core", true },
	{ "handle_sys_enter", "syscalls", true },
	{ "handle_sys_exit", "syscalls", true },
	{ "handle_sched_switch", "sched", true },
	{ "handle_sched_wakeup", "sched", true },
	{ "handle_sched_wakeup_new", "sched", true },
	{ "handle_bio_queue", "block", false },
	{ "handle_rq_issue", "block", true },
	{ "handle_rq_complete", "block", true },
	{ "handle_page_fault", "faults", false },
};
#define NSPECS (sizeof(specs) / sizeof(specs[0]))

static const char *const type_names[ST_EV_MAX] = {
	[ST_EV_SYS_ENTER] = "sys_enter",
	[ST_EV_SYS_EXIT] = "sys_exit",
	[ST_EV_SCHED_SWITCH] = "sched_switch",
	[ST_EV_SCHED_WAKEUP] = "sched_wakeup",
	[ST_EV_SCHED_WAKEUP_NEW] = "sched_wakeup_new",
	[ST_EV_BIO_QUEUE] = "bio_queue",
	[ST_EV_BLK_ISSUE] = "blk_issue",
	[ST_EV_BLK_COMPLETE] = "blk_complete",
	[ST_EV_OP_BEGIN] = "op_begin",
	[ST_EV_OP_END] = "op_end",
	[ST_EV_PAGE_FAULT] = "page_fault",
};

struct status {
	const char *name;
	char state[96];
};

struct out_ctx {
	FILE *f;
	bool binary;
	unsigned long long received[ST_EV_MAX];
	unsigned long long malformed;
};

/*
 * Binary event file (default): a 16-byte header, then raw struct st_event
 * records exactly as the kernel produced them. Writing records unformatted
 * keeps the collector fast enough to drain the ring buffer; formatting JSON
 * per event used about half a CPU and caused drops on busy workloads.
 * src/correlation/breakdown.py decodes it.
 */
struct bin_header {
	char magic[8];		/* "STEVENT\0" */
	__u32 schema;		/* STALLTRACE_SCHEMA_VERSION */
	__u32 record_size;	/* sizeof(struct st_event) */
};

static volatile sig_atomic_t stop_requested;
static struct options opt = {
	.rb_bytes = 256 * 1024,
	.fam_syscalls = true,
	.fam_sched = true,
	.fam_block = true,
	.fam_faults = true,
	.fam_markers = true,
	.linger_ms = 200,
	.attach_pid = 0,
};

static void on_signal(int sig)
{
	(void)sig;
	stop_requested = 1;
}

static int libbpf_print(enum libbpf_print_level level, const char *fmt, va_list args)
{
	if (level == LIBBPF_DEBUG && !opt.verbose)
		return 0;
	return vfprintf(stderr, fmt, args);
}

static unsigned long long mono_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (unsigned long long)ts.tv_sec * 1000000000ULL + (unsigned long long)ts.tv_nsec;
}

static void usage(FILE *f)
{
	fprintf(f,
"Usage:\n"
"  stalltrace -o OUTDIR [options] -- COMMAND [ARGS...]\n"
"  stalltrace -o OUTDIR -p PID [options]\n"
"\n"
"Options:\n"
"  -o DIR        output directory (created; existing results are never overwritten)\n"
"  -d MAJ:MIN    record block events only for this device (repeatable, max %d);\n"
"                default: all devices\n"
"  -b BYTES      ring buffer size, power of two >= 4096 (default 262144)\n"
"  -u PATH       binary to hook op_begin/op_end in (default: COMMAND)\n"
"  -p PID        trace an already running process instead of a command\n"
"  -l MS         keep recording this long after the workload exits (default 200)\n"
"  -S -C -K -F -M  disable syscalls / scheduler / block / page-fault / marker events\n"
"  -J            write events.jsonl (one JSON object per event) instead of the\n"
"                default compact events.bin; slower, for inspection\n"
"  -v            verbose libbpf output\n",
		STALLTRACE_MAX_DEVS);
}

static bool parse_dev(const char *s, __u32 *dev)
{
	unsigned int maj, min;
	char extra;

	if (sscanf(s, "%u:%u%c", &maj, &min, &extra) != 2 || maj > 0xfff || min > 0xfffff)
		return false;
	*dev = (maj << 20) | min;	/* kernel-internal dev_t used by tracepoints */
	return true;
}

static void parse_args(int argc, char **argv)
{
	int c;

	while ((c = getopt(argc, argv, "+o:d:b:u:p:l:SCKFMJvh")) != -1) {
		switch (c) {
		case 'o':
			opt.out_dir = optarg;
			break;
		case 'd':
			if (opt.ndevs == STALLTRACE_MAX_DEVS) {
				fprintf(stderr, "too many -d options\n");
				exit(2);
			}
			if (!parse_dev(optarg, &opt.devs[opt.ndevs])) {
				fprintf(stderr, "bad device '%s' (expected MAJ:MIN)\n", optarg);
				exit(2);
			}
			snprintf(opt.dev_str[opt.ndevs], sizeof(opt.dev_str[0]), "%s", optarg);
			opt.ndevs++;
			break;
		case 'b':
			opt.rb_bytes = strtoul(optarg, NULL, 0);
			break;
		case 'u':
			opt.uprobe_bin = optarg;
			break;
		case 'p':
			opt.attach_pid = (pid_t)strtol(optarg, NULL, 10);
			break;
		case 'l':
			opt.linger_ms = atoi(optarg);
			break;
		case 'S': opt.fam_syscalls = false; break;
		case 'C': opt.fam_sched = false; break;
		case 'K': opt.fam_block = false; break;
		case 'F': opt.fam_faults = false; break;
		case 'M': opt.fam_markers = false; break;
		case 'J': opt.json = true; break;
		case 'v': opt.verbose = true; break;
		case 'h': usage(stdout); exit(0);
		default: usage(stderr); exit(2);
		}
	}
	if (optind < argc)
		opt.cmd = &argv[optind];

	if (!opt.out_dir) {
		fprintf(stderr, "-o OUTDIR is required\n");
		exit(2);
	}
	if (!opt.cmd == !opt.attach_pid) {
		fprintf(stderr, "give either a command after '--' or -p PID, not both\n");
		exit(2);
	}
	if (opt.attach_pid < 0) {
		fprintf(stderr, "bad -p value\n");
		exit(2);
	}
	if (opt.rb_bytes < 4096 || (opt.rb_bytes & (opt.rb_bytes - 1))) {
		fprintf(stderr, "-b must be a power of two >= 4096\n");
		exit(2);
	}
	if (opt.linger_ms < 0)
		opt.linger_ms = 0;
}

static bool family_enabled(const char *fam)
{
	if (!strcmp(fam, "syscalls")) return opt.fam_syscalls;
	if (!strcmp(fam, "sched")) return opt.fam_sched;
	if (!strcmp(fam, "block")) return opt.fam_block;
	if (!strcmp(fam, "faults")) return opt.fam_faults;
	return true;
}

/* Resolve COMMAND the way execvp would, to find the binary for uprobes. */
static bool resolve_binary(const char *name, char *out, size_t outlen)
{
	const char *path;
	char buf[PATH_MAX];

	if (strchr(name, '/'))
		return realpath(name, out) != NULL;
	path = getenv("PATH");
	if (!path)
		path = "/usr/local/bin:/usr/bin:/bin";
	while (*path) {
		size_t n = strcspn(path, ":");

		if (n > 0 && n + strlen(name) + 2 < sizeof(buf)) {
			snprintf(buf, sizeof(buf), "%.*s/%s", (int)n, path, name);
			if (access(buf, X_OK) == 0 && realpath(buf, out) && strlen(out) < outlen)
				return true;
		}
		path += n;
		if (*path == ':')
			path++;
	}
	return false;
}

static const char *rw_str(__u16 flags, char *buf)
{
	char *p = buf;

	if (flags & STF_RW_FLUSH) *p++ = 'F';
	if (flags & STF_RW_READ) *p++ = 'R';
	if (flags & STF_RW_WRITE) *p++ = 'W';
	if (flags & STF_RW_DISCARD) *p++ = 'D';
	if (flags & STF_RW_SYNC) *p++ = 'S';
	if (flags & STF_RW_META) *p++ = 'M';
	*p = '\0';
	return buf;
}

static int handle_event(void *ctx, void *data, size_t len)
{
	struct out_ctx *o = ctx;
	const struct st_event *e = data;
	unsigned long long a, b;
	char rw[8];

	if (len < sizeof(*e) || e->type == 0 || e->type >= ST_EV_MAX) {
		o->malformed++;
		return 0;
	}
	o->received[e->type]++;
	if (o->binary) {
		fwrite(e, sizeof(*e), 1, o->f);
		return 0;
	}
	a = e->a;
	b = e->b;

	fprintf(o->f, "{\"ts\":%llu,\"type\":\"%s\",\"cpu\":%u,\"pid\":%u,\"tid\":%u",
		(unsigned long long)e->ts_ns, type_names[e->type], e->cpu, e->pid, e->tid);
	switch (e->type) {
	case ST_EV_SYS_ENTER:
		fprintf(o->f, ",\"nr\":%llu", a);
		break;
	case ST_EV_SYS_EXIT:
		fprintf(o->f, ",\"nr\":%llu,\"ret\":%lld", a, (long long)b);
		break;
	case ST_EV_SCHED_SWITCH:
		fprintf(o->f, ",\"prev_state\":%llu,\"next_tid\":%llu,\"next_pid\":%u,"
			"\"preempt\":%d,\"iowait\":%d,\"iowait_known\":%d,\"prev_idle\":%d",
			a, b, e->c, !!(e->flags & STF_PREEMPT), !!(e->flags & STF_IOWAIT),
			!!(e->flags & STF_IOWAIT_KNOWN), !!(e->flags & STF_PREV_IDLE));
		break;
	case ST_EV_BIO_QUEUE:
	case ST_EV_BLK_ISSUE:
		fprintf(o->f, ",\"dev\":\"%u:%u\",\"sector\":%llu,\"bytes\":%llu,\"rw\":\"%s\"",
			e->dev >> 20, e->dev & 0xfffff, a, b, rw_str(e->flags, rw));
		break;
	case ST_EV_BLK_COMPLETE:
		fprintf(o->f, ",\"dev\":\"%u:%u\",\"sector\":%llu,\"bytes\":%llu,\"rw\":\"%s\","
			"\"error\":%d",
			e->dev >> 20, e->dev & 0xfffff, a, b, rw_str(e->flags, rw), (int)e->c);
		break;
	case ST_EV_OP_BEGIN:
	case ST_EV_OP_END:
		fprintf(o->f, ",\"op_id\":%llu", a);
		break;
	case ST_EV_PAGE_FAULT:
		fprintf(o->f, ",\"addr\":%llu,\"error_code\":%llu", a, b);
		break;
	}
	fputs("}\n", o->f);
	return 0;
}

/* Sum a per-CPU counter array map into totals[ST_EV_MAX]. */
static void read_counters(int map_fd, unsigned long long *totals)
{
	int ncpu = libbpf_num_possible_cpus();
	__u64 *vals;

	memset(totals, 0, sizeof(*totals) * ST_EV_MAX);
	if (ncpu <= 0)
		return;
	vals = calloc((size_t)ncpu, sizeof(*vals));
	if (!vals)
		return;
	for (__u32 k = 0; k < ST_EV_MAX; k++) {
		if (bpf_map_lookup_elem(map_fd, &k, vals) != 0)
			continue;
		for (int c = 0; c < ncpu; c++)
			totals[k] += vals[c];
	}
	free(vals);
}

static void json_str(FILE *f, const char *s)
{
	fputc('"', f);
	for (; *s; s++) {
		if (*s == '"' || *s == '\\')
			fprintf(f, "\\%c", *s);
		else if ((unsigned char)*s < 0x20)
			fprintf(f, "\\u%04x", *s);
		else
			fputc(*s, f);
	}
	fputc('"', f);
}

/* Count registered processes; print "host_tgid:ns_tgid" pairs if f is given. */
static int registered_targets(struct stalltrace_bpf *skel, FILE *f)
{
	int fd = bpf_map__fd(skel->maps.traced_tgids), n = 0;
	__u32 key, next, val;
	__u32 *prev = NULL;

	while (bpf_map_get_next_key(fd, prev, &next) == 0) {
		if (bpf_map_lookup_elem(fd, &next, &val) == 0) {
			if (f)
				fprintf(f, "%s{\"host_tgid\": %u, \"ns_tgid\": %u}", n ? ", " : "",
					next, val);
			n++;
		}
		key = next;
		prev = &key;
	}
	return n;
}

static void write_stats(const char *path, struct stalltrace_bpf *skel, struct out_ctx *o,
			struct status *st, int nst, const char *bin,
			unsigned long long t_start, unsigned long long t_end,
			pid_t target, int exit_status, bool exited, bool interrupted)
{
	unsigned long long produced[ST_EV_MAX], dropped[ST_EV_MAX];
	unsigned long long tot_p = 0, tot_d = 0, tot_r = 0;
	struct rusage ru;
	struct utsname un;
	FILE *f = fopen(path, "w");

	if (!f) {
		fprintf(stderr, "cannot write %s: %s\n", path, strerror(errno));
		return;
	}
	read_counters(bpf_map__fd(skel->maps.produced), produced);
	read_counters(bpf_map__fd(skel->maps.dropped), dropped);
	getrusage(RUSAGE_SELF, &ru);
	uname(&un);

	fprintf(f, "{\n  \"schema\": %d,\n  \"tool\": \"stalltrace\",\n", STALLTRACE_SCHEMA_VERSION);
	fprintf(f, "  \"kernel\": ");
	json_str(f, un.release);
	fprintf(f, ",\n  \"machine\": ");
	json_str(f, un.machine);
	fprintf(f, ",\n  \"ringbuf_bytes\": %lu,\n", opt.rb_bytes);
	fprintf(f, "  \"families\": {\"syscalls\": %s, \"sched\": %s, \"block\": %s, "
		"\"faults\": %s, \"markers\": %s},\n",
		opt.fam_syscalls ? "true" : "false", opt.fam_sched ? "true" : "false",
		opt.fam_block ? "true" : "false", opt.fam_faults ? "true" : "false",
		opt.fam_markers ? "true" : "false");
	fprintf(f, "  \"devices\": [");
	for (int i = 0; i < opt.ndevs; i++)
		fprintf(f, "%s\"%s\"", i ? ", " : "", opt.dev_str[i]);
	fprintf(f, "],\n  \"uprobe_binary\": ");
	json_str(f, bin ? bin : "");
	fprintf(f, ",\n  \"programs\": {");
	for (int i = 0; i < nst; i++) {
		fprintf(f, "%s\n    \"%s\": ", i ? "," : "", st[i].name);
		json_str(f, st[i].state);
	}
	fprintf(f, "\n  },\n");
	fprintf(f, "  \"target_pid\": %d,\n  \"target_exited\": %s,\n  \"exit_status\": %d,\n"
		"  \"interrupted\": %s,\n",
		(int)target, exited ? "true" : "false", exit_status, interrupted ? "true" : "false");
	fprintf(f, "  \"pid_namespace_inum\": %u,\n  \"registered\": [",
		skel->rodata->pidns_inum);
	registered_targets(skel, f);
	fprintf(f, "],\n");
	fprintf(f, "  \"t_start_ns\": %llu,\n  \"t_end_ns\": %llu,\n", t_start, t_end);
	fprintf(f, "  \"collector_cpu_user_s\": %.6f,\n  \"collector_cpu_sys_s\": %.6f,\n"
		"  \"collector_max_rss_kb\": %ld,\n",
		ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6,
		ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6, ru.ru_maxrss);
	fprintf(f, "  \"events\": {");
	for (int t = 1; t < ST_EV_MAX; t++) {
		fprintf(f, "%s\n    \"%s\": {\"produced\": %llu, \"dropped\": %llu, \"received\": %llu}",
			t > 1 ? "," : "", type_names[t], produced[t], dropped[t], o->received[t]);
		tot_p += produced[t];
		tot_d += dropped[t];
		tot_r += o->received[t];
	}
	fprintf(f, "\n  },\n  \"totals\": {\"produced\": %llu, \"dropped\": %llu, "
		"\"received\": %llu, \"drop_rate\": %.6f},\n",
		tot_p, tot_d, tot_r, tot_p ? (double)tot_d / (double)tot_p : 0.0);
	fprintf(f, "  \"malformed\": %llu\n}\n", o->malformed);
	fclose(f);

	fprintf(stderr, "stalltrace: %llu events received, %llu dropped (%.4f%%)\n",
		tot_r, tot_d, tot_p ? 100.0 * (double)tot_d / (double)tot_p : 0.0);
}

int main(int argc, char **argv)
{
	struct stalltrace_bpf *skel = NULL;
	struct ring_buffer *rb = NULL;
	struct bpf_link *links[MAX_LINKS] = { 0 };
	struct status st[NSPECS + 2];
	struct out_ctx out = { 0 };
	char events_path[PATH_MAX], stats_path[PATH_MAX], bin[PATH_MAX];
	const char *bin_used = NULL;
	unsigned long long t_start = 0, t_end, done_at = 0;
	int nlinks = 0, nst = 0, gate[2] = { -1, -1 };
	int exit_status = -1, err, ret = 1;
	bool exited = false;
	pid_t target = 0;
	__u8 one = 1;

	parse_args(argc, argv);
	libbpf_set_print(libbpf_print);

	if (mkdir(opt.out_dir, 0755) != 0 && errno != EEXIST) {
		fprintf(stderr, "mkdir %s: %s\n", opt.out_dir, strerror(errno));
		return 1;
	}
	snprintf(events_path, sizeof(events_path), "%s/events.%s", opt.out_dir,
		 opt.json ? "jsonl" : "bin");
	snprintf(stats_path, sizeof(stats_path), "%s/stats.json", opt.out_dir);
	{
		char other[PATH_MAX];

		snprintf(other, sizeof(other), "%s/events.%s", opt.out_dir, opt.json ? "bin" : "jsonl");
		if (access(other, F_OK) == 0) {
			fprintf(stderr, "%s already holds results; raw data is never overwritten\n",
				opt.out_dir);
			return 1;
		}
	}
	if (access(events_path, F_OK) == 0 || access(stats_path, F_OK) == 0) {
		fprintf(stderr, "%s already holds results; raw data is never overwritten\n",
			opt.out_dir);
		return 1;
	}

	/* ---- open, configure, load */
	skel = stalltrace_bpf__open();
	if (!skel) {
		fprintf(stderr, "failed to open BPF object: %s\n", strerror(errno));
		return 1;
	}
	skel->rodata->filter_devs = opt.ndevs > 0;
	{
		struct stat ns;

		/* PIDs we see (fork, -p) are local to this namespace; the BPF side
		 * matches them against tasks in the namespace with this inode. */
		if (stat("/proc/self/ns/pid", &ns) != 0) {
			fprintf(stderr, "stat /proc/self/ns/pid: %s\n", strerror(errno));
			goto cleanup;
		}
		skel->rodata->pidns_inum = (__u32)ns.st_ino;
	}
	err = bpf_map__set_max_entries(skel->maps.events, (__u32)opt.rb_bytes);
	if (err) {
		fprintf(stderr, "bad ring buffer size: %s\n", strerror(-err));
		goto cleanup;
	}
	for (size_t i = 0; i < NSPECS; i++) {
		struct bpf_program *p = bpf_object__find_program_by_name(skel->obj, specs[i].name);

		if (p && !family_enabled(specs[i].family))
			bpf_program__set_autoload(p, false);
	}
	if (!opt.fam_markers) {
		bpf_program__set_autoload(skel->progs.handle_op_begin, false);
		bpf_program__set_autoload(skel->progs.handle_op_end, false);
	}
	err = stalltrace_bpf__load(skel);
	if (err) {
		fprintf(stderr, "failed to load BPF programs: %s (run with -v for the verifier log)\n",
			strerror(-err));
		goto cleanup;
	}

	for (int i = 0; i < opt.ndevs; i++)
		bpf_map_update_elem(bpf_map__fd(skel->maps.traced_devs), &opt.devs[i], &one, BPF_ANY);

	/* ---- attach tracepoints */
	for (size_t i = 0; i < NSPECS; i++) {
		struct bpf_program *p = bpf_object__find_program_by_name(skel->obj, specs[i].name);
		struct bpf_link *l;

		st[nst].name = specs[i].name;
		if (!p) {
			snprintf(st[nst++].state, sizeof(st[0].state), "not built for this architecture");
			continue;
		}
		if (!family_enabled(specs[i].family)) {
			snprintf(st[nst++].state, sizeof(st[0].state), "disabled");
			continue;
		}
		l = bpf_program__attach(p);
		if (!l) {
			err = -errno;
			snprintf(st[nst++].state, sizeof(st[0].state), "unavailable: %s", strerror(-err));
			if (specs[i].required) {
				fprintf(stderr, "required program %s failed to attach: %s\n",
					specs[i].name, strerror(-err));
				goto cleanup;
			}
			fprintf(stderr, "warning: optional program %s unavailable: %s\n",
				specs[i].name, strerror(-err));
			continue;
		}
		links[nlinks++] = l;
		snprintf(st[nst++].state, sizeof(st[0].state), "attached");
	}

	/* ---- attach marker uprobes */
	if (opt.fam_markers) {
		const char *src = opt.uprobe_bin ? opt.uprobe_bin : (opt.cmd ? opt.cmd[0] : NULL);
		struct bpf_program *mp[2] = { skel->progs.handle_op_begin, skel->progs.handle_op_end };
		const char *fn[2] = { "op_begin", "op_end" };

		if (src && resolve_binary(src, bin, sizeof(bin)))
			bin_used = bin;
		for (int k = 0; k < 2; k++) {
			st[nst].name = mp[k] == skel->progs.handle_op_begin ? "handle_op_begin"
									    : "handle_op_end";
			if (!bin_used) {
				snprintf(st[nst++].state, sizeof(st[0].state),
					 "unavailable: no binary (use -u)");
				continue;
			}
			LIBBPF_OPTS(bpf_uprobe_opts, uo, .func_name = fn[k]);
			struct bpf_link *l = bpf_program__attach_uprobe_opts(mp[k], -1, bin_used, 0, &uo);

			if (!l) {
				err = -errno;
				snprintf(st[nst++].state, sizeof(st[0].state), "unavailable: %s",
					 strerror(-err));
				fprintf(stderr, "warning: cannot hook %s in %s: %s (markers missing; "
					"PID-only correlation still works)\n", fn[k], bin_used, strerror(-err));
				continue;
			}
			links[nlinks++] = l;
			snprintf(st[nst++].state, sizeof(st[0].state), "attached");
		}
	} else {
		st[nst].name = "handle_op_begin";
		snprintf(st[nst++].state, sizeof(st[0].state), "disabled");
		st[nst].name = "handle_op_end";
		snprintf(st[nst++].state, sizeof(st[0].state), "disabled");
	}

	/* ---- output and ring buffer */
	out.f = fopen(events_path, "wx");
	if (!out.f) {
		fprintf(stderr, "open %s: %s\n", events_path, strerror(errno));
		goto cleanup;
	}
	setvbuf(out.f, NULL, _IOFBF, 1 << 20);
	out.binary = !opt.json;
	if (out.binary) {
		struct bin_header h = { .magic = "STEVENT", .schema = STALLTRACE_SCHEMA_VERSION,
					.record_size = sizeof(struct st_event) };

		fwrite(&h, sizeof(h), 1, out.f);
	}
	rb = ring_buffer__new(bpf_map__fd(skel->maps.events), handle_event, &out, NULL);
	if (!rb) {
		fprintf(stderr, "failed to create ring buffer: %s\n", strerror(errno));
		goto cleanup;
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	/* ---- start or adopt the target */
	if (opt.cmd) {
		if (pipe2(gate, O_CLOEXEC) != 0) {
			perror("pipe2");
			goto cleanup;
		}
		target = fork();
		if (target < 0) {
			perror("fork");
			goto cleanup;
		}
		if (target == 0) {
			char go;

			close(gate[1]);
			if (read(gate[0], &go, 1) != 1)
				_exit(127);
			signal(SIGINT, SIG_DFL);
			signal(SIGTERM, SIG_DFL);
			execvp(opt.cmd[0], opt.cmd);
			fprintf(stderr, "stalltrace: exec %s: %s\n", opt.cmd[0], strerror(errno));
			_exit(127);
		}
		close(gate[0]);
		gate[0] = -1;
	} else {
		target = opt.attach_pid;
		if (kill(target, 0) != 0) {
			fprintf(stderr, "process %d not found: %s\n", (int)target, strerror(errno));
			goto cleanup;
		}
	}
	{
		/* The BPF side registers the process the first time it runs a hook
		 * (for a command: at exec, right after the gate opens). */
		__u32 key = (__u32)target;

		if (bpf_map_update_elem(bpf_map__fd(skel->maps.pending_tgids), &key, &one, BPF_ANY)) {
			fprintf(stderr, "cannot add pid %d to pending set: %s\n", (int)target,
				strerror(errno));
			if (opt.cmd)
				kill(target, SIGKILL);
			goto cleanup;
		}
		skel->bss->registration_open = 1;
	}
	t_start = mono_ns();
	if (opt.cmd) {
		if (write(gate[1], "g", 1) != 1)
			perror("gate");
		close(gate[1]);
		gate[1] = -1;
	}
	fprintf(stderr, "stalltrace: tracing pid %d -> %s\n", (int)target, opt.out_dir);

	/* ---- poll until the target exits (plus linger) or we are interrupted */
	for (;;) {
		err = ring_buffer__poll(rb, 100);
		if (err < 0 && err != -EINTR) {
			fprintf(stderr, "ring buffer poll failed: %s\n", strerror(-err));
			break;
		}
		if (!exited) {
			if (opt.cmd) {
				int wst;
				pid_t r = waitpid(target, &wst, WNOHANG);

				if (r == target) {
					exited = true;
					exit_status = WIFEXITED(wst) ? WEXITSTATUS(wst) : 128 + WTERMSIG(wst);
				}
			} else if (kill(target, 0) != 0 && errno == ESRCH) {
				exited = true;
			}
			if (exited)
				done_at = mono_ns();
		}
		if (stop_requested)
			break;
		if (exited && mono_ns() - done_at >= (unsigned long long)opt.linger_ms * 1000000ULL)
			break;
	}
	ring_buffer__consume(rb);
	t_end = mono_ns();

	if (opt.cmd && !exited) {
		int wst;

		kill(target, SIGTERM);
		if (waitpid(target, &wst, 0) == target)
			exit_status = WIFEXITED(wst) ? WEXITSTATUS(wst) : 128 + WTERMSIG(wst);
	}

	if (registered_targets(skel, NULL) == 0)
		fprintf(stderr, "warning: target pid %d never registered; no per-process events "
			"were recorded\n", (int)target);
	if (fflush(out.f) != 0)
		fprintf(stderr, "write %s: %s\n", events_path, strerror(errno));
	write_stats(stats_path, skel, &out, st, nst, bin_used, t_start, t_end, target,
		    exit_status, exited, stop_requested != 0);
	ret = opt.cmd ? (exit_status == 0 ? 0 : 1) : 0;

cleanup:
	if (gate[0] >= 0)
		close(gate[0]);
	if (gate[1] >= 0)
		close(gate[1]);
	ring_buffer__free(rb);
	if (out.f)
		fclose(out.f);
	for (int i = 0; i < nlinks; i++)
		bpf_link__destroy(links[i]);
	stalltrace_bpf__destroy(skel);
	return ret;
}
