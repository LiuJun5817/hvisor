//! Region insert/remove and zone create/remove loops driven by tools/bench_region.sh.

use super::verihymem::{hv_mem, VMemorySet};
use super::{MemFlags, MemoryRegion, PAGE_SIZE};
use crate::arch::ivc::IVC_INFOS;
use crate::config::{
    HvConfigMemoryRegion, HvIvcConfig, HvPciConfig, HvPciDevConfig, HvZoneConfig,
    CONFIG_INTERRUPTS_BITMAP_BITS_PER_WORD, CONFIG_MAX_INTERRUPTS, CONFIG_MAX_IVC_CONFIGS,
    CONFIG_MAX_MEMORY_REGIONS, CONFIG_MAX_PCI_DEV, CONFIG_NAME_MAXLEN, CONFIG_PCI_BUS_MAXNUM,
    MEM_TYPE_RAM,
};
use crate::consts::{hv_end, MAX_ZONE_NUM};
use crate::zone::{find_zone, remove_zone, zone_create, ZonePayload};
use alloc::vec::Vec;
use core::arch::asm;

// Exclusive test RAM: no guest is started by the QEMU test image. This range
// is outside hvisor/its allocator and images loaded by test/runner.sh. Region
// tests only create mappings; zone creation also performs RAM cache maintenance.
const PA_START: usize = 0x6000_0000;
const PA_END: usize = 0x6800_0000;
const GPA_START: usize = 0x1000_0000;
const REGION_ZONE_ID: usize = MAX_ZONE_NUM - 1;
const ZONE_ID: usize = 1;

fn parameter(value: Option<&str>, default: usize, max: usize) -> usize {
    let value = value
        .map(|s| s.parse::<usize>().unwrap())
        .unwrap_or(default);
    assert!(
        (1..=max).contains(&value),
        "invalid memory benchmark parameter"
    );
    value
}

fn ticks() -> u64 {
    let value: u64;
    unsafe {
        asm!("dsb ish", "isb", "mrs {value}, cntpct_el0", "isb",
             value = out(reg) value, options(nostack, preserves_flags));
    }
    value
}

fn report(operation: &str, elapsed: u64, hz: u64, operations: usize) {
    // Generic timer ticks are not CPU cycles. Keep integer arithmetic so this
    // no_std test needs no floating-point support.
    let ns = elapsed as u128 * 1_000_000_000 / hz as u128;
    println!(
        "{}: {} operations, total={} us, average={} ns/op",
        operation,
        operations,
        ns / 1000,
        ns / operations as u128
    );
}

fn zone_config(count: usize, size: usize) -> HvZoneConfig {
    let mut regions = [HvConfigMemoryRegion {
        mem_type: MEM_TYPE_RAM,
        physical_start: 0,
        virtual_start: 0,
        size: 0,
    }; CONFIG_MAX_MEMORY_REGIONS];
    for (i, region) in regions[..count].iter_mut().enumerate() {
        *region = HvConfigMemoryRegion {
            mem_type: MEM_TYPE_RAM,
            physical_start: (PA_START + i * size) as u64,
            virtual_start: (GPA_START + i * size) as u64,
            size: size as u64,
        };
    }
    let mut name = [0; CONFIG_NAME_MAXLEN];
    name[..8].copy_from_slice(b"membench");
    HvZoneConfig::new(
        ZONE_ID as u32,
        0, // No vCPUs: construct the zone without starting a guest.
        count as u32,
        regions,
        [0; CONFIG_MAX_INTERRUPTS / CONFIG_INTERRUPTS_BITMAP_BITS_PER_WORD],
        0,
        [HvIvcConfig::default(); CONFIG_MAX_IVC_CONFIGS],
        0, // entry_point
        0, // kernel_load_paddr
        0, // kernel_size
        0, // dtb_load_paddr
        0, // dtb_size
        name,
        crate::platform::ROOT_ARCH_ZONE_CONFIG.clone(),
        0,
        [HvPciConfig::new_empty(); CONFIG_PCI_BUS_MAXNUM],
        0,
        [HvPciDevConfig::default(); CONFIG_MAX_PCI_DEV],
    )
}

