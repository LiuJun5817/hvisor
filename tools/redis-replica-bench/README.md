# Redis benchmark runners

These runners compare native and integrated hvisor with one root VM, a Redis
primary at `10.20.0.11`, and a read-only replica at `10.20.0.12`. The builder's
managed worktrees under `/tmp/redis-hvisor-worktrees/` are the default runner
inputs; override them with the runner options when needed.

## Fresh-clone reproduction

On the pinned AArch64 build host, clone this repository and check out the
revision containing the Redis platform and benchmark scripts:

```sh
git clone <this-repository-url> hvisor
cd hvisor
git checkout <experiment-commit>
git submodule update --init --recursive
```

The hvisor candidates are fetched from the upstream source repository. Fetch
one source checkout so both builds use the same object database:

```sh
git clone https://github.com/liujun5817/hvisor.git /tmp/redis-hvisor-source
git -C /tmp/redis-hvisor-source fetch --all --tags
```

Build both candidates with the same platform revision and per-CPU size:

```sh
PLATFORM_COMMIT=$(git rev-parse HEAD)
SOURCE=/tmp/redis-hvisor-source

tools/redis-replica-bench/build-hvisor.sh \
  --variant native \
  --commit 9dbacc423100c1deb2e7d3be6d1bd6bef06e520c \
  --source-dir "$SOURCE" \
  --platform-commit "$PLATFORM_COMMIT" \
  --per-cpu-size 1MiB

tools/redis-replica-bench/build-hvisor.sh \
  --variant integrated \
  --commit a90772b1d3b669b973d6ecaf7301b4b4d682af6d \
  --source-dir "$SOURCE" \
  --platform-commit "$PLATFORM_COMMIT" \
  --per-cpu-size 1MiB
```

The image recipe also requires the pinned `hvisor-tool` checkout as a sibling
directory (`../hvisor-tool`) and a Linux checkout/configuration matching the
inputs in [`image-build/versions.env`](../../platform/aarch64/qemu-gicv3-redis/image-build/versions.env):

```sh
git clone https://github.com/syswonder/hvisor-tool.git ../hvisor-tool
git -C ../hvisor-tool checkout da5f225745f8082396b7487d3f2395f5a96bffb4
LINUX_DIR=/absolute/path/to/linux-5.4
make -C ../hvisor-tool all ARCH=arm64 LOG=LOG_INFO KDIR="$LINUX_DIR"
sudo env KERNEL_DIR="$LINUX_DIR" \
  platform/aarch64/qemu-gicv3-redis/image-build/build-images.sh
```

No hvisor-tool source edits are needed. Use the pinned full commit shown above;
the image recipe verifies its source and output hashes. A newer hvisor-tool
revision is a new image/input revision and must be pinned in `versions.env`
with newly generated hashes before rebuilding.

The image builder validates the Linux commit/configuration, hvisor-tool
artifacts, package pins, and output hashes. It must be run only when the three
completed images are absent. Once the images exist, run the small diagnostic:

```sh
python3 tools/redis-replica-bench/run-workload-paired.py \
  --pairs 1 --key-count 1000 --value-size 64 --duration 10 \
  --get-set-ratio 7:3 --qemu-cpus 0-5 \
  --root-disk platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4 \
  --output target/redis-replica-bench/workload-paired-small
```

For a deployment-only check, replace the final command with
`run-deployment-paired.py --pairs 5` and the same `--root-disk` argument.

## Build one hvisor variant

Run the builder once for each candidate. It clones a missing source into `/tmp`,
creates a persistent detached worktree, adds the committed Redis platform,
applies the selected per-CPU size, and records the exact inputs:

```sh
tools/redis-replica-bench/build-hvisor.sh \
  --variant native \
  --commit 9dbacc423100c1deb2e7d3be6d1bd6bef06e520c \
  --per-cpu-size 1MiB

tools/redis-replica-bench/build-hvisor.sh \
  --variant integrated \
  --commit a90772b1d3b669b973d6ecaf7301b4b4d682af6d \
  --per-cpu-size 1MiB
```

The default platform commit is the current checkout `HEAD`. Override it with
`--platform-commit` when reproducing an older platform revision. Worktrees are
kept under `/tmp/redis-hvisor-worktrees/{native,integrated}` and binaries plus
`build-info.txt` are written under `target/redis-replica-bench/hvisors/`.
Use `--refresh` when rebuilding an existing variant worktree.

The two candidate commits both define `PER_CPU_SIZE` as 512 KiB. Their current
working trees have an uncommitted 1 MiB edit. Treat the committed revisions as
the version identifiers and use the builder's explicit common `--per-cpu-size`
setting for comparison; this avoids comparing a clean 512 KiB native build
with a modified integrated build.

## Images

Build the immutable images only when they are absent:

```sh
sudo platform/aarch64/qemu-gicv3-redis/image-build/build-images.sh
```

The builder refuses to overwrite completed images and installs the root-side
deployment, population, and workload scripts as part of the root image. QEMU
uses a disposable snapshot for every sample, so the source image is unchanged.

## Deployment timing

`run-deployment.py` measures one empty-dataset deployment. The paired runner
alternates native and integrated order and stops on the first failure unless
`--keep-going` is supplied:

```sh
python3 tools/redis-replica-bench/run-deployment-paired.py \
  --pairs 5 --qemu-cpus 0-5 \
  --root-disk platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4
```

These results cover VM readiness, Redis readiness, initial synchronization,
and the deployment marker. They do not measure steady-state workload time.

## Steady-state workload

`run-workload-sample.py` runs one sample; `run-workload-paired.py` runs balanced
pairs. Population uses one pipelined RESP stream. The measured memtier load
uses pipeline depth 1 and interprets `--get-set-ratio` as GET:SET.

The default paired command is a small diagnostic: one pair, 1,000 keys,
64-byte values, 10 seconds, and a 7:3 GET:SET ratio:

```sh
python3 tools/redis-replica-bench/run-workload-paired.py \
  --pairs 1 --key-count 1000 --value-size 64 --duration 10 \
  --get-set-ratio 7:3 --qemu-cpus 0-5 \
  --root-disk platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4 \
  --output target/redis-replica-bench/workload-paired-small
```

Use at least five pairs for a pilot after this check is stable. Every sample
starts a new QEMU process, populates a fresh primary, synchronizes the replica,
and records stage timings, memtier output, replication polls, and console logs.
The current gate checks root CPUs, Redis roles, valid replication polls, zero
reported GET misses, a final `WAIT 1` marker read on the replica, and fatal
console signatures. It does not yet enforce lag, resynchronization, eviction,
or resource-use bounds.

The verified small pair is
`target/redis-replica-bench/workload-paired-small-20260918`: both samples
passed; native/integrated throughput was 157.72/142.76 ops/s, p50 latency was
166.91/183.29 ms, and p99 latency was 860.16/872.45 ms. The integrated sample
reported a 1,746-byte sampled maximum lag, showing why a zero final lag alone
is not a sufficient continuous-replication result.

The completed four-pair pilot is
`target/redis-replica-bench/workload-paired-20260917T170051Z`. It used the
older five-second monitor, so its zero-lag values are descriptive only and do
not establish continuously zero lag.

Outputs are ignored under `target/redis-replica-bench/`; retain a result
directory when its report or raw logs are needed.
