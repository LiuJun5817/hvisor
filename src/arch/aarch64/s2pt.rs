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
use crate::memory::addr::HostPhysAddr;
use aarch64_cpu::registers::{Writeable, VTTBR_EL2};

pub unsafe fn activate_stage2_page_table(root_paddr: HostPhysAddr, vmid: usize) {
    assert!(
        vmid <= u8::MAX as usize,
        "VMID exceeds the configured 8-bit width"
    );
    debug!(
        "activating stage 2 page table at {:#x} with VMID {}",
        root_paddr, vmid
    );
    core::arch::asm!("dsb ish");
    VTTBR_EL2
        .write(VTTBR_EL2::BADDR.val((root_paddr as u64) >> 1) + VTTBR_EL2::VMID.val(vmid as u64));
    core::arch::asm!("isb");
    core::arch::asm!("tlbi vmalls12e1is");
    core::arch::asm!("dsb ish");
    core::arch::asm!("isb");
}

pub fn stage2_mode_detect() {
    info!("Dynamical detection of stage-2 paging mode is not supported yet.");
}
