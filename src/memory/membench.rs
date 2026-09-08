//! Minimal region insert/delete loop driven by tools/bench_region.sh.

use super::verihymem::{hv_mem, VMemorySet};
use super::{MemFlags, MemoryRegion, PAGE_SIZE};
use crate::config::CONFIG_NAME_MAXLEN;
use crate::consts::{hv_end, MAX_ZONE_NUM};
use crate::zone::ZonePayload;
use alloc::vec::Vec;
use core::arch::asm;

// Exclusive test RAM: no guest is started by the QEMU test image. This range
// is outside hvisor/its allocator and images loaded by test/runner.sh. Only
// mappings are created; the backing RAM is neither allocated nor accessed.
const PA_START: usize = 0x6000_0000;
const PA_END: usize = 0x6800_0000;
const GPA_START: usize = 0x1000_0000;
const ZONE: usize = MAX_ZONE_NUM - 1;

fn parameter(value: Option<&str>, default: usize, max: usize) -> usize {
    let value = value
        .map(|s| s.parse::<usize>().unwrap())
        .unwrap_or(default);
    assert!(
        (1..=max).contains(&value),
        "invalid region benchmark parameter"
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
    let size = pages * PAGE_SIZE;
    assert!(hv_end() <= PA_START && PA_START + count * size <= PA_END);
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
            .add_zone(ZONE, ZonePayload::new(&[0; CONFIG_NAME_MAXLEN]))
            .unwrap();
        let mut set = VMemorySet::new(ZONE, false);
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
        hv_mem().remove_zone(ZONE).expect("zone cleanup failed");
        assert!(hv_mem().with_zone(ZONE, |_| ()).is_none());
        if round != 0 {
            insert_ticks += inserted;
            remove_ticks += removed;
        }
    }
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
}
