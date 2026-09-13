//! Native hvisor API adaptation; timing and workload live in memory_ops.rs.

use hvisor_memory_bench::{
    arch::{
        paging::{GenericPageTable, GenericPageTableImmut, PageSize, PagingResult},
        Stage2PageTable,
    },
    error::HvResult,
    host,
    memory::{Frame, MemFlags, MemoryRegion},
};

pub use hvisor_memory_bench::{host::POOL_FRAMES, memory::PAGE_SIZE};
pub type Allocation = Frame;
pub type Client = ();
pub type Table = Stage2PageTable;
pub type Mapping = MemoryRegion<usize>;
pub type MapResult = HvResult;
pub type UnmapResult = HvResult;
pub type QueryResult = PagingResult<(usize, MemFlags, PageSize)>;

pub struct Fixture;

impl Fixture {
    pub fn new() -> Self {
        host::init();
        Self
    }

    pub fn new_client(&self) -> Client {}

    #[inline]
    pub fn alloc(&self, _client: &mut Client) -> Allocation {
        // Keep native Frame ownership without storing a large error enum or
        // adding an Option around each successful allocation in the harness.
        Frame::new().expect("allocation failed")
    }

    #[inline]
    pub fn dealloc(&self, _client: &mut Client, frame: Allocation) {
        drop(frame);
    }

    pub fn frame_index(&self, frame: &Allocation) -> usize {
        host::frame_index(frame.start_paddr())
    }

    pub fn assert_all_frames_returned(&self) {
        host::assert_all_frames_returned();
    }

    pub fn new_table(&self) -> Table {
        Table::new(3)
    }

    pub fn destroy_table(&self, table: Table) {
        drop(table);
    }

    #[inline]
    pub fn map(&self, table: &mut Table, mapping: &Mapping) -> MapResult {
        table.map(mapping)
    }

    #[inline]
    pub fn unmap(&self, table: &mut Table, mapping: &Mapping) -> UnmapResult {
        table.unmap(mapping)
    }

    #[inline]
    pub fn query(&self, table: &Table, addr: usize) -> QueryResult {
        table.query(addr)
    }
}

pub fn allocation_size(frame: &Allocation) -> usize {
    frame.size()
}

pub fn mapping(index: usize) -> Mapping {
    Mapping::new_with_offset_mapper(
        0x1000_0000 + index * PAGE_SIZE,
        0x6000_0000 + index * PAGE_SIZE,
        PAGE_SIZE,
        MemFlags::READ | MemFlags::WRITE,
    )
}

pub fn check_map(result: &MapResult) {
    result.as_ref().expect("map failed");
}

pub fn check_unmap(result: &UnmapResult, _index: usize) {
    result.as_ref().expect("unmap failed");
}

pub fn check_query(result: &QueryResult, index: usize, offset: usize) {
    let (paddr, flags, size) = result.as_ref().expect("query failed");
    assert_eq!(*paddr, 0x6000_0000 + index * PAGE_SIZE + offset);
    assert_eq!(flags.bits(), (MemFlags::READ | MemFlags::WRITE).bits());
    assert_eq!(*size, PageSize::Size4K);
}

pub fn check_missing(result: &QueryResult) {
    assert!(matches!(
        result,
        Err(hvisor_memory_bench::paging::PagingError::NotMapped)
    ));
}
