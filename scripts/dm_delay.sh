#!/usr/bin/env bash
# Manage a dm-delay device on the disposable test disk, so every block request
# gets a known extra latency (ground truth for I/O-wait operations).
#
#   sudo scripts/dm_delay.sh create DISK DELAY_MS     e.g. create sdb 10
#   sudo scripts/dm_delay.sh format --wipe DISK       mkfs.ext4 on the delay device (DESTROYS DISK)
#   sudo scripts/dm_delay.sh mount MOUNTPOINT
#   sudo scripts/dm_delay.sh set DELAY_MS             change the delay without remounting
#   sudo scripts/dm_delay.sh status                   show table and the -d options for stalltrace
#   sudo scripts/dm_delay.sh remove                   unmount and remove the delay device
#
# Note: dm-delay holds bios before passing them to DISK, so block_rq_issue on
# DISK happens after the delay. The injected time shows up as I/O wait in the
# scheduler events and as bio_queue -> blk_issue gap, not inside issue->complete.
set -euo pipefail

NAME=stall_delay
MAPPER=/dev/mapper/$NAME
STATE=/run/stall_delay.disk

die() { echo "dm_delay.sh: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

check_disk_safe() {
  local disk="$1"
  [[ -b "/dev/$disk" ]] || die "/dev/$disk is not a block device"
  [[ "$(lsblk -dno TYPE "/dev/$disk")" == disk ]] || die "/dev/$disk is not a whole disk"
  if lsblk -nro MOUNTPOINT "/dev/$disk" | grep -q .; then
    die "/dev/$disk (or a partition on it) is mounted; use an unused test disk"
  fi
  local root_src
  root_src="$(findmnt -nro SOURCE /)"
  if lsblk -nro NAME "/dev/$disk" | sed 's|^|/dev/|' | grep -qx "$root_src"; then
    die "/dev/$disk holds the root filesystem"
  fi
}

table_for() {  # table_for DISK DELAY_MS
  local sectors
  sectors="$(blockdev --getsz "/dev/$1")"
  # Same delay for reads and writes: <dev> <off> <delay> <wdev> <woff> <wdelay>
  echo "0 $sectors delay /dev/$1 0 $2 /dev/$1 0 $2"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  create)
    [[ $# -eq 2 ]] || die "usage: create DISK DELAY_MS"
    disk="$1"; delay="$2"
    is_uint "$delay" || die "DELAY_MS must be a whole number"
    check_disk_safe "$disk"
    dmsetup info "$NAME" >/dev/null 2>&1 && die "$NAME already exists (use set or remove)"
    modprobe dm_delay 2>/dev/null || true
    dmsetup create "$NAME" --table "$(table_for "$disk" "$delay")"
    echo "$disk" > "$STATE"
    echo "created $MAPPER on /dev/$disk with ${delay} ms delay"
    ;;
  format)
    [[ "${1:-}" == "--wipe" && $# -eq 2 ]] || die "usage: format --wipe DISK  (erases everything on DISK)"
    disk="$2"
    [[ -f "$STATE" && "$(cat "$STATE")" == "$disk" ]] || die "$NAME is not on $disk; run create first"
    mkfs.ext4 -F -q -L stalltest "$MAPPER"
    echo "formatted $MAPPER as ext4"
    ;;
  mount)
    [[ $# -eq 1 ]] || die "usage: mount MOUNTPOINT"
    mkdir -p "$1"
    mount -o noatime "$MAPPER" "$1"
    [[ -n "${SUDO_USER:-}" ]] && chown "$SUDO_USER": "$1"
    echo "mounted $MAPPER at $1"
    ;;
  set)
    [[ $# -eq 1 ]] && is_uint "$1" || die "usage: set DELAY_MS"
    [[ -f "$STATE" ]] || die "no delay device; run create first"
    disk="$(cat "$STATE")"
    dmsetup suspend "$NAME"
    dmsetup reload "$NAME" --table "$(table_for "$disk" "$1")"
    dmsetup resume "$NAME"
    echo "delay set to $1 ms"
    ;;
  status)
    dmsetup table "$NAME" 2>/dev/null || die "no delay device"
    disk="$(cat "$STATE" 2>/dev/null || true)"
    dm_dev="$(lsblk -dno MAJ:MIN "$MAPPER" | tr -d ' ')"
    echo "delay device: $MAPPER ($dm_dev)"
    if [[ -n "$disk" ]]; then
      disk_dev="$(lsblk -dno MAJ:MIN "/dev/$disk" | tr -d ' ')"
      echo "underlying:   /dev/$disk ($disk_dev)"
      echo "stalltrace options: -d $dm_dev -d $disk_dev"
    fi
    ;;
  remove)
    if mnt="$(findmnt -nro TARGET -S "$MAPPER" 2>/dev/null)" && [[ -n "$mnt" ]]; then
      umount "$mnt"
    fi
    dmsetup remove "$NAME"
    rm -f "$STATE"
    echo "removed $NAME"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
