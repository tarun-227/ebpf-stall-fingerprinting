#!/usr/bin/env bash
# Report which kernel hooks and features this project needs are available.
# Read-only: attaches nothing, changes no settings.
# Run with: sudo ./scripts/check_capabilities.sh [test-disk-device, e.g. sdb]
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/experiments/env"
mkdir -p "$OUT_DIR"
OUT="${OUT_DIR}/capabilities_$(date -u +%Y%m%dT%H%M%SZ).txt"
TEST_DISK="${1:-}"

TRACEFS=/sys/kernel/tracing
[[ -d "$TRACEFS/events" ]] || TRACEFS=/sys/kernel/debug/tracing
MISSING=0

ok()   { printf '[OK]      %s\n' "$*"; }
miss() { printf '[MISSING] %s\n' "$*"; MISSING=$((MISSING + 1)); }
info() { printf '          %s\n' "$*"; }

check_tp() {  # check_tp <category>/<event> [field ...]
  local tp="$1"; shift
  local fmt="$TRACEFS/events/$tp/format"
  if [[ ! -r "$fmt" ]]; then
    miss "tracepoint $tp"
    return
  fi
  ok "tracepoint $tp"
  local f
  for f in "$@"; do
    if grep -qE "field:.*[ *]${f}(\[.*\])?;" "$fmt"; then
      ok "  field $tp.$f"
    else
      miss "  field $tp.$f"
    fi
  done
}

kconfig() {  # kconfig <CONFIG_NAME>
  local cfg="/boot/config-$(uname -r)" val=""
  if [[ -r "$cfg" ]]; then
    val="$(grep -E "^$1=" "$cfg" | cut -d= -f2)"
  elif [[ -r /proc/config.gz ]]; then
    val="$(zcat /proc/config.gz | grep -E "^$1=" | cut -d= -f2)"
  fi
  if [[ "$val" == "y" || "$val" == "m" ]]; then ok "$1=$val"; else miss "$1 (not y/m)"; fi
}

{
  echo "# Capability report"
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "## Machine"
  info "kernel: $(uname -r) ($(uname -m))"
  info "distro: $(. /etc/os-release && echo "$PRETTY_NAME")"
  info "cpu: $(lscpu | sed -n 's/^Model name:[[:space:]]*//p') x $(nproc)"
  info "ram: $(free -h | awk '/^Mem:/{print $2}')"
  info "virtualisation: $(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo
  echo "## Block devices"
  lsblk -o NAME,SIZE,ROTA,TYPE,FSTYPE,MOUNTPOINT,SCHED | sed 's/^/          /'
  echo
  echo "## Kernel config"
  kconfig CONFIG_BPF_SYSCALL
  kconfig CONFIG_DEBUG_INFO_BTF
  kconfig CONFIG_BPF_EVENTS
  kconfig CONFIG_UPROBE_EVENTS
  kconfig CONFIG_BLK_DEV_DM
  kconfig CONFIG_DM_DELAY
  echo
  echo "## BTF"
  if [[ -r /sys/kernel/btf/vmlinux ]]; then
    ok "/sys/kernel/btf/vmlinux (CO-RE possible)"
    if command -v bpftool >/dev/null; then
      BTF_DUMP="$(bpftool btf dump file /sys/kernel/btf/vmlinux format raw 2>/dev/null)"
      grep -q "'in_iowait'" <<<"$BTF_DUMP" && ok "task_struct.in_iowait in BTF" \
        || miss "task_struct.in_iowait in BTF (fall back to block-overlap evidence)"
      grep -q "'__state'" <<<"$BTF_DUMP" && ok "task_struct.__state in BTF" \
        || info "task_struct.__state absent; older 'state' name may apply"
      grep -q "'btf_trace_sched_switch'" <<<"$BTF_DUMP" && ok "BTF tp_btf/sched_switch" \
        || miss "BTF tp_btf/sched_switch"
    else
      miss "bpftool (run setup_vm.sh first)"
    fi
  else
    miss "/sys/kernel/btf/vmlinux (CO-RE not possible)"
  fi
  echo
  echo "## Tracepoints (tracefs: $TRACEFS)"
  check_tp raw_syscalls/sys_enter id
  check_tp raw_syscalls/sys_exit id ret
  check_tp sched/sched_switch prev_pid prev_state next_pid
  check_tp sched/sched_wakeup pid target_cpu
  check_tp sched/sched_wakeup_new pid
  check_tp block/block_rq_issue dev sector nr_sector bytes
  check_tp block/block_rq_complete dev sector nr_sector error
  check_tp exceptions/page_fault_user address error_code
  echo
  echo "## dm-delay"
  if modinfo dm_delay >/dev/null 2>&1 || grep -qw delay <(dmsetup targets 2>/dev/null); then
    ok "dm-delay target available"
  else
    miss "dm-delay target"
  fi
  echo
  echo "## Test disk"
  if [[ -z "$TEST_DISK" ]]; then
    info "no test disk given; rerun with the device name, e.g. sdb"
  elif [[ -b "/dev/$TEST_DISK" ]]; then
    ok "/dev/$TEST_DISK exists"
    info "rotational: $(cat "/sys/block/$TEST_DISK/queue/rotational" 2>/dev/null)"
    info "scheduler:  $(cat "/sys/block/$TEST_DISK/queue/scheduler" 2>/dev/null)"
    if findmnt -rno TARGET -S "/dev/$TEST_DISK" | grep -qx /; then
      miss "/dev/$TEST_DISK is the root disk; use a separate test disk"
    fi
  else
    miss "/dev/$TEST_DISK"
  fi
  echo
  echo "## Summary"
  info "missing items: $MISSING"
} | tee "$OUT"

echo
echo "Report saved to ${OUT#"$REPO_ROOT"/}"
