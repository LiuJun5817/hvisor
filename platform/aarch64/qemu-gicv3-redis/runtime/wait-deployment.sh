#!/bin/bash
set -euo pipefail

EVAL_DIR=${EVAL_DIR:-/eval}
RUN_DIR=$EVAL_DIR/run
LOG_DIR=$EVAL_DIR/logs
virtio_pid=$(cat "$RUN_DIR/virtio.pid")

show_diagnostics() {
    echo '--- network ---' >&2
    ip -br address >&2 || true
    bridge link >&2 || true
    ip route >&2 || true
    ip neigh show dev br-redis >&2 || true
    echo '--- zones ---' >&2
    "$EVAL_DIR/bin/hvisor" zone list >&2 || true
    echo '--- virtio backend ---' >&2
    journalctl --no-pager -o cat "_PID=$virtio_pid" | tail -n 100 >&2 || true
    for zone_id in 1 2; do
        echo "--- zone $zone_id console ---" >&2
        tail -n 100 "$LOG_DIR/zone$zone_id-console.log" >&2 || true
    done
}

wait_for_redis() {
    local role=$1 address=$2 expected_role=$3 deadline=$((SECONDS + 300))
    while :; do
        ping=$(timeout 2 redis-cli --raw -h "$address" PING 2>/dev/null || true)
        info=$(timeout 2 redis-cli --raw -h "$address" INFO replication 2>/dev/null | tr -d '\r' || true)
        actual_role=$(sed -n 's/^role://p' <<<"$info")
        [[ "$ping" == PONG && "$actual_role" == "$expected_role" ]] && break
        kill -0 "$virtio_pid" 2>/dev/null || {
            echo 'virtio backend exited during guest boot' >&2
            show_diagnostics
            exit 1
        }
        if grep -Eqi 'Kernel panic|Out of memory:|VFS: Unable to mount root fs' \
            "$LOG_DIR/zone1-console.log" "$LOG_DIR/zone2-console.log"; then
            echo 'guest boot failure detected' >&2
            show_diagnostics
            exit 1
        fi
        if (( SECONDS >= deadline )); then
            echo "Redis $role did not become reachable at $address" >&2
            show_diagnostics
            exit 1
        fi
        sleep 0.1
    done
    printf 'REDIS_%s_READY address=%s\n' "${role^^}" "$address"
}

deadline=$((SECONDS + 300))
while ! grep -q '^REDIS_GUEST_READY role=replica ' "$LOG_DIR/zone2-console.log" 2>/dev/null; do
    kill -0 "$virtio_pid" 2>/dev/null || {
        echo 'virtio backend exited during replica boot' >&2
        show_diagnostics
        exit 1
    }
    if grep -Eqi 'REDIS_GUEST_INIT_FAILED|Kernel panic|Out of memory:|VFS: Unable to mount root fs' \
        "$LOG_DIR/zone2-console.log"; then
        echo 'replica guest boot failure detected' >&2
        show_diagnostics
        exit 1
    fi
    (( SECONDS < deadline )) || {
        echo 'replica guest readiness timeout' >&2
        show_diagnostics
        exit 1
    }
    sleep 0.1
done
printf 'REDIS_REPLICA_VM_READY %s\n' \
    "$(grep -m1 '^REDIS_GUEST_READY role=replica ' "$LOG_DIR/zone2-console.log")"

wait_for_redis replica 10.20.0.12 slave

deadline=$((SECONDS + 600))
while :; do
    primary_info=$(timeout 5 redis-cli --raw -h 10.20.0.11 INFO replication 2>/dev/null | tr -d '\r')
    replica_info=$(timeout 5 redis-cli --raw -h 10.20.0.12 INFO replication 2>/dev/null | tr -d '\r')
    primary_role=$(sed -n 's/^role://p' <<<"$primary_info")
    replica_role=$(sed -n 's/^role://p' <<<"$replica_info")
    link_status=$(sed -n 's/^master_link_status://p' <<<"$replica_info")
    sync_status=$(sed -n 's/^master_sync_in_progress://p' <<<"$replica_info")
    primary_offset=$(sed -n 's/^master_repl_offset://p' <<<"$primary_info")
    replica_offset=$(sed -n 's/^slave_repl_offset://p' <<<"$replica_info")
    if [[ "$primary_role" == master && "$replica_role" == slave && \
          "$link_status" == up && "$sync_status" == 0 && \
          "$primary_offset" =~ ^[0-9]+$ && "$replica_offset" =~ ^[0-9]+$ && \
          "$replica_offset" -ge "$primary_offset" ]]; then
        break
    fi
    if (( SECONDS >= deadline )); then
        echo 'replica did not complete initial synchronization' >&2
        show_diagnostics
        exit 1
    fi
    sleep 0.1
done

printf 'REDIS_SYNC_COMPLETE primary_offset=%s replica_offset=%s\n' \
    "$primary_offset" "$replica_offset"

marker="deployment-${RANDOM}-${SECONDS}"
mapfile -t marker_result < <(printf 'SET deployment-ready %s\nWAIT 1 10000\n' "$marker" | \
    timeout 15 redis-cli --raw -h 10.20.0.11)
[[ ${marker_result[0]:-} == OK && ${marker_result[1]:-} == 1 ]] || {
    echo "deployment marker was not acknowledged: ${marker_result[*]:-no output}" >&2
    show_diagnostics
    exit 1
}

deadline=$((SECONDS + 30))
while [[ $(timeout 5 redis-cli --raw -h 10.20.0.12 GET deployment-ready 2>/dev/null) != "$marker" ]]; do
    (( SECONDS < deadline )) || { echo 'replica did not return deployment marker' >&2; exit 1; }
    sleep 0.1
done
readonly_output=$(timeout 5 redis-cli --raw -h 10.20.0.12 SET must-fail value 2>&1)
grep -q READONLY <<<"$readonly_output" || { echo 'replica accepted a direct write' >&2; exit 1; }

printf 'REDIS_DEPLOYMENT_READY marker=%s wait_replicas=1\n' "$marker"
