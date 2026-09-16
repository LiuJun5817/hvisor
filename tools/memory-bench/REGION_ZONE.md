# Host Criterion region and zone-memory benchmarks

Run the baseline memory implementation directly on the host:

```bash
make membench-criterion

# Repeat the defaults explicitly: 32 background regions and a 4 MiB target.
make membench-criterion PREFILL_REGIONS=32 PREFILL_REGION_PAGES=1024 REGION_PAGES=1024

# Keep 32 background regions, with smaller background mappings.
make membench-criterion PREFILL_REGIONS=32 PREFILL_REGION_PAGES=1 REGION_PAGES=1024

# Select a case or override Criterion's sampling settings.
python3 tools/memory-bench/run.py region/
python3 tools/memory-bench/run.py zone_memory/
python3 tools/memory-bench/run.py --sample-size 20 --warm-up-time 1 --measurement-time 2
```

The standalone crate reuses this branch's production `MemorySet`, AArch64
stage-2 page table, and frame allocator. Existing allocator/page-table tests
remain available through `make bench_memory_ops` and `make compare_memory_ops`.

## Shared measurement settings

The runner retains `performance-test`'s measurement settings: Rust **1.95.0**,
Criterion **0.8.2**, the same Criterion dependency graph, and `opt-level=3`,
thin LTO, one codegen unit, and disabled debug assertions/overflow checks/
incremental compilation.

Each region measurement uses 32 background regions by default and times one
operation on a 1024-page (4 MiB) target. Insertion changes the region count
from 32 to 33. Removal setup inserts that same target after the background
regions; the timed removal deletes the last-inserted target and changes the
count from 33 to 32. Background regions also contain 1024 pages by default;
their size can be changed independently. Zone-memory cases create and remove
an empty zone, with no RAM mappings.

The old `REGIONS`, `ZONE_REGIONS`, and `ZONE_REGION_PAGES` environment variables
are rejected, including when inherited from the shell. Unset them and use the
parameters below. `PREFILL_REGIONS` specifies the background region count; it
excludes the target and is not the number of timed operations. The previous
batch benchmark cannot be reproduced by simply renaming `REGIONS`, and its
results measure a different workload.

Defaults are 100 samples, 3 seconds of warmup, 10 seconds of measurement per
case, and 95% confidence intervals. Criterion chooses the iteration count;
the bare-metal `ROUNDS` setting does not apply. On Linux the benchmark child
is pinned to the first allowed CPU. Set `BENCH_CPU` to select another allowed
CPU. Build processes and the parent shell keep their original affinity.

| Parameter | Default | Range | Meaning |
| --- | ---: | --- | --- |
| `PREFILL_REGIONS` | 32 | 1–4096 | Background regions, excluding the target inserted or removed |
| `PREFILL_REGION_PAGES` | 1024 | 1–32768 | 4 KiB pages in each background region |
| `REGION_PAGES` | 1024 | 1–32768 | 4 KiB pages in the one region being inserted or removed |

All parameters are positive integers. Mappings occupy fixed address slots with
stride `max(PREFILL_REGION_PAGES, REGION_PAGES)` pages, so background and target
mappings cannot overlap. The following must hold:

```text
PREFILL_REGIONS * max(PREFILL_REGION_PAGES, REGION_PAGES) + REGION_PAGES <= 65536
```

This 256 MiB limit bounds the leaf mapping address range, including gaps between
slots; it is not the size of the host frame pool. Defaults occupy 128 MiB before
insertion and 132 MiB afterward. Guest and leaf physical bases are `0x10000000`
and `0x60000000`. Leaf data is never allocated or dereferenced by the benchmark.
Page-table frames use 1,024 real, aligned host pages committed before timing.
Three-level page tables and `NO_HUGEPAGES` ensure 4 KiB entries, matching
`huge_pages=false` on `performance-test`. Region mappings are read/write.

## Timed operations

| Case | Work inside the timer | Operations per Criterion iteration |
| --- | --- | ---: |
| `region/insert` | One native `MemorySet::insert` after pre-filling the set | 1 |
| `region/remove/after_insert` | One native `MemorySet::delete` of the last-inserted target | 1 |
| `zone_memory/create` | Construct empty CPU/IOMMU memory sets and register their owner | 1 |
| `zone_memory/remove` | Unregister the empty owner and destroy both memory sets/page tables | 1 |

