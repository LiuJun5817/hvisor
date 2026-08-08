// Copyright (c) 2025 Syswonder
// hvisor is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//     http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER
// EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY OR
// FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.
//
// Syswonder Website:
//      https://www.syswonder.org
//
// Authors:
//
//! Physical memory allocation.

use alloc::vec::Vec;

use super::addr::{align_down, align_up, is_aligned, PhysAddr};
use crate::consts::PAGE_SIZE;
use crate::error::HvResult;
use crate::memory::addr::virt_to_phys;

use vstd::prelude::Tracked;
use verified_hv_mem::global_allocator::GbAlloc;

pub fn gb_allocator() -> &'static GbAlloc {
    crate::memory::verihymem::global_allocator()
}

/// A safe wrapper for physical frame allocation.
#[derive(Debug)]
pub struct Frame {
    start_paddr: PhysAddr,
    frame_count: usize,
}

#[allow(dead_code)]
impl Frame {
    /// Allocate one physical frame.
    pub fn new() -> HvResult<Self> {
        let (start_paddr, _) = gb_allocator().alloc(Tracked::assume_new());
        Ok(Self {
            start_paddr: start_paddr.0,
            frame_count: 1,
        })
    }

    /// Allocate one physical frame and fill with zero.
    pub fn new_zero() -> HvResult<Self> {
        let mut f = Self::new()?;
        f.clear();
        Ok(f)
    }

    /// Allocate contiguous physical frames.
    ///
    /// `align_to` specifies the byte alignment of the start physical address.
    /// Must be a power of 2 and a multiple of `PAGE_SIZE`. Pass `0` for no
    /// alignment requirement.
    pub fn new_contiguous(frame_count: usize, align_to: usize) -> HvResult<Self> {
        let align_log2 = if align_to == 0 {
            0
        } else {
            debug_assert!(
                align_to.is_power_of_two(),
                "new_contiguous: align_to {:#x} is not a power of 2",
                align_to
            );
            debug_assert!(
                align_to % PAGE_SIZE == 0,
                "new_contiguous: align_to {:#x} is not a multiple of PAGE_SIZE",
                align_to
            );
            (align_to / PAGE_SIZE).trailing_zeros() as usize
        };
        let (start_paddr, _) =
            gb_allocator().alloc_contiguous(Tracked::assume_new(), frame_count, align_log2);
        Ok(Self {
            start_paddr: start_paddr.0,
            frame_count,
        })
    }

    /// Constructs a frame from a raw physical address without automatically calling the destructor.
    ///
    /// # Safety
    ///
    /// This function is unsafe because the user must ensure that this is an available physical
    /// frame.
    pub unsafe fn from_paddr(start_paddr: PhysAddr) -> Self {
        assert!(is_aligned(start_paddr));
        Self {
            start_paddr,
            frame_count: 0,
        }
    }

    /// Get the start physical address of this frame.
    pub fn start_paddr(&self) -> PhysAddr {
        self.start_paddr
    }

    /// Get the total size (in bytes) of this frame.
    pub fn size(&self) -> usize {
        self.frame_count * PAGE_SIZE
    }

    /// convert to raw a pointer.
    pub fn as_ptr(&self) -> *const u8 {
        self.start_paddr as *const u8
    }

    /// convert to a mutable raw pointer.
    pub fn as_mut_ptr(&self) -> *mut u8 {
        self.start_paddr as *mut u8
    }

    /// Fill `self` with `byte`.
    pub fn fill(&mut self, byte: u8) {
        let ptr = self.as_mut_ptr();
        for i in 0..self.size() {
            unsafe {
                *ptr.add(i) = byte;
            }
        }
    }

    /// Fill `self` with zero.
    pub fn clear(&mut self) {
        self.fill(0)
    }

    /// Forms a slice that can read data.
    pub fn as_slice(&self) -> &[u8] {
        unsafe { core::slice::from_raw_parts(self.as_ptr(), self.size()) }
    }

    /// Forms a mutable slice that can write data.
    pub fn as_slice_mut(&mut self) -> &mut [u8] {
        unsafe { core::slice::from_raw_parts_mut(self.as_mut_ptr(), self.size()) }
    }

    pub fn copy_data_from(&mut self, data: &[u8]) {
        let len = data.len();
        assert!(data.len() <= self.size());
        self.as_slice_mut()[..len].copy_from_slice(data);
    }
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
    crate::memory::verihymem::init_hv_mem(
        align_up(virt_to_phys(mem_pool_start)),
        page_count,
        pt_level,
    );

    info!(
        "Frame allocator initialization finished: {:#x?}",
        mem_pool_start..mem_pool_end
    );
}

pub fn test() {
    let mut v: Vec<Frame> = Vec::new();
    for _ in 0..5 {
        let frame = Frame::new().unwrap();
        // println!("{:x?}", frame);
        v.push(frame);
    }
    v.clear();
    for _ in 0..5 {
        let frame = Frame::new().unwrap();
        // println!("{:x?}", frame);
        v.push(frame);
    }
    drop(v);
    info!("frame_allocator_test passed!");
}
