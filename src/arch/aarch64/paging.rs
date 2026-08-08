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

#![allow(unused)]
use crate::error::{HvError, HvResult};
use crate::memory::addr::is_aligned;
use crate::memory::{Frame, MemFlags, MemoryRegion, PhysAddr, VirtAddr};
use alloc::{sync::Arc, vec::Vec};
use core::{fmt::Debug, marker::PhantomData};
use spin::Mutex;

use crate::memory::frame::gb_allocator;
use verified_hv_mem::{
    address::{
        addr::{PAddr, VAddr},
        frame::{Frame as PtFrame, FrameSize, MemAttr},
    },
    bitmap_allocator::bitmap_impl::BitAlloc1M,
    page_table::{
        pt_arch::{PTArch, PTArchLevel},
        Aarch64PTE, ExPageTable, PTConstants, PageTable,
    },
};

#[derive(Debug)]
pub enum PagingError {
    NoMemory,
    NotMapped,
    AlreadyMapped,
    MappedToHugePage,
}

pub type PagingResult<T = ()> = Result<T, PagingError>;

impl From<PagingError> for HvError {
    fn from(err: PagingError) -> Self {
        match err {
            PagingError::NoMemory => hv_err!(ENOMEM),
            _ => hv_err!(EFAULT, format!("{:?}", err)),
        }
    }
}

#[repr(usize)]
#[derive(Debug, Copy, Clone, Eq, PartialEq)]
pub enum PageSize {
    Size4K = 0x1000,
    Size2M = 0x20_0000,
    Size1G = 0x4000_0000,
}

#[derive(Debug, Copy, Clone)]
pub struct Page<VA> {
    vaddr: VA,
    size: PageSize,
}

impl PageSize {
    pub const fn is_aligned(self, addr: usize) -> bool {
        self.page_offset(addr) == 0
    }

    pub const fn align_down(self, addr: usize) -> usize {
        addr & !(self as usize - 1)
    }

    pub const fn page_offset(self, addr: usize) -> usize {
        addr & (self as usize - 1)
    }

    pub const fn is_huge(self) -> bool {
        matches!(self, Self::Size1G | Self::Size2M)
    }
}

impl<VA: Into<usize> + Copy> Page<VA> {
    pub fn new_aligned(vaddr: VA, size: PageSize) -> Self {
        debug_assert!(size.is_aligned(vaddr.into()));
        Self { vaddr, size }
    }
}

pub trait GenericPTE: Debug + Clone {
    /// Returns the physical address mapped by this entry.
    fn addr(&self) -> PhysAddr;
    /// Returns the flags of this entry.
    fn flags(&self) -> MemFlags;
    /// Returns whether this entry is zero.
    fn is_unused(&self) -> bool;
    /// Returns whether this entry flag indicates present.
    fn is_present(&self) -> bool;
    /// Returns whether this entry maps to a huge frame.
    fn is_huge(&self) -> bool;
    /// Set physical address for terminal entries.
    fn set_addr(&mut self, paddr: PhysAddr);
    /// Set flags for terminal entries.
    fn set_flags(&mut self, flags: MemFlags, is_huge: bool);
    /// Set physical address and flags for intermediate table entries.
    fn set_table(&mut self, paddr: PhysAddr);
    /// Set this entry to zero.
    fn clear(&mut self);
}

const ENTRY_COUNT: usize = 512;

pub trait PagingInstr {
    unsafe fn activate(root_paddr: PhysAddr);
    fn flush(vaddr: Option<usize>);
}

/// A basic read-only page table for address query only.
pub trait GenericPageTableImmut: Sized {
    type VA: From<usize> + Into<usize> + Copy;

    fn level(&self) -> usize;
    fn starting_level(&self) -> usize;

    fn root_paddr(&self) -> PhysAddr;
    fn query(&self, vaddr: Self::VA) -> PagingResult<(PhysAddr, MemFlags, PageSize)>;
}

/// A extended mutable page table can change mappings.
pub trait GenericPageTable: GenericPageTableImmut {
    fn new(level: usize) -> Self;

