# Redis application benchmarks

The completed 2026-09-16 experiment is documented in [RESULTS.md](RESULTS.md).
The original-build boot failure and controlled workaround are recorded in
[BOOT_DIAGNOSIS.md](BOOT_DIAGNOSIS.md).

This suite compares the normal release builds of baseline hvisor and hvisor with
VeriHyMem, using the same Linux guest, Redis executable, disk, configuration and
request generator. It runs three experiments:

| Experiment | Timed interval / reported metrics |
| --- | --- |
| System and service startup | Host sends U-Boot `bootm` to the first successful remote Redis `PING`; a subsequent `SET`/`GET` must pass |
| Redis process startup | In an already running guest, immediately before `fork` to the first successful local `PING`; child ownership and `SET`/`GET` are validated afterward |
| Sustained requests | Completed requests/s and client-observed P50/P95/P99 for GET, SET overwrite, and 90% GET / 10% SET |

The first experiment starts **hvisor and its root Linux VM (zone0)**. It includes
hypervisor initialization, Linux boot, guest network setup and Redis startup.
QEMU process creation and firmware initialization before `bootm` are excluded.
It is **not** a measurement of the dynamic non-root `zone_start` operation.
The downloaded image's old hvisor tool/module use config ABI version 1, whereas
these hvisor builds require version 5; they cannot be used to launch a non-root
VM without rebuilding the matching management components.

The current host is x86_64 WSL2 and the guest is AArch64 under QEMU TCG. These
results describe this emulated setup, **not physical AArch64 performance**.

## Fixed environment

- Baseline hvisor: `d2a078c40979122a30999651553ecaca3b5371db`.
- Integrated hvisor: `df547ceda31ec3d41d1f8f14fdff71ac7757e0e5`.
- Resolved VeriHyMem dependency: `e3897688e448b34e914fe95d8aaabe7eca59b303`.
  This is the integrated branch's pinned dependency, not the newer sibling
  repository used in some memory microbenchmarks.
- Both builds: `nightly-2024-05-05`, AArch64 `qemu-gicv3`, release, `LOG=error`,
  no benchmark feature. The controlled experiment described below applies the
  same explicit per-CPU area increase to both build snapshots; the original
  source branches and their pinned dependencies remain unchanged.
- QEMU 8.2.7: four Cortex-A72 CPUs, 2 GiB machine RAM; root Linux owns two vCPUs
  and reports approximately 983 MiB usable RAM after reserved memory.
- Redis 7.2.5, built once for AArch64 with musl 1.2.5, `MALLOC=libc`, `-O2`.
  RDB and AOF are disabled. `maxmemory=256mb`, `noeviction`, one Redis I/O thread.
  Redis runs on guest CPU 0. The process-startup observer runs on guest CPU 1.
- Request generator: native memtier 2.5.1, one thread, eight connections, pipeline one,
  fixed default random seed, 65,536 existing keys (`bench:1` through
  `bench:65536`), 1,024-byte values. The dataset contains 64 MiB of value data,
  plus Redis overhead. GET misses and evictions invalidate a run.
- QEMU vCPU threads 0–3 are pinned to host logical CPUs 0–3. Its other initial
  threads use CPUs 4–5; later threads inherit their parent's affinity. The
  client uses CPUs 6–7. Per-VM thread mappings are saved. Host activity and WSL
  scheduling remain sources of variance; WSL CPU IDs do not establish fixed
  assignments to physical host cores.
- A shared read-only base image and a fresh QEMU disk snapshot for every boot.
  Host file caches are warm; no host or guest cache-dropping operation is used.
  Process-startup samples use a warm executable after the boot Redis has run.

Redis traffic uses an identical virtio MMIO network device at `0xa003a00`
(IRQ 77) in both variants, through QEMU user networking. The original PCI
virtio-net path stalls while Linux 5.4 waits for a control-queue completion.
This experiment therefore does not evaluate the PCI/SMMU network data path.
The original three PCI devices remain present and idle in both variants.
Only localhost `127.0.0.1:16379` is forwarded to the test service.

