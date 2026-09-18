# Redis platform layout and images

This platform boots one root Linux VM and two Redis application VMs under
QEMU `virt-9.0`. Zone 1 is the primary (`10.20.0.11`); zone 2 is the
read-only replica (`10.20.0.12`). Root Linux owns the combined virtio backend,
the bridge, and the management tools.

## Physical memory layout

QEMU RAM is `[0x40000000, 0xc0000000)` (2 GiB). The regions are 2 MiB aligned
and disjoint:

| Owner | Zone | CPUs | Physical range | Size | Load addresses (kernel / DTB) |
|---|---:|---|---|---:|---|
| Firmware and hvisor | — | — | `[0x40000000,0x50000000)` | 256 MiB | hvisor `0x40400000` |
| Redis primary | 1 | 2 | `[0x50000000,0x70000000)` | 512 MiB | `0x50400000` / `0x50000000` |
| Redis replica | 2 | 3 | `[0x70000000,0x90000000)` | 512 MiB | `0x70400000` / `0x70000000` |
| Root Linux | 0 | 0–1 | `[0x90000000,0xc0000000)` | 768 MiB | `0xa0400000` / `0xa0000000` |

Root's DTB describes only `[0x90000000,0xc0000000)` as ordinary memory and
reserves the two guest ranges with `no-map`. Root still needs stage-2 access to
the guest ranges for image loading and virtio; `no-map` is not an isolation
boundary from root.

The root devices use PL011 at `0x09000000`, virtio-MMIO at
`[0x0a000000,0x0a004000)`, and root IRQs 33 (serial), 64 (hvisor wakeup), and
79 (the root block device). The guest virtio devices are:

| Device | Guest address | Length | hvisor IRQ | DT SPI |
|---|---:|---:|---:|---:|
| Network | `0x0a003600` | `0x200` | 75 | 43 |
| Console | `0x0a003800` | `0x200` | 76 | 44 |
| Block | `0x0a003c00` | `0x200` | 78 | 46 |

The guest DTBs use GICv3 without an ITS. The root DTB reserves guest RAM and
the guest DTBs expose only their assigned CPU, RAM interval, timer, console,
network, and block devices.

## Image configuration

The immutable image recipe is
[`image-build/build-images.sh`](image-build/build-images.sh). It pins the
Ubuntu snapshot, Redis package, kernel, hvisor-tool, module, and debootstrap
inputs. Run it only when the three completed outputs are absent; it refuses to
overwrite them. Set `OUT_DIR` to build a separate image set.

The completed files in `image/virtdisk/` are:

| File | Configuration |
|---|---|
| `redis-primary.ext4` | 1 GiB Jammy guest, `10.20.0.11/24`, primary role, empty database |
| `redis-replica.ext4` | 1 GiB Jammy guest, `10.20.0.12/24`, replica role, empty database |
| `redis-root.ext4` | 4 GiB management root with hvisor-tool/module, kernel, DTBs, zone JSON, both guest disks, `redis-cli`, and memtier |

The guest images share a filesystem base and differ in hostname, address,
filesystem identity, and Redis role configuration. Persistence files are
removed during image construction. The application DTBs select
`redis-eval-init` as PID 1; it mounts the required virtual filesystems,
configures the static network, emits `REDIS_GUEST_READY`, and starts Redis as
the Redis user. Systemd packages remain installed but are bypassed during this
measured guest boot.

The root image contains the combined virtio configuration and these guest
resources:

```text
/eval/images/redis-primary.ext4
/eval/images/redis-replica.ext4
/eval/zone1-redis-primary.json
/eval/zone2-redis-replica.json
/eval/redis-virtio.json
```

The two zones share one virtio backend. The JSON maps guest RAM identity-wise:

```json
{"zone0_ipa":"0x50000000","zonex_ipa":"0x50000000","size":"0x20000000"}
{"zone0_ipa":"0x70000000","zonex_ipa":"0x70000000","size":"0x20000000"}
```

The root image already contains the root-side deployment, population, and
workload scripts installed by the image builder. Host-side runners use
`image/virtdisk/redis-root.ext4` directly with QEMU's disposable snapshot
mode.

To rebuild device trees after changing a DTS file:

```sh
make -C platform/aarch64/qemu-gicv3-redis/image/dts
```

Inspect decoded DTBs before booting and verify CPU IDs, RAM bounds, load
addresses, virtio addresses, and interrupt numbers against the tables above.
