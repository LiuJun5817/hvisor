#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR=${IMAGE_BUILD_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
PLATFORM_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
HVISOR_DIR=$(cd -- "$PLATFORM_DIR/../../.." && pwd)
WORKSPACE_DIR=$(cd -- "$HVISOR_DIR/.." && pwd)
KERNEL_DIR=${KERNEL_DIR:-/home/jingx/os/linux/linux-5.4}
OUT_DIR=${OUT_DIR:-$PLATFORM_DIR/image/virtdisk}
ARTIFACT_DIR=${ARTIFACT_DIR:-$SCRIPT_DIR/artifacts}
WORK_DIR=${IMAGE_BUILD_WORK_DIR:-$(mktemp -d /tmp/redis-hvisor-images.XXXXXX)}

# Run an immutable driver copy so edits during a long debootstrap cannot change
# Bash's instruction stream.
if [[ ${IMAGE_BUILD_FROZEN:-0} != 1 ]]; then
    cp "$SCRIPT_DIR/build-images.sh" "$WORK_DIR/driver.sh"
    exec env IMAGE_BUILD_FROZEN=1 IMAGE_BUILD_SOURCE_DIR="$SCRIPT_DIR" \
        IMAGE_BUILD_WORK_DIR="$WORK_DIR" bash "$WORK_DIR/driver.sh" "$@"
fi
source "$SCRIPT_DIR/versions.env"

COMMON_IMAGE=$OUT_DIR/redis-common.building.ext4
PRIMARY_IMAGE=$OUT_DIR/redis-primary.building.ext4
REPLICA_IMAGE=$OUT_DIR/redis-replica.building.ext4
ROOT_IMAGE=$OUT_DIR/redis-root.building.ext4
MOUNTS=()

log() {
    printf '[redis-image-build] %s\n' "$*"
}

die() {
    printf '[redis-image-build] ERROR: %s\n' "$*" >&2
    exit 1
}

unmount_one() {
    local path=$1 target attempt
    for target in "$path/dev/pts" "$path/dev" "$path/proc" "$path/sys" "$path"; do
        for ((attempt=0; attempt<10; attempt++)); do
            mountpoint -q "$target" || break
            if umount "$target"; then break; fi
            sleep 1
        done
        mountpoint -q "$target" && return 1
    done
    return 0
}

