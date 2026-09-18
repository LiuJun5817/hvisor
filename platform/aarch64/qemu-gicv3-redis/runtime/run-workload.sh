#!/bin/bash
set -euo pipefail

# Run steady-state mixed GET/SET workload and collect latency/throughput metrics.
# Sample replication offsets during load and verify write propagation afterward.

PRIMARY_ADDRESS=${PRIMARY_ADDRESS:-10.20.0.11}
REPLICA_ADDRESS=${REPLICA_ADDRESS:-10.20.0.12}
DURATION_SEC=${DURATION_SEC:-300}
THREADS=${THREADS:-4}
CLIENTS_PER_THREAD=${CLIENTS_PER_THREAD:-10}
PIPELINE=${PIPELINE:-1}
GET_SET_RATIO=${GET_SET_RATIO:-3:1}
KEY_COUNT=${KEY_COUNT:-100000}
VALUE_SIZE=${VALUE_SIZE:-1024}

OUTPUT_DIR=${OUTPUT_DIR:-/tmp}
WORKLOAD_LOG=$OUTPUT_DIR/workload.log
REPLICATION_LOG=$OUTPUT_DIR/replication.log

deadline=$((SECONDS + 30))
while ! timeout 2 redis-cli -h "$PRIMARY_ADDRESS" PING >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { echo "primary not reachable" >&2; exit 1; }
    sleep 0.5
done