fn bench_zones(config: &HvZoneConfig, rounds: usize) -> (u64, u64) {
    assert!(!cfg!(viommu), "zone benchmark requires VIOMMU disabled");
    assert!(!IVC_INFOS.lock().contains_key(&ZONE_ID));
    let (mut create_ticks, mut remove_ticks) = (0, 0);
    for round in 0..=rounds {
        // This branch's native zone_create also registers the zone in HvMem.
        let start = ticks();
        let created = zone_create(config);
        let elapsed = ticks().wrapping_sub(start);
        let zone = created.expect("zone create failed");

        // Validate CPU ownership and both sets' metadata outside the timers.
        // VMemorySet::page_table_query is not a hardware page-table walk.
        assert_eq!(zone.id(), ZONE_ID);
        assert_eq!(zone.cpu_set().bitmap, 0);
        assert_eq!(zone.cpu_num(), 0);
        let gpm = zone.gpm();
        let iommu_pt = zone.iommu_pt();
        assert_eq!(iommu_pt.is_some(), cfg!(iommu));
        for region in config.memory_regions() {
            for offset in [0, region.size as usize - PAGE_SIZE] {
                let gpa = region.virtual_start as usize + offset;
                let pa = region.physical_start as usize + offset;
                assert_eq!(unsafe { gpm.page_table_query(gpa) }.unwrap().0, pa);
                if let Some(pt) = iommu_pt {
                    assert_eq!(unsafe { pt.page_table_query(gpa) }.unwrap().0, pa);
                }
            }
        }
        assert!(find_zone(ZONE_ID).is_some());

        // Zone and VMemorySet are ID handles. The native remove operation
        // clears mappings and frees the zone payload and page tables in HvMem.
        let start = ticks();
        remove_zone(ZONE_ID);
        let removed = ticks().wrapping_sub(start);
        assert!(find_zone(ZONE_ID).is_none());
        // Even an empty IVC configuration creates a global info record.
        // remove_zone does not clear it; restore the fixture outside timing.
        let ivc_info = IVC_INFOS.lock().remove(&ZONE_ID).unwrap();
        let ivc_count = ivc_info.len;
        assert_eq!(ivc_count, 0);
        if round != 0 {
            create_ticks += elapsed;
            remove_ticks += removed;
        }
    }
    (create_ticks, remove_ticks)
}

pub fn run() {
    assert_eq!(option_env!("ARCH"), Some("aarch64"));
    assert_eq!(option_env!("BOARD"), Some("qemu-gicv3"));
    for zid in 0..MAX_ZONE_NUM {
        assert!(
            hv_mem().with_zone(zid, |_| ()).is_none(),
            "guest zone is running"
        );
    }
    let count = parameter(option_env!("MEMBENCH_REGIONS"), 1024, 4096);
    let pages = parameter(option_env!("MEMBENCH_REGION_PAGES"), 1, 32768);
    let rounds = parameter(option_env!("MEMBENCH_ROUNDS"), 100, 10000);
    let zone_regions = parameter(
        option_env!("MEMBENCH_ZONE_REGIONS"),
        1,
        CONFIG_MAX_MEMORY_REGIONS,
    );
    let size = pages * PAGE_SIZE;
    assert!(hv_end() <= PA_START && PA_START + count * size <= PA_END);
    assert!(PA_START + zone_regions * size <= PA_END);
    let config = zone_config(zone_regions, size);
    let regions: Vec<_> = (0..count)
        .map(|i| {
            MemoryRegion::new_with_offset_mapper(
                GPA_START + i * size,
                PA_START + i * size,
                size,
                MemFlags::READ | MemFlags::WRITE,
            )
        })
        .collect();

    let hz: u64;
    let daif: u64;
    unsafe {
        asm!("mrs {hz}, cntfrq_el0", "mrs {daif}, daif", "msr daifset, #0xf",
             hz = out(reg) hz, daif = out(reg) daif, options(nostack, preserves_flags));
    }
    assert_ne!(hz, 0);
    let (mut insert_ticks, mut remove_ticks) = (0, 0);
    for round in 0..=rounds {
        // Each round starts with an empty zone; round zero is warmup.
        hv_mem()
            .add_zone(REGION_ZONE_ID, ZonePayload::new(&[0; CONFIG_NAME_MAXLEN]))
            .unwrap();
        let mut set = VMemorySet::new(REGION_ZONE_ID, false);
        let start = ticks();
        for region in &regions {
            set.insert(region.clone()).expect("region insert failed");
        }
        let inserted = ticks().wrapping_sub(start);
        for region in &regions {
            // This API checks region metadata, not a hardware page-table walk.
            assert_eq!(
                unsafe { set.page_table_query(region.start) }.unwrap().0,
                region.mapper.map_fn(region.start)
            );
        }

        let start = ticks();
        for region in &regions {
            set.delete(region.start, region.size)
                .expect("region delete failed");
        }
        let removed = ticks().wrapping_sub(start);
        for region in &regions {
            assert!(unsafe { set.page_table_query(region.start) }.is_err());
        }
        hv_mem()
            .remove_zone(REGION_ZONE_ID)
            .expect("zone cleanup failed");
        assert!(hv_mem().with_zone(REGION_ZONE_ID, |_| ()).is_none());
        if round != 0 {
            insert_ticks += inserted;
            remove_ticks += removed;
        }
    }
    let (zone_create_ticks, zone_remove_ticks) = bench_zones(&config, rounds);
    unsafe {
        asm!("msr daif, {daif}", daif = in(reg) daif, options(nostack, preserves_flags));
    }
    println!("=== Region Benchmark ===");
    println!(
        "regions={}, region_size={} bytes, rounds={} (+1 warmup)",
        count, size, rounds
    );
    report("insert", insert_ticks, hz, count * rounds);
    report("remove", remove_ticks, hz, count * rounds);
    println!("=== Region Benchmark Done ===");
    println!("=== Zone Benchmark ===");
    println!(
        "zones_per_round=1, regions_per_zone={}, region_size={} bytes, rounds={} (+1 warmup)",
        zone_regions, size, rounds
    );
    println!("zone_id={}, vcpus=0, iommu={}", ZONE_ID, cfg!(iommu));
    report("create", zone_create_ticks, hz, rounds);
    report("remove", zone_remove_ticks, hz, rounds);
    println!("=== Zone Benchmark Done ===");
}