    fn map(&mut self, region: &MemoryRegion<Self::VA>) -> HvResult;
    fn unmap(&mut self, region: &MemoryRegion<Self::VA>) -> HvResult;
    fn update(
        &mut self,
        vaddr: Self::VA,
        paddr: PhysAddr,
        flags: MemFlags,
    ) -> PagingResult<PageSize>;

    fn clone(&self) -> Self;

    unsafe fn activate(&self);
    fn flush(&self, vaddr: Option<Self::VA>);
}

/// Page table implementation for aarch64.
pub struct HvPageTable<VA: From<usize> + Into<usize> + Copy, I: PagingInstr> {
    inner: ExPageTable<BitAlloc1M, Aarch64PTE>,
    /// Make sure all accesses to the page table and its clonees is exclusive.
    clonee_lock: Arc<Mutex<()>>,
    _phantom: PhantomData<(VA, I)>,
}

impl<VA, I> HvPageTable<VA, I>
where
    VA: From<usize> + Into<usize> + Copy,
    I: PagingInstr,
{
    /// Clone only the top level page table mapping from `src`.
    pub fn clone_from(src: &impl GenericPageTableImmut) -> Self {
        unimplemented!("verified_hv_mem::ExPageTable does not support clone_from yet")
    }
}

impl<VA, I> GenericPageTableImmut for HvPageTable<VA, I>
where
    VA: From<usize> + Into<usize> + Copy,
    I: PagingInstr,
{
    type VA = VA;

    fn level(&self) -> usize {
        self.inner.0.constants.arch.level_count()
    }

    fn starting_level(&self) -> usize {
        0
    }

    fn root_paddr(&self) -> PhysAddr {
        self.inner.0.pt_mem.root.0
    }

    fn query(&self, vaddr: Self::VA) -> PagingResult<(PhysAddr, MemFlags, PageSize)> {
        let _lock = self.clonee_lock.lock();
        let va = vaddr.into();
        self.inner
            .query(VAddr(va))
            .map(|(vb, frame)| {
                let paddr = frame.base.0 + (va - vb.0);
                (
                    paddr,
                    attr_to_flags(frame.attr),
                    frame_size_to_page_size(frame.size),
                )
            })
            .map_err(|_| PagingError::NotMapped)
    }
}

