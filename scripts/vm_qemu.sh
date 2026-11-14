#!/usr/bin/env bash
# Robustness VM: Ubuntu 24.04 (its own 6.8 kernel) under QEMU/KVM, runnable
# inside WSL2 (nested virtualisation) or any Linux host with /dev/kvm.
#
#   scripts/vm_qemu.sh create      build the VM files in $VM_DIR (no root)
#   scripts/vm_qemu.sh start       boot it headless; SSH on localhost:$VM_PORT
#   scripts/vm_qemu.sh ssh [CMD]   run a command in the VM (or open a shell)
#   scripts/vm_qemu.sh stop        power it off
#
# Needs: qemu-system-x86, qemu-utils, cloud-image-utils, and access to /dev/kvm
# (user in group kvm). The Ubuntu cloud image must be at $VM_DIR/noble.img:
#   curl -fLo $VM_DIR/noble.img \
#     https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
#
# Layout: vda = 25 GB system disk (copy-on-write overlay of the cloud image);
# vdb = 4 GB raw test disk with host caching off (cache=none, aio=native), so
# every read really reaches the host disk. vdb is what dm-delay wraps.
# Login is by an SSH key generated here; the VM user has no password.
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/vm}"
VM_PORT="${VM_PORT:-2222}"
VM_CPUS="${VM_CPUS:-4}"
VM_MEM="${VM_MEM:-4096}"
KEY="$VM_DIR/id_vm"
SSH=(ssh -i "$KEY" -p "$VM_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o LogLevel=ERROR -o ConnectTimeout=5 stall@localhost)

die() { echo "vm_qemu.sh: $*" >&2; exit 1; }

create() {
  [[ -f "$VM_DIR/noble.img" ]] || die "missing $VM_DIR/noble.img (see header for the download)"
  cd "$VM_DIR"
  [[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N '' -f "$KEY"
  qemu-img create -q -f qcow2 -F qcow2 -b noble.img system.qcow2 25G
  qemu-img create -q -f raw testdisk.raw 4G
  cat > user-data <<EOF
#cloud-config
hostname: stallvm
users:
  - name: stall
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
package_update: false
EOF
  printf 'instance-id: stallvm-1\nlocal-hostname: stallvm\n' > meta-data
  cloud-localds seed.iso user-data meta-data
  echo "created VM files in $VM_DIR"
}

start() {
  cd "$VM_DIR"
  [[ -f system.qcow2 ]] || die "run: $0 create"
  [[ -r /dev/kvm && -w /dev/kvm ]] || die "no access to /dev/kvm (add yourself to group kvm and log in again)"
  qemu-system-x86_64 -enable-kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEM" \
    -drive file=system.qcow2,if=virtio,format=qcow2 \
    -drive file=testdisk.raw,if=virtio,format=raw,cache=none,aio=native \
    -drive file=seed.iso,if=virtio,format=raw,readonly=on \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$VM_PORT"-:22 -device virtio-net-pci,netdev=n0 \
    -display none -serial file:console.log -daemonize -pidfile qemu.pid
  echo -n "booting"
  for _ in $(seq 1 90); do
    if "${SSH[@]}" true 2>/dev/null; then echo " ready (ssh -p $VM_PORT stall@localhost)"; return; fi
    echo -n "."; sleep 2
  done
  die "VM did not answer on port $VM_PORT; see $VM_DIR/console.log"
}

case "${1:-}" in
  create) create ;;
  start)  start ;;
  ssh)    shift; exec "${SSH[@]}" "$@" ;;
  stop)   "${SSH[@]}" sudo poweroff 2>/dev/null || true; echo "powering off" ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
