# Bare-metal memory benchmarks

Run both region and zone workloads inside hvisor at EL2 on QEMU:

```bash
make membench
make membench REGIONS=100 REGION_PAGES=1 ZONE_REGIONS=4 ROUNDS=500
```

The runner supports `aarch64/qemu-gicv3` in release mode, sets `LOG=error`,
and uses the existing bare-metal test runner. No guest is started.

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

The region workload starts each round with a fresh stage-2 memory set.
`insert` measures inserting all configured regions; `remove` measures deleting
them. Memory-set creation, mapping checks, and final destruction are outside
the timers.

The zone workload creates and removes one zone per round. `create` measures
`zone_create(&config)`; configuration construction, mapping checks, and
`add_zone` registration are outside the timer. `remove` measures `remove_zone`,
including the final `Arc` and zone destruction, unmapping, and release of the
guest and IOMMU page tables. No other `Arc` references remain during removal.

The fixture uses zone ID 1, no CPUs, an empty IRQ bitmap, and no PCI devices or
IVC channels. It uses the board's GIC configuration, so creation includes GIC
MMIO registration, guest RAM cache maintenance, and IOMMU mappings when IOMMU
is enabled (as in the default qemu-gicv3 configuration). The empty IVC metadata
record created by zone initialization is removed outside the timer after each
round. These measurements cover zone object creation and removal; they do not
include guest boot or the shutdown hypercall.

Both workloads perform one unmeasured warmup round, mask interrupts during the
benchmark loops, and use the ARM physical timer counter. Validation runs outside
the timers. Results report total microseconds and arithmetic mean nanoseconds
per operation; timer ticks are not CPU cycles. Under QEMU these are measurements
of the emulated environment.

The complete build and QEMU log is saved to `target/bench_region.log`.
`target/bench_region.txt` contains both the `Region Benchmark` and
`Zone Benchmark` sections. The script requires both completion markers before
reporting success and limits the test run to 300 seconds.
