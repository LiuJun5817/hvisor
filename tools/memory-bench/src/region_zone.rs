//! Host adaptation of native region operations and zone memory ownership.
//!
//! The allocator, memory sets and page tables are production implementations.
//! Region timing calls the owned CPU set directly. The zone-memory fixture
//! retains two sets and the native Arc/RwLock/Vec ownership pattern, with an
//! empty integration payload; it does not call the full hardware Zone API.

use std::sync::Arc;

use spin::RwLock;

use crate::{
    error::HvResult,
    host,
    memory::{MemFlags, MemoryRegion, MemorySet, PAGE_SIZE},
    paging::PageSize,
    s2pt::Stage2PageTable,
};

pub type Mapping = MemoryRegion<usize>;
pub type OpResult = HvResult;

const MAX_MAPPED_PAGES: usize = 65536;
const MAX_REGION_PAGES: usize = 32768;
const ZONE_ID: usize = 1;
const GUEST_BASE: usize = 0x1000_0000;
const DATA_PA: usize = 0x6000_0000;

type Set = MemorySet<Stage2PageTable>;

struct MemorySets {
    cpu: Set,
    iommu: Set,
}

impl MemorySets {
    fn new() -> Self {
        Self {
            cpu: Set::new(3),
            iommu: Set::new(3),
        }
    }
}

struct MemoryZone {
    id: usize,
    inner: RwLock<MemorySets>,
}

pub struct Fixture {
    // Region cases own their sets directly, preserving the native &mut API
    // without adding an interior-mutability check or a zone lock per operation.
    region: Option<MemorySets>,
    // This reduced registry mirrors src/zone.rs ownership and teardown. The
    // hardware Zone type and its non-memory metadata are deliberately absent.
    zones: RwLock<Vec<Arc<MemoryZone>>>,
}

impl Fixture {
    pub fn new() -> Self {
        host::init();
        Self {
            region: None,
            zones: RwLock::new(Vec::new()),
        }
    }

    fn require_removed(&self) -> OpResult {
        if self.region.is_some() || self.zones.read().iter().any(|zone| zone.id == ZONE_ID) {
            return hv_result_err!(EEXIST);
        }
        Ok(())
    }

    /// Construct the region fixture outside region measurements. Its unused
    /// IOMMU root matches the two-root working set of the other branch.
    pub fn add_empty_zone(&mut self) -> OpResult {
        self.require_removed()?;
        self.region = Some(MemorySets::new());
        Ok(())
    }

    #[inline]
    pub fn insert(&mut self, mapping: &Mapping) -> OpResult {
        self.region
            .as_mut()
            .expect("region fixture is missing")
            .cpu
            .insert(mapping.clone())
    }

    #[inline]
    pub fn remove(&mut self, mapping: &Mapping) -> OpResult {
        self.region
            .as_mut()
            .expect("region fixture is missing")
            .cpu
            .delete(mapping.start, mapping.size)
    }

    /// Construct both native translation sets, populate them and register the
    /// memory owner. As in native zone creation, each set is populated under
    /// one write guard, then the completed owner is wrapped in Arc and added.
    pub fn create_zone(&mut self, mappings: &[Mapping]) -> OpResult {
        self.require_removed()?;
        let zone = MemoryZone {
            id: ZONE_ID,
            inner: RwLock::new(MemorySets::new()),
        };
        {
            let mut sets = zone.inner.write();
            for mapping in mappings {
                sets.cpu.insert(mapping.clone())?;
            }
        }
        {
            let mut sets = zone.inner.write();
            for mapping in mappings {
                sets.iommu.insert(mapping.clone())?;
            }
        }
        let zone = Arc::new(zone);
        self.zones.write().push(zone);
        Ok(())
    }

    /// Native MemorySet::drop unmaps regions and drops all page-table frames.
    /// For zone cases, destroy the last Arc while still holding the registry
    /// write guard, matching the ownership ordering in src/zone.rs.
    pub fn remove_zone(&mut self) -> OpResult {
        if let Some(sets) = self.region.take() {
            drop(sets);
            return Ok(());
        }
        let mut zones = self.zones.write();
        let Some(index) = zones.iter().position(|zone| zone.id == ZONE_ID) else {
            return hv_result_err!(ENOENT);
        };
        let removed = zones.remove(index);
        assert_eq!(Arc::strong_count(&removed), 1);
        drop(removed);
        Ok(())
    }

