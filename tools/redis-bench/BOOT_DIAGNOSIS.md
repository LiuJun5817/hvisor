# Redis benchmark boot failure diagnosis

The original verified build can overflow a secondary CPU's stack while creating
the parking page table. Its compiled call chain exceeds the 512 KiB per-CPU area
and overwrites the preceding CPU's stack. A GDB capture confirms the predicted
return to address zero. This is a hypervisor boot failure, before Redis starts.

The affected build is hvisor `df547ceda31ec3d41d1f8f14fdff71ac7757e0e5`, with
`verified-hv-mem` pinned to `e3897688e448b34e914fe95d8aaabe7eca59b303`, compiled
using `nightly-2024-05-05` for AArch64 in release mode. See the original
[build metadata](../../target/redis-bench/build/verified/metadata.json) and
[ELF](../../target/redis-bench/build/verified/src/target/aarch64-unknown-none/release/hvisor).
All addresses below refer to that original ELF, whose SHA256 is
`3d7f8a179c57327bb03a6eaa808c06d46af31becbc84c254ca94ed154fc8f644`.

## Compiled stack usage

The following frames coexist when a secondary CPU initializes
`PARKING_PAGE_TABLE` from `ArchCpu::idle`:

| Function | Entry address | Stack frame, bytes |
| --- | --- | ---: |
| `rust_main` | `0x4043b9e0` | 576 |
| `PerCpu::run_vm` | `0x40429710` | 176 |
| `ArchCpu::idle` | `0x40412000` | 208 |
| Parking table `Once::try_call_once_slow` | `0x404473b0` | 279,872 |
| `PageTable::map` | `0x4043cf60` | 279,792 |
| **Total before deeper allocator calls** | | **560,624** |

`idle` calls the initializer at `0x40412148`; the initializer calls `map` at
`0x40447714`. Their large frames are respectively `96 + 278528 + 1248` and
`96 + 278528 + 1168` bytes. The total already exceeds `PER_CPU_SIZE = 524288`
by **36,336 bytes**, even before accounting for CPU-local data and deeper calls.

The pinned allocator's `GlobalAllocator::alloc` takes the entire `BitAlloc1M`
out of a `PCell`, modifies it, and puts it back. In the compiled `map`, the
`memcpy` call at `0x4043d178` copies **139,810 bytes** into `sp + 62`. This
overwrites a continuous portion of the preceding CPU's stack; the damage is
not limited to the page probes emitted in the function prologue.

## Runtime confirmation

The [GDB capture](../../target/redis-bench/diagnostic/gdb-abort-172550/gdb.log)
stops CPU 2 at `0x4041ca00`, the current-EL synchronous exception vector, before
the vector saves registers. The capture records:

| Register | Value |
| --- | --- |
| `SP` / `SP_EL2` | `0x406f8c40` |
| `X30` / LR | `0x0` |
| `ELR_EL2` | `0x0` |
| `ESR_EL2` | `0x86000010` |
| `SPSR_EL2` | `0x600003c9` |

CPU 2's stack top is `0x406f9000`. The observed SP is exactly
`stack_top - (576 + 176 + 208)`, the expected SP after the parking `Once`
returns. Its saved LR was at `stack_top - 1048`, inside the region reached by
CPU 3's overflowing initializer. The zero LR and ELR match a return through
that overwritten slot. CPU 3 is already halted at guest PC `0x4`, consistent
with its parking loop. The capture also preserves the
[QEMU command](../../target/redis-bench/diagnostic/gdb-abort-172550/command.json)
and [console log](../../target/redis-bench/diagnostic/gdb-abort-172550/console.log).

## Controlled workaround for the comparison

Build both baseline and verified snapshots with the same **1 MiB per-CPU
area**, keeping the original commits, dependency pin, toolchain, and guest
configuration:

```sh
python3 tools/redis-bench/build-hvisor.py \
  --output target/redis-bench/build-stack1m \
  --per-cpu-size 1048576
```

This changes only `PER_CPU_SIZE` in each isolated source snapshot. Across four
CPUs, the reserved per-CPU areas grow from 2 MiB to 4 MiB: **2 MiB additional
hypervisor memory in both variants**. This accommodates the observed call chain
while preserving the pinned library's allocator behavior. It is a benchmark
control, not a proof that every possible hypervisor path fits within 1 MiB.
Build metadata records the exact diff, size, and source hashes; `--reuse`
requires the same option and validates those records. Results from these
builds must be identified as using the common 1 MiB control patch.

The sibling `verified-hv-mem` repository contains commit `70a9ef5`, which changes
allocator operations to borrow the bitmap in place and reduces other copies.
That source change addresses the large temporary objects, but the newer
library was not substituted into this controlled comparison. This diagnosis
does not establish its VM behavior or formal verification status.

The linked raw artifacts live under ignored `target/`; retain them alongside
published results because they are not included in a source-only checkout.
