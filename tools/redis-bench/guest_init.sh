#!/bin/sh
set -eu
export PATH=/redis-bench:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs -o size=64m tmpfs /tmp
echo never > /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true
echo never > /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || true
interface=""
for candidate in /sys/class/net/*; do
    name=${candidate##*/}
    case "$(readlink -f "$candidate/device")" in
        *a003a00*) interface=$name; break ;;
    esac
done
[ -n "$interface" ]
guest_net "$interface"
taskset -c 0 redis-server /redis-bench/redis.conf --daemonize yes
echo REDIS_BENCH_GUEST_READY
exec /bin/sh -i
