# Host Criterion region and zone-memory benchmarks

Run the current branch's pinned VeriHyMem implementation directly on the host:

```bash
make membench-criterion
make membench-criterion REGIONS=100 REGION_PAGES=1 ZONE_REGIONS=4

# Criterion filters and options can be passed through the runner.
python3 tools/memory-bench/run.py region/
python3 tools/memory-bench/run.py --sample-size 20 --warm-up-time 1 --measurement-time 2
```

This standalone crate keeps `std` and Criterion out of the hypervisor build. It
uses Rust **1.95.0**, Criterion **0.8.2**, and the same benchmark optimization
profile as the allocator/page-table host benchmarks on `baseline-performance`.
The VeriHyMem revision is **e389768**, matching this branch's `Cargo.toml`;
the runner uses the checked-in lockfile and an explicit host target.

The default settings are 100 samples, 3 seconds of warmup, 10 seconds of
measurement per case, and 95% confidence intervals. Criterion adjusts the
iteration count; the bare-metal `ROUNDS` setting does not apply here. On Linux,
the runner pins the benchmark child to the first CPU allowed by its affinity
mask. Set `BENCH_CPU` to choose another allowed CPU. Build processes and the
calling shell keep their original affinity.

| Parameter | Default | Range | Meaning |
| --- | ---: | --- | --- |
| `REGIONS` | 100 | 1–4096 | Insertions/removals per region batch |
| `REGION_PAGES` | 1 | 1–32768 | 4 KiB pages per region |
| `ZONE_REGIONS` | 1 | 1–64 | RAM regions in each zone-memory case |

Both `REGIONS * REGION_PAGES` and `ZONE_REGIONS * REGION_PAGES` must be at
most 32768. Leaf mappings use the same guest/physical base addresses as
`make membench`. Page-table frames have real, aligned host backing memory,
committed before timing. Huge pages are disabled so all mappings use 4 KiB
entries, matching the allocator/page-table host benchmarks. Each round starts
with a fresh zone at ID 1; CPU and IOMMU RAM mappings are read/write.

## Timed operations

| Case | Work inside the timer | Operations per Criterion iteration |
| --- | --- | ---: |
| `region/insert` | `HvMem::insert_region` for each region | `REGIONS` |
| `region/remove` | `HvMem::remove_region` for each region | `REGIONS` |
| `zone_memory/create` | `HvMem::add_zone`, then CPU and IOMMU RAM mappings | 1 |
| `zone_memory/remove` | Clear CPU/IOMMU mappings, then `HvMem::remove_zone` | 1 |

The adapter invokes the dependency's actual allocator, locking, region lookup,
mapping, unmapping, and page-table reclamation code. It supplies an empty zone
payload and replaces privileged MMU/SMMU instructions with host no-ops. These
are **software memory-management measurements**. They exclude hvisor's
`VMemorySet` compatibility conversion, GIC/PCI/IVC initialization, GITS mappings,
cache maintenance, hardware TLB invalidation, and guest startup/shutdown. They
do not measure the complete `src/zone.rs::zone_create` function.

Use [`make membench`](../membench.md) for complete zone construction and removal
at EL2. Its measurements include the emulated hardware path and should be kept
separate from the host Criterion results.

For region cases, empty-zone creation and final destruction are outside the
timer. For zone cases, the corresponding setup or cleanup operation is outside
the timer. Both kinds keep input construction, result checks, mapping checks,
and resource-recovery checks outside measured intervals. The fixture checks
both region metadata and actual page-table translations, and verifies that all
page-table frames have returned before and after each group.

## Results and comparisons

Output is written under `tools/memory-bench/target/`:

- `membench-criterion.log`: build and Criterion output.
- `membench-environment.json`: source revision/state, toolchain, arguments,
  workload, CPU information, and affinity.
- `membench-summary.json`: measured means and confidence intervals in **ns/op**.
- `criterion/`: Criterion estimates, samples, and HTML reports.

Criterion's raw region `time:` is **per batch**, while its throughput is in
operations per second. The runner divides region estimates and confidence
intervals by `REGIONS` when producing the ns/op summary. Zone-memory estimates
already represent one operation. The summary consistently uses Criterion's
`mean` estimate; the raw console `time:` can instead show a regression slope
when sampling is linear. Filtered or validation-only runs must not reuse stale
results from previous cases.

Compare runs with identical implementations, parameters, toolchains,
optimization settings, and hardware scope. Use distributions and confidence
intervals rather than comparing a historical minimum with a later average.
CPU affinity and repeated sampling reduce noise; they do not make host load or
frequency variation disappear.

The old `baseline-performance` bare-metal region benchmark calls `MemorySet`
directly. This branch calls `VMemorySet` and `HvMem`, with additional locking,
region checks, and hardware-maintenance work. The zone benchmark runs after all
region measurements, so its elapsed time is not included in region results.
The observed cross-branch gap alone does not establish a regression caused by
adding zone cases. A causal before/after comparison requires the same backend
and controlled repeated runs.
