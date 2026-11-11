#!/usr/bin/env bash
# Install the toolchain for the eBPF stall-fingerprinting project.
# Target: Ubuntu 24.04 VM (disposable). Run with: sudo ./scripts/setup_vm.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if ! grep -q 'VERSION_ID="24.04"' /etc/os-release; then
  echo "Warning: tested on Ubuntu 24.04 only; continuing anyway." >&2
fi

KREL="$(uname -r)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_USER="${SUDO_USER:-$USER}"

apt-get update
apt-get install -y \
  build-essential clang llvm make pkg-config git \
  libbpf-dev libelf-dev zlib1g-dev \
  "linux-tools-${KREL}" linux-tools-common "linux-headers-${KREL}" \
  dmsetup fio sysstat util-linux \
  python3 python3-venv python3-pip

# Python environment for correlation, classification and plots.
sudo -u "$REAL_USER" python3 -m venv "${REPO_ROOT}/.venv"
sudo -u "$REAL_USER" "${REPO_ROOT}/.venv/bin/pip" install --upgrade pip
sudo -u "$REAL_USER" "${REPO_ROOT}/.venv/bin/pip" install \
  numpy pandas scikit-learn matplotlib scipy

# Record exact versions for the environment manifest.
mkdir -p "${REPO_ROOT}/experiments/env"
{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "kernel: ${KREL}"
  echo "clang: $(clang --version | head -n1)"
  echo "bpftool: $(bpftool version 2>/dev/null | head -n1 || echo missing)"
  echo "libbpf: $(pkg-config --modversion libbpf 2>/dev/null || echo missing)"
  echo "fio: $(fio --version 2>/dev/null || echo missing)"
  echo "python: $("${REPO_ROOT}/.venv/bin/python" --version)"
  "${REPO_ROOT}/.venv/bin/pip" freeze | sed 's/^/pip: /'
} > "${REPO_ROOT}/experiments/env/toolchain_versions.txt"
chown -R "$REAL_USER": "${REPO_ROOT}/experiments" "${REPO_ROOT}/.venv"

echo "Toolchain installed. Versions saved to experiments/env/toolchain_versions.txt"
echo "Next: sudo ./scripts/check_capabilities.sh"
