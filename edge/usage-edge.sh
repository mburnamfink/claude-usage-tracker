#!/usr/bin/env bash
# Usage-tracker edge agent: ships this machine's Claude Code data to the tracker server.
#
#   usage-edge.sh snap   call the usage endpoint (only if this machine's OAuth token is
#                        fresh) into a local outbox, then push the outbox
#   usage-edge.sh sync   mirror ~/.claude/projects/**/*.jsonl to the server
#
# The server (tracker/ingest.py) does all parsing; this side only moves files, so it
# never needs updating when the transcript format changes. The OAuth token never leaves
# this machine — only the endpoint's response does.
#
# A failed push is not an error: the outbox and the transcripts are the buffer, and the
# next run retries. If unpushed changes pile up for STALE_HOURS this script alerts via
# ntfy itself, because the server can't notice an edge it never hears from.
# Config: ~/.config/usage-edge/env (written by install.sh).
set -euo pipefail

CONF="${USAGE_EDGE_CONF:-$HOME/.config/usage-edge/env}"
# shellcheck source=/dev/null
. "$CONF"
: "${DEVICE:?DEVICE unset in $CONF}" "${SERVER:?SERVER unset in $CONF}" \
  "${REMOTE_INBOX:?REMOTE_INBOX unset in $CONF}"
STALE_HOURS="${STALE_HOURS:-6}"
NTFY_TOPIC="${NTFY_TOPIC:-}"

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/usage-edge"
OUTBOX="$STATE/outbox"
PROJECTS="$HOME/.claude/projects"
CREDS="$HOME/.claude/.credentials.json"
DEST="$SERVER:$REMOTE_INBOX/$DEVICE"
# BatchMode: a prompt (host key, password, Tailscale SSH "check" re-auth) must fail
# fast instead of hanging the timer
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
USAGE_URL="https://api.anthropic.com/api/oauth/usage"

mkdir -p "$OUTBOX"

poll() {
    local token exp now body err code neterr stamp
    token=$(jq -r '.claudeAiOauth.accessToken // empty' "$CREDS" 2>/dev/null || true)
    exp=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CREDS" 2>/dev/null || echo 0)
    now=$(date +%s)
    # stale token = Claude Code idle here; the server or another device covers it
    [ -n "$token" ] && [ $((exp / 1000)) -gt $((now + 60)) ] || return 0

    body=$(mktemp "$OUTBOX/.body.XXXXXX")
    err=$(mktemp "$OUTBOX/.err.XXXXXX")
    # header via --config on stdin keeps the token out of argv (visible in ps)
    code=$(printf 'header = "Authorization: Bearer %s"\n' "$token" |
        curl -sS --max-time 20 --config - -H "anthropic-beta: oauth-2025-04-20" \
            -o "$body" -w '%{http_code}' "$USAGE_URL" 2>"$err") || true
    neterr=""
    [ "$code" != "000" ] || neterr=$(head -c 300 "$err")
    jq -e 'type == "object"' "$body" >/dev/null 2>&1 || echo null >"$body"

    stamp=$(date -u -d "@$now" +%Y%m%dT%H%M%SZ)
    # dot-prefixed temp + mv: the push and the server only ever see complete files
    jq -n --arg ts "$(date -u -d "@$now" +%Y-%m-%dT%H:%M:%S+00:00)" \
        --arg code "$code" --arg err "$neterr" --slurpfile r "$body" \
        '{captured_at: $ts,
          http_code: (if $code == "000" or $code == "" then null else ($code | tonumber) end),
          neterr: (if $err == "" then null else $err end),
          response: $r[0]}' >"$OUTBOX/.snap.tmp"
    mv "$OUTBOX/.snap.tmp" "$OUTBOX/snap-$stamp.json"
    rm -f "$body" "$err"
}

push_outbox() {
    # --remove-source-files deletes each local file only after it transferred
    rsync -a --mkpath --remove-source-files --include='snap-*.json' --exclude='*' \
        -e "$SSH" "$OUTBOX/" "$DEST/outbox/"
}

push_projects() {
    # no --delete: the server keeps transcripts after Claude Code's cleanup removes them.
    # --append-verify writes in place (the server's resume cursor survives) and falls
    # back to a full copy if the file was rewritten rather than appended.
    rsync -a --mkpath --append-verify --prune-empty-dirs \
        --include='*/' --include='*.jsonl' --exclude='*' \
        -e "$SSH" "$PROJECTS/" "$DEST/projects/"
}

maybe_alert() {
    local ok="$STATE/last_push_ok" last="$STATE/last_alert" mins=$((STALE_HOURS * 60))
    [ -n "$NTFY_TOPIC" ] || return 0
    if [ -f "$ok" ]; then
        [ -z "$(find "$ok" -mmin -"$mins")" ] || return 0
        # idle machine (nothing new since the last good push) = nothing at risk
        [ -n "$(find "$PROJECTS" "$OUTBOX" -type f -newer "$ok" -print -quit 2>/dev/null)" ] ||
            return 0
    fi
    [ -z "$(find "$last" -mmin -"$mins" 2>/dev/null)" ] || return 0
    curl -sS --max-time 15 -o /dev/null \
        -H "Title: Usage edge $DEVICE: can't reach $SERVER" -H "Priority: high" -H "Tags: warning" \
        -d "No successful push in over ${STALE_HOURS}h with unpushed Claude Code data. Check: journalctl --user -u 'usage-edge-*'" \
        "https://ntfy.sh/$NTFY_TOPIC" && touch "$last"
}

# last_push_ok must only move on a real transfer, or an idle snap run would mask a
# failing sync and suppress the alert
push() {
    if "$@"; then
        touch "$STATE/last_push_ok"
    else
        echo "push to $SERVER failed; will retry next run" >&2
        maybe_alert || true
    fi
}

case "${1:-}" in
    snap)
        poll
        if compgen -G "$OUTBOX/snap-*.json" >/dev/null; then push push_outbox; fi ;;
    sync)
        if [ -d "$PROJECTS" ]; then push push_projects; fi ;;
    *) echo "usage: $0 snap|sync" >&2; exit 2 ;;
esac
