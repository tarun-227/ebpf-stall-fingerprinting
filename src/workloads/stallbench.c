// SPDX-License-Identifier: GPL-2.0
/*
 * stallbench: controlled workloads with a known stall cause per operation.
 *
 * Each operation is wrapped in op_begin(op_id) / op_end(op_id), which the
 * tracer hooks with uprobes, and timed with CLOCK_MONOTONIC (the clock used by
 * bpf_ktime_get_ns). Per-operation records are kept in memory and written
 * only after all operations finish, so writing the ground-truth file never
 * overlaps a measured operation.
 *
 * Usage: see usage() below or run `stallbench --help`.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SCHEMA_VERSION 1
#define PAGE 4096UL

/*
 * Marker functions. They must stay out-of-line, keep their symbols, and must
 * not be merged with each other (identical-code folding would give both the
 * same address). The asm bodies differ on purpose.
 */
#if defined(__clang__)
#define MARKER __attribute__((noinline, used, optnone, visibility("default")))
#else
#define MARKER __attribute__((noipa, used, visibility("default")))
#endif

MARKER void op_begin(uint64_t op_id)
{
	__asm__ volatile("nop" : : "r"(op_id) : "memory");
}

MARKER void op_end(uint64_t op_id)
{
	__asm__ volatile("nop\n\tnop" : : "r"(op_id) : "memory");
}

/* ------------------------------------------------------------------ config */

enum wl {
	WL_DIRECT_READ,
	WL_COLD_READ,
	WL_SYNC_WRITE,
	WL_WARM_READ,
	WL_BUFFERED_WRITE,
	WL_CPU,
	WL_CPU_CONTENDED,
	WL_SLEEP,
	WL_NEIGHBOR_IO,
	WL_MIXED,
	WL_MMAP_COLD,
	WL_MMAP_WARM,
	WL_COUNT,
};

static const char *const wl_names[WL_COUNT] = {
	[WL_DIRECT_READ] = "direct_read",
	[WL_COLD_READ] = "cold_read",
	[WL_SYNC_WRITE] = "sync_write",
	[WL_WARM_READ] = "warm_read",
	[WL_BUFFERED_WRITE] = "buffered_write",
	[WL_CPU] = "cpu",
	[WL_CPU_CONTENDED] = "cpu_contended",
	[WL_SLEEP] = "sleep",
	[WL_NEIGHBOR_IO] = "neighbor_io",
	[WL_MIXED] = "mixed",
	[WL_MMAP_COLD] = "mmap_cold",
	[WL_MMAP_WARM] = "mmap_warm",
};

struct config {
	enum wl wl;
	const char *file;
	const char *out;
	const char *run_id;
	uint64_t seed;
	int threads;
	int ops;		/* per thread */
	size_t io_size;
	size_t file_size;	/* 0 = use the file's size */
	bool random;
	int hogs;
	int hog_cpu;
	long sleep_us;
	double cpu_ms;
	long gap_us;
	long settle_ms;
	bool prepare;
	bool no_ref;
};

static struct config cfg = {
	.wl = WL_COUNT,
	.seed = 1,
	.threads = 1,
	.ops = 100,
	.io_size = 64 * 1024,
	.hogs = 3,
	.hog_cpu = 0,
	.sleep_us = 2000,
	.cpu_ms = 2.0,
	.gap_us = 0,
	.settle_ms = 200,
};

/* Filled in during setup, reported in the run header. */
static cpu_set_t base_mask;	/* CPUs this process may use */
static cpu_set_t worker_mask;	/* CPUs workers may use (neighbor_io excludes one) */
static int usable_cpus;
static int neighbor_cpu = -1;
static uint64_t iters_per_ms;
static uint64_t cpu_iters;

/* ----------------------------------------------------------------- helpers */

static void die(const char *fmt, ...) __attribute__((format(printf, 1, 2), noreturn));
static void die(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	fputs("stallbench: ", stderr);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	exit(1);
}

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static pid_t get_tid(void)
{
	return (pid_t)syscall(SYS_gettid);
}

static uint64_t xorshift64(uint64_t *s)
{
	uint64_t x = *s;

	x ^= x << 13;
	x ^= x >> 7;
	x ^= x << 17;
	*s = x;
	return x;
}

/* Busy loop with no syscalls. volatile keeps the compiler from removing it. */
static uint64_t spin(uint64_t iters)
{
	volatile uint64_t x = 0;

	for (uint64_t i = 0; i < iters; i++)
		x += i ^ (x >> 3);
	return x;
}

static void pin_to_cpu(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	if (sched_setaffinity(0, sizeof(set), &set) != 0)
		die("sched_setaffinity(cpu %d): %s", cpu, strerror(errno));
}

static bool valid_run_id(const char *s)
{
	if (!*s)
		return false;
	for (; *s; s++) {
		if (!((*s >= 'a' && *s <= 'z') || (*s >= 'A' && *s <= 'Z') ||
		      (*s >= '0' && *s <= '9') || *s == '_' || *s == '-' || *s == '.'))
			return false;
	}
	return true;
}

static bool needs_file(enum wl w)
{
	return w != WL_CPU && w != WL_CPU_CONTENDED && w != WL_SLEEP;
}

