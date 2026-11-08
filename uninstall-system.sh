#!/bin/sh
# fanmon: remove the sampler, socket and legacy systemd service. Run as root.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo or pkexec)" >&2
    exit 1
fi

systemctl disable --now fanmon.socket 2>/dev/null || true
systemctl disable --now fanmon.service 2>/dev/null || true
systemctl stop 'fanmon@*.service' 2>/dev/null || true
rm -f /etc/systemd/system/fanmon.socket \
    /etc/systemd/system/fanmon@.service \
    /etc/systemd/system/fanmon.service \
    /usr/local/libexec/fanmon-sampler
systemctl daemon-reload
echo "fanmon sampler and systemd units removed."
