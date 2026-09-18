#!/bin/bash
set -euo pipefail

EVAL_DIR=${EVAL_DIR:-/eval}
RUN_DIR=$EVAL_DIR/run
LOG_DIR=$EVAL_DIR/logs
virtio_pid=$(cat "$RUN_DIR/virtio.pid")
deadline=$((SECONDS + 300))

while ! grep -q '^REDIS_GUEST_READY role=primary ' "$LOG_DIR/zone1-console.log" 2>/dev/null; do
    kill -0 "$virtio_pid" 2>/dev/null || { echo 'virtio backend exited during primary boot' >&2; exit 1; }
    if grep -Eqi 'REDIS_GUEST_INIT_FAILED|Kernel panic|Out of memory:|VFS: Unable to mount root fs' \
        "$LOG_DIR/zone1-console.log"; then
        echo 'primary guest boot failure detected' >&2
        tail -n 100 "$LOG_DIR/zone1-console.log" >&2 || true
        exit 1
    fi
    (( SECONDS < deadline )) || { echo 'primary guest readiness timeout' >&2; exit 1; }
    sleep 0.1
done
printf 'REDIS_PRIMARY_VM_READY %s\n' \
    "$(grep -m1 '^REDIS_GUEST_READY role=primary ' "$LOG_DIR/zone1-console.log")"

while :; do
    ping=$(timeout 2 redis-cli --raw -h 10.20.0.11 PING 2>/dev/null || true)
    info=$(timeout 2 redis-cli --raw -h 10.20.0.11 INFO replication 2>/dev/null | tr -d '\r' || true)
    role=$(sed -n 's/^role://p' <<<"$info")
    [[ "$ping" == PONG && "$role" == master ]] && break
    kill -0 "$virtio_pid" 2>/dev/null || { echo 'virtio backend exited during primary boot' >&2; exit 1; }
    if grep -Eqi 'REDIS_GUEST_INIT_FAILED|Kernel panic|Out of memory:|VFS: Unable to mount root fs' \
        "$LOG_DIR/zone1-console.log"; then
        echo 'primary guest boot failure detected' >&2
        tail -n 100 "$LOG_DIR/zone1-console.log" >&2 || true
        exit 1
    fi
    (( SECONDS < deadline )) || { echo 'primary Redis readiness timeout' >&2; exit 1; }
    sleep 0.1
done

key_count=$(timeout 5 redis-cli --raw -h 10.20.0.11 DBSIZE)
if [[ -n ${EXPECTED_PRIMARY_KEYS:-} && "$key_count" != "$EXPECTED_PRIMARY_KEYS" ]]; then
    echo "primary key count is $key_count, expected $EXPECTED_PRIMARY_KEYS" >&2
    exit 1
fi

printf 'REDIS_PRIMARY_READY address=10.20.0.11 keys=%s\n' "$key_count"
