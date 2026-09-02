# VeriHyMem-hvisor integration

This repository integrates the Verus-verified VeriHyMem memory manager into hvisor. The current integration and assurance claim are scoped to AArch64.

## Status

All AArch64 guest CPU and IOMMU stage-2 region insertion, removal, query, root lookup, and zone lifecycle operations are routed through the global `HvMem`. The legacy hvisor `MemorySet` is not exported on AArch64; `MemoryRegion` and `Mapper` remain only as compatibility input types. CPU activation installs the VeriHyMem root and zone VMID together. The CPU parking table is a separate VeriHyMem page table and is not a zone mapping.

The integration checks region alignment, nonzero size, overflow, VeriHyMem physical bounds, stage-2 virtual bounds, query bounds, and zone-ID/VMID width. A release claim still requires the configuration and allocator checks listed in the [proof boundary](#proof-boundary).

`BudgetSpec` now partitions physical pages into disjoint zone-private budgets and a global-shared budget. A region whose complete physical footprint is in `global_shared_pages()` may therefore be mapped by multiple zones. This covers AArch64 IVC and GICv2 GICV sharing when the trusted configuration classifies all of their backing pages as global-shared.

## Integration Effort

The integration-effort metric counts only newly added Rust code because we are replacing the legacy hvisor memory management. Each integrated snapshot is compared directly with the original hvisor commit `9dbacc4`. The counts use the Rust `+ code` result from `cloc --git --diff`; modified (`!= code`) and removed code, comments, blank lines, manifests, lockfiles, and all other non-Rust files are excluded. The results are cumulative rather than incremental between integration stages.

| Version | Commit | Rust LOC added |
|---|---|---:|
| Original hvisor | `9dbacc4` | Baseline |
| Memory allocator integrated | `c84d377` | **5** |
| Page table integrated | `4b089a7` | **105** |
| Full `HvMem` integrated | `dc59a9ac` | **718** |

The full integration's 718 newly added Rust lines are distributed as follows:

| Area | Rust LOC added | Principal additions |
|---|---:|---|
| VeriHyMem adapter and memory glue | **263** | `src/memory/verihymem.rs` 233, `src/memory/mm.rs` 25, `src/memory/mod.rs` 5 |
| PCI integration | **213** | `src/pci/pci_handler.rs` 152, `src/pci/pci_config.rs` 54, `src/pci/pci_test.rs` 6, `src/pci/msix.rs` 1 |
| Zone integration | **119** | `src/zone.rs` 119 |
| AArch64 architecture and paging | **78** | `src/arch/aarch64/hardware.rs` 46, `cpu.rs` 18, `zone.rs` 9, `paging.rs` 4, `mod.rs` 1 |
| IOMMU and interrupt controllers | **45** | SMMU 31, IOMMU glue 12, VGIC 2 |
| **Total** | **718** | |

## Proof Boundary

This section defines the assurance boundary for the AArch64 hvisor integration. It is not an end-to-end verification claim for hvisor.

### Verified scope

Subject to VeriHyMem's preconditions, axioms, and external hardware semantics, its proofs establish:

- **RegionDisjoint:** zone-private physical-page budgets are disjoint across zones and from the global-shared budget; pages in the global-shared budget may intentionally be mapped by multiple zones.
- **ZoneIsolated:** CPU and IOMMU translations stay within the zone's private physical-page budget or the global-shared budget.
- **PTMemDisjoint:** VeriHyMem page-table frames remain disjoint from other live allocations made through the same verified allocator.

On AArch64, zone CPU and IOMMU stage-2 operations use `HvMem`; no legacy hvisor page-table mutation remains. The adapter checks executable region validity and stage-2 bounds, query bounds, zone-ID range, and the 8-bit VMID used for activation and invalidation.

### Trust boundaries

The following facts are not proved by the integration:

1. **Budget configuration.** Every inserted CPU or IOMMU region must have its complete physical-page footprint in either `zone_private_pages(zid)` or `global_shared_pages()`. Zone-private budgets must be pairwise disjoint and disjoint from the global-shared budget. A static configuration check must establish those facts, region validity, and exclusion of hvisor, allocator, firmware, and reserved memory. IVC and GICv2 GICV backing pages must be classified as global-shared. The checker and its configuration inputs are trusted.
2. **RAII allocation.** `Tracked::assume_new()` bridges erased linear tokens to hvisor ownership. Every external allocator client must keep one owning `Frame` per allocation, drop it exactly once, avoid use or hardware references after drop, and use the same allocator. Under this discipline, VeriHyMem's page-table allocations are disjoint from other live RAII allocations.
3. **Allocator environment.** The initialized permission set must correspond exactly to the aligned reserved pool. The pool is directly addressable with the configured HVA-to-PA offset, does not overlap mapped guest/device memory, and never exhausts. Initialization must enforce bitmap capacity and address-span bounds.
4. **Configuration checks.** Array counts and CPU/device IDs must be within their declared limits; regions must fit the active CPU IPA/PA and SMMU address widths. `Frame::new_contiguous` must reject zero or excessive counts and invalid alignment. Until these checks are executable, their callers are trusted boundaries.
5. **Hardware and hvisor.** AArch64 PTE encoding, barriers, TLB/cache maintenance, CPU/SMMU coherence, and device quiescence satisfy VeriHyMem's external hardware contracts. hvisor must program SMMU geometry compatible with the supplied page table and detach streams before freeing it. These are pre-existing hvisor driver responsibilities, not VeriHyMem limitations.
6. **Toolchain and unverified code.** Verus, Z3, `vstd`, Rust/LLVM, axioms, `assume`, `external_body`, unsafe adapter code, firmware, hvisor outside the integration, and the platform are trusted.

### Unsupported behavior

The current `BudgetSpec` justifies AArch64 IVC and GICv2 GICV sharing when their complete physical footprints are included in `global_shared_pages()`. Dynamic PCI BAR/ROM relocation remains unsupported by the integration proof and must be disabled or represented by a proof model that covers its update and lifecycle semantics.
