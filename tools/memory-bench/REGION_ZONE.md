# Host Criterion region and zone-memory benchmarks

Run the baseline memory implementation directly on the host:

```bash
make membench-criterion

# Measure a growing set of 100 regions instead of the default first insertion.
make membench-criterion REGIONS=100 REGION_PAGES=1 ZONE_REGIONS=4

# Select a case or override Criterion's sampling settings.
python3 tools/memory-bench/run.py region/
python3 tools/memory-bench/run.py --sample-size 20 --warm-up-time 1 --measurement-time 2
```

The standalone crate reuses this branch's production `MemorySet`, AArch64
stage-2 page table, and frame allocator. Existing allocator/page-table tests
remain available through `make bench_memory_ops` and `make compare_memory_ops`.

## Shared measurement settings

The runner follows `performance-test`'s `tools/memory-bench/run.py`, with a
default of one region. Set `REGIONS=1` explicitly in both repositories when
comparing with a branch that still defaults to 100 regions.
Both use Rust **1.95.0**, Criterion **0.8.2**, the same Criterion dependency
graph, and `opt-level=3`, thin LTO, one codegen unit, and disabled debug
assertions/overflow checks/incremental compilation.

Defaults are 100 samples, 3 seconds of warmup, 10 seconds of measurement per
case, and 95% confidence intervals. Criterion chooses the iteration count;
the bare-metal `ROUNDS` setting does not apply. On Linux the benchmark child
is pinned to the first allowed CPU. Set `BENCH_CPU` to select another allowed
CPU. Build processes and the parent shell keep their original affinity.

| Parameter | Default | Range | Meaning |
| --- | ---: | --- | --- |
| `REGIONS` | 1 | 1–4096 | Operations per region batch, shared by insert and remove |
| `REGION_PAGES` | 1 | 1–32768 | 4 KiB pages per region |
| `ZONE_REGIONS` | 1 | 1–64 | RAM regions in each zone-memory case |

Both `REGIONS * REGION_PAGES` and `ZONE_REGIONS * REGION_PAGES` must be at
most 32768. Guest and leaf physical bases are `0x10000000` and `0x60000000`.
Leaf data is never dereferenced. Page-table frames use 1,024 real, aligned host
pages committed before timing. Three-level page tables and `NO_HUGEPAGES`
ensure 4 KiB entries, matching `huge_pages=false` on `performance-test`.
CPU and IOMMU mappings are read/write.

The default `region/insert` case measures the first insertion into an empty CPU memory
set: the root already exists, but intermediate page-table allocation is timed.
With `REGION_PAGES=1` and `REGIONS=100`, that allocation is amortized across 100
insertions into the same set. These are different workloads, so keep both when
studying scaling.
One region also makes the per-interval clock and loop overhead more visible;
the reported time includes that overhead. Zone creation still constructs and
populates both CPU and IOMMU sets, so its work is not identical to one insertion.

## Timed operations

| Case | Work inside the timer | Operations per Criterion iteration |
| --- | --- | ---: |
| `region/insert` | Native `MemorySet::insert` for every region | `REGIONS` |
| `region/remove` | Native `MemorySet::delete` for every region | `REGIONS` |
| `zone_memory/create` | Construct CPU/IOMMU memory sets, map RAM, register their owner | 1 |
| `zone_memory/remove` | Unregister the owner and destroy both memory sets/page tables | 1 |

Region operations borrow the CPU memory set mutably. The adapter adds no
per-operation `RefCell`, registry lookup, or lock; the implementation's own
page-table and allocator locks remain. Empty-set setup and final destruction
are outside region timing. The unused IOMMU root is also created outside
timing, matching the two-root fixture used by the other branch.

The zone-memory fixture is a reduced owner of two real memory sets. Its
`RwLock<Vec<Arc<...>>>` registry and per-owner lock follow the baseline zone
ownership structure. Creation includes registration; removal holds the registry
write lock while removing the final `Arc` and running native destructors.
This benchmark-only owner omits the non-memory integration payload. It does
**not** call the complete `src/zone.rs::zone_create` function. GIC/PCI/IVC,
GITS mappings, cache/TLB maintenance, and guest execution are excluded. Use
[`make membench`](../membench.md) for the complete EL2 path.

The other branch uses real `HvMem` zone registration and its CPU/IOMMU memory
sets with an empty integration payload. This branch uses the reduced baseline
owner described above; it does not substitute an HvMem implementation. Native
locking, metadata storage, and reclamation differences remain part of the
comparison. In particular, baseline region removal retains intermediate page
tables until memory-set destruction; zone-memory removal includes that final
destruction.

The timing loop follows `performance-test`, with API adaptations for mutable
baseline memory sets and native non-Clone results. Results are written directly
to preallocated uninitialized slots, then checked and consumed outside timing;
no Result/Option wrapper or result destructor is added inside the measured
loop. Input construction, validation, and the opposite lifecycle operation
are outside each measured interval. Checks cover metadata, actual page-table
translations, and recovery of all allocator frames before and after each group.

## Results

Output is written under `tools/memory-bench/target/`:

- `membench-criterion.log`: build and Criterion output.
- `membench-environment.json`: source state, toolchain, workload, CPU, and affinity.
- `membench-summary.json`: means and confidence intervals in **ns/op**.
- `criterion/`: Criterion samples, estimates, and HTML reports.

Criterion's raw region `time:` describes **one batch of `REGIONS` operations**.
With the default `REGIONS=1`, a batch contains one operation, so no division by
100 is needed. For an explicit 100-region run, 63 µs is about 630 ns per
insertion. A zone-memory
iteration performs **one** create or remove, so its raw time is already per
operation. The runner normalizes both means and confidence intervals to ns/op.
It consistently uses the arithmetic `mean` estimate; the raw console can show
a regression slope with linear sampling. Filtered and validation-only runs do
not reuse stale estimates.

Compare matching workloads, toolchains, build flags, CPU affinity, and host
conditions. Host results exclude hardware costs and should be kept separate
from QEMU timings. Existing allocator/page-table paired comparisons retain
their separate runner and report format.

Criterion's automatic `change` section compares with data already stored in
the output directory, which can belong to a different branch. Keep each run's
environment and results together when comparing branches; that message alone
does not establish a speedup caused by a code change within one implementation.