    pub fn check_mappings<'a>(&self, mappings: impl IntoIterator<Item = &'a Mapping>) {
        let sets = self.region.as_ref().expect("region fixture is missing");
        check_set(&sets.cpu, mappings);
        check_empty_set(&sets.iommu);
    }

    /// Check every target PTE outside timing, including deletion of interior pages.
    pub fn check_region_pages(&self, mapping: &Mapping, present: bool) {
        let sets = self.region.as_ref().expect("region fixture is missing");
        for page in (0..mapping.size).step_by(PAGE_SIZE) {
            for offset in [page, page + PAGE_SIZE - 1] {
                let vaddr = mapping.start + offset;
                // SAFETY: the fixture owns this live, host-backed page table.
                let translation = unsafe { sets.cpu.page_table_query(vaddr) };
                if present {
                    let (paddr, flags, size) = translation.expect("target page is missing");
                    assert_eq!(paddr, mapping.mapper.map_fn(vaddr));
                    assert_eq!(flags.bits(), (MemFlags::READ | MemFlags::WRITE).bits());
                    assert_eq!(size, PageSize::Size4K);
                } else {
                    assert!(translation.is_err(), "removed target page remains mapped");
                }
            }
        }
    }

    pub fn check_zone(&self, mappings: &[Mapping]) {
        assert!(self.region.is_none());
        let zones = self.zones.read();
        assert_eq!(zones.len(), 1, "unexpected zone count");
        let zone = zones
            .iter()
            .find(|zone| zone.id == ZONE_ID)
            .expect("memory zone is missing");
        let sets = zone.inner.read();
        check_set(&sets.cpu, mappings);
        check_set(&sets.iommu, mappings);
    }

    pub fn check_empty(&self) {
        let sets = self.region.as_ref().expect("region fixture is missing");
        check_empty_set(&sets.cpu);
        check_empty_set(&sets.iommu);
    }

    pub fn check_removed(&self) {
        assert!(self.region.is_none(), "region fixture was not removed");
        assert!(self.zones.read().is_empty(), "zone is still registered");
    }

    pub fn assert_all_frames_returned(&self) {
        self.check_removed();
        host::assert_all_frames_returned();
    }
}

impl Default for Fixture {
    fn default() -> Self {
        Self::new()
    }
}

pub fn mapping(index: usize, pages: usize) -> Mapping {
    mapping_at(index.checked_mul(pages).unwrap(), pages)
}

/// Place differently sized regions in fixed, nonoverlapping page slots.
pub fn mapping_at(start_page: usize, pages: usize) -> Mapping {
    assert!(
        (1..=MAX_REGION_PAGES).contains(&pages),
        "invalid region page count"
    );
    let end_page = start_page.checked_add(pages).unwrap();
    assert!(
        end_page <= MAX_MAPPED_PAGES,
        "mapping exceeds the 256 MiB test range"
    );
    let offset = start_page * PAGE_SIZE;
    Mapping::new_with_offset_mapper(
        GUEST_BASE + offset,
        DATA_PA + offset,
        pages * PAGE_SIZE,
        MemFlags::READ | MemFlags::WRITE | MemFlags::NO_HUGEPAGES,
    )
}

fn check_set<'a>(set: &Set, mappings: impl IntoIterator<Item = &'a Mapping>) {
    let mut expected = mappings.into_iter();
    set.for_each_region(|region| {
        let mapping = expected.next().expect("unexpected region");
        assert_eq!(region.start, mapping.start, "wrong region start");
        assert_eq!(region.size, mapping.size, "wrong region size");
        assert_eq!(region.flags.bits(), mapping.flags.bits());
        for offset in [0, mapping.size - 1] {
            let vaddr = mapping.start + offset;
            let expected = mapping.mapper.map_fn(vaddr);
            assert_eq!(region.mapper.map_fn(vaddr), expected);
            // SAFETY: the fixture owns this live, host-backed page table, and
            // checking mappings neither activates translation nor accesses RAM.
            let (paddr, flags, size) =
                unsafe { set.page_table_query(vaddr) }.expect("page-table mapping missing");
            assert_eq!(paddr, expected, "wrong page-table physical address");
            assert_eq!(flags.bits(), (MemFlags::READ | MemFlags::WRITE).bits());
            assert_eq!(size, PageSize::Size4K, "unexpected huge page");
        }
    });
    assert!(expected.next().is_none(), "expected region is missing");
}

fn check_empty_set(set: &Set) {
    set.for_each_region(|_| panic!("regions were not removed"));
    // SAFETY: the fixture owns this live page table; this only reads its PTEs.
    assert!(unsafe { set.page_table_query(GUEST_BASE) }.is_err());
}
