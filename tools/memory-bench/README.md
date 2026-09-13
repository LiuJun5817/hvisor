# Matched hvisor / VeriHyMem Memory Operation Benchmarks

Both repositories use identical `benches/memory_ops.rs` files. Each
`support/mod.rs` adapts only the library's native APIs, resource initialization,
and result validation. The comparison runner rejects experiments with mismatched
benchmark files, Criterion dependency versions or checksums, compiled Criterion
features, or build profiles.

## Run a Matched Comparison

Run from the hvisor repository root. Rust **1.95.0**, Python 3.11, and Linux CPU
affinity support are required. The reference repository defaults to
`../verified-hv-mem`.

```sh
make compare_memory_ops

# Explicitly select the reference repository, CPU, and a new output directory.
make compare_memory_ops MEMORY_BENCH_REFERENCE=../verified-hv-mem \
  MEMORY_COMPARE_ARGS='--cpu 0 --output target/memory-bench/comparison-example'

# Equivalent direct invocation; --output must not already exist.
python3.11 tools/memory-bench/compare.py --reference ../verified-hv-mem --cpu 0
```

By default, the runner builds both implementations and runs all smoke checks
before executing two complete pairs sequentially:
**hvisor → VeriHyMem; VeriHyMem → hvisor**. Each benchmark uses 100 samples,
3 seconds of warmup, a 10-second measurement target, and a 95% confidence
interval. Each iteration performs 100 operations. A complete comparison contains
20 benchmark measurements; preparation and validation add to the wall time.
Builds are offline and use locked dependencies. If dependencies are missing,
first run `cargo fetch --locked` with the corresponding toolchain.

The runner enforces matching measurement settings:

- The same specified toolchain, host target, logical CPU, Criterion 0.8.2, and
  complete Criterion dependency graph.
- `opt-level=3`, thin LTO, and one codegen unit, with debug assertions, overflow
  checks, and incremental compilation disabled, using the same explicit build
  environment.
- Builds from the same temporary working directory, with environment overrides
  that could change compiler options cleared. Measurement starts only after
  both builds finish. The actual compiler version and hash, commands, source
  snapshots, and binary hashes are recorded.
- Both implementations export **Criterion mean / 100** and the corresponding
  **95% CI / 100**. The console may display the slope estimate; do not compare
  that value with the exported mean.
- A/B and B/A results and timing ratios are reported separately, without pooling
  samples or combining confidence intervals across runs. The runner checks that
  every case has 100 valid samples, throughput is configured for 100 operations,
  and source files remain unchanged during the experiment. A failed check or
  subprocess prevents generation of the final report.

Output defaults to `target/memory-bench/comparison-<UTC-timestamp>/`:

- `report.md` / `results.csv`: comparisons for each pair, mean time per operation,
  95% confidence intervals, and timing ratios.
- `metadata.json`: environment, compiler options, dependencies, execution order,
  commits, and source hashes.
- `raw/`: Criterion samples, estimates, and HTML reports for each implementation
  and run.
- `logs/` / `sources/`: complete logs and snapshots of the measured source code.

To check or benchmark hvisor alone:

```sh
make bench_memory_ops BENCH_ARGS=--test
make bench_memory_ops
```

Standalone runs do not check environment or source consistency against the other
implementation. Use the paired results from `compare_memory_ops` for comparisons
between implementations. `MEMORY_BENCH_TOOLCHAIN` can select another installed
toolchain for both builds; the runner records the actual version. Do not mix
older experiments into a new comparison.

## Shared Benchmark Loops and Timing Boundaries

The pool contains 1,024 pages of 4 KiB each, with every page touched during
initialization. Mappings use three AArch64 page-table levels with 512 entries
per level. Each batch maps 100 consecutive single pages, starting at virtual
address `0x1000_0000` and data physical address `0x6000_0000`. These data
addresses are only encoded and queried, never dereferenced. Mapping inputs and
query addresses are prepared outside the timed regions.

| ID | Timed work per batch | Identical steps outside timing in both implementations |
|---|---|---|
| `allocator/alloc/100` | Allocate 100 pages and retain native ownership values | Validate bounds, sizes, and uniqueness, then release pages in allocation order |
| `allocator/dealloc/100` | Consume ownership and release 100 pages | Allocate and validate the 100 pages beforehand |
| `page_table/map_page/100` | Map 100 pages from an empty root, including allocation of two intermediate table pages | Create a root for each batch; afterward check results, query, unmap, check query misses, and destroy the table |
| `page_table/unmap_page/100` | Remove 100 mappings | Create a root, populate it, and validate queries for each batch; afterward check results and query misses, then destroy the table |
| `page_table/query/100` | Query offset 17 within each page and retain native results | Populate once; validate returned addresses, attributes, and page sizes after each batch, then clean up at the end |

Both implementations create a fresh root for each map/unmap batch and destroy
it outside timing; query retains a populated table. Setup and cleanup order,
timer calls, `black_box` placement, and loops all come from the same benchmark
file. Both use `MaybeUninit<T>` to store native values, with each slot initialized
and then consumed exactly once. This removes the additional
`Option<Result<Frame, HvError>>` wrapper, `take()` calls, and destructor branches
when overwriting previous results.

The allocation failure check converts hvisor's native fallible API result into
a successful ownership value; it does not bypass the real `Frame::new()` or its
destructor. Map, unmap, and query retain their native return types. Semantic
conversion and validation happen outside timing, without constructing an
additional common result structure in the measured path.

Before sampling, the harness checks repeated mapping/unmapping, page boundaries,
and queries with offsets. Before and after each group, it verifies that all
1,024 pages can be recovered and allocations are unique. Checks of each
implementation's guarantees about initially zeroed pages, or checks for allocator
exhaustion, run only before and after the groups. hvisor initializes its global
pool once and retains it until
process exit. VeriHyMem's fixture owns its pool and releases it only after all
page tables have been destroyed.

## Native API Differences Retained

The shared benchmark methodology preserves the production implementations:

- hvisor allocations return a native `Frame` (16 bytes), while VeriHyMem returns
  a `PAddr` (8 bytes). Map/unmap success and error return types also differ.
  Startup logs print the actual native type sizes; buffers retain only the
  values required by each API.
- hvisor map/unmap/query calls retain the real page-table lock. VeriHyMem's
  corresponding entry points do not take that same lock. Both physical page
  allocators retain their actual locks and runtime checks.
- hvisor allocates and clears intermediate tables during map, then reclaims them
  when the whole table is destroyed. VeriHyMem prunes, clears, and reclaims
  intermediate tables during unmap. Both start from the same empty-root state,
  but charge these costs to different operations. `map + unmap` does not
  represent the full lifecycle cost including table destruction.
- Logging produces no output, but production log-level checks are not removed.
  Metadata uses each implementation's native structures and the host heap.
  Privileged page-table activation and TLB flushing are not executed.

The report measures native API costs under the same host workload. CPU affinity
and alternating execution order cannot completely eliminate WSL/VM frequency
and scheduling variation. These measurements do not represent EL2 execution,
hardware TLB behavior, cold caches, sparse mappings, or concurrent contention.
Do not calculate performance improvements against earlier results that used
different toolchains or benchmark loops.
