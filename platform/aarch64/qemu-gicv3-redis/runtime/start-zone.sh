#!/bin/bash
set -euo pipefail

EVAL_DIR=${EVAL_DIR:-/eval}
RUN_DIR=$EVAL_DIR/run
LOG_DIR=$EVAL_DIR/logs
role=${1:-}

case "$role" in
    primary)
        zone_id=1
        config=$EVAL_DIR/zone1-redis-primary.json
        ;;
    replica)
        zone_id=2
        config=$EVAL_DIR/zone2-redis-replica.json
        ;;
    *)
        echo 'usage: start-zone.sh primary|replica' >&2
        exit 2
        ;;
esac

[[ -r "$RUN_DIR/virtio.pid" ]] || { echo 'virtio backend is not prepared' >&2; exit 1; }
virtio_pid=$(cat "$RUN_DIR/virtio.pid")
kill -0 "$virtio_pid" 2>/dev/null || { echo 'virtio backend is not running' >&2; exit 1; }

"$EVAL_DIR/bin/hvisor" zone start "$config" >"$LOG_DIR/zone$zone_id-start.log" 2>&1
zones=$("$EVAL_DIR/bin/hvisor" zone list || true)
if ! grep -Eq "^\\|[[:space:]]*$zone_id[[:space:]]*\\|" <<<"$zones"; then
    cat "$LOG_DIR/zone$zone_id-start.log" >&2
    echo "zone $zone_id did not appear after start" >&2
    exit 1
fi
printf 'REDIS_ZONE_STARTED role=%s zone_id=%s\n' "$role" "$zone_id"
