#!/bin/bash
set -euo pipefail

EVAL_DIR=${EVAL_DIR:-/eval}
RUN_DIR=$EVAL_DIR/run
LOG_DIR=$EVAL_DIR/logs
mkdir -p "$RUN_DIR" "$LOG_DIR"

[[ $(id -u) -eq 0 ]] || { echo 'prepare-redis must run as root' >&2; exit 1; }
[[ -e /dev/hvisor ]] || insmod "$EVAL_DIR/hvisor.ko"

zones=$("$EVAL_DIR/bin/hvisor" zone list || true)
grep -q 'zone_id' <<<"$zones" || { echo 'hvisor zone list failed' >&2; exit 1; }
if grep -Eq '^\|[[:space:]]*[12][[:space:]]*\|' <<<"$zones"; then
    echo 'Redis zones already exist; boot a fresh QEMU instance' >&2
    exit 1
fi

"$EVAL_DIR/bin/setup-network.sh"
"$EVAL_DIR/bin/hvisor" virtio start "$EVAL_DIR/redis-virtio.json" \
    >"$LOG_DIR/virtio-stdio.log" 2>&1 &
virtio_pid=$!
printf '%s\n' "$virtio_pid" >"$RUN_DIR/virtio.pid"

deadline=$((SECONDS + 60))
while :; do
    kill -0 "$virtio_pid" 2>/dev/null || { echo 'virtio backend exited' >&2; exit 1; }
    journalctl --no-pager -o cat "_PID=$virtio_pid" >"$LOG_DIR/virtio-journal.log"
    grep -q 'virtio request handler loop started' "$LOG_DIR/virtio-journal.log" && break
    (( SECONDS < deadline )) || { echo 'virtio initialization timeout' >&2; exit 1; }
    sleep 1
done

mapfile -t consoles < <(sed -nE \
    's@.*char device redirected to (/dev/pts/[0-9]+).*@\1@p' \
    "$LOG_DIR/virtio-journal.log")
[[ ${#consoles[@]} == 2 ]] || { echo 'missing guest console PTYs' >&2; exit 1; }

capture_console() {
    local zone_id=$1 console=$2 log_file=$3 line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        printf '%s\n' "$line" >>"$log_file"
        case "$line" in
            REDIS_GUEST_READY\ *)
                # Relay only the readiness event to the root serial console. The
                # host timestamps this line as it arrives; all other guest output
                # remains in the per-zone log.
                printf 'REDIS_GUEST_CONSOLE_EVENT zone=%s %s\n' "$zone_id" "$line"
                ;;
        esac
    done <"$console"
}

for index in 0 1; do
    zone_id=$((index + 1))
    capture_console "$zone_id" "${consoles[$index]}" \
        "$LOG_DIR/zone${zone_id}-console.log" &
    printf '%s\n' "$!" >"$RUN_DIR/zone$((index + 1))-console-reader.pid"
done

printf 'REDIS_PREPARED virtio_pid=%s\n' "$virtio_pid"