actual_keys=$(timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" DBSIZE)
expected_min=$((KEY_COUNT - 10))
expected_max=$((KEY_COUNT + 10))
if [[ "$actual_keys" -lt "$expected_min" || "$actual_keys" -gt "$expected_max" ]]; then
    echo "primary contains $actual_keys keys, expected approximately $KEY_COUNT" >&2
    exit 1
fi

primary_info=$(timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" INFO replication | tr -d '\r')
replica_info=$(timeout 5 redis-cli --raw -h "$REPLICA_ADDRESS" INFO replication | tr -d '\r')
primary_offset=$(sed -n 's/^master_repl_offset://p' <<<"$primary_info")
replica_offset=$(sed -nE 's/^slave[0-9]+:.*offset=([0-9]+).*/\1/p' <<<"$primary_info")
link_status=$(sed -n 's/^master_link_status://p' <<<"$replica_info")

if [[ "$link_status" != up || ! "$primary_offset" =~ ^[0-9]+$ || ! "$replica_offset" =~ ^[0-9]+$ ]]; then
    echo "invalid initial replication state: link=$link_status primary=$primary_offset replica_ack=$replica_offset" >&2
    exit 1
fi

initial_lag=$((primary_offset - replica_offset))
if (( initial_lag > 1000 )); then
    echo "initial replication lag is $initial_lag bytes, too high" >&2
    exit 1
fi

printf 'REDIS_WORKLOAD_STARTING duration=%s threads=%s clients=%s ratio=%s initial_lag=%s\n' \
    "$DURATION_SEC" "$THREADS" "$CLIENTS_PER_THREAD" "$GET_SET_RATIO" "$initial_lag"

IFS=: read -r get_weight set_weight extra <<<"$GET_SET_RATIO"
if [[ -n ${extra:-} || ! $get_weight =~ ^[1-9][0-9]*$ || ! $set_weight =~ ^[1-9][0-9]*$ ]]; then
    echo "GET_SET_RATIO must contain two positive integers, for example 7:3" >&2
    exit 1
fi
# memtier names this option --ratio but interprets it as SET:GET.
memtier_ratio="$set_weight:$get_weight"

monitor_replication() {
    local interval=2
    printf 'time_sec\tprimary_offset\treplica_ack_offset\tlag_bytes\tlink_status\n' >"$REPLICATION_LOG"
    while sleep "$interval"; do
        p_info=$(timeout 2 redis-cli --raw -h "$PRIMARY_ADDRESS" INFO replication 2>/dev/null | tr -d '\r' || true)
        r_info=$(timeout 2 redis-cli --raw -h "$REPLICA_ADDRESS" INFO replication 2>/dev/null | tr -d '\r' || true)
        p_off=$(sed -n 's/^master_repl_offset://p' <<<"$p_info")
        r_off=$(sed -nE 's/^slave[0-9]+:.*offset=([0-9]+).*/\1/p' <<<"$p_info")
        link=$(sed -n 's/^master_link_status://p' <<<"$r_info")
        if [[ "$p_off" =~ ^[0-9]+$ && "$r_off" =~ ^[0-9]+$ && "$p_off" -ge "$r_off" ]]; then
            lag=$((p_off - r_off))
            printf '%s\t%s\t%s\t%s\t%s\n' "$SECONDS" "$p_off" "$r_off" "$lag" "$link" >>"$REPLICATION_LOG"
        else
            printf '%s\t-\t-\t-\terror\n' "$SECONDS" >>"$REPLICATION_LOG"
        fi
    done
}

monitor_replication &
monitor_pid=$!
trap "kill $monitor_pid 2>/dev/null || true" EXIT

memtier_benchmark \
    --server="$PRIMARY_ADDRESS" \
    --port=6379 \
    --protocol=redis \
    --clients="$CLIENTS_PER_THREAD" \
    --threads="$THREADS" \
    --test-time="$DURATION_SEC" \
    --data-size="$VALUE_SIZE" \
    --key-prefix="key:" \
    --key-minimum=1 \
    --key-maximum="$KEY_COUNT" \
    --ratio="$memtier_ratio" \
    --pipeline="$PIPELINE" \
    --hide-histogram \
    --print-percentiles=50,95,99,99.9 2>&1 | tee "$WORKLOAD_LOG"

kill $monitor_pid 2>/dev/null || true
wait $monitor_pid 2>/dev/null || true

# A marker written after the measured load must reach the replica; replication
# applies writes in order, so observing it confirms earlier writes arrived too.
mapfile -t marker_result < <(printf 'SET __redis_bench_end__ complete\nWAIT 1 30000\n' | \
    timeout 35 redis-cli --raw -h "$PRIMARY_ADDRESS")
if [[ ${marker_result[0]:-} != OK || ${marker_result[1]:-} != 1 ]]; then
    echo "replica did not acknowledge final write: ${marker_result[*]:-no response}" >&2
    exit 1
fi
replication_deadline=$((SECONDS + 30))
while [[ $(timeout 2 redis-cli --raw -h "$REPLICA_ADDRESS" GET __redis_bench_end__ 2>/dev/null || true) != complete ]]; do
    (( SECONDS < replication_deadline )) || { echo 'replica did not receive workload completion marker' >&2; exit 1; }
    sleep 0.5
done

final_primary=$(timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" INFO replication | tr -d '\r')
final_replica=$(timeout 5 redis-cli --raw -h "$REPLICA_ADDRESS" INFO replication | tr -d '\r')
final_primary_offset=$(sed -n 's/^master_repl_offset://p' <<<"$final_primary")
final_replica_offset=$(sed -nE 's/^slave[0-9]+:.*offset=([0-9]+).*/\1/p' <<<"$final_primary")
final_link=$(sed -n 's/^master_link_status://p' <<<"$final_replica")
if [[ ! "$final_primary_offset" =~ ^[0-9]+$ || ! "$final_replica_offset" =~ ^[0-9]+$ || "$final_primary_offset" -lt "$final_replica_offset" || "$final_link" != up ]]; then
    echo 'replica is not connected with valid offsets after workload' >&2
    exit 1
fi
final_lag=$((final_primary_offset - final_replica_offset))

if ! awk 'NR > 1 { count++; if ($5 != "up") bad=1 } END { if (!count || bad) exit 1 }' "$REPLICATION_LOG"; then
    echo 'replication monitoring missed a sample or observed a link/offset error' >&2
    exit 1
fi

ops_sec=$(awk '/Totals/{getline; print $2}' "$WORKLOAD_LOG")
hits_sec=$(awk '/Totals/{getline; print $3}' "$WORKLOAD_LOG")
misses_sec=$(awk '/Totals/{getline; print $4}' "$WORKLOAD_LOG")
p50=$(awk '/Totals/{getline; print $6}' "$WORKLOAD_LOG")
p99=$(awk '/Totals/{getline; print $8}' "$WORKLOAD_LOG")
if [[ ! "$ops_sec" =~ ^[0-9]+([.][0-9]+)?$ || ! "$misses_sec" =~ ^0+([.]0+)?$ || ! "$p50" =~ ^[0-9]+([.][0-9]+)?$ || ! "$p99" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "invalid workload totals or GET misses: ops=$ops_sec misses=$misses_sec" >&2
    exit 1
fi

max_lag=$(awk 'NR > 1 {if ($4 > max) max = $4} END {print max+0}' "$REPLICATION_LOG")

printf 'REDIS_WORKLOAD_COMPLETE ops_sec=%s hits_sec=%s p50_ms=%s p99_ms=%s final_lag=%s max_lag=%s\n' \
    "$ops_sec" "$hits_sec" "$p50" "$p99" "$final_lag" "$max_lag"