static bool needs_direct_alignment(enum wl w)
{
	return w == WL_DIRECT_READ || w == WL_MIXED || w == WL_NEIGHBOR_IO;
}

/*
 * Ground-truth label by design. A cause must take more than 60% of the
 * operation's wall time to count as dominant (declared in PROJECT.md).
 *
 * Busy-loop operations: with n runnable loops sharing c CPUs, each loop runs
 * about c/n of the time and waits 1 - c/n of it.
 *   cpu_contended: n = hogs + threads on one CPU (c = 1). Two or more
 *                  competitors means >= 67% waiting.
 *   cpu, neighbor_io: n = threads on the usable CPUs. 16 threads on 4 CPUs
 *                  waits 75% (scheduler wait); 8 on 4 waits 50% (neither).
 */
#define DOMINANCE 0.6

static const char *busy_loop_label(int runnable, int cpus)
{
	double wait = runnable > cpus ? 1.0 - (double)cpus / runnable : 0.0;

	if (wait > DOMINANCE)
		return "SCHEDULER_WAIT_DOMINATED";
	if (1.0 - wait > DOMINANCE)
		return "SYSCALL_CPU_DOMINATED";
	return "INDETERMINATE";
}

static const char *expected_label(void)
{
	switch (cfg.wl) {
	case WL_DIRECT_READ:
	case WL_COLD_READ:
	case WL_SYNC_WRITE:
	case WL_MMAP_COLD:
		return "IO_WAIT_DOMINATED";
	case WL_WARM_READ:
	case WL_BUFFERED_WRITE:
	case WL_MMAP_WARM:
		return "SYSCALL_CPU_DOMINATED";
	case WL_CPU:
	case WL_NEIGHBOR_IO:
		return busy_loop_label(cfg.threads, usable_cpus);
	case WL_CPU_CONTENDED:
		return busy_loop_label(cfg.hogs + cfg.threads, 1);
	case WL_SLEEP:
	case WL_MIXED:
	default:
		return "INDETERMINATE";
	}
}

/* --------------------------------------------------------------- prepare */

static void prepare_file(void)
{
	size_t chunk = 1 << 20, done = 0;
	uint64_t s = cfg.seed ? cfg.seed : 1;
	uint64_t *buf;
	int fd;

	if (!cfg.file_size)
		die("--prepare needs --size");
	buf = malloc(chunk);
	if (!buf)
		die("out of memory");
	fd = open(cfg.file, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd < 0)
		die("open %s: %s", cfg.file, strerror(errno));
	while (done < cfg.file_size) {
		size_t n = cfg.file_size - done < chunk ? cfg.file_size - done : chunk;

		for (size_t i = 0; i < chunk / 8; i++)
			buf[i] = xorshift64(&s);
		if (write(fd, buf, n) != (ssize_t)n)
			die("write %s: %s", cfg.file, strerror(errno));
		done += n;
	}
	if (fsync(fd) != 0)
		die("fsync %s: %s", cfg.file, strerror(errno));
	close(fd);
	free(buf);
	printf("prepared %s (%zu bytes)\n", cfg.file, cfg.file_size);
}

/* ------------------------------------------------------- helper processes */

static pid_t helpers[64];
static int nhelpers;

static void start_hogs(void)
{
	for (int i = 0; i < cfg.hogs; i++) {
		pid_t p = fork();

		if (p < 0)
			die("fork: %s", strerror(errno));
		if (p == 0) {
			pin_to_cpu(cfg.hog_cpu);
			for (;;)
				spin(1000000);
		}
		helpers[nhelpers++] = p;
	}
}

/*
 * A separate process doing random direct reads on the same file. It is
 * pinned to its own CPU so it adds block activity but not CPU contention.
 */
static void start_neighbor(void)
{
	pid_t p = fork();

	if (p < 0)
		die("fork: %s", strerror(errno));
	if (p == 0) {
		uint64_t s = cfg.seed * 7919 + 13;

		pin_to_cpu(neighbor_cpu);
		size_t blocks = cfg.file_size / cfg.io_size;
		void *buf;
		int fd = open(cfg.file, O_RDONLY | O_DIRECT);

		if (fd < 0 || posix_memalign(&buf, PAGE, cfg.io_size) != 0)
			_exit(1);
		for (;;) {
			off_t off = (off_t)((xorshift64(&s) % blocks) * cfg.io_size);

			if (pread(fd, buf, cfg.io_size, off) < 0)
				_exit(1);
		}
	}
	helpers[nhelpers++] = p;
}

static void stop_helpers(void)
{
	for (int i = 0; i < nhelpers; i++)
		kill(helpers[i], SIGKILL);
	for (int i = 0; i < nhelpers; i++)
		waitpid(helpers[i], NULL, 0);
	nhelpers = 0;
}

/* ------------------------------------------------------------ calibration */

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

	return x < y ? -1 : x > y;
}

/* Median wall time of spin(iters), in ns. */
static uint64_t median_spin_ns(uint64_t iters, int reps)
{
	uint64_t t[15];

	if (reps > 15)
		reps = 15;
	for (int i = 0; i < reps; i++) {
		uint64_t t0 = now_ns();

		spin(iters);
		t[i] = now_ns() - t0;
	}
	qsort(t, (size_t)reps, sizeof(t[0]), cmp_u64);
	return t[reps / 2];
}

