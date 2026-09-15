# Bare-metal memory benchmarks

Run both region and zone workloads inside hvisor at EL2 on QEMU:

```bash
make membench
make membench REGIONS=100 REGION_PAGES=1 ZONE_REGIONS=4 ROUNDS=500
```

The runner supports `aarch64/qemu-gicv3` in release mode, sets `LOG=error`,
and uses the existing bare-metal test runner. No guest is started.

For host sampling with Criterion and confidence intervals, use
[`make membench-criterion`](memory-bench/README.md). Those benchmarks isolate
region and zone memory-management software; this runner retains the complete
EL2 zone initialization and hardware-maintenance path.

| Parameter | Default | Allowed range | Meaning |
| --- | ---: | --- | --- |
| `REGIONS` | 100 | 1–4096 | Regions inserted and removed per region round |
| `REGION_PAGES` | 1 | 1–32768 | 4 KiB pages per region in both workloads |
| `ZONE_REGIONS` | 1 | 1–64 | RAM regions configured in each benchmark zone |
| `ROUNDS` | 500 | 1–10000 | Measured rounds for each workload |

Each workload has its own 128 MiB RAM limit:
`REGIONS * REGION_PAGES <= 32768` and
`ZONE_REGIONS * REGION_PAGES <= 32768`. The workloads run sequentially and
reuse exclusive test RAM at `0x60000000..0x68000000`.

The region workload starts each round with a fresh, empty HvMem zone.
`insert` measures inserting all configured regions; `remove` measures deleting
them. Zone registration, mapping checks, and final zone destruction are outside
the timers.

The zone workload creates and removes one zone per round. `create` measures
`zone_create(&config)`, including the registration that this API performs inside
HvMem. Configuration construction and mapping checks are outside the timer.
`remove` measures `remove_zone`, including clearing mappings, releasing the
guest and IOMMU page tables, and destroying the zone payload. Here `Zone` is a
copyable ID handle; its removal does not depend on dropping an `Arc` reference.

The fixture uses zone ID 1, no CPUs, an empty IRQ bitmap, and no PCI devices or
IVC channels. It uses the board's GIC configuration, so creation includes GIC
MMIO registration, guest RAM cache maintenance, and IOMMU mappings when IOMMU
is enabled (as in the default qemu-gicv3 configuration). The empty IVC metadata
record created by zone initialization is removed outside the timer after each
round. These measurements cover zone object creation and removal; they do not
include guest boot or the shutdown hypercall.

Both workloads perform one unmeasured warmup round, mask interrupts during the
benchmark loops, and use the ARM physical timer counter. Validation runs outside
the timers. `VMemorySet::page_table_query` validates region metadata rather than
walking hardware page tables. Results report total microseconds and arithmetic
mean nanoseconds per operation; timer ticks are not CPU cycles. Under QEMU these
are measurements of the emulated environment.

For comparisons with `baseline-performance`, use the same parameters, Rust
toolchain (`nightly-2024-05-05`), QEMU executable, and test runner. The runner,
warmup, timing, and statistics follow that branch's benchmark. One native API
difference affects the timed scope: this branch's `zone_create` includes HvMem
registration, while the baseline performs its separate `add_zone` call outside
the create timer.

The complete build and QEMU log is saved to `target/bench_region.log`.
`target/bench_region.txt` contains both the `Region Benchmark` and
`Zone Benchmark` sections. The script requires both completion markers before
reporting success and limits the test run to 300 seconds.
