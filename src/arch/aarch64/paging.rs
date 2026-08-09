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

use alloc::vec::Vec;

use crate::error::HvError;
use verified_hv_mem::{
    address::frame::FrameSize,
    page_table::{
        pt_arch::{PTArch, PTArchLevel},
        Aarch64PTE, PTConstants,
    },
};

/// Architecture-selected VeriHyMem page-table entry type.
///
/// Generic integration code uses this alias instead of depending on the
/// concrete AArch64 PTE implementation directly.
pub type HvisorPTE = Aarch64PTE;

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

const ENTRY_COUNT: usize = 512;

pub(crate) fn hvisor_pt_constants(level: usize) -> PTConstants {
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