/*
 * Scale iters so spin(iters) takes target_ms by median, then measure again
 * and correct once (a single short probe tends to run faster than typical).
 */
static uint64_t iters_for_ms(double target_ms)
{
	uint64_t iters = (uint64_t)(target_ms * (double)iters_per_ms);

	for (int pass = 0; pass < 2; pass++) {
		uint64_t ns;

		if (iters == 0)
			iters = 1;
		ns = median_spin_ns(iters, 7);
		if (ns)
			iters = (uint64_t)((double)iters * target_ms * 1e6 / (double)ns);
	}
	return iters ? iters : 1;
}

static void calibrate_cpu(void)
{
	const uint64_t probe = 2000000;
	uint64_t ns;

	spin(probe);	/* warm up */
	ns = median_spin_ns(probe, 7);
	iters_per_ms = ns ? probe * 1000000ULL / ns : probe;
	cpu_iters = iters_for_ms(cfg.cpu_ms);
}

/* Record usable CPUs; for neighbor_io reserve the last one for the neighbour. */
static void setup_cpus(void)
{
	if (sched_getaffinity(0, sizeof(base_mask), &base_mask) != 0)
		die("sched_getaffinity: %s", strerror(errno));
	worker_mask = base_mask;
	usable_cpus = CPU_COUNT(&base_mask);

	if (cfg.wl == WL_CPU_CONTENDED && !CPU_ISSET(cfg.hog_cpu, &base_mask))
		die("--hog-cpu %d is not available to this process", cfg.hog_cpu);
	if (cfg.wl == WL_NEIGHBOR_IO) {
		if (usable_cpus < 2)
			die("neighbor_io needs at least 2 CPUs");
		for (int c = CPU_SETSIZE - 1; c >= 0; c--) {
			if (CPU_ISSET(c, &base_mask)) {
				neighbor_cpu = c;
				break;
			}
		}
		CPU_CLR(neighbor_cpu, &worker_mask);
		usable_cpus--;
	}
}

/* ---------------------------------------------------------------- workers */

struct rec {
	uint64_t op_id;
	uint64_t t_begin;
	uint64_t t_end;
	int64_t bytes;
	int err;
	/* Kernel's own per-thread accounting around the operation (not eBPF). */
	uint64_t ref_t0;	/* CLOCK_MONOTONIC at the two reference readings */
	uint64_t ref_t1;
	int64_t ref_wall_ns;
	int64_t ref_cpu_ns;
	int64_t ref_runq_ns;	/* -1 if schedstat is unavailable */
};

/*
 * Reference measurement taken just outside each operation:
 *   on-CPU time   CLOCK_THREAD_CPUTIME_ID (exact; brings the runtime up to date)
 *   run-queue wait  run_delay, 2nd field of /proc/thread-self/schedstat; it is
 *                 final for every completed wait once this thread is running.
 *   blocked       the rest of the wall time.
 */
struct ref_sample {
	uint64_t wall;
	uint64_t cpu;
	int64_t runq;
};

static int64_t read_run_delay(int ss_fd)
{
	char buf[128];
	unsigned long long run_ns, wait_ns;
	ssize_t n;

	if (ss_fd < 0 || lseek(ss_fd, 0, SEEK_SET) != 0)
		return -1;
	n = read(ss_fd, buf, sizeof(buf) - 1);
	if (n <= 0)
		return -1;
	buf[n] = '\0';
	if (sscanf(buf, "%llu %llu", &run_ns, &wait_ns) != 2)
		return -1;
	return (int64_t)wait_ns;
}

/*
 * One consistent sample. If the thread is preempted between reading
 * run_delay and reading the clocks, the wait would fall inside the window
 * but outside run_delay; re-reading run_delay afterwards detects that (it
 * changes whenever the thread comes back from a wait), and we retry.
 */
static void read_ref(int ss_fd, struct ref_sample *s)
{
	struct timespec ts;

	for (int tries = 0; tries < 8; tries++) {
		int64_t before = read_run_delay(ss_fd);

		clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
		s->cpu = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
		s->wall = now_ns();
		s->runq = before;
		if (before < 0 || read_run_delay(ss_fd) == before)
			return;
	}
	s->runq = -1;	/* never got a stable sample: mark the reference unknown */
}

/* Does this workload's operation itself read or write the file? */
static bool op_does_io(void)
{
	switch (cfg.wl) {
	case WL_DIRECT_READ:
	case WL_COLD_READ:
	case WL_SYNC_WRITE:
	case WL_MMAP_COLD:
	case WL_MIXED:
		return true;
	default:
		return false;
	}
}

/*
 * Rate of the kernel's per-thread clocks (sched_clock: thread CPU time and
 * schedstat run_delay) relative to CLOCK_MONOTONIC. They should agree, but in
 * a VM with a mis-calibrated TSC sched_clock can run fast or slow (1.09 was
 * seen under WSL2). Measured by spinning, uncontended, before helpers start;
 * the best of three is kept because preemption can only lower the ratio.
 */
static double clock_ratio = 1.0;
static double clock_ratio_end = 0.0;	/* re-measured after the run, as a drift check */

static double measure_ratio_once(void);

