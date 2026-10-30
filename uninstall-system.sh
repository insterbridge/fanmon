#!/bin/sh
# fanmon: remove the root sampler and systemd service. Run as root.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo or pkexec)" >&2
    exit 1
fi

systemctl disable --now fanmon.service 2>/dev/null || true
rm -f /etc/systemd/system/fanmon.service /usr/local/libexec/fanmon-sampler
systemctl daemon-reload
echo "fanmon sampler removed."
