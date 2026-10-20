#!/usr/bin/env bash
# Install or upgrade the usage-tracker edge agent for the current user. Idempotent.
#
#   edge/install.sh                  # server defaults to piehole
#   SERVER=otherhost edge/install.sh
#
# Device name = this machine's Tailscale node name, fixed in the config on first install
# so a later rename can't silently start a second inbox on the server.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
CONF_DIR="$HOME/.config/usage-edge"
CONF="$CONF_DIR/env"
UNIT_DIR="$HOME/.config/systemd/user"
SETTINGS="$HOME/.claude/settings.json"
KEEP_DAYS=365

for c in jq rsync curl ssh tailscale systemctl systemd-run; do
    command -v "$c" >/dev/null || { echo "missing required command: $c" >&2; exit 1; }
done

if [ ! -f "$CONF" ]; then
    device=$(tailscale status --json | jq -r '.Self.DNSName | split(".")[0]')
    [ -n "$device" ] || { echo "could not read Tailscale node name" >&2; exit 1; }
    topic=${NTFY_TOPIC:-$(jq -r '.ntfy_topic // empty' "$here/../alert_config.json" 2>/dev/null || true)}
    if [ -z "$topic" ] && [ -t 0 ]; then
        read -rp "ntfy topic (same as the server's alert_config.json; blank = no alerts): " topic
    fi
    mkdir -p "$CONF_DIR"
    cat >"$CONF" <<EOF
DEVICE=$device
SERVER=${SERVER:-piehole}
REMOTE_INBOX=dev/claude-usage-tracker/data/inbox
NTFY_TOPIC=$topic
STALE_HOURS=6
EOF
    chmod 600 "$CONF"   # the ntfy topic is a shared secret
    echo "wrote $CONF (device=$device)"
fi
# shellcheck source=/dev/null
. "$CONF"

# Transcripts on this machine are the buffer while the server is unreachable; Claude
# Code's default 30-day cleanup would make a month-long outage lose data for good.
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
if [ "$(jq '.cleanupPeriodDays // 0' "$SETTINGS")" -lt "$KEEP_DAYS" ]; then
    cp "$SETTINGS" "$SETTINGS.bak-usage-edge"
    jq --argjson d "$KEEP_DAYS" '.cleanupPeriodDays = $d' "$SETTINGS" >"$SETTINGS.tmp"
    mv "$SETTINGS.tmp" "$SETTINGS"
    echo "set cleanupPeriodDays=$KEEP_DAYS in $SETTINGS (backup: $SETTINGS.bak-usage-edge)"
fi

# Test from inside a user unit: the timers run without this shell's ssh agent or
# terminal, so an interactive-only login would pass here but fail every timer run.
if ! systemd-run --user --wait --pipe --quiet \
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    "$SERVER" true; then
    echo "cannot ssh to $SERVER non-interactively from a systemd user unit." >&2
    echo "Check Tailscale SSH (ACL action must be \"accept\", not \"check\") or ssh keys." >&2
    exit 1
fi

install -Dm755 "$here/usage-edge.sh" "$HOME/.local/bin/usage-edge.sh"
mkdir -p "$UNIT_DIR"
install -m644 "$here"/usage-edge-{snap,sync}.{service,timer} "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now usage-edge-snap.timer usage-edge-sync.timer

# first push in the background: a large corpus can take a while over a DERP relay
systemctl --user start --no-block usage-edge-sync.service usage-edge-snap.service
echo "installed. First sync running; watch: journalctl --user -fu usage-edge-sync"