static void measure_clock_ratio(void)
{
	double r = measure_ratio_once();

	if (r > 0.5 && r < 2.0)
		clock_ratio = r;
}

static double measure_ratio_once(void)
{
	double best = 0.0;

	for (int i = 0; i < 3; i++) {
		struct timespec a, b;
		uint64_t w0, w1;
		double cpu, r;

		clock_gettime(CLOCK_THREAD_CPUTIME_ID, &a);
		w0 = now_ns();
		while (now_ns() - w0 < 100000000ULL)
			spin(10000);
		clock_gettime(CLOCK_THREAD_CPUTIME_ID, &b);
		w1 = now_ns();
		cpu = (double)(b.tv_sec - a.tv_sec) * 1e9 + (double)(b.tv_nsec - a.tv_nsec);
		r = cpu / (double)(w1 - w0);
		if (r > best)
			best = r;
	}
	return best;
}

/* Kernel CPU and run-queue time converted to wall-clock nanoseconds. */
static double ref_cpu_wall(const struct rec *r) { return (double)r->ref_cpu_ns / clock_ratio; }
static double ref_runq_wall(const struct rec *r) { return (double)r->ref_runq_ns / clock_ratio; }

/*
 * Dominance rule applied to the reference measurement, on the wall-clock
 * scale. Blocked time counts as I/O only when the operation does I/O (a sleep
 * blocks without I/O); the design label is not used, so a mixed op whose read
 * happened to be slow is correctly I/O-dominated.
 */
static const char *ref_label(const struct rec *r)
{
	double wall = (double)r->ref_wall_ns, blocked;

	if (r->ref_runq_ns < 0 || wall <= 0)
		return "UNKNOWN";
	blocked = wall - ref_cpu_wall(r) - ref_runq_wall(r);
	if (ref_cpu_wall(r) / wall > DOMINANCE)
		return "SYSCALL_CPU_DOMINATED";
	if (ref_runq_wall(r) / wall > DOMINANCE)
		return "SCHEDULER_WAIT_DOMINATED";
	if (blocked / wall > DOMINANCE)
		return op_does_io() ? "IO_WAIT_DOMINATED" : "INDETERMINATE";
	return "INDETERMINATE";
}

struct worker {
	int idx;
	pthread_t thread;
	pid_t tid;
	struct rec *recs;
	int nrecs;
	int setup_err;
	uint64_t sink;
	uint64_t cpu_ns;	/* thread CPU time over all operations */
};

static pthread_barrier_t start_barrier;
static char *map_base;	/* shared mapping for mmap workloads */

static int open_flags(void)
{
	switch (cfg.wl) {
	case WL_DIRECT_READ:
	case WL_MIXED:
		return O_RDONLY | O_DIRECT;
	case WL_SYNC_WRITE:
	case WL_BUFFERED_WRITE:
		return O_WRONLY;
	default:
		return O_RDONLY;
	}
}

static off_t next_offset(struct worker *w, uint64_t *rng, int i)
{
	size_t region = cfg.file_size / cfg.threads;
	size_t blocks = region / cfg.io_size;
	size_t blk = cfg.random ? xorshift64(rng) % blocks : (size_t)i % blocks;

	return (off_t)((size_t)w->idx * region + blk * cfg.io_size);
}

/* Work done outside the measured window to set up the intended cache state. */
static void before_op(int fd, off_t off)
{
	switch (cfg.wl) {
	case WL_COLD_READ:
		posix_fadvise(fd, off, (off_t)cfg.io_size, POSIX_FADV_DONTNEED);
		break;
	case WL_MMAP_COLD:
		/* Drop our page-table entries, then evict the clean pages. */
		madvise(map_base + off, cfg.io_size, MADV_DONTNEED);
		posix_fadvise(fd, off, (off_t)cfg.io_size, POSIX_FADV_DONTNEED);
		break;
	default:
		break;
	}
}

/* The measured operation. Returns bytes moved, or -errno. */
static int64_t do_op(struct worker *w, int fd, void *buf, off_t off)
{
	struct timespec ts;
	ssize_t n;

	switch (cfg.wl) {
	case WL_DIRECT_READ:
	case WL_COLD_READ:
	case WL_WARM_READ:
		n = pread(fd, buf, cfg.io_size, off);
		return n < 0 ? -errno : n;
	case WL_SYNC_WRITE:
		n = pwrite(fd, buf, cfg.io_size, off);
		if (n < 0)
			return -errno;
		if (fdatasync(fd) != 0)
			return -errno;
		return n;
	case WL_BUFFERED_WRITE:
		n = pwrite(fd, buf, cfg.io_size, off);
		return n < 0 ? -errno : n;
	case WL_CPU:
	case WL_CPU_CONTENDED:
	case WL_NEIGHBOR_IO:
		w->sink += spin(cpu_iters);
		return 0;
	case WL_SLEEP:
		ts.tv_sec = cfg.sleep_us / 1000000;
		ts.tv_nsec = (cfg.sleep_us % 1000000) * 1000;
		while (nanosleep(&ts, &ts) != 0 && errno == EINTR)
			;
		return 0;
	case WL_MIXED: {
		/*
		 * Spin for exactly as long as this read took, so I/O and CPU each
		 * take about half the operation whatever the device speed, cache
		 * state or access pattern (sizing from a separate calibration was
		 * off by 2x when sequential reads were faster than the probes).
		 */
		uint64_t t0 = now_ns(), d, t1;

		n = pread(fd, buf, cfg.io_size, off);
		if (n < 0)
			return -errno;
		t1 = now_ns();
		d = t1 - t0;
		while (now_ns() - t1 < d)
			w->sink += spin(256);
		return n;
	}
	case WL_MMAP_COLD:
	case WL_MMAP_WARM: {
		uint64_t acc = 0;

		for (size_t p = 0; p < cfg.io_size; p += PAGE)
			acc += *(volatile char *)(map_base + off + p);
		w->sink += acc;
		return (int64_t)cfg.io_size;
	}
	default:
		return -EINVAL;
	}
}

