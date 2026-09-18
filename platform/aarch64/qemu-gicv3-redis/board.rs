// Copyright (c) 2025 Syswonder
// hvisor is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of Mulan PSL v2.

use crate::{
    arch::{
        mmu::MemoryType,
        zone::{GicConfig, Gicv3Config, HvArchZoneConfig},
    },
    config::*,
};

#[allow(unused)]
pub const BOARD_NAME: &str = "qemu-gicv3-redis";

pub const BOARD_NCPUS: usize = 4;
pub const BOARD_UART_BASE: u64 = 0x0900_0000;

#[rustfmt::skip]
pub static BOARD_MPIDR_MAPPINGS: [u64; BOARD_NCPUS] = [
    0x0,
    0x1,
    0x2,
    0x3,
];

/// QEMU virt-9.0 with 2 GiB starting at 0x4000_0000.
#[rustfmt::skip]
pub const BOARD_PHYSMEM_LIST: &[(u64, u64, MemoryType)] = &[
    (         0x0, 0x1000_0000, MemoryType::Device),
    ( 0x4000_0000, 0xc000_0000, MemoryType::Normal),
];

pub const ROOT_ZONE_DTB_ADDR: u64 = 0xa000_0000;
pub const ROOT_ZONE_KERNEL_ADDR: u64 = 0xa040_0000;
pub const ROOT_ZONE_ENTRY: u64 = ROOT_ZONE_KERNEL_ADDR;
pub const ROOT_ZONE_CPUS: u64 = (1 << 0) | (1 << 1);
pub const ROOT_ZONE_NAME: &str = "root-linux";

/// Root maps both guest backing ranges for image loading and the virtio
/// backend. The root DTB reserves them from ordinary Linux allocation.
/// Separate entries preserve the eventual Shared/root-Private budget boundary.
pub const ROOT_ZONE_MEMORY_REGIONS: &[HvConfigMemoryRegion] = &[
    HvConfigMemoryRegion {
        mem_type: MEM_TYPE_RAM,
        physical_start: 0x5000_0000,
        virtual_start: 0x5000_0000,
        size: 0x2000_0000,
    }, // zone 1 / Redis primary, shared with root
    HvConfigMemoryRegion {
        mem_type: MEM_TYPE_RAM,
        physical_start: 0x7000_0000,
        virtual_start: 0x7000_0000,
        size: 0x2000_0000,
    }, // zone 2 / Redis replica, shared with root
    HvConfigMemoryRegion {
        mem_type: MEM_TYPE_RAM,
        physical_start: 0x9000_0000,
        virtual_start: 0x9000_0000,
        size: 0x3000_0000,
    }, // root-private usable RAM
    HvConfigMemoryRegion {
        mem_type: MEM_TYPE_IO,
        physical_start: 0x0900_0000,
        virtual_start: 0x0900_0000,
        size: 0x1000,
    }, // PL011
    HvConfigMemoryRegion {
        mem_type: MEM_TYPE_IO,
        physical_start: 0x0a00_0000,
        virtual_start: 0x0a00_0000,
        size: 0x4000,
    }, // QEMU virtio-MMIO transports
];

pub const IRQ_WAKEUP_VIRTIO_DEVICE: usize = 32 + 0x20;

/// PL011, hvisor virtio wakeup, and the root disk in virtio-MMIO slot 31.
pub const ROOT_ZONE_IRQS_BITMAP: &[BitmapWord] = &get_irqs_bitmap(&[33, 64, 79]);

pub const ROOT_ARCH_ZONE_CONFIG: HvArchZoneConfig = HvArchZoneConfig {
    is_aarch32: 0,
    gic_config: GicConfig::Gicv3(Gicv3Config {
        gicd_base: 0x0800_0000,
        gicd_size: 0x1_0000,
        gicr_base: 0x080a_0000,
        gicr_size: 0xf6_0000,
        gits_base: 0,
        gits_size: 0,
    }),
};

pub const ROOT_ZONE_IVC_CONFIG: [HvIvcConfig; 0] = [];
pub const ROOT_PCI_CONFIG: [HvPciConfig; 0] = [];
pub const ROOT_PCI_DEVS: &[HvPciDevConfig] = &[];
