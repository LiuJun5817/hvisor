#!/bin/bash
set -euo pipefail

# Populate primary Redis with a fixed dataset for steady-state testing.
# Streams deterministic RESP commands through one pipelined redis-cli connection.
# Population is setup rather than part of the measured workload, so avoid paying
# one emulated network round trip per key.

PRIMARY_ADDRESS=${PRIMARY_ADDRESS:-10.20.0.11}
KEY_COUNT=${KEY_COUNT:-100000}
VALUE_SIZE=${VALUE_SIZE:-1024}

deadline=$((SECONDS + 600))
while ! timeout 2 redis-cli -h "$PRIMARY_ADDRESS" PING >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { echo "primary not reachable at $PRIMARY_ADDRESS" >&2; exit 1; }
    sleep 0.5
done

initial_keys=$(timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" DBSIZE)
if [[ "$initial_keys" != "0" ]]; then
    echo "primary already contains $initial_keys keys; expected empty database" >&2
    exit 1
fi

awk -v key_count="$KEY_COUNT" -v value_size="$VALUE_SIZE" '
    BEGIN {
        value = ""
        for (byte = 0; byte < value_size; byte++)
            value = value "x"
        for (number = 1; number <= key_count; number++) {
            key = "key:" number
            printf "*3\r\n$3\r\nSET\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n", \
                length(key), key, value_size, value
        }
    }
' | redis-cli -h "$PRIMARY_ADDRESS" --pipe 2>&1 | tee /tmp/populate.log

grep -q '^errors: 0, replies: ' /tmp/populate.log || {
    echo "redis-cli --pipe did not confirm error-free population" >&2
    exit 1
}

final_keys=$(timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" DBSIZE)
if [[ "$final_keys" != "$KEY_COUNT" ]]; then
    echo "population resulted in $final_keys keys, expected $KEY_COUNT" >&2
    exit 1
fi

midpoint=$(((KEY_COUNT + 1) / 2))
estimated_dataset_mb=$(for key in "key:1" "key:$midpoint" "key:$KEY_COUNT"; do
    timeout 5 redis-cli --raw -h "$PRIMARY_ADDRESS" MEMORY USAGE "$key"
done | awk -v keys="$KEY_COUNT" '{total += $1; count++} END {printf "%.1f", total/count*keys/(1024*1024)}')

printf 'REDIS_DATASET_POPULATED keys=%s value_size=%s estimated_dataset_mb=%s\n' \
    "$final_keys" "$VALUE_SIZE" "$estimated_dataset_mb"