static void *worker_main(void *arg)
{
	struct worker *w = arg;
	uint64_t rng = cfg.seed * 1000003ULL + (uint64_t)w->idx + 1;
	void *buf = NULL;
	int fd = -1;
	int ss_fd = open("/proc/thread-self/schedstat", O_RDONLY | O_CLOEXEC);

	w->tid = get_tid();
	if (cfg.wl == WL_CPU_CONTENDED)
		pin_to_cpu(cfg.hog_cpu);
	else if (cfg.wl == WL_NEIGHBOR_IO &&
		 sched_setaffinity(0, sizeof(worker_mask), &worker_mask) != 0)
		die("sched_setaffinity: %s", strerror(errno));

	if (needs_file(cfg.wl)) {
		fd = open(cfg.file, open_flags());
		if (fd < 0) {
			w->setup_err = errno;
			goto wait;
		}
		if (cfg.wl == WL_COLD_READ)
			posix_fadvise(fd, 0, 0, POSIX_FADV_RANDOM);
		if (posix_memalign(&buf, PAGE, cfg.io_size) != 0) {
			w->setup_err = ENOMEM;
			goto wait;
		}
		memset(buf, 0xA5 ^ w->idx, cfg.io_size);
	}

	/* warm_read / mmap_warm: load this thread's region before measuring. */
	if (cfg.wl == WL_WARM_READ || cfg.wl == WL_MMAP_WARM) {
		size_t region = cfg.file_size / cfg.threads;

		for (size_t o = 0; o + cfg.io_size <= region; o += cfg.io_size) {
			off_t off = (off_t)((size_t)w->idx * region + o);

			if (cfg.wl == WL_WARM_READ) {
				if (pread(fd, buf, cfg.io_size, off) < 0)
					break;
			} else {
				for (size_t p = 0; p < cfg.io_size; p += PAGE)
					w->sink += *(volatile char *)(map_base + off + p);
			}
		}
	}

wait:
	pthread_barrier_wait(&start_barrier);
	if (w->setup_err)
		goto out;

	{
		struct timespec c0, c1;

		clock_gettime(CLOCK_THREAD_CPUTIME_ID, &c0);
		for (int i = 0; i < cfg.ops; i++) {
			struct rec *r = &w->recs[i];
			off_t off = needs_file(cfg.wl) ? next_offset(w, &rng, i) : 0;
			struct ref_sample s0 = { 0, 0, -1 }, s1 = { 0, 0, -1 };

			r->op_id = ((uint64_t)w->idx << 32) | (uint32_t)i;
			before_op(fd, off);

			/* --no-ref skips the per-op reference readings (about ten extra
			 * syscalls per op), so overhead runs measure only the tracer. */
			if (!cfg.no_ref)
				read_ref(ss_fd, &s0);
			op_begin(r->op_id);
			r->t_begin = now_ns();
			r->bytes = do_op(w, fd, buf, off);
			r->t_end = now_ns();
			op_end(r->op_id);
			if (!cfg.no_ref)
				read_ref(ss_fd, &s1);

			r->err = r->bytes < 0 ? (int)-r->bytes : 0;
			r->ref_t0 = s0.wall;
			r->ref_t1 = s1.wall;
			r->ref_wall_ns = (int64_t)(s1.wall - s0.wall);
			r->ref_cpu_ns = (int64_t)(s1.cpu - s0.cpu);
			r->ref_runq_ns = (s0.runq >= 0 && s1.runq >= 0) ? s1.runq - s0.runq : -1;
			w->nrecs = i + 1;
			if (cfg.gap_us > 0)
				usleep((useconds_t)cfg.gap_us);
		}
		clock_gettime(CLOCK_THREAD_CPUTIME_ID, &c1);
		w->cpu_ns = (uint64_t)((c1.tv_sec - c0.tv_sec) * 1000000000LL + (c1.tv_nsec - c0.tv_nsec));
	}
out:
	if (fd >= 0)
		close(fd);
	if (ss_fd >= 0)
		close(ss_fd);
	free(buf);
	return NULL;
}

/* ---------------------------------------------------------------- output */

