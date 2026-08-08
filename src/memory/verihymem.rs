//! hvisor's executable adapter types for VeriHyMem.
//!
//! The region representation is architecture-neutral. Architectures select
//! their concrete VeriHyMem PTE and hardware implementation behind the
//! `HvisorPTE` and `HvisorHardware` aliases in their architecture modules.

use crate::arch::paging::{PageSize, PagingError, PagingResult};
use crate::error::HvResult;
use crate::memory::{
    addr::{GuestPhysAddr, HostPhysAddr},
    mapper::Mapper,
    mm::MemoryRegion as HvisorMemoryRegion,
    MemFlags, PhysAddr, PAGE_SIZE,
};
use core::fmt::{Debug, Formatter, Result as FmtResult};
use spin::Once;
use verified_hv_mem::{
    address::{
        addr::{PAddr, VAddr},
        frame::{FrameSize, MemAttr},
        region::MemoryRegion,
    },
    bitmap_allocator::bitmap_impl::BitAlloc1M,
    global_allocator::GbAlloc,
    hardware::{spec::MmuVmToken, MmuHardware},
    hv_mem::{protocol::BudgetProtocol, HvMem},
    memory_set::{MemorySet as VeriHyMemMemorySetOps, VecMemorySet},
    page_table::{ExPageTable, PageTable},
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

static HV_MEM: Once<HvisorHvMem> = Once::new();

/// Return hvisor's single global verified memory manager.
pub fn hv_mem() -> &'static HvisorHvMem {
    HV_MEM
        .get()
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
    HV_MEM.call_once(|| HvisorHvMem::new(allocator, pt_constants));
}

/// Compatibility wrapper exposing hvisor-style memory-set operations over
/// VeriHyMem's `VecMemorySet`.
///
/// The wrapper owns the MMU handle and threads the zone's VM token through
/// every mapping mutation. Region metadata stays solely in `VecMemorySet`.
pub struct VMemorySet {
    inner: VeriHyMemMemorySet,
    mmu: MmuHardware<HvisorHardware>,
    s2_token: Option<Tracked<MmuVmToken>>,
    zone_id: usize,
    iommu: bool,
}

impl VMemorySet {
    pub fn new(pt_level: usize, zone_id: usize, iommu: bool) -> Self {
        let inner: VeriHyMemMemorySet =
            VeriHyMemMemorySetOps::new(global_allocator(), hvisor_pt_constants(pt_level));
        let (mmu, _) = MmuHardware::<HvisorHardware>::new();
        Self {
            inner,
            mmu,
            s2_token: Some(Tracked::assume_new()),
            zone_id,
            iommu,
        }
    }

    fn inner(&self) -> &VeriHyMemMemorySet {
        &self.inner
    }

    pub fn root_paddr(&self) -> usize {
        VeriHyMemMemorySetOps::pt_root(self.inner()).0
    }

    pub fn for_each_region<F>(&self, mut f: F)
    where
        F: FnMut(&HvisorMemoryRegion<GuestPhysAddr>),
    {
        for region in &self.inner().regions {
            let region = from_verihymem_region(region)
                .expect("VecMemorySet contains an invalid compatibility region");
            f(&region);
        }
    }

    pub fn insert(&mut self, region: HvisorMemoryRegion<GuestPhysAddr>) -> HvResult {
        if region.size == 0 {
            return Ok(());
        }
        let converted = to_verihymem_region(&region)?;
        if VeriHyMemMemorySetOps::overlaps_vmem(self.inner(), &converted) {
            return hv_result_err!(EINVAL, "memory region overlaps an existing mapping");
        }
        let token = self.s2_token.take().expect("missing VecMemorySet VM token");
        let zone_id = self.zone_id;
        let iommu = self.iommu;
        let new_token = VeriHyMemMemorySetOps::insert(
            &mut self.inner,
            global_allocator(),
            converted,
            zone_id,
            &self.mmu,
            token,
            iommu,
        );
        self.s2_token = Some(new_token);
        Ok(())
    }

    pub fn try_insert(&mut self, region: HvisorMemoryRegion<GuestPhysAddr>) -> HvResult {
        let converted = to_verihymem_region(&region)?;
        if VeriHyMemMemorySetOps::overlaps_vmem(self.inner(), &converted) {
            return Ok(());
        }
        self.insert(region)
    }

    fn delete(&mut self, start: GuestPhysAddr, size: usize) -> HvResult {
        let matches_region = self.inner().regions.iter().any(|region| {
            region.vstart.0 == start && region.pages.checked_mul(PAGE_SIZE) == Some(size)
        });
        if !matches_region {
            return hv_result_err!(EINVAL, "memory region size does not match");
        }

        let token = self.s2_token.take().expect("missing VecMemorySet VM token");
        let zone_id = self.zone_id;
        let iommu = self.iommu;
        let new_token = VeriHyMemMemorySetOps::remove(
            &mut self.inner,
            global_allocator(),
            VAddr(start),
            zone_id,
            &self.mmu,
            token,
            iommu,
        );
        self.s2_token = Some(new_token);
        Ok(())
    }

    pub fn try_delete(&mut self, start: GuestPhysAddr, size: usize) -> HvResult {
        let matches_region = self.inner().regions.iter().any(|region| {
            region.vstart.0 == start && region.pages.checked_mul(PAGE_SIZE) == Some(size)
        });
        if matches_region {
            self.delete(start, size)
        } else {
            Err(hv_err!(ENOMEM))
        }
    }

    pub unsafe fn activate(&self) {
        activate_stage2_page_table(self.root_paddr());
    }

    pub unsafe fn page_table_query(
        &self,
        vaddr: GuestPhysAddr,
    ) -> PagingResult<(PhysAddr, MemFlags, PageSize)> {
        let (vbase, frame) = self
            .inner()
            .pt
            .query(VAddr(vaddr))
            .map_err(|_| PagingError::NotMapped)?;
        let page_size = match frame.size {
            FrameSize::Size4K => PageSize::Size4K,
            FrameSize::Size2M => PageSize::Size2M,
            FrameSize::Size1G => PageSize::Size1G,
            _ => return Err(PagingError::NotMapped),
        };
        Ok((
            frame.base.0 + (vaddr - vbase.0),
            attr_to_mem_flags(frame.attr),
            page_size,
        ))
    }
}

impl Debug for VMemorySet {
    fn fmt(&self, f: &mut Formatter<'_>) -> FmtResult {
        f.debug_struct("VMemorySet")
            .field("zone_id", &self.zone_id)
            .field("iommu", &self.iommu)
            .field("region_count", &self.inner().regions.len())
            .field("page_table_root", &self.root_paddr())
            .finish()
    }
}
