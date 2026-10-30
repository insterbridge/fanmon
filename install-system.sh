#!/bin/sh
# fanmon: install the root sampler and systemd service. Run as root:
#   sudo ./install-system.sh
set -eu
SRC="$(dirname "$(readlink -f "$0")")"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo or pkexec)" >&2
    exit 1
fi

install -Dm755 "$SRC/sampler.py" /usr/local/libexec/fanmon-sampler
install -Dm644 "$SRC/fanmon.service" /etc/systemd/system/fanmon.service
systemctl daemon-reload
systemctl enable --now fanmon.service

if systemctl is-active --quiet fanmon.service; then
    echo "fanmon sampler installed and running."
    echo "status file: /run/fanmon/status.json"
else
    echo "error: fanmon.service failed to start" >&2
    systemctl --no-pager status fanmon.service >&2 || true
    exit 1
fi