static void write_results(struct worker *ws, uint64_t t_start, uint64_t t_end)
{
	FILE *f = fopen(cfg.out, "w");
	const char *label = expected_label();
	long total_ops = 0, failed = 0;
	uint64_t worker_cpu = 0;

	if (!f)
		die("open %s: %s", cfg.out, strerror(errno));
	for (int t = 0; t < cfg.threads; t++) {
		total_ops += ws[t].nrecs;
		worker_cpu += ws[t].cpu_ns;
		for (int i = 0; i < ws[t].nrecs; i++)
			failed += ws[t].recs[i].err != 0;
	}

	fprintf(f,
		"{\"type\":\"run\",\"schema\":%d,\"run_id\":\"%s\",\"pid\":%d,"
		"\"workload\":\"%s\",\"expected\":\"%s\",\"file\":\"%s\","
		"\"file_size\":%zu,\"io_size\":%zu,\"pattern\":\"%s\",\"threads\":%d,"
		"\"ops_per_thread\":%d,\"seed\":%" PRIu64 ",\"hogs\":%d,\"hog_cpu\":%d,"
		"\"sleep_us\":%ld,\"cpu_ms\":%.3f,\"gap_us\":%ld,"
		"\"usable_cpus\":%d,\"neighbor_cpu\":%d,"
		"\"iters_per_ms\":%" PRIu64 ",\"cpu_iters\":%" PRIu64 ",\"clock_ratio\":%.5f,"
		"\"clock_ratio_end\":%.5f,\"clock_drift_flag\":%s,"
		"\"t_start_ns\":%" PRIu64 ",\"t_end_ns\":%" PRIu64 ","
		"\"no_ref\":%s,\"worker_cpu_ns\":%" PRIu64 ","
		"\"ops_total\":%ld,\"ops_failed\":%ld}\n",
		SCHEMA_VERSION, cfg.run_id, (int)getpid(), wl_names[cfg.wl], label,
		needs_file(cfg.wl) ? cfg.file : "", needs_file(cfg.wl) ? cfg.file_size : 0,
		cfg.io_size, cfg.random ? "random" : "sequential", cfg.threads, cfg.ops,
		cfg.seed, cfg.wl == WL_CPU_CONTENDED ? cfg.hogs : 0, cfg.hog_cpu,
		cfg.wl == WL_SLEEP ? cfg.sleep_us : 0, cfg.cpu_ms, cfg.gap_us,
		usable_cpus, neighbor_cpu, iters_per_ms, cpu_iters, clock_ratio, clock_ratio_end,
		fabs(clock_ratio_end - clock_ratio) > 0.01 * clock_ratio ? "true" : "false",
		t_start, t_end, cfg.no_ref ? "true" : "false", worker_cpu,
		total_ops, failed);

	for (int t = 0; t < cfg.threads; t++) {
		for (int i = 0; i < ws[t].nrecs; i++) {
			const struct rec *r = &ws[t].recs[i];

			/* ref_cpu_ns/ref_runq_ns are on the wall-clock scale (divided by
			 * clock_ratio); the *_raw_ns fields keep the kernel's values. */
			int64_t cpu = r->ref_runq_ns < 0 ? -1 : (int64_t)ref_cpu_wall(r);
			int64_t runq = r->ref_runq_ns < 0 ? -1 : (int64_t)ref_runq_wall(r);
			int64_t blocked = r->ref_runq_ns < 0 ? -1 : r->ref_wall_ns - cpu - runq;

			fprintf(f,
				"{\"type\":\"op\",\"run_id\":\"%s\",\"op_id\":%" PRIu64 ","
				"\"thread\":%d,\"tid\":%d,\"workload\":\"%s\",\"expected\":\"%s\","
				"\"t_begin_ns\":%" PRIu64 ",\"t_end_ns\":%" PRIu64 ","
				"\"lat_ns\":%" PRIu64 ",\"bytes\":%" PRId64 ",\"err\":%d,"
				"\"ref_t0_ns\":%" PRIu64 ",\"ref_t1_ns\":%" PRIu64 ","
				"\"ref_wall_ns\":%" PRId64 ",\"ref_cpu_ns\":%" PRId64 ","
				"\"ref_runq_ns\":%" PRId64 ",\"ref_blocked_ns\":%" PRId64 ","
				"\"ref_cpu_raw_ns\":%" PRId64 ",\"ref_runq_raw_ns\":%" PRId64 ","
				"\"ref_label\":\"%s\"}\n",
				cfg.run_id, r->op_id, t, (int)ws[t].tid, wl_names[cfg.wl], label,
				r->t_begin, r->t_end, r->t_end - r->t_begin,
				r->bytes < 0 ? 0 : r->bytes, r->err,
				r->ref_t0, r->ref_t1, r->ref_wall_ns, cpu, runq,
				blocked < 0 ? 0 : blocked, r->ref_cpu_ns, r->ref_runq_ns, ref_label(r));
		}
	}
	if (fclose(f) != 0)
		die("close %s: %s", cfg.out, strerror(errno));
}

/* ------------------------------------------------------------------ main */

