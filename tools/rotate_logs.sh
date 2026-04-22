#!/bin/bash
# rotate_logs.sh — cap passivbot log files at MAX_BYTES, keep KEEP gz snapshots.
#
# Why truncate-in-place instead of mv: launchd holds the .log and _error.log
# open for append. Moving them would leave the kernel writing into the moved
# inode, making the "rotated" file the active one. Truncate-in-place (`: > f`)
# keeps the same inode and resets the file offset the next write lands on.
#
# Run via com.tradingbots.passivbot-logrotate.plist (daily 04:00 UTC).

set -euo pipefail

LOG_DIR="/Users/5tuktau/Projects/trading-bots/passivbot/logs"
MAX_BYTES=$((50 * 1024 * 1024))   # 50 MB
KEEP=5

cd "$LOG_DIR" || exit 0

file_size() {
    # Works on macOS (stat -f%z) and Linux (stat -c%s). Missing file → 0.
    if [ -f "$1" ]; then
        stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

rotate_one() {
    local f="$1"
    [ -f "$f" ] || return 0
    local size
    size=$(file_size "$f")
    if [ "$size" -gt "$MAX_BYTES" ]; then
        local ts
        ts=$(date -u +%Y%m%dT%H%M%SZ)
        local base
        base=$(basename "$f")
        if gzip -c "$f" > "${base}.${ts}.gz"; then
            : > "$f"
            echo "rotated $base ($size bytes → ${base}.${ts}.gz)"
        fi
    fi
    # Prune old .gz snapshots beyond KEEP.
    local base
    base=$(basename "$f")
    # shellcheck disable=SC2012
    ls -t "${base}".*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
        rm -f -- "$old"
        echo "pruned $old"
    done
}

rotate_one "passivbot.log"
rotate_one "passivbot_error.log"
