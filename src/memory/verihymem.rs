//! hvisor's executable adapter types for VeriHyMem.
//!
//! The region representation is architecture-neutral. Architectures select
//! their concrete VeriHyMem PTE and hardware implementation behind the
//! `HvisorPTE` and `HvisorHardware` aliases in their architecture modules.

use crate::arch::paging::{PageSize, PagingError, PagingResult};
use crate::error::HvResult;
use crate::memory::{
    addr::{align_down, align_up, virt_to_phys, GuestPhysAddr, HostPhysAddr},
    mapper::Mapper,
    mm::MemoryRegion as HvisorMemoryRegion,
    MemFlags, PhysAddr, PAGE_SIZE,
};
use alloc::boxed::Box;
use core::fmt::{Debug, Formatter, Result as FmtResult};
use spin::Once;
use verified_hv_mem::{
    address::{
        addr::{PAddr, VAddr},
        frame::MemAttr,
        region::MemoryRegion,
    },
    bitmap_allocator::bitmap_impl::BitAlloc1M,
    global_allocator::GbAlloc,
    hv_mem::{protocol::BudgetProtocol, HvMem},
    memory_set::VecMemorySet,
    page_table::ExPageTable,
};
use vstd::prelude::Tracked;

#[cfg(target_arch = "aarch64")]
use crate::arch::aarch64::{
    hardware::HvisorHardware,
    paging::{hvisor_pt_constants, HvisorPTE},
    s2pt::activate_stage2_page_table,
};

/// The canonical linear region used by VeriHyMem.
pub type VeriHyMemMemoryRegion = MemoryRegion;

/// Convert hvisor's flag vocabulary to VeriHyMem's executable attributes.
pub fn mem_flags_to_attr(flags: MemFlags) -> MemAttr {
    MemAttr::new(
        flags.contains(MemFlags::READ),
        flags.contains(MemFlags::WRITE),
        flags.contains(MemFlags::EXECUTE),
        flags.contains(MemFlags::IO),
    )
}

/// Construct a validated VeriHyMem region from an explicit linear mapping.
pub fn make_memory_region(
    vstart: GuestPhysAddr,
    pstart: HostPhysAddr,
    size: usize,
    flags: MemFlags,
) -> VeriHyMemMemoryRegion {
    assert!(size != 0 && size % PAGE_SIZE == 0);
    assert!(vstart % PAGE_SIZE == 0 && pstart % PAGE_SIZE == 0);
    let region = VeriHyMemMemoryRegion {
        vstart: VAddr(vstart),
        pstart: PAddr(pstart),
        pages: size / PAGE_SIZE,
        attr: mem_flags_to_attr(flags),
    };
    assert!(region.valid());
    region
}

/// Convert a legacy hvisor region into VeriHyMem's explicit linear region.
///
/// VeriHyMem stores the physical start directly.  hvisor's active regions use
/// `Mapper::Offset`, so the physical start is obtained by translating the
/// first virtual address.  A fixed mapper cannot describe a linear region and
/// is rejected rather than silently changing its mapping semantics.
pub fn to_verihymem_region<VA>(region: &HvisorMemoryRegion<VA>) -> HvResult<VeriHyMemMemoryRegion>
where
    VA: Into<usize> + Copy,
{
    if region.size == 0 || region.size % PAGE_SIZE != 0 || region.start.into() % PAGE_SIZE != 0 {
        return hv_result_err!(EINVAL, "invalid hvisor memory region alignment or size");
    }
    if !matches!(&region.mapper, Mapper::Offset(_)) {
        return hv_result_err!(EINVAL, "fixed mapper is not representable by VeriHyMem");
    }

    let vstart = region.start.into();
    let pstart = region.mapper.map_fn(vstart);
    if pstart % PAGE_SIZE != 0 {
        return hv_result_err!(EINVAL, "translated physical address is not page aligned");
    }

    let converted = VeriHyMemMemoryRegion {
        vstart: VAddr(vstart),
        pstart: PAddr(pstart),
        pages: region.size / PAGE_SIZE,
        attr: mem_flags_to_attr(region.flags),
    };
    if !converted.valid() {
        return hv_result_err!(EINVAL, "memory region is outside VeriHyMem bounds");
    }
    Ok(converted)
}

fn attr_to_mem_flags(attr: MemAttr) -> MemFlags {
    let mut flags = MemFlags::empty();
    if attr.readable {
        flags |= MemFlags::READ;
    }
    if attr.writable {
        flags |= MemFlags::WRITE;
    }
    if attr.executable {
        flags |= MemFlags::EXECUTE;
    }
    if attr.device {
        flags |= MemFlags::IO;
    }
    flags
}

/// Convert VeriHyMem's explicit linear region back to hvisor's compatibility
/// representation.  The resulting hvisor region always uses `Mapper::Offset`.
pub fn from_verihymem_region<VA>(region: &VeriHyMemMemoryRegion) -> HvResult<HvisorMemoryRegion<VA>>
where
    VA: From<usize> + Into<usize> + Copy,
{
    if !region.valid() {
        return hv_result_err!(EINVAL, "invalid VeriHyMem memory region");
    }
    let size = region
        .pages
        .checked_mul(PAGE_SIZE)
        .ok_or_else(|| hv_err!(ERANGE, "memory region size overflows"))?;
    Ok(HvisorMemoryRegion::new_with_offset_mapper(
        VA::from(region.vstart.0),
        region.pstart.0,
        size,
        attr_to_mem_flags(region.attr),
    ))
}