static void usage(FILE *f)
{
	fprintf(f,
"Usage:\n"
"  stallbench --prepare --file PATH --size BYTES [--seed N]\n"
"  stallbench --workload NAME --run-id ID --out GT.jsonl [options]\n"
"\n"
"Workloads (expected label):\n"
"  direct_read     O_DIRECT pread                         IO_WAIT\n"
"  cold_read       buffered pread after evicting range    IO_WAIT\n"
"  sync_write      pwrite + fdatasync                     IO_WAIT\n"
"  mmap_cold       touch evicted mmap pages               IO_WAIT\n"
"  warm_read       buffered pread of cached data          SYSCALL_CPU\n"
"  buffered_write  pwrite into the page cache             SYSCALL_CPU\n"
"  cpu             busy loop, no contention               SYSCALL_CPU\n"
"  neighbor_io     busy loop while another process reads  SYSCALL_CPU\n"
"  mmap_warm       touch resident mmap pages              SYSCALL_CPU\n"
"  cpu_contended   busy loop pinned with --hogs hogs      SCHEDULER_WAIT (>=2 competitors)\n"
"  sleep           nanosleep                              INDETERMINATE\n"
"  mixed           direct read + equal-length busy loop   INDETERMINATE\n"
"\n"
"Options:\n"
"  --file PATH       data file (create it first with --prepare)\n"
"  --size BYTES      file size for --prepare; region used otherwise (default: whole file)\n"
"  --io-size BYTES   bytes per operation, multiple of 4096 (default 65536)\n"
"  --threads N       worker threads (default 1)\n"
"  --ops N           operations per thread (default 100)\n"
"  --random          random offsets (default sequential)\n"
"  --seed N          RNG seed (default 1)\n"
"  --hogs N          CPU hog processes for cpu_contended (default 3)\n"
"  --hog-cpu N       CPU that hogs and contended workers are pinned to (default 0)\n"
"  --sleep-us N      sleep length for sleep (default 2000)\n"
"  --cpu-ms X        busy-loop length for cpu workloads (default 2.0)\n"
"  --gap-us N        pause between operations (default 0)\n"
"  --settle-ms N     wait after starting helpers (default 200)\n"
"  --no-ref          skip per-op kernel-accounting readings (for overhead runs;\n"
"                    reference labels become UNKNOWN)\n");
}

static uint64_t parse_u64(const char *s, const char *name)
{
	char *end;
	unsigned long long v;

	errno = 0;
	v = strtoull(s, &end, 0);
	if (errno || *end || end == s)
		die("bad value for %s: %s", name, s);
	return v;
}

static void parse_args(int argc, char **argv)
{
	enum { O_PREPARE = 256, O_WORKLOAD, O_FILE, O_OUT, O_RUN_ID, O_SEED, O_THREADS,
	       O_OPS, O_IO_SIZE, O_SIZE, O_RANDOM, O_HOGS, O_HOG_CPU, O_SLEEP_US,
	       O_CPU_MS, O_GAP_US, O_SETTLE_MS, O_NO_REF, O_HELP };
	static const struct option longopts[] = {
		{ "prepare", no_argument, NULL, O_PREPARE },
		{ "workload", required_argument, NULL, O_WORKLOAD },
		{ "file", required_argument, NULL, O_FILE },
		{ "out", required_argument, NULL, O_OUT },
		{ "run-id", required_argument, NULL, O_RUN_ID },
		{ "seed", required_argument, NULL, O_SEED },
		{ "threads", required_argument, NULL, O_THREADS },
		{ "ops", required_argument, NULL, O_OPS },
		{ "io-size", required_argument, NULL, O_IO_SIZE },
		{ "size", required_argument, NULL, O_SIZE },
		{ "random", no_argument, NULL, O_RANDOM },
		{ "hogs", required_argument, NULL, O_HOGS },
		{ "hog-cpu", required_argument, NULL, O_HOG_CPU },
		{ "sleep-us", required_argument, NULL, O_SLEEP_US },
		{ "cpu-ms", required_argument, NULL, O_CPU_MS },
		{ "gap-us", required_argument, NULL, O_GAP_US },
		{ "settle-ms", required_argument, NULL, O_SETTLE_MS },
		{ "no-ref", no_argument, NULL, O_NO_REF },
		{ "help", no_argument, NULL, O_HELP },
		{ 0 },
	};
	int c;

	while ((c = getopt_long(argc, argv, "", longopts, NULL)) != -1) {
		switch (c) {
		case O_PREPARE: cfg.prepare = true; break;
		case O_WORKLOAD:
			for (int i = 0; i < WL_COUNT; i++)
				if (!strcmp(optarg, wl_names[i]))
					cfg.wl = (enum wl)i;
			if (cfg.wl == WL_COUNT)
				die("unknown workload: %s", optarg);
			break;
		case O_FILE: cfg.file = optarg; break;
		case O_OUT: cfg.out = optarg; break;
		case O_RUN_ID: cfg.run_id = optarg; break;
		case O_SEED: cfg.seed = parse_u64(optarg, "--seed"); break;
		case O_THREADS: cfg.threads = (int)parse_u64(optarg, "--threads"); break;
		case O_OPS: cfg.ops = (int)parse_u64(optarg, "--ops"); break;
		case O_IO_SIZE: cfg.io_size = parse_u64(optarg, "--io-size"); break;
		case O_SIZE: cfg.file_size = parse_u64(optarg, "--size"); break;
		case O_RANDOM: cfg.random = true; break;
		case O_HOGS: cfg.hogs = (int)parse_u64(optarg, "--hogs"); break;
		case O_HOG_CPU: cfg.hog_cpu = (int)parse_u64(optarg, "--hog-cpu"); break;
		case O_SLEEP_US: cfg.sleep_us = (long)parse_u64(optarg, "--sleep-us"); break;
		case O_CPU_MS: cfg.cpu_ms = strtod(optarg, NULL); break;
		case O_GAP_US: cfg.gap_us = (long)parse_u64(optarg, "--gap-us"); break;
		case O_SETTLE_MS: cfg.settle_ms = (long)parse_u64(optarg, "--settle-ms"); break;
		case O_NO_REF: cfg.no_ref = true; break;
		case O_HELP: usage(stdout); exit(0);
		default: usage(stderr); exit(2);
		}
	}
	if (optind != argc)
		die("unexpected argument: %s", argv[optind]);
}

