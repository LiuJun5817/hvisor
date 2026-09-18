# Redis image build

`build-images.sh` creates the pinned, empty Redis primary, replica, and root
management images. The guest DTBs select `redis-eval-init` as PID 1; it mounts
the required virtual filesystems, configures the static address, emits
`REDIS_GUEST_READY`, and starts Redis directly. Systemd packages remain in the
filesystem but are bypassed during measured guest boot.

Run from the hvisor checkout on the pinned AArch64 host:

```sh
sudo platform/aarch64/qemu-gicv3-redis/image-build/build-images.sh
```

The recipe pins the Ubuntu snapshot, Redis package, kernel, hvisor-tool,
module, debootstrap, and package inputs. It requires network access and root
privileges while building, validates each filesystem, and refuses to overwrite
completed outputs. Use a new directory for a rebuild:

```sh
sudo env OUT_DIR=/absolute/new/output \
  platform/aarch64/qemu-gicv3-redis/image-build/build-images.sh
```

Outputs are written to `image/virtdisk/`:

| File | Contents |
|---|---|
| `redis-primary.ext4` | 1 GiB Jammy guest, `10.20.0.11`, empty primary |
| `redis-replica.ext4` | 1 GiB Jammy guest, `10.20.0.12`, empty replica |
| `redis-root.ext4` | 4 GiB management root, hvisor artifacts, DTBs, zone JSON, guest disks, `redis-cli`, and memtier |
| `*-packages.txt` | Package inventories |
| `images.sha256` / `build-manifest.txt` | Output hashes and pinned inputs |

The root image contains no seeded dataset. Each workload sample prepares a
disposable root-image copy and populates a fresh primary at run time.