/// The architecture-selected page-table type used by `VecMemorySet`.
pub type VeriHyMemPageTable = ExPageTable<BitAlloc1M, HvisorPTE>;

/// Architecture-selected VeriHyMem memory set. Allocator and MMU token
/// arguments are supplied by the eventual `HvMem` integration layer.
pub type VeriHyMemMemorySet = VecMemorySet<VeriHyMemPageTable, BitAlloc1M, HvisorHardware>;

/// Concrete global memory manager used by hvisor.
pub type HvisorHvMem =
    HvMem<VeriHyMemPageTable, VeriHyMemMemorySet, BitAlloc1M, BudgetProtocol, HvisorHardware>;

static HV_MEM: Once<Box<HvisorHvMem>> = Once::new();

/// Return hvisor's single global verified memory manager.
pub fn hv_mem() -> &'static HvisorHvMem {
    HV_MEM
        .get()
        .map(|hv_mem| hv_mem.as_ref())
        .expect("HvisorHvMem is not initialized before use")
}

/// Return the allocator owned by the global verified memory manager.
pub fn global_allocator() -> &'static GbAlloc {
    &hv_mem().allocator
}

/// Initialize the global verified memory manager and its allocator exactly once.
pub fn init_hv_mem(base: PhysAddr, page_count: usize, pt_level: usize) {
    let allocator = GbAlloc::default(PAddr(base));
    allocator.init(page_count, Tracked::assume_new());
    let pt_constants = hvisor_pt_constants(pt_level);
    HV_MEM.call_once(|| Box::new(HvisorHvMem::new(allocator, pt_constants)));
}

/// Initialize hvisor's global verified memory manager and physical frame allocator.
pub fn init() {
    let mem_pool_start = crate::consts::mem_pool_start();
    let mem_pool_end = align_down(crate::consts::hv_end());
    let mem_pool_size = mem_pool_end - mem_pool_start;

    let page_count = align_up(mem_pool_size) / PAGE_SIZE;
    let pt_level = if crate::arch::aarch64::mm::is_s2_pt_level3() {
        3
    } else {
        4
    };
    init_hv_mem(align_up(virt_to_phys(mem_pool_start)), page_count, pt_level);

    info!(
        "Frame allocator initialization finished: {:#x?}",
        mem_pool_start..mem_pool_end
    );
}

/// Lightweight hvisor handle for one of `HvMem`'s per-zone memory sets.
///
/// The page table and region metadata live in `HvisorHvMem`; this value only
/// identifies the zone and whether the CPU or IOMMU stage-2 set is addressed.
pub struct VMemorySet {
    zone_id: usize,
    iommu: bool,
}

impl VMemorySet {
    pub const fn new(zone_id: usize, iommu: bool) -> Self {
        Self { zone_id, iommu }
    }

    pub fn root_paddr(&self) -> usize {
        let root = if self.iommu {
            hv_mem().iommu_pt_root(self.zone_id)
        } else {
            hv_mem().pt_root(self.zone_id)
        };
        root.expect("HvMem zone is not registered").0
    }

    pub fn insert(&mut self, region: HvisorMemoryRegion<GuestPhysAddr>) -> HvResult {
        if region.size == 0 {
            return Ok(());
        }
        let converted = to_verihymem_region(&region)?;
        let result = if self.iommu {
            hv_mem().insert_iommu_region(self.zone_id, converted)
        } else {
            hv_mem().insert_region(self.zone_id, converted)
        };
        result.map_err(|_| hv_err!(EINVAL, "memory region insertion failed"))
    }

    pub fn delete(&mut self, start: GuestPhysAddr, size: usize) -> HvResult {
        let region = make_memory_region(start, 0, size, MemFlags::empty());
        let result = if self.iommu {
            hv_mem().remove_iommu_region(self.zone_id, region)
        } else {
            hv_mem().remove_region(self.zone_id, region)
        };
        result.map_err(|_| hv_err!(EINVAL, "memory region removal failed"))
    }

    pub unsafe fn activate(&self) {
        activate_stage2_page_table(self.root_paddr());
    }

    pub unsafe fn page_table_query(
        &self,
        vaddr: GuestPhysAddr,
    ) -> PagingResult<(PhysAddr, MemFlags, PageSize)> {
        let result = if self.iommu {
            hv_mem().iommu_query_vaddr(self.zone_id, VAddr(vaddr))
        } else {
            hv_mem().query_vaddr(self.zone_id, VAddr(vaddr))
        };
        let (paddr, attr) = result.map_err(|_| PagingError::NotMapped)?;
        Ok((paddr.0, attr_to_mem_flags(attr), PageSize::Size4K))
    }
}

impl Debug for VMemorySet {
    fn fmt(&self, f: &mut Formatter<'_>) -> FmtResult {
        f.debug_struct("VMemorySet")
            .field("zone_id", &self.zone_id)
            .field("iommu", &self.iommu)
            .field("page_table_root", &self.root_paddr())
            .finish()
    }
}