cleanup() {
    local exit_status=$?
    set +e
    local i
    for ((i=${#MOUNTS[@]}-1; i>=0; i--)); do
        unmount_one "${MOUNTS[$i]}"
    done
    if [[ "$WORK_DIR" == /tmp/redis-hvisor-images.* ]] &&
       ! findmnt -rn -o TARGET | grep -Fq "$WORK_DIR/"; then
        rm -rf -- "$WORK_DIR"
    fi
    return "$exit_status"
}
trap cleanup EXIT

[[ $(id -u) -eq 0 ]] || die 'run with sudo; loop mounts and chroot are required'
[[ $(uname -m) == aarch64 ]] || die 'the recipe requires an AArch64 build host'

# Check required commands
missing_commands=()
for command in debootstrap mkfs.ext4 mount umount chroot sha256sum e2fsck tune2fs git autoconf automake make pkg-config; do
    command -v "$command" >/dev/null || missing_commands+=("$command")
done
if [[ ${#missing_commands[@]} -gt 0 ]]; then
    die "missing host commands: ${missing_commands[*]}"
fi

# Check for memtier_benchmark build dependencies
missing_libs=()
pkg-config --exists libpcre 2>/dev/null || missing_libs+=("libpcre3-dev")
pkg-config --exists libevent 2>/dev/null || missing_libs+=("libevent-dev")
pkg-config --exists zlib 2>/dev/null || missing_libs+=("zlib1g-dev")
if ! pkg-config --exists openssl 2>/dev/null && ! pkg-config --exists libssl 2>/dev/null; then
    missing_libs+=("libssl-dev")
fi
if [[ ${#missing_libs[@]} -gt 0 ]]; then
    die "missing development packages for memtier_benchmark: ${missing_libs[*]}"
fi
actual_debootstrap=$(debootstrap --version 2>&1 | awk 'NR == 1 {print $2}')
[[ "$actual_debootstrap" == "$DEBOOTSTRAP_VERSION" ]] ||
    die "debootstrap $actual_debootstrap found, expected $DEBOOTSTRAP_VERSION"

mkdir -p "$OUT_DIR" "$ARTIFACT_DIR/debootstrap-cache"
for completed in redis-primary.ext4 redis-replica.ext4 redis-root.ext4; do
    [[ ! -e "$OUT_DIR/$completed" ]] || die "$OUT_DIR/$completed already exists"
done
exec > >(tee -a "$OUT_DIR/build.log") 2>&1

verify_file() {
    local expected=$1 path=$2
    printf '%s  %s\n' "$expected" "$path" | sha256sum -c - >/dev/null
}

check_image() {
    local image=$1 status=0
    e2fsck -fy "$image" >/dev/null || status=$?
    (( status <= 1 )) || die "e2fsck failed for $image with status $status"
}

new_image() {
    local path=$1 size=$2 label=$3 uuid=$4
    log "creating $path ($size)"
    truncate --size "$size" "$path"
    mkfs.ext4 -q -F -L "$label" -U "$uuid" \
        -E lazy_itable_init=0,lazy_journal_init=0 "$path"
}

mount_image() {
    local image=$1 name=$2 root
    root=$WORK_DIR/$name
    mkdir -p "$root"
    mount -o loop "$image" "$root"
    MOUNTS+=("$root")
    MOUNT_RESULT=$root
}

mount_chroot_fs() {
    local root=$1
    mkdir -p "$root/dev/pts" "$root/proc" "$root/sys"
    mount --bind /dev "$root/dev"
    mount --make-rslave "$root/dev"
    mount --bind /dev/pts "$root/dev/pts"
    mount --make-rslave "$root/dev/pts"
    mount -t proc proc "$root/proc"
    mount --bind /sys "$root/sys"
    mount --make-rslave "$root/sys"
}

write_base_config() {
    local root=$1 hostname=$2
    printf '%s\n' "$hostname" >"$root/etc/hostname"
    printf '127.0.0.1 localhost\n127.0.1.1 %s\n' "$hostname" >"$root/etc/hosts"
    printf '/dev/vda / ext4 defaults 0 1\n' >"$root/etc/fstab"
    : >"$root/etc/machine-id"
    ln -sf /proc/self/mounts "$root/etc/mtab"
    cat >"$root/etc/apt/sources.list" <<EOF
deb [check-valid-until=no] $UBUNTU_SNAPSHOT_URL $UBUNTU_SUITE main universe
deb [check-valid-until=no] $UBUNTU_SNAPSHOT_URL $UBUNTU_SUITE-updates main universe
deb [check-valid-until=no] $UBUNTU_SNAPSHOT_URL $UBUNTU_SUITE-security main universe
EOF
    cat >"$root/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
    chmod 0755 "$root/usr/sbin/policy-rc.d"
}

install_rootfs() {
    local root=$1 hostname=$2 packages=$3
    log "debootstrapping $hostname from Ubuntu snapshot $UBUNTU_SNAPSHOT"
    debootstrap --arch=arm64 --variant=minbase --components=main,universe \
        --cache-dir="$ARTIFACT_DIR/debootstrap-cache" \
        "$UBUNTU_SUITE" "$root" "$UBUNTU_SNAPSHOT_URL"
    write_base_config "$root" "$hostname"
    mount_chroot_fs "$root"
    cp -L /etc/resolv.conf "$root/etc/resolv.conf"
    chroot "$root" env DEBIAN_FRONTEND=noninteractive \
        apt-get -o Acquire::Check-Valid-Until=false update
    chroot "$root" env DEBIAN_FRONTEND=noninteractive \
        apt-get -o Acquire::Check-Valid-Until=false \
        -o APT::Install-Recommends=false install -y $packages
    chroot "$root" passwd -l root >/dev/null
}

configure_console() {
    local root=$1 tty=$2
    install -d "$root/etc/systemd/system/serial-getty@$tty.service.d"
    install -m 0644 "$SCRIPT_DIR/console-autologin.conf" \
        "$root/etc/systemd/system/serial-getty@$tty.service.d/autologin.conf"
    chroot "$root" systemctl enable "serial-getty@$tty.service" >/dev/null
}

disable_background_work() {
    local root=$1 unit
    install -d "$root/etc/systemd/journald.conf.d"
    cat >"$root/etc/systemd/journald.conf.d/volatile.conf" <<'EOF'
[Journal]
Storage=volatile
EOF
    for unit in apt-daily.service apt-daily.timer apt-daily-upgrade.service \
                apt-daily-upgrade.timer e2scrub_all.timer logrotate.timer; do
        chroot "$root" systemctl mask "$unit" >/dev/null 2>&1 || true
    done
}

finish_rootfs() {
    local root=$1 manifest=$2
    chroot "$root" dpkg-query -W -f='${binary:Package}\t${Version}\n' |
        LC_ALL=C sort >"$manifest"
    chroot "$root" apt-get clean
    rm -f "$root/usr/sbin/policy-rc.d"
    rm -rf "$root/var/lib/apt/lists"/* "$root/tmp"/* "$root/var/tmp"/*
    rm -f "$root/var/lib/systemd/random-seed"
    : >"$root/etc/machine-id"
    unmount_one "$root"
}

build_common_guest() {
    new_image "$COMMON_IMAGE" "$GUEST_IMAGE_SIZE" redis-common \
        879c4cce-85a4-4c6a-a318-d06622dac2e3
    local root
    mount_image "$COMMON_IMAGE" redis-common
    root=$MOUNT_RESULT
    install_rootfs "$root" redis-common \
        "systemd-sysv udev kmod iproute2 procps redis-server=$REDIS_PACKAGE_VERSION redis-tools=$REDIS_PACKAGE_VERSION"
    [[ $(chroot "$root" dpkg-query -W -f='${Version}' redis-server) == "$REDIS_PACKAGE_VERSION" ]] ||
        die 'installed Redis version differs from the pin'
    configure_console "$root" hvc0
    chroot "$root" systemctl enable systemd-networkd.service >/dev/null
    chroot "$root" systemctl enable systemd-networkd-wait-online.service >/dev/null
    chroot "$root" systemctl enable redis-server.service >/dev/null
    disable_background_work "$root"
    install -m 0644 "$SCRIPT_DIR/redis/redis.conf" "$root/etc/redis/redis.conf"
    install -m 0644 "$SCRIPT_DIR/redis/primary-role.conf" "$root/etc/redis/role.conf"
    install -m 0755 "$SCRIPT_DIR/redis-eval-init.sh" \
        "$root/usr/local/sbin/redis-eval-init"
    rm -f "$root/var/lib/redis/dump.rdb" "$root/var/lib/redis/appendonly.aof"
    finish_rootfs "$root" "$OUT_DIR/redis-guest-packages.txt"
    check_image "$COMMON_IMAGE"
}

customize_guest_role() {
    local image=$1 role=$2 address=$3 address_file=$4 role_file=$5 uuid=$6
    cp --reflink=auto --sparse=always "$COMMON_IMAGE" "$image"
    tune2fs -U "$uuid" -L "redis-$role" "$image" >/dev/null
    local root
    mount_image "$image" "redis-$role"
    root=$MOUNT_RESULT
    printf 'redis-%s\n' "$role" >"$root/etc/hostname"
    printf '127.0.0.1 localhost\n127.0.1.1 redis-%s\n' "$role" >"$root/etc/hosts"
    install -m 0644 "$SCRIPT_DIR/network/$address_file" \
        "$root/etc/systemd/network/10-eth0.network"
    install -m 0644 "$SCRIPT_DIR/redis/$role_file" "$root/etc/redis/role.conf"
    printf 'REDIS_ROLE=%s\nREDIS_ADDRESS=%s\n' "$role" "$address" \
        >"$root/etc/redis/eval.env"
    rm -f "$root/var/lib/redis/dump.rdb" "$root/var/lib/redis/appendonly.aof"
    : >"$root/etc/machine-id"
    unmount_one "$root"
    check_image "$image"
}

build_guest_pair() {
    build_common_guest
    log 'cloning the common guest into empty primary and replica roles'
    customize_guest_role "$PRIMARY_IMAGE" primary 10.20.0.11 primary.network primary-role.conf \
        ec197354-1659-47a8-9073-adcaf1b7650b
    customize_guest_role "$REPLICA_IMAGE" replica 10.20.0.12 replica.network replica-role.conf \
        255762f8-6768-495a-8a58-ef29080e2576
    rm -f "$COMMON_IMAGE"
}

build_root_image() {
    new_image "$ROOT_IMAGE" "$ROOT_IMAGE_SIZE" redis-root \
        d76f3168-c74f-4b7d-aa41-6ba289db38c2
    local root runtime_script
    mount_image "$ROOT_IMAGE" redis-root
    root=$MOUNT_RESULT
    install_rootfs "$root" redis-root \
        "systemd-sysv udev kmod iproute2 procps redis-tools=$REDIS_PACKAGE_VERSION libevent-2.1-7 libevent-openssl-2.1-7"
    [[ $(chroot "$root" dpkg-query -W -f='${Version}' redis-tools) == "$REDIS_PACKAGE_VERSION" ]] ||
        die 'installed redis-tools version differs from the pin'
    configure_console "$root" ttyAMA0
    disable_background_work "$root"
    install -d -m 0755 "$root/eval/bin" "$root/eval/images" "$root/eval/logs" \
        "$root/eval/run" "$root/eval/manifest"
    install -m 0755 "$WORKSPACE_DIR/hvisor-tool/output/hvisor" "$root/eval/bin/hvisor"
    install -m 0644 "$WORKSPACE_DIR/hvisor-tool/output/hvisor.ko" "$root/eval/hvisor.ko"
    install -m 0755 "$MEMTIER_BINARY" "$root/usr/local/bin/memtier_benchmark"
    for runtime_script in setup-network.sh start-redis.sh prepare-redis.sh \
            start-zone.sh wait-primary.sh wait-deployment.sh \
            populate-dataset.sh run-workload.sh; do
        install -m 0755 "$PLATFORM_DIR/runtime/$runtime_script" \
            "$root/eval/bin/$runtime_script"
    done
    install -m 0644 "$HVISOR_DIR/platform/aarch64/qemu-gicv3/image/kernel/Image" "$root/eval/Image"
    install -m 0644 "$PLATFORM_DIR/image/dts/zone1-redis-primary.dtb" \
        "$root/eval/zone1-redis-primary.dtb"
    install -m 0644 "$PLATFORM_DIR/image/dts/zone2-redis-replica.dtb" \
        "$root/eval/zone2-redis-replica.dtb"
    install -m 0644 "$PLATFORM_DIR/configs/zone1-redis-primary.json" \
        "$root/eval/zone1-redis-primary.json"
    install -m 0644 "$PLATFORM_DIR/configs/zone2-redis-replica.json" \
        "$root/eval/zone2-redis-replica.json"
    install -m 0644 "$PLATFORM_DIR/configs/redis-virtio.json" "$root/eval/redis-virtio.json"
    cp --reflink=auto --sparse=always "$PRIMARY_IMAGE" "$root/eval/images/redis-primary.ext4"
    cp --reflink=auto --sparse=always "$REPLICA_IMAGE" "$root/eval/images/redis-replica.ext4"
    install -m 0644 "$SCRIPT_DIR/versions.env" "$root/eval/manifest/versions.env"
    install -m 0644 "$OUT_DIR/redis-guest-packages.txt" \
        "$root/eval/manifest/redis-guest-packages.txt"
    (cd "$root/eval" && sha256sum images/redis-primary.ext4 images/redis-replica.ext4) \
        >"$root/eval/manifest/guest-images.sha256"
    finish_rootfs "$root" "$OUT_DIR/redis-root-packages.txt"
    check_image "$ROOT_IMAGE"
}

verify_inputs() {
    verify_file "$LINUX_IMAGE_SHA256" "$HVISOR_DIR/platform/aarch64/qemu-gicv3/image/kernel/Image"
    verify_file "$LINUX_CONFIG_SHA256" "$KERNEL_DIR/.config"
    [[ $(git -C "$KERNEL_DIR" rev-parse HEAD) == "$LINUX_COMMIT" ]] ||
        die 'kernel checkout differs from the pinned revision'
    verify_file "$HVISOR_TOOL_SHA256" "$WORKSPACE_DIR/hvisor-tool/output/hvisor"
    verify_file "$HVISOR_MODULE_SHA256" "$WORKSPACE_DIR/hvisor-tool/output/hvisor.ko"
    git -C "$WORKSPACE_DIR/hvisor-tool" cat-file -e "$HVISOR_TOOL_COMMIT^{commit}" ||
        die 'pinned hvisor-tool source commit is unavailable locally'
    make -C "$PLATFORM_DIR/image/dts" clean all
}

publish_images() {
    mv "$PRIMARY_IMAGE" "$OUT_DIR/redis-primary.ext4"
    mv "$REPLICA_IMAGE" "$OUT_DIR/redis-replica.ext4"
    mv "$ROOT_IMAGE" "$OUT_DIR/redis-root.ext4"
    PRIMARY_IMAGE=$OUT_DIR/redis-primary.ext4
    REPLICA_IMAGE=$OUT_DIR/redis-replica.ext4
    ROOT_IMAGE=$OUT_DIR/redis-root.ext4
}

write_manifest() {
    (cd "$OUT_DIR" && sha256sum redis-primary.ext4 redis-replica.ext4 redis-root.ext4) \
        >"$OUT_DIR/images.sha256"
    {
        printf 'stage=first-stage-empty-redis-pair\n'
        printf 'ubuntu_snapshot=%s\n' "$UBUNTU_SNAPSHOT"
        printf 'redis_package_version=%s\n' "$REDIS_PACKAGE_VERSION"
        printf 'linux_commit=%s\n' "$LINUX_COMMIT"
        printf 'linux_image_sha256=%s\n' "$LINUX_IMAGE_SHA256"
        printf 'linux_config_sha256=%s\n' "$LINUX_CONFIG_SHA256"
        printf 'hvisor_tool_commit=%s\n' "$HVISOR_TOOL_COMMIT"
        printf 'hvisor_tool_sha256=%s\n' "$HVISOR_TOOL_SHA256"
        printf 'hvisor_module_sha256=%s\n' "$HVISOR_MODULE_SHA256"
        sed "s@  @  $OUT_DIR/@" "$OUT_DIR/images.sha256"
    } >"$OUT_DIR/build-manifest.txt"
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown "$SUDO_UID:$SUDO_GID" "$OUT_DIR" "$OUT_DIR"/*
    fi
}

build_memtier() {
    if [[ -n "${MEMTIER_BINARY:-}" && -x "$MEMTIER_BINARY" ]]; then
        log "using pre-built memtier_benchmark from $MEMTIER_BINARY"
        return 0
    fi

    local memtier_src=$WORK_DIR/memtier_benchmark
    local memtier_version=2.0.0
    log "building memtier_benchmark $memtier_version"

    git clone --depth 1 --branch "$memtier_version" \
        https://github.com/RedisLabs/memtier_benchmark.git "$memtier_src" ||
        die "failed to clone memtier_benchmark repository"

    (cd "$memtier_src" && \
        autoreconf -ivf && \
        ./configure --quiet && \
        make -j$(nproc) >/dev/null) ||
        die "memtier_benchmark build failed"

    [[ -x "$memtier_src/memtier_benchmark" ]] ||
        die "memtier_benchmark binary not found after build"

    log "memtier_benchmark built successfully"
    MEMTIER_BINARY=$memtier_src/memtier_benchmark
}

verify_inputs
build_memtier
build_guest_pair
build_root_image
publish_images
write_manifest
log "Redis images are ready in $OUT_DIR"