impl<VA, I> GenericPageTable for HvPageTable<VA, I>
where
    VA: From<usize> + Into<usize> + Copy,
    I: PagingInstr,
{
    fn new(level: usize) -> Self {
        assert!(level == 3 || level == 4);
        let constants = hvisor_pt_constants(level);
        Self {
            inner: ExPageTable::<BitAlloc1M, Aarch64PTE>::new(gb_allocator(), constants),
            clonee_lock: Arc::new(Mutex::new(())),
            _phantom: PhantomData,
        }
    }

    fn map(&mut self, region: &MemoryRegion<Self::VA>) -> HvResult {
        let _lock = self.clonee_lock.lock();
        let mut vaddr = region.start.into();
        let mut size = region.size;
        while size > 0 {
            let paddr = region.mapper.map_fn(vaddr);
            let frame_size = if PageSize::Size1G.is_aligned(vaddr)
                && PageSize::Size1G.is_aligned(paddr)
                && size >= PageSize::Size1G as usize
                && !region.flags.contains(MemFlags::NO_HUGEPAGES)
            {
                FrameSize::Size1G
            } else if PageSize::Size2M.is_aligned(vaddr)
                && PageSize::Size2M.is_aligned(paddr)
                && size >= PageSize::Size2M as usize
                && !region.flags.contains(MemFlags::NO_HUGEPAGES)
            {
                FrameSize::Size2M
            } else {
                FrameSize::Size4K
            };
            let frame = PtFrame {
                base: PAddr(paddr),
                size: frame_size,
                attr: flags_to_attr(region.flags),
            };
            self.inner
                .map(gb_allocator(), VAddr(vaddr), frame)
                .map_err(|_| PagingError::AlreadyMapped)?;
            vaddr += frame_size.as_usize();
            size -= frame_size.as_usize();
        }
        Ok(())
    }

    fn unmap(&mut self, region: &MemoryRegion<Self::VA>) -> HvResult {
        let _lock = self.clonee_lock.lock();
        let mut vaddr = region.start.into();
        let mut size = region.size;
        while size > 0 {
            let (vbase, page_size) = self
                .inner
                .query(VAddr(vaddr))
                .map(|(vb, frame)| (vb, frame_size_to_page_size(frame.size)))
                .map_err(|_| PagingError::NotMapped)?;
            if !page_size.is_aligned(vaddr) {
                error!("error vaddr={:#x?}", vaddr);
                loop {}
            }
            self.inner
                .unmap(gb_allocator(), vbase)
                .map_err(|_| PagingError::NotMapped)?;
            vaddr += page_size as usize;
            size -= page_size as usize;
        }
        Ok(())
    }

    fn update(
        &mut self,
        vaddr: Self::VA,
        paddr: PhysAddr,
        flags: MemFlags,
    ) -> PagingResult<PageSize> {
        let _lock = self.clonee_lock.lock();
        let va = vaddr.into();
        let (vbase, old_frame) = self
            .inner
            .query(VAddr(va))
            .map_err(|_| PagingError::NotMapped)?;
        let page_size = frame_size_to_page_size(old_frame.size);
        let offset = va - vbase.0;
        let new_base = paddr.checked_sub(offset).ok_or(PagingError::NotMapped)?;
        self.inner
            .unmap(gb_allocator(), vbase)
            .map_err(|_| PagingError::NotMapped)?;
        let new_frame = PtFrame {
            base: PAddr(new_base),
            size: old_frame.size,
            attr: flags_to_attr(flags),
        };
        self.inner
            .map(gb_allocator(), vbase, new_frame)
            .map(|_| page_size)
            .map_err(|_| {
                let _ = self.inner.map(gb_allocator(), vbase, old_frame);
                PagingError::NoMemory
            })
    }

    fn clone(&self) -> Self {
        let mut pt = Self::clone_from(self);
        // clone with lock to avoid data racing between it and its clonees.
        pt.clonee_lock = self.clonee_lock.clone();
        pt
    }

    unsafe fn activate(&self) {
        I::activate(self.root_paddr())
    }

    fn flush(&self, vaddr: Option<Self::VA>) {
        I::flush(vaddr.map(Into::into))
    }
}

fn hvisor_pt_constants(level: usize) -> PTConstants {
    let mut levels = Vec::new();
    if level == 4 {
        levels.push(PTArchLevel {
            entry_count: ENTRY_COUNT,
            frame_size: FrameSize::Size512G,
        });
    }
    levels.push(PTArchLevel {
        entry_count: ENTRY_COUNT,
        frame_size: FrameSize::Size1G,
    });
    levels.push(PTArchLevel {
        entry_count: ENTRY_COUNT,
        frame_size: FrameSize::Size2M,
    });
    levels.push(PTArchLevel {
        entry_count: ENTRY_COUNT,
        frame_size: FrameSize::Size4K,
    });
    PTConstants {
        arch: PTArch(levels),
        hva_to_pa_offset: 0,
        huge_pages: true,
    }
}

fn attr_to_flags(attr: MemAttr) -> MemFlags {
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

fn flags_to_attr(flags: MemFlags) -> MemAttr {
    MemAttr {
        readable: flags.contains(MemFlags::READ),
        writable: flags.contains(MemFlags::WRITE),
        executable: flags.contains(MemFlags::EXECUTE),
        device: flags.contains(MemFlags::IO),
    }
}

fn page_size_to_frame_size(size: PageSize) -> FrameSize {
    match size {
        PageSize::Size4K => FrameSize::Size4K,
        PageSize::Size2M => FrameSize::Size2M,
        PageSize::Size1G => FrameSize::Size1G,
    }
}

fn frame_size_to_page_size(size: FrameSize) -> PageSize {
    match size {
        FrameSize::Size4K => PageSize::Size4K,
        FrameSize::Size2M => PageSize::Size2M,
        FrameSize::Size1G => PageSize::Size1G,
        _ => panic!("Unsupported frame size"),
    }
}
