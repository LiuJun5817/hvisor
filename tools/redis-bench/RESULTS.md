# Redis application experiment — 2026-09-16

All three experiments completed in AArch64 QEMU TCG on an x86_64 WSL2 host.
These results apply to the two explicitly patched builds below, not to native
AArch64 hardware. The full run took approximately 36 minutes.

The observed mean system-startup increase was **4.16%**. Redis process startup
and throughput comparisons have confidence intervals spanning zero. SET tail
latency needs further investigation: its mean run P99 increased **13.88%**,
with a wide interval. This experiment does not establish negligible overhead
or equivalence across all metrics.

## Builds and shared configuration

| Component | Version / configuration |
| --- | --- |
| Baseline hvisor | `d2a078c40979122a30999651553ecaca3b5371db` |
| Integrated hvisor | `df547ceda31ec3d41d1f8f14fdff71ac7757e0e5` |
| Pinned VeriHyMem | `e3897688e448b34e914fe95d8aaabe7eca59b303` |
| Both hvisor builds | `nightly-2024-05-05`, release, AArch64 `qemu-gicv3`, `LOG=error`, no benchmark feature |
| Common source adjustment | `PER_CPU_SIZE`: 512 KiB → 1 MiB in both isolated snapshots |
| VM | QEMU 8.2.7, Cortex-A72, four machine CPUs; root Linux owns two vCPUs |
| Redis | 7.2.5, static musl 1.2.5, `MALLOC=libc`, persistence disabled, guest CPU 0 |
| Client | Native memtier 2.5.1, one thread, eight connections, pipeline one |
| Request dataset | 65,536 existing keys, 1,024-byte values; SET overwrites existing keys |
| Networking | Identical virtio-MMIO device and QEMU user networking |

The original integrated build overflows its 512 KiB per-CPU area during parking
page-table initialization. The [boot diagnosis](BOOT_DIAGNOSIS.md) records the
compiled stack frames and GDB evidence. Both benchmark snapshots therefore
reserve 1 MiB per CPU, adding 2 MiB total hypervisor reservation in each build
while keeping guest memory fixed. This accommodates the observed stack usage;
it does not change the pinned allocator implementation. Original branches,
builds, and failed-run logs remain intact. Original failed runs and exploratory
smoke runs are excluded from the results below.

Packaged hvisor image SHA256:

```text
9000218eb16d2cdeef62f26718062212f6a64c69af5f7a29a02b9a6899559958  baseline
dcc98d72be10dc0e08301c8f9a1bead76d835d8168d28f2e6fbc77c1b6528c28  verified
```

## Startup

Each measurement has 30 paired samples from fresh VMs. System startup starts
when the host sends U-Boot `bootm` and ends at the first successful remote
Redis PING. It includes hvisor, root Linux zone0, networking and empty Redis
startup, excluding preceding QEMU/firmware startup. It is not a dynamic
non-root zone launch.

Process startup runs inside the already booted VM, with a warm executable,
from immediately before `fork` through executable loading and initialization
to the first local PING. SET/GET correctness checks follow both timers.

| Measurement | Baseline mean | VeriHyMem mean | Change | 95% CI of change |
| --- | ---: | ---: | ---: | ---: |
| hvisor + root VM → Redis ready | 1962.010 ms | 2043.719 ms | +4.16% | +1.08% to +7.97% |
| Redis startup in running VM | 60.829 ms | 62.424 ms | +2.62% | −5.80% to +13.03% |

The process-startup medians were 58.971 ms and 57.159 ms, respectively. The
verified maximum was 158.857 ms, illustrating why a small difference of means
should be interpreted alongside variation across runs.

## Sustained requests

Uncapped throughput uses four paired 10-second runs per workload, each preceded
by five seconds of warmup. This is throughput at the configured client count,
not a search for maximum server capacity.

| Workload | Baseline requests/s | VeriHyMem requests/s | Change | 95% CI of change |
| --- | ---: | ---: | ---: | ---: |
| GET | 2483.5 | 2571.5 | +3.54% | −11.60% to +16.90% |
| SET overwrite | 2298.4 | 2415.6 | +5.10% | −2.72% to +13.32% |
| 90% GET / 10% SET | 2478.7 | 2417.2 | −2.48% | −9.46% to +8.95% |

