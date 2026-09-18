#!/bin/bash
set -Eeuo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
source /etc/redis/eval.env

fail() {
    status=$?
    printf 'REDIS_GUEST_INIT_FAILED role=%s status=%s\n' "${REDIS_ROLE:-unknown}" "$status"
    exec /bin/bash -i
}
trap fail ERR

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /proc /sys /run/redis
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t tmpfs tmpfs /run 2>/dev/null || true
mkdir -p /run/redis
chown redis:redis /run/redis /var/lib/redis

hostname "redis-$REDIS_ROLE"
ip link set lo up

interface=
for ((attempt=0; attempt<300; attempt++)); do
    for candidate in /sys/class/net/*; do
        name=${candidate##*/}
        if [[ "$name" != lo ]]; then
            interface=$name
            break 2
        fi
    done
    sleep 0.1
done
[[ -n "$interface" ]]

ip link set "$interface" up
ip address replace "$REDIS_ADDRESS/24" dev "$interface"

# VM boot ends here. Redis dataset loading and replica synchronization follow.
printf 'REDIS_GUEST_READY role=%s address=%s interface=%s\n' \
    "$REDIS_ROLE" "$REDIS_ADDRESS" "$interface"

exec setpriv --reuid=redis --regid=redis --init-groups -- \
    redis-server /etc/redis/redis.conf --supervised no