For insertion, setup creates `PREFILL_REGIONS` background mappings in slots
`0..PREFILL_REGIONS`. The timed operation inserts the target into the next slot.
For removal, setup creates the same `PREFILL_REGIONS` background mappings and
then inserts the same target into slot `PREFILL_REGIONS`, all outside timing.
The timed operation removes only that last-inserted target, leaving all
background regions in place. Thus removal starts with `PREFILL_REGIONS + 1`
regions and ends with `PREFILL_REGIONS` regions.

Each region iteration rebuilds and populates the entire memory set before
starting the clock. Only the target insert or remove is timed. Input
construction, setup, result checks, mapping validation, and final destruction
are outside the interval. In particular, insertion does not remove and reinsert
the target into a reused set: baseline removal retains intermediate page tables,
which would omit their allocation cost from later insertions. Clock overhead
within each interval remains part of the reported time.

Region operations borrow the CPU memory set mutably. The adapter adds no
per-operation `RefCell`, registry lookup, or lock; the implementation's own
page-table and allocator locks remain. Empty-set setup and final destruction
are outside region timing. The unused IOMMU root is also created outside
timing, matching the two-root fixture used by the other branch.

The zone-memory fixture is a reduced owner of two real memory sets. Its
`RwLock<Vec<Arc<...>>>` registry and per-owner lock follow the baseline zone
ownership structure. Creation includes registration; removal holds the registry
write lock while removing the final `Arc` and running native destructors.
An empty zone still allocates the CPU and IOMMU root page tables and the owner,
and performs registration. It has no RAM regions or intermediate page tables;
the empty-zone cases do not measure populated-zone teardown.
This benchmark-only owner omits the non-memory integration payload. It does
**not** call the complete `src/zone.rs::zone_create` function. GIC/PCI/IVC,
GITS mappings, cache/TLB maintenance, and guest execution are excluded. Use
[`make membench`](../membench.md) for the complete EL2 path.

The other branch uses real `HvMem` zone registration and its CPU/IOMMU memory
sets with an empty integration payload. This branch uses the reduced baseline
owner described above; it does not substitute an HvMem implementation. Native
locking, metadata storage, and reclamation differences remain part of the
comparison. In particular, baseline region removal retains intermediate page
tables until memory-set destruction, which occurs outside region timing.

The timing loop preserves native results and consumes them after the timer
stops. Checks cover metadata, actual page-table translations, and recovery of
all allocator frames before and after each group.

## Results

Output is written under `tools/memory-bench/target/`:

- `membench-criterion.log`: build and Criterion output.
- `membench-environment.json`: source state, toolchain, workload, CPU, and affinity.
- `membench-summary.json`: means and confidence intervals in **ns/op**.
- `criterion/`: Criterion samples, estimates, and HTML reports.

The default benchmark IDs are:

```text
region/insert/prefill_32_regions_1024_pages/target_1024_pages
region/remove/after_insert/prefill_32_regions_1024_pages/target_1024_pages
zone_memory/create/empty
zone_memory/remove/empty
```

The `after_insert` component distinguishes this removal workload from the
earlier case that removed a selected region from a total of 32 regions.

Every measured interval performs **one** operation. The 32 background regions
and the target's 1024 pages are not divisors: the runner reports ns per target
region operation or empty-zone lifecycle operation. Criterion's raw `time:` is
therefore already per operation. The runner verifies `Throughput::Elements(1)`
before reporting the mean and confidence interval in ns/op.
It consistently uses the arithmetic `mean` estimate; the raw console can show
a regression slope with linear sampling. Filtered and validation-only runs do
not reuse stale estimates.

Compare matching workloads, toolchains, build flags, CPU affinity, and host
conditions. Both implementations must use the same background count and size,
target size, slot layout, last-inserted removal target, state reconstruction,
and empty-zone semantics. A branch still using batch insertion, removal from
32 total regions, or populated zone creation needs the corresponding benchmark
changes before its results are comparable. Host results exclude hardware costs
and should be kept separate from QEMU timings. Existing allocator/page-table
paired comparisons retain their separate runner and report format.

Criterion's automatic `change` section compares with data already stored in
the output directory, which can belong to a different branch. Keep each run's
environment and results together when comparing branches; that message alone
does not establish a speedup caused by a code change within one implementation.