static void validate(void)
{
	struct stat st;

	if (cfg.prepare) {
		if (!cfg.file)
			die("--prepare needs --file");
		return;
	}
	if (cfg.wl == WL_COUNT)
		die("--workload is required (see --help)");
	if (!cfg.out || !cfg.run_id)
		die("--out and --run-id are required");
	if (!valid_run_id(cfg.run_id))
		die("--run-id may contain only letters, digits, '_', '-' and '.'");
	if (cfg.threads < 1 || cfg.threads > 256)
		die("--threads must be 1..256");
	if (cfg.ops < 1)
		die("--ops must be >= 1");
	if (cfg.hogs < 0 || cfg.hogs > 32)
		die("--hogs must be 0..32");
	if (cfg.cpu_ms <= 0)
		die("--cpu-ms must be > 0");
	if (cfg.hog_cpu < 0 || cfg.hog_cpu >= sysconf(_SC_NPROCESSORS_ONLN))
		die("--hog-cpu must be an online CPU");
	if (cfg.io_size == 0 || cfg.io_size % PAGE)
		die("--io-size must be a non-zero multiple of 4096");

	if (!needs_file(cfg.wl))
		return;
	if (!cfg.file)
		die("workload %s needs --file", wl_names[cfg.wl]);
	if (stat(cfg.file, &st) != 0)
		die("stat %s: %s (create it with --prepare)", cfg.file, strerror(errno));
	if (!cfg.file_size || cfg.file_size > (size_t)st.st_size)
		cfg.file_size = (size_t)st.st_size;
	if (cfg.file_size / cfg.threads < cfg.io_size)
		die("file too small: each thread needs at least --io-size bytes");
	if (needs_direct_alignment(cfg.wl) && cfg.file_size % PAGE)
		die("file size must be a multiple of 4096 for O_DIRECT");
}

int main(int argc, char **argv)
{
	struct worker *ws;
	uint64_t t_start, t_end;
	int setup_failures = 0;
	int fd_map = -1;

	parse_args(argc, argv);
	validate();
	if (cfg.prepare) {
		prepare_file();
		return 0;
	}

	setup_cpus();
	measure_clock_ratio();
	calibrate_cpu();

	if (cfg.wl == WL_MMAP_COLD || cfg.wl == WL_MMAP_WARM) {
		fd_map = open(cfg.file, O_RDONLY);
		if (fd_map < 0)
			die("open %s: %s", cfg.file, strerror(errno));
		map_base = mmap(NULL, cfg.file_size, PROT_READ, MAP_SHARED, fd_map, 0);
		if (map_base == MAP_FAILED)
			die("mmap: %s", strerror(errno));
		if (cfg.wl == WL_MMAP_COLD)
			madvise(map_base, cfg.file_size, MADV_RANDOM);
	}

	ws = calloc((size_t)cfg.threads, sizeof(*ws));
	if (!ws)
		die("out of memory");
	for (int t = 0; t < cfg.threads; t++) {
		ws[t].idx = t;
		ws[t].recs = calloc((size_t)cfg.ops, sizeof(struct rec));
		if (!ws[t].recs)
			die("out of memory");
	}

	if (cfg.wl == WL_CPU_CONTENDED)
		start_hogs();
	if (cfg.wl == WL_NEIGHBOR_IO)
		start_neighbor();
	if (nhelpers)
		usleep((useconds_t)cfg.settle_ms * 1000);

	pthread_barrier_init(&start_barrier, NULL, (unsigned)cfg.threads + 1);
	for (int t = 0; t < cfg.threads; t++)
		if (pthread_create(&ws[t].thread, NULL, worker_main, &ws[t]) != 0)
			die("pthread_create failed");
	pthread_barrier_wait(&start_barrier);
	t_start = now_ns();
	for (int t = 0; t < cfg.threads; t++)
		pthread_join(ws[t].thread, NULL);
	t_end = now_ns();

	stop_helpers();
	clock_ratio_end = measure_ratio_once();
	if (map_base && map_base != MAP_FAILED)
		munmap(map_base, cfg.file_size);
	if (fd_map >= 0)
		close(fd_map);

	for (int t = 0; t < cfg.threads; t++) {
		if (ws[t].setup_err) {
			fprintf(stderr, "stallbench: thread %d setup failed: %s\n", t,
				strerror(ws[t].setup_err));
			setup_failures++;
		}
	}
	write_results(ws, t_start, t_end);
	for (int t = 0; t < cfg.threads; t++)
		free(ws[t].recs);
	free(ws);
	return setup_failures ? 1 : 0;
}
