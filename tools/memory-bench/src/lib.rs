//! Host adapters for the production allocator, page table and memory sets.
//! This unpublished crate is only used by the Criterion executable.

#![allow(dead_code, unused_imports)]

#[cfg(not(feature = "host-bench"))]
compile_error!("the host-bench feature is required by this benchmark crate");
#[cfg(not(all(target_pointer_width = "64", target_endian = "little")))]
compile_error!("the AArch64 page-table benchmark requires a 64-bit little-endian host");

#[macro_use]
extern crate alloc;
#[macro_use]
extern crate log;

#[macro_use]
#[path = "../../../src/error.rs"]
pub mod error;
#[path = "../../../src/memory/mod.rs"]
pub mod memory;

#[path = "../../../src/arch/aarch64/paging.rs"]
pub mod paging;
#[path = "../../../src/arch/aarch64/s2pt.rs"]
pub mod s2pt;

pub mod region_zone;
pub use memory::PAGE_SIZE;
pub use region_zone::{mapping, Fixture, Mapping, OpResult};

pub mod arch {
    pub use crate::paging;
    pub use crate::s2pt::Stage2PageTable;
}

// map/unmap/query never call these hardware operations. Fail explicitly if a
// future benchmark accidentally tries to activate or flush a host page table.
impl paging::PagingInstr for s2pt::S2PTInstr {
    unsafe fn activate(_root_paddr: usize) {
        panic!("cannot activate stage-2 translation in a host benchmark");
    }

    fn flush(_vaddr: Option<usize>) {
        panic!("cannot flush a hardware TLB in a host benchmark");
    }
}

// Replace linker-provided pool bounds only; frame::init and the allocation,
// locking, RAII release, PTE encoding and page-table walk code remain shared.
mod consts {
    pub const PAGE_SIZE: usize = 4096;

    pub fn mem_pool_start() -> usize {
        crate::host::pool_base()
    }

    pub fn hv_end() -> usize {
        mem_pool_start() + crate::host::POOL_FRAMES * PAGE_SIZE
    }
}

pub mod host {
    use std::{
        alloc::{alloc_zeroed, handle_alloc_error, Layout},
        sync::{Once, OnceLock},
    };

    use crate::{
        consts::PAGE_SIZE,
        error::HvErrorNum,
        memory::{frame, Frame},
    };

    pub const POOL_FRAMES: usize = 1024;

    pub(crate) fn pool_base() -> usize {
        static BASE: OnceLock<usize> = OnceLock::new();
        *BASE.get_or_init(|| {
            let layout = Layout::from_size_align(POOL_FRAMES * PAGE_SIZE, PAGE_SIZE).unwrap();
            // SAFETY: nonzero aligned layout. The allocation is kept for the
            // process lifetime because the production allocator is global.
            let ptr = unsafe { alloc_zeroed(layout) };
            if ptr.is_null() {
                handle_alloc_error(layout);
            }
            let base = ptr as usize;
            assert!(
                base.checked_add(layout.size()).unwrap() <= (1usize << 48),
                "host pool pointers must fit the AArch64 PTE's 48-bit address field"
            );
            // Commit each host page before any measured operation.
            for page in 0..POOL_FRAMES {
                // SAFETY: in-bounds writes before any Frame can own the memory.
                unsafe { ptr.add(page * PAGE_SIZE).write_volatile(0) };
            }
            base
        })
    }

    /// Initialize the real frame allocator once; Criterion runs cases serially.
    pub fn init() {
        static INIT: Once = Once::new();
        INIT.call_once(frame::init);
    }

    /// Validate a physical frame address against the real backing allocation.
    pub fn frame_index(paddr: usize) -> usize {
        let offset = paddr.checked_sub(pool_base()).expect("frame below pool");
        assert!(offset < POOL_FRAMES * PAGE_SIZE, "frame outside pool");
        assert_eq!(offset % PAGE_SIZE, 0, "unaligned frame");
        offset / PAGE_SIZE
    }

    /// Check recovery, uniqueness, alignment and exhaustion outside timing.
    /// Only call when no benchmark owns frames or page tables.
    pub fn assert_all_frames_returned() {
        init();
        let mut frames = Vec::with_capacity(POOL_FRAMES);
        let mut seen = [false; POOL_FRAMES];
        for _ in 0..POOL_FRAMES {
            let frame = Frame::new().expect("a pool frame was not returned");
            assert_eq!(frame.size(), PAGE_SIZE);
            let index = frame_index(frame.start_paddr());
            assert!(!seen[index], "duplicate allocation");
            seen[index] = true;
            frames.push(frame);
        }
        assert!(seen.into_iter().all(|present| present));
        assert_eq!(Frame::new().unwrap_err().num, HvErrorNum::ENOMEM);
        drop(frames);
    }
}
