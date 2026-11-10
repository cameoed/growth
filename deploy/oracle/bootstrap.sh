#!/bin/bash
# Run over SSH as: sudo bash -s < deploy/oracle/bootstrap.sh
set -euo pipefail
test "$(id -u)" -eq 0
test -n "${SUDO_USER:-}"
test "$SUDO_USER" != root
export DEBIAN_FRONTEND=noninteractive
# The Always Free AMD fallback has 1 GB RAM. Bounded OS swap prevents a transient
# memory spike from killing the SQLite/backend process; it is not a data backup.
if [ "$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)" -lt 2097152 ]; then
    if [ ! -e /swapfile ]; then
        (umask 077; fallocate -l 4G /swapfile)
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    if ! swapon --noheadings --show=NAME | grep -qx /swapfile; then
        swapon /swapfile
    fi
    if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
        printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
    fi
fi
apt-get update
apt-get install -y python3 python3-venv rsync ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Use Ubuntu 24.04 or newer"'
if ! id beings-growth >/dev/null 2>&1; then
    useradd --system --home-dir /opt/beings-growth --shell /usr/sbin/nologin beings-growth
fi
# The SSH operator must traverse the parent to reach its private incoming directory.
# Operational data and provider secrets remain restricted to the application owner.
install -d -o beings-growth -g "$(id -gn "$SUDO_USER")" -m 0750 /opt/beings-growth
install -d -o beings-growth -g beings-growth -m 0700 /opt/beings-growth/data
install -d -o "$SUDO_USER" -g "$(id -gn "$SUDO_USER")" -m 0700 /opt/beings-growth/incoming
# The service is installed separately and never started by this setup script.
