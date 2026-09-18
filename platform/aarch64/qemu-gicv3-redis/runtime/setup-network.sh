#!/bin/sh
set -eu

ip link show br-redis >/dev/null 2>&1 || ip link add br-redis type bridge
ip address show dev br-redis | grep -q '10\.20\.0\.1/24' || \
    ip address add 10.20.0.1/24 dev br-redis
ip link set br-redis up

for tap in tap-primary tap-replica; do
    ip link show "$tap" >/dev/null 2>&1 || ip tuntap add dev "$tap" mode tap
    ip link set "$tap" master br-redis
    ip link set "$tap" up
done
