//! hvisor's AArch64 implementation of VeriHyMem hardware maintenance.
//!
//! CPU stage-2 invalidation is implemented here.  SMMU operations are routed
//! through the hvisor IOMMU boundary so the backend remains independent of the
//! concrete SMMUv3 register implementation.

use core::arch::asm;
use verified_hv_mem::hardware::{HardwareInstr, MmuInstr, SmmuInstr, ZoneIdInstr};

/// hvisor-owned, zero-sized AArch64 hardware backend for `MmuHardware`.
pub struct HvisorAarch64Hardware;

impl ZoneIdInstr for HvisorAarch64Hardware {
    // `valid_zone_id` is a VeriHyMem specification-only method and therefore
    // has no executable implementation in hvisor.
}

impl MmuInstr for HvisorAarch64Hardware {
    fn issue_tlbi_s2_sync(zone_id: usize, ipa_page: usize) {
        // `IPAS2E1IS` uses the IPA page number.  Temporarily select the VMID
        // in VTTBR_EL2 and restore the caller's value after completion.
        unsafe {
            let old_vttbr: u64;
            asm!("mrs {old}, vttbr_el2", old = out(reg) old_vttbr);
            let target_vttbr =
                (old_vttbr & 0x0000_ffff_ffff_ffff) | (((zone_id as u64) & 0xff) << 48);
            asm!("msr vttbr_el2, {target}", target = in(reg) target_vttbr);
            asm!("isb");
            asm!("tlbi ipas2e1is, {ipa}", ipa = in(reg) ipa_page);
            asm!("dsb ish");
            asm!("msr vttbr_el2, {old}", old = in(reg) old_vttbr);
            asm!("isb");
        }
    }

    fn issue_tlbi_s2_range_sync(zone_id: usize, ipa_page: usize, page_count: usize) {
        // The hvisor page-table path currently supplies block bases.  A
        // conservative page walk keeps this backend correct for both block and
        // page mappings until range TLBI selection is centralized.
        for page in 0..page_count {
            Self::issue_tlbi_s2_sync(zone_id, ipa_page + page);
        }
    }

    fn issue_dsb_ish() {
        unsafe {
            asm!("dsb ish");
        }
    }
}

impl SmmuInstr for HvisorAarch64Hardware {
    fn issue_smmu_tlbi_s2(zone_id: usize, ipa_page: usize) {
        crate::device::iommu::stage2_tlbi_s2(zone_id, ipa_page);
    }

    fn issue_smmu_tlbi_s2_range(zone_id: usize, ipa_page: usize, page_count: usize) {
        for page in 0..page_count {
            Self::issue_smmu_tlbi_s2(zone_id, ipa_page + page);
        }
    }

    fn issue_smmu_sync() {
        crate::device::iommu::stage2_tlbi_sync();
    }
}

impl HardwareInstr for HvisorAarch64Hardware {}

pub type HvisorHardware = HvisorAarch64Hardware;
