#!/bin/sh
# fanmon: install the Rust sampler and socket-activated systemd units.
# Run as root with sudo or pkexec; FANMON_USER may override caller detection.
set -eu
SRC="$(dirname "$(readlink -f "$0")")"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo or pkexec)" >&2
    exit 1
fi

if [ -n "${FANMON_USER:-}" ]; then
    ACCOUNT_KEY=$FANMON_USER
    PASSWD_ENTRY=$(getent passwd "$ACCOUNT_KEY" || true)
    if [ -z "$PASSWD_ENTRY" ]; then
        echo "error: no account found for FANMON_USER='$ACCOUNT_KEY'" >&2
        exit 1
    fi
elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    ACCOUNT_KEY=$SUDO_USER
    PASSWD_ENTRY=$(getent passwd "$ACCOUNT_KEY" || true)
else
    PASSWD_ENTRY=
fi

if [ -z "$PASSWD_ENTRY" ] && [ -n "${PKEXEC_UID:-}" ]; then
    ACCOUNT_KEY=$PKEXEC_UID
    PASSWD_ENTRY=$(getent passwd "$ACCOUNT_KEY" || true)
fi
if [ -z "$PASSWD_ENTRY" ]; then
    echo "error: cannot identify the desktop user; set FANMON_USER or use sudo/pkexec" >&2
    exit 1
fi
SOCKET_USER=$(printf '%s\n' "$PASSWD_ENTRY" | cut -d: -f1)
SOCKET_UID=$(printf '%s\n' "$PASSWD_ENTRY" | cut -d: -f3)
SOCKET_HOME=$(printf '%s\n' "$PASSWD_ENTRY" | cut -d: -f6)
case "$SOCKET_UID" in
    ''|*[!0-9]*)
        echo "error: could not resolve a numeric UID for '$SOCKET_USER'" >&2
        exit 1
        ;;
esac
if [ "$SOCKET_UID" -eq 0 ]; then
    echo "error: the fanmon socket must belong to a non-root desktop user" >&2
    exit 1
fi
if ! command -v runuser >/dev/null 2>&1; then
    echo "error: runuser is required to build the sampler without root privileges" >&2
    exit 1
fi

USER_PATH="$SOCKET_HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
if ! runuser -u "$SOCKET_USER" -- env HOME="$SOCKET_HOME" \
    CARGO_HOME="$SOCKET_HOME/.cargo" RUSTUP_HOME="$SOCKET_HOME/.rustup" \
    PATH="$USER_PATH" sh -c 'command -v cargo >/dev/null 2>&1'; then
    echo "error: cargo is required; install Rust for '$SOCKET_USER' first" >&2
    exit 1
fi

echo "Building fanmon-sampler as $SOCKET_USER (not root)..."
runuser -u "$SOCKET_USER" -- env HOME="$SOCKET_HOME" \
    CARGO_HOME="$SOCKET_HOME/.cargo" RUSTUP_HOME="$SOCKET_HOME/.rustup" \
    CARGO_TARGET_DIR="$SRC/sampler-rs/target" PATH="$USER_PATH" \
    cargo build --release --manifest-path "$SRC/sampler-rs/Cargo.toml"

BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/fanmon-install.XXXXXX")
trap 'rm -rf "$BUILD_DIR"' EXIT
trap 'exit 1' HUP INT TERM
sed "s/@FANMON_SOCKET_UID@/$SOCKET_UID/" "$SRC/fanmon.socket" > "$BUILD_DIR/fanmon.socket"

# Stop old persistent and socket-activated units before replacing their files.
systemctl disable --now fanmon.socket 2>/dev/null || true
systemctl disable --now fanmon.service 2>/dev/null || true
systemctl stop 'fanmon@*.service' 2>/dev/null || true
rm -f /etc/systemd/system/fanmon.service

install -Dm755 "$SRC/sampler-rs/target/release/fanmon-sampler" /usr/local/libexec/fanmon-sampler
install -Dm644 "$BUILD_DIR/fanmon.socket" /etc/systemd/system/fanmon.socket
install -Dm644 "$SRC/fanmon@.service" /etc/systemd/system/fanmon@.service
systemctl daemon-reload
systemctl enable --now fanmon.socket

if systemctl is-active --quiet fanmon.socket; then
    echo "fanmon sampler installed; on-demand socket active for UID $SOCKET_UID."
    echo "No sampler process runs until a GUI client connects."
else
    echo "error: fanmon.socket failed to start" >&2
    systemctl --no-pager status fanmon.socket >&2 || true
    exit 1
fi