The runner requires memtier 2.4.4 or later. Earlier exploratory runs with 2.1.4
exposed its timestamp-averaging bug: the JSON duration could lose or gain a
second, corrupting throughput. The [upstream 2.4.4 release](https://github.com/redis/memtier_benchmark/releases/tag/2.4.4)
fixes this issue. Those early smoke results are diagnostic artifacts and must
not be used as performance evidence.

## Prepare

Run commands from the repository root. Large sources, images, binaries and raw
results are stored under the ignored `target/redis-bench/` directory. The build
scripts do not install system packages or change the current branch.

Required host tools include QEMU AArch64, the repository's Rust nightly and
Kconfig environment, `rust-objcopy`, `rust-nm`, `readelf`, `mkimage`, `dtc`,
Python 3 with `tarfile.extractall(filter="data")` support, `debugfs`,
Clang/LLVM 14, `ld.lld`, CMake, Ninja, GCC/G++, Make, Git, `pkg-config`,
`dpkg-deb`, m4, Perl, curl and unzip. The
Redis build also needs host OpenSSL/zlib development files. `build-redis.sh`
downloads additional build tools into its private directory when memtier is
requested. All third-party source/package downloads have pinned SHA256 checks.
The hvisor builder uses `cargo build --offline --locked`: both refs' locked
crate and Git dependencies must already be cached. On a new machine, fetch
the exact locked dependencies for each ref before using the offline builder.

```bash
python3 tools/redis-bench/build-hvisor.py \
  --output target/redis-bench/build-stack1m --per-cpu-size 1048576
bash tools/redis-bench/build-redis.sh target/redis-bench/redis-build --memtier

mkdir -p target/redis-bench/assets
curl --fail --location --output target/redis-bench/assets/rootfs1.zip \
  https://github.com/CHonghaohao/hvisor_env_img/releases/download/v2025.04.11/rootfs1.zip
curl --fail --location --output target/redis-bench/assets/Image \
  https://github.com/CHonghaohao/hvisor_env_img/releases/download/v2025.04.11/Image
sha256sum target/redis-bench/assets/rootfs1.zip target/redis-bench/assets/Image
unzip -n target/redis-bench/assets/rootfs1.zip -d target/redis-bench/assets

target/redis-bench/redis-build/aarch64-musl-clang \
  -std=c11 -O2 -Wall -Wextra -Werror -static \
  tools/redis-bench/guest_startup.c \
  -o target/redis-bench/redis-build/bin/aarch64/guest-startup
target/redis-bench/redis-build/aarch64-musl-clang \
  -std=c11 -O2 -Wall -Wextra -Werror -static \
  tools/redis-bench/guest_net.c \
  -o target/redis-bench/redis-build/bin/aarch64/guest_net

python3 tools/redis-bench/prepare_guest.py \
  --output target/redis-bench/assets/rootfs-redis-final.ext4
```

Expected guest download SHA256 values:

```text
94c328ce59d6d33031a43d6ffc8740684d3da4077172dd505ed52dab932426ea  rootfs1.zip
b6fe6ae72bf03e6622486ada3acd84d6c22244425bcf041c403037d1361081d2  Image
```

`prepare_guest.py` copies the original disk, writes the Redis files with
`debugfs`, and compiles the same DTS with an explicit guest init script. It
does not mount a filesystem or modify the original disk. It refuses to
overwrite an existing output. The Redis configuration disables authentication
for this isolated local benchmark; it is not a production deployment template.

Existing hvisor builds can be validated and reused with
`python3 tools/redis-bench/build-hvisor.py --output target/redis-bench/build-stack1m --per-cpu-size 1048576 --reuse`. It checks source identity,
compiler, build options and artifact hashes. Builds set
`CARGO_ENCODED_RUSTFLAGS` explicitly: Cargo would otherwise merge ancestor and
snapshot configuration and apply the linker script twice.

The pinned integrated build overflows its original 512 KiB per-CPU area during
parking page-table initialization: nested frames consume at least 560,624 bytes
before allocator callees. A GDB capture found CPU 2 at the predicted stack
pointer `0x406f8c40`, with LR and `ELR_EL2` both zero and `ESR_EL2=0x86000010`.
The allocator moves a large bitmap through stack temporaries, overwriting the
preceding CPU's return context. Both controlled builds therefore use a 1 MiB
per-CPU area. This adds 2 MiB of reserved hypervisor memory across four CPUs;
guest RAM stays fixed. The build manifest records the exact patch. This is a
workaround for the experiment, not an allocator implementation fix. Results
must identify this adjustment and must not describe the binaries as unmodified.
Original failures and the debugger capture remain under `target/redis-bench/`.

## Run and summarize

```bash
# Short end-to-end validation; do not use these numbers as final results.
python3 tools/redis-bench/run.py --output target/redis-bench/results/smoke \
  --build-root target/redis-bench/build-stack1m \
  --startup-runs 1 --process-runs 3 --capacity-runs 1 --capacity-seconds 5 \
  --request-runs 1 --seconds 5 --warmup 3 --keys 4096

# Full paired experiment.
python3 tools/redis-bench/run.py --output target/redis-bench/results/full \
  --build-root target/redis-bench/build-stack1m \
  --capacity-runs 4 --request-runs 6
python3 tools/redis-bench/summarize.py target/redis-bench/results/full
```

The full run uses 30 fresh boots per variant and 30 guest process startups,
distributed across those boots. Variants alternate AB/BA between pairs. It
then collects four uncapped request runs of 10 seconds for each workload and
variant, followed by six fixed-rate runs of 30 seconds each. Every request
run has a separate five-second warmup. The workload order rotates between
replicates. The command above balances AB/BA ordering in both phases (the CLI
defaults remain three and five runs). It takes roughly tens of minutes under TCG.

The `capacity` output phase means **uncapped throughput at the configured eight
connections and pipeline one**, not a demonstrated maximum server throughput.
For each workload, the fixed-rate phase uses half the lowest observed uncapped
throughput across both variants. memtier's rate limit is **per connection**:
the requested total rate is the configured integer limit times eight.
The same target is applied to both variants. Final samples must achieve
within 5% of it; otherwise the run fails rather than presenting an underloaded
server as having better latency. memtier's generator is connection based and
does not establish arbitrary open-loop overload behavior.

All timing, preload and teardown boundaries are explicit:

- System readiness probing starts immediately after sending `bootm`, before
  waiting for a guest serial marker. Start and end timestamps come from the
  host monotonic clock. A 1 ms retry delay and a 50 ms socket-attempt timeout
  are recorded; the retry delay is not a guaranteed measurement resolution.
- Guest process startup uses one guest monotonic clock. Configuration and
  temporary-directory preparation precede timing; fork, executable loading,
  Redis initialization and first PING are included. INFO PID and SET/GET
  validation and process teardown follow timing. The helper owns and reaps
  only the process it launched.
- Dataset loading and warmup are outside request measurements. SET overwrites
  existing keys. Every GET must hit. Server errors, evictions, changed key
  counts, client failures and incomplete runs fail validation.
- Raw client JSON, logs, full command lines, pre/post Redis INFO snapshots,
  guest console logs, source snapshots and binary/configuration hashes are
  retained. A VM must have both guest CPUs online. Degraded single-CPU boots
  and fatal/timeout boots are recorded as `boot_attempt_failure`. The default
  allows at most five attempts for each scheduled VM (`--boot-attempts`). Their
  service-readiness times and logs are retained; healthy-boot latency is
  conditional on a valid two-CPU boot and must be reported alongside these
  failure counts. Request errors are not retried or counted as successes.

`samples.jsonl` contains the observations; `metadata.json` identifies the exact
environment. Summaries should distinguish mean per-run P99 from a pooled
request P99 and include uncertainty across independent runs. A few percent
difference in a noisy emulated setup is not evidence of a few percent cost on
a real board. Re-run the same workloads on hardware for that conclusion.

In this hvisor configuration, guest RAM receives its stage-2 mappings when the
VM is created. Redis allocations normally use the guest Linux allocator and
stage-1 page tables; they do not call hvisor's software `alloc`/`map` functions
on every request. Steady-state results test the whole application path, while
the existing memory microbenchmarks isolate the software memory operations.
Neither experiment replaces the other.

The two hvisor revisions also differ in zone ownership and metadata access,
including interrupt-controller permission checks. These measurements compare
the complete integrated revisions; they cannot attribute a throughput or
latency difference solely to allocator or page-table operations.

The startup helper's protocol, failure and process-cleanup checks can be run
with `python3 tools/redis-bench/test_guest_startup.py`. They require permission
to open local sockets.
