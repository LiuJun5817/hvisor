#!/usr/bin/env python3
"""QEMU console control for the AArch64 Redis experiment (stdlib only)."""

import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / "target/redis-bench"


class Console:
    def __init__(self, command, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        self.log = (self.directory / "console.log").open("wb")
        self.data = bytearray()
        self.condition = threading.Condition()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        bufsize=0, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            chunk = self.process.stdout.read(4096)
            if not chunk:
                break
            with self.condition:
                self.log.write(chunk)
                self.log.flush()
                self.data.extend(chunk)
                self.condition.notify_all()
        with self.condition:
            self.condition.notify_all()

    def send(self, command):
        self.process.stdin.write(command.encode() + b"\r")
        self.process.stdin.flush()

    def expect(self, pattern, timeout=120, offset=0):
        regex = re.compile(pattern, re.MULTILINE)
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                match = regex.search(self.data, offset)
                if match:
                    return match
                if self.process.poll() is not None:
                    raise RuntimeError(f"QEMU exited ({self.process.returncode}); see {self.directory}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"console pattern {pattern!r}; see {self.directory}")
                self.condition.wait(min(remaining, 0.25))

    def command(self, command, timeout=120):
        marker = "BENCH_DONE_" + uuid.uuid4().hex
        offset = len(self.data)
        self.send(command + "; printf '\\n" + marker + ":%s\\n' \"$?\"")
        match = self.expect(rb"^" + marker.encode() + rb":(\d+)\r?$", timeout, offset)
        output = bytes(self.data[offset:match.start()]).decode(errors="replace")
        if int(match[1]) != 0:
            raise RuntimeError(f"guest command failed ({match[1].decode()}): {command}\n{output}")
        return output

    def start_boot(self, timeout=120):
        self.expect(rb"Hit any key to stop autoboot:", timeout)
        self.send("")
        self.expect(rb"=> ", 10)
        allowed = sorted(os.sched_getaffinity(self.process.pid))
        if len(allowed) < 6:
            raise ValueError("use at least six QEMU host CPUs: four vCPU CPUs and two I/O CPUs")
        mappings = []
        for task in (Path("/proc") / str(self.process.pid) / "task").iterdir():
            name = (task / "comm").read_text().strip()
            match = re.fullmatch(r"CPU ([0-3])/TCG", name)
            affinity = {allowed[int(match[1])]} if match else set(allowed[4:])
            os.sched_setaffinity(int(task.name), affinity)
            mappings.append({"tid": int(task.name), "name": name, "cpus": sorted(affinity)})
        (self.directory / "thread-affinity.json").write_text(json.dumps(mappings, indent=2) + "\n")
        if sum(bool(re.fullmatch(r"CPU ([0-3])/TCG", item["name"])) for item in mappings) != 4:
            raise RuntimeError(f"could not identify all four QEMU TCG vCPU threads: {mappings}")
        start_ns = time.monotonic_ns()
        self.send("bootm 0x40400000 - 0x40000000")
        return start_ns

    def boot(self, timeout=120, prepared=False):
        start_ns = self.start_boot(timeout)
        self.expect(rb"REDIS_BENCH_GUEST_READY" if prepared else rb"job control turned off", timeout)
        if not prepared:
            self.command("mount -t proc proc /proc 2>/dev/null || true")
            self.command("mount -t sysfs sysfs /sys 2>/dev/null || true")
        return start_ns

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def qemu_command(variant, disk, port=16379, cpus="0-5", prepared=False, build_root=None):
    build_root = Path(build_root) if build_root is not None else ARTIFACTS / "build"
    image = build_root / variant / "src/target/aarch64-unknown-none/release/hvisor.bin"
    assets = ARTIFACTS / "assets"
    command = ["taskset", "-c", cpus, "qemu-system-aarch64",
               "-name", "redis-bench,debug-threads=on",
               "-machine", "virt,secure=on,gic-version=3,virtualization=on,iommu=smmuv3",
               "-accel", "tcg,thread=multi", "-global", "arm-smmuv3.stage=2",
               "-cpu", "cortex-a72", "-smp", "4", "-m", "2G", "-nographic",
               "-monitor", "none", "-bios",
               str(REPO / "platform/aarch64/qemu-gicv3/image/bootloader/u-boot-atf.bin")]
    dtb = Path(disk).resolve().parent / "zone0-redis.dtb" if prepared else assets / "zone0.dtb"
    for file, address in [(image, "0x40400000"), (assets / "Image", "0xa0400000"),
                          (dtb, "0xa0000000")]:
        command += ["-device", f"loader,file={file},addr={address},force-raw=on"]
    command += ["-drive", f"if=none,file={Path(disk).resolve()},id=rootfs,format=raw,snapshot=on",
                "-device", "virtio-blk-device,drive=rootfs,bus=virtio-mmio-bus.31"]
    for index in range(1, 4):
        command += ["-netdev", f"user,id=net{index}", "-device",
                    f"virtio-net-pci,netdev=net{index},disable-legacy=on,disable-modern=off,iommu_platform=on"]
    if prepared:
        # IRQ 77 and the full MMIO window already belong to root zone0.
        command += ["-netdev", f"user,id=bench,hostfwd=tcp:127.0.0.1:{port}-:6379",
                    "-device", "virtio-net-device,netdev=bench,bus=virtio-mmio-bus.29"]
    return command


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["baseline", "verified"], required=True)
    parser.add_argument("--disk", type=Path, default=ARTIFACTS / "assets/rootfs1.ext4")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--command", default="uname -a; ip -brief address; ls /home/arm64")
    parser.add_argument("--prepared", action="store_true")
    parser.add_argument("--build-root", type=Path, default=ARTIFACTS / "build")
    args = parser.parse_args()
    with Console(qemu_command(args.variant, args.disk, prepared=args.prepared,
                              build_root=args.build_root.resolve()), args.output) as console:
        console.boot(prepared=args.prepared)
        print(console.command(args.command), flush=True)
