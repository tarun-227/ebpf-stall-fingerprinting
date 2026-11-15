/*
 * How to mark operations in your own program so that stalltrace and
 * src/label_trace.py can label each one (I/O wait, scheduler wait,
 * syscall/CPU, or indeterminate).
 *
 * 1. Copy the two marker functions below into your program. They must keep
 *    exactly these names and stay real (not inlined) functions, because the
 *    tracer attaches uprobes to them by name.
 * 2. Call op_begin(id) before and op_end(id) after each operation you care
 *    about. Use any id; the same id must be passed to both calls.
 * 3. Build with gcc -O0 -g (or keep the attributes below at higher -O levels).
 *
 * Build and run (from code/, as root for the tracer):
 *   gcc -O0 -g examples/marked_example.c -o build/marked_example
 *   sudo build/stalltrace -o /tmp/example_run -- build/marked_example $HOME/example.dat
 *   python3 src/label_trace.py /tmp/example_run
 *
 * This example does three kinds of operation, five times each:
 *   ids 0-4    read 4 MiB with O_DIRECT (bypasses the page cache) -> IO_WAIT
 *   ids 10-14  a 20 ms busy loop                                   -> SYSCALL_CPU
 *   ids 20-24  sleep 20 ms                                          -> INDETERMINATE
 *              (blocked, but not on I/O: the tool does not guess)
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* ---- the two markers: copy these into your program ---- */
__attribute__((noinline, used)) void op_begin(uint64_t id) { __asm__ volatile("" ::"r"(id)); }
__attribute__((noinline, used)) void op_end(uint64_t id) { __asm__ volatile("" ::"r"(id)); }
/* -------------------------------------------------------- */

#define CHUNK (4u << 20)

static double now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "example.dat";
	void *buf;
	if (posix_memalign(&buf, 4096, CHUNK)) return 1;
	memset(buf, 'x', CHUNK);

	/* Data file for the reads: 5 chunks, written once before any operation. */
	int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0644);
	if (fd < 0) { perror(path); return 1; }
	for (int i = 0; i < 5; i++)
		if (write(fd, buf, CHUNK) != (ssize_t)CHUNK) { perror("write"); return 1; }
	fsync(fd);
	close(fd);

	fd = open(path, O_RDONLY | O_DIRECT);
	if (fd < 0) { perror("open O_DIRECT (use a disk-backed file system, not tmpfs)"); return 1; }
	for (uint64_t i = 0; i < 5; i++) {
		op_begin(i);
		if (pread(fd, buf, CHUNK, (off_t)(i * CHUNK)) < 0) perror("pread");
		op_end(i);
	}
	close(fd);

	volatile uint64_t x = 0;
	for (uint64_t i = 10; i < 15; i++) {
		op_begin(i);
		double t0 = now_ms();
		while (now_ms() - t0 < 20) x++;
		op_end(i);
	}

	struct timespec ts = {0, 20 * 1000 * 1000};
	for (uint64_t i = 20; i < 25; i++) {
		op_begin(i);
		nanosleep(&ts, NULL);
		op_end(i);
	}

	unlink(path);
	free(buf);
	printf("done: 15 operations\n");
	return 0;
}