Latency uses six paired 30-second runs per workload, again with separate
five-second warmups. The target rate is shared by both variants and selected
at approximately half the lowest observed uncapped throughput. P99 below means
the **mean of the six per-run P99 values**, not a pooled request percentile.

| Workload | Target requests/s | Baseline P99 | VeriHyMem P99 | Change | 95% CI of change |
| --- | ---: | ---: | ---: | ---: | ---: |
| GET | 1056 | 7.396 ms | 7.631 ms | +3.17% | −1.11% to +6.28% |
| SET overwrite | 1088 | 7.567 ms | 8.618 ms | +13.88% | −1.44% to +31.68% |
| 90% GET / 10% SET | 1064 | 8.154 ms | 8.079 ms | −0.92% | −9.25% to +7.53% |

Actual rates across all fixed-load samples were 96.77%–99.80% of target,
within the preconfigured ±5% acceptance range. Mean baseline/verified rates
were 1042.2/1034.7 for GET, 1062.8/1056.6 for SET, and 1036.4/1038.9 for mixed.
The generator is connection based; this is not an arbitrary open-loop
overload test.

## Validation and interpretation

- Each build completed **40/40 healthy VM boots**, with both guest CPUs online:
  30 startup VMs, four throughput VMs and six fixed-load VMs. No retries were
  required in this controlled run.
- All **180 observations** are present: 60 VM startups, 60 Redis process
  startups, 24 throughput samples and 36 fixed-load samples.
- The measured request samples completed **1,716,052 requests**, excluding
  preload and warmup. GET misses, Redis errors, client connection errors and
  evictions were zero; key counts remained unchanged.
- No interrupted/incomplete request sample was accepted. Console inspection
  found no EL2 exception, kernel panic or failed guest CPU startup. The final
  summary reports no validation warnings.
- Variants alternate AB/BA; workload order rotates. Intervals use 10,000 paired
  bootstrap resamples of the ratio of means, seed `20260916`. Positive time
  changes mean slower; positive throughput changes mean faster. An interval
  crossing zero does not prove equivalence or a bound such as overhead ≤5%.

The narrow conclusion is that this configuration showed approximately 82 ms
additional mean system startup time, with no resolved throughput regression
in these samples. The data still permit a material SET tail-latency increase.
More independent repetitions and native AArch64 measurements are needed for a
tighter claim about small overhead, with an acceptable margin chosen in advance.

The integrated revision also changes zone ownership and metadata access,
including interrupt-controller permission checks. This comparison measures
the whole integration. Guest RAM is mapped when the zone is created; Redis
allocations inside that RAM use guest Linux rather than requiring one hvisor
allocation or map operation per request. PCI/SMMU network performance,
persistence, loading a saved dataset, and dynamic non-root VM startup are not
covered by this experiment.

## Reproduce and inspect

See [README.md](README.md) for preparation. The completed command was:

```sh
python3 tools/redis-bench/run.py \
  --output target/redis-bench/results/full-stack1m-20260916 \
  --build-root target/redis-bench/build-stack1m \
  --capacity-runs 4 --request-runs 6
python3 tools/redis-bench/summarize.py \
  target/redis-bench/results/full-stack1m-20260916
```

Use a new output directory to repeat the experiment; the runner refuses to
overwrite existing results. Complete local artifacts:

- [Detailed generated report](../../target/redis-bench/results/full-stack1m-20260916/results.md)
- [Machine-readable summary](../../target/redis-bench/results/full-stack1m-20260916/summary.json)
- [Raw observations](../../target/redis-bench/results/full-stack1m-20260916/samples.jsonl)
- [Environment, hashes and build patches](../../target/redis-bench/results/full-stack1m-20260916/metadata.json)
- [Guest logs, client output and runner snapshot](../../target/redis-bench/results/full-stack1m-20260916/)

Raw artifacts are under ignored `target/`; retain them with any exported or
published result. This document preserves the primary findings in the source
tree.
