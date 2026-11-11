# Build the workload (stallbench) and the tracer/collector (stalltrace).
#   make            build both into build/
#   make workload   build only stallbench (needs only a C compiler)
#   make clean
#
# The tracer needs clang, libbpf-dev and bpftool. On Ubuntu, bpftool ships in
# linux-tools-<kernel>; any installed copy works for generating vmlinux.h and
# the skeleton, so we fall back to the first one under /usr/lib/linux-tools*.

OUT      := build
CC       ?= gcc
CLANG    ?= clang
CFLAGS   ?= -O2 -g -Wall -Wextra
BPFTOOL  ?= $(or $(shell command -v bpftool 2>/dev/null),$(firstword $(wildcard /usr/lib/linux-tools*/*/bpftool /usr/lib/linux-tools/*/bpftool /usr/sbin/bpftool)))
VMLINUX_BTF ?= /sys/kernel/btf/vmlinux
ARCH     := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')
LIBBPF_CFLAGS := $(shell pkg-config --cflags libbpf 2>/dev/null)
LIBBPF_LIBS   := $(or $(shell pkg-config --libs libbpf 2>/dev/null),-lbpf) -lelf -lz

.PHONY: all workload tracer clean
all: workload tracer
workload: $(OUT)/stallbench
tracer: $(OUT)/stalltrace

$(OUT):
	mkdir -p $@

$(OUT)/stallbench: src/workloads/stallbench.c | $(OUT)
	$(CC) $(CFLAGS) -pthread $< -o $@

$(OUT)/vmlinux.h: | $(OUT)
	@test -n "$(BPFTOOL)" || { echo "bpftool not found; install linux-tools-$$(uname -r) or linux-tools-generic" >&2; exit 1; }
	$(BPFTOOL) btf dump file $(VMLINUX_BTF) format c > $@

$(OUT)/stalltrace.bpf.o: src/ebpf/stalltrace.bpf.c src/ebpf/stalltrace.h $(OUT)/vmlinux.h
	$(CLANG) -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) \
		-I$(OUT) -Isrc/ebpf $(LIBBPF_CFLAGS) -c $< -o $@

$(OUT)/stalltrace.skel.h: $(OUT)/stalltrace.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

$(OUT)/stalltrace: src/collector/stalltrace.c src/ebpf/stalltrace.h $(OUT)/stalltrace.skel.h
	$(CC) $(CFLAGS) -I$(OUT) -Isrc/ebpf $(LIBBPF_CFLAGS) $< -o $@ $(LIBBPF_LIBS)

clean:
	rm -rf $(OUT)
