QEMU ?= qemu-system-aarch64
QEMU_MACHINE ?= virt-9.0

# Reuse bootstrap firmware, kernel, and root disk from qemu-gicv3. A prepared
# Redis management image can be selected with ROOTFS_IMAGE on the make line.
QEMU_GICV3_IMAGE_DIR ?= platform/aarch64/qemu-gicv3/image
UBOOT ?= $(QEMU_GICV3_IMAGE_DIR)/bootloader/u-boot-atf.bin
zone0_kernel ?= $(QEMU_GICV3_IMAGE_DIR)/kernel/Image
ROOTFS_IMAGE ?= $(QEMU_GICV3_IMAGE_DIR)/virtdisk/rootfs1.ext4
zone0_dtb := $(image_dir)/dts/zone0.dtb

QEMU_ARGS := -machine $(QEMU_MACHINE),secure=on,gic-version=3,virtualization=on,its=off
QEMU_ARGS += -accel tcg,thread=multi
QEMU_ARGS += -cpu cortex-a72
QEMU_ARGS += -smp 4
QEMU_ARGS += -m 2G
QEMU_ARGS += -nic none
QEMU_ARGS += -nographic
QEMU_ARGS += -bios $(UBOOT)

QEMU_ARGS += -device loader,file="$(hvisor_bin)",addr=0x40400000,force-raw=on
QEMU_ARGS += -device loader,file="$(zone0_kernel)",addr=0xa0400000,force-raw=on
QEMU_ARGS += -device loader,file="$(zone0_dtb)",addr=0xa0000000,force-raw=on

# Keep root's bootstrap disk on the highest QEMU virtio-MMIO transport.
QEMU_ARGS += -drive if=none,file="$(ROOTFS_IMAGE)",id=rootfs,format=raw
QEMU_ARGS += -device virtio-blk-device,drive=rootfs,bus=virtio-mmio-bus.31

MESSAGE := "Redis evaluation: GICv3, 4 CPUs, 2 GiB, MMIO only"

$(hvisor_bin): elf
	@if ! command -v mkimage > /dev/null; then \
		sudo apt update && sudo apt install u-boot-tools; \
	fi && \
	$(OBJCOPY) $(hvisor_elf) --strip-all -O binary $(hvisor_bin).tmp && \
	mkimage -n hvisor_img -A arm64 -O linux -C none -T kernel -a 0x40400000 \
		-e 0x40400000 -d $(hvisor_bin).tmp $(hvisor_bin)
