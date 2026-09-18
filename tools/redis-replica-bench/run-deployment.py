#!/usr/bin/env python3
"""Run one measured empty-dataset primary--replica deployment."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from qemu import Console, PLATFORM, REPO, qemu_command


VARIANT_DIRS = {
    "native": Path(os.environ.get("REDIS_NATIVE_HVISOR_DIR", str(REPO.parent / "tmp/hvisor-native"))),
    "integrated": Path(
        os.environ.get("REDIS_INTEGRATED_HVISOR_DIR", str(REPO.parent / "tmp/hvisor-integrated"))
    ),
}
DEFAULT_ROOT_DISK = (
    REPO / "platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4"
)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_success(result, label):
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed ({result.returncode}):\n{result.output}")


def marker_lines(output, prefix):
    return re.findall(re.escape(prefix) + r"[^\r\n]*", output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=VARIANT_DIRS, required=True,
                        help="select the native or integrated hvisor build")
    parser.add_argument("--output", type=Path,
                        help="result directory (default: timestamped directory under target)")
    parser.add_argument("--hvisor-dir", type=Path,
                        help="override the source/build directory selected by --variant")
    parser.add_argument("--hvisor-binary", type=Path,
                        help="override <hvisor-dir>/target/aarch64-unknown-none/release/hvisor.bin")
    parser.add_argument("--hvisor-commit",
                        help="commit label for an externally supplied binary")
    parser.add_argument("--root-disk", type=Path, default=DEFAULT_ROOT_DISK)
    parser.add_argument("--expected-primary-keys", type=int, default=0,
                        help="required primary DBSIZE before replica launch (default: 0)")
    parser.add_argument("--require-guest-markers", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="require pre-Redis VM-ready markers (default: enabled)")
    parser.add_argument("--platform-dir", type=Path, default=PLATFORM)
    parser.add_argument("--firmware", type=Path,
                        default=REPO / "platform/aarch64/qemu-gicv3/image/bootloader/u-boot-atf.bin")
    parser.add_argument("--kernel", type=Path,
                        default=REPO / "platform/aarch64/qemu-gicv3/image/kernel/Image")
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--qemu-cpus", default="0-5",
                        help="host CPUs assigned to QEMU with taskset (default: 0-5)")
    args = parser.parse_args()
    if args.expected_primary_keys < 0:
        parser.error("--expected-primary-keys must be nonnegative")
    args.hvisor_dir = (args.hvisor_dir or VARIANT_DIRS[args.variant]).resolve()
    args.hvisor_binary = (
        args.hvisor_binary
        or args.hvisor_dir / "target/aarch64-unknown-none/release/hvisor.bin"
    ).resolve()
    if args.output is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.output = (
            REPO / "target/redis-replica-bench" / f"deployment-{args.variant}-{timestamp}"
        )
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error("output directory already exists")

    paths = {
        "hvisor": args.hvisor_binary.resolve(),
        "root_disk": args.root_disk.resolve(),
        "root_dtb": (args.platform_dir / "image/dts/zone0.dtb").resolve(),
        "firmware": args.firmware.resolve(),
        "kernel": args.kernel.resolve(),
    }
    for name, path in paths.items():
        if not path.is_file():
            parser.error(f"missing {name}: {path}")

    if args.hvisor_commit:
        hvisor_commit = args.hvisor_commit
    elif (args.hvisor_binary.parent / "build-info.txt").is_file():
        info = (args.hvisor_binary.parent / "build-info.txt").read_text()
        hvisor_commit = next((line.split("=", 1)[1] for line in info.splitlines()
                              if line.startswith("commit=")), "unknown")
    elif (args.hvisor_dir / ".git").exists():
        hvisor_commit = subprocess.check_output(
            ["git", "-C", str(args.hvisor_dir), "rev-parse", "HEAD"], text=True
        ).strip()
    else:
        hvisor_commit = "unknown"
    args.output.mkdir(parents=True)
    command = qemu_command(
        paths["hvisor"], paths["root_disk"], paths["root_dtb"],
        paths["firmware"], paths["kernel"], args.qemu, args.qemu_cpus,
    )
    metadata = {
        "status": "running",
        "scope": "empty-dataset application deployment startup",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "variant": args.variant,
        "expected_primary_keys": args.expected_primary_keys,
        "qemu_cpus": args.qemu_cpus,
        "hvisor_commit": hvisor_commit,
        "hvisor_sha256": sha256(paths["hvisor"]),
        "artifacts": {
            name: {"path": str(path), "bytes": path.stat().st_size}
            for name, path in paths.items()
        },
        "qemu_version": subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
    }
    write_json(args.output / "metadata.json", metadata)
    print(f"starting {args.variant} deployment; results: {args.output}", flush=True)

    try:
        with Console(command, args.output / "vm") as console:
            bootm_ns = console.start_boot()
            _, root_ready_ns = console.expect(
                rb"root@redis-root[^\r\n]*#", timeout=240,
                failure_patterns=(
                    rb"Timed out waiting for device[^\r\n]*/dev/ttyAMA0",
                    rb"Dependency failed for[^\r\n]*Serial Getty on ttyAMA0",
                ),
            )
            cpu_result = console.command("cat /sys/devices/system/cpu/online")
            require_success(cpu_result, "root CPU check")
            online = re.findall(r"(?:\r|\n)([0-9]+(?:[-,][0-9]+)*)(?=\r|\n)", cpu_result.output)
            if not online or online[-1] != "0-1":
                raise RuntimeError(f"expected root CPUs 0-1, got: {cpu_result.output!r}")

            prepare = console.command("/eval/bin/prepare-redis.sh", timeout=120)
            require_success(prepare, "deployment preparation")
            if "REDIS_PREPARED" not in prepare.output:
                raise RuntimeError("deployment preparation marker is missing")

            primary = console.command("/eval/bin/start-zone.sh primary", timeout=120)
            require_success(primary, "primary start")
            primary_wait = console.command(
                f"EXPECTED_PRIMARY_KEYS={args.expected_primary_keys} /eval/bin/wait-primary.sh",
                timeout=360,
            )
            require_success(primary_wait, "primary readiness")
            replica = console.command("/eval/bin/start-zone.sh replica", timeout=120)
            require_success(replica, "replica start")
            wait = console.command("/eval/bin/wait-deployment.sh", timeout=600)
            require_success(wait, "deployment readiness")

            primary_vm_markers = marker_lines(primary_wait.output, "REDIS_PRIMARY_VM_READY")
            replica_vm_markers = marker_lines(wait.output, "REDIS_REPLICA_VM_READY")
            if args.require_guest_markers and (not primary_vm_markers or not replica_vm_markers):
                raise RuntimeError("required guest VM readiness marker is missing")
            direct_primary = rb"(?:^|\r|\n)REDIS_GUEST_CONSOLE_EVENT zone=1 REDIS_GUEST_READY role=primary[^\r\n]*"
            direct_replica = rb"(?:^|\r|\n)REDIS_GUEST_CONSOLE_EVENT zone=2 REDIS_GUEST_READY role=replica[^\r\n]*"
            primary_vm_ready_ns = console.event(direct_primary, primary.start_offset)[1]
            replica_vm_ready_ns = console.event(direct_replica, replica.start_offset)[1]
            primary_redis_ready_ns = console.event(
                rb"(?:^|\r|\n)REDIS_PRIMARY_READY[^\r\n]*",
                primary_wait.start_offset,
            )[1]
            patterns = {
                "replica_redis_ready": rb"(?:^|\r|\n)REDIS_REPLICA_READY[^\r\n]*",
                "sync_complete": rb"(?:^|\r|\n)REDIS_SYNC_COMPLETE[^\r\n]*",
                "deployment_ready": rb"(?:^|\r|\n)REDIS_DEPLOYMENT_READY[^\r\n]*",
            }
            event_ns = {
                name: console.event(pattern, wait.start_offset)[1]
                for name, pattern in patterns.items()
            }
            fatal = re.search(
                rb"EL2 Exception|Kernel panic|panicked at|Out of memory:", console.data, re.I
            )
            if fatal:
                raise RuntimeError(f"fatal console signature: {fatal[0].decode(errors='replace')}")

            timestamps = {
                "clock": "host CLOCK_MONOTONIC",
                "bootm_ns": bootm_ns,
                "root_ready_ns": root_ready_ns,
                "prepare_complete_ns": prepare.completed_ns,
                "deploy_start_ns": primary.sent_ns,
                "primary_start_ns": primary.sent_ns,
                "primary_start_return_ns": primary.completed_ns,
                "replica_start_ns": replica.sent_ns,
                "replica_start_return_ns": replica.completed_ns,
                "primary_vm_ready_ns": primary_vm_ready_ns,
                "replica_vm_ready_ns": replica_vm_ready_ns,
                "primary_redis_ready_ns": primary_redis_ready_ns,
                **{name + "_ns": value for name, value in event_ns.items()},
                "deployment_startup_ns": event_ns["deployment_ready"] - primary.sent_ns,
                "primary_vm_startup_ns": primary_vm_ready_ns - primary.sent_ns,
                "primary_redis_startup_ns": primary_redis_ready_ns - primary.sent_ns,
                "replica_vm_startup_ns": replica_vm_ready_ns - replica.sent_ns,
                "replica_redis_startup_ns": event_ns["replica_redis_ready"] - replica.sent_ns,
            }
            validation = {
                "status": "passed",
                "root_cpus": online[-1],
                "prepare_markers": marker_lines(prepare.output, "REDIS_PREPARED"),
                "zone_start_markers": (
                    marker_lines(primary.output, "REDIS_ZONE_STARTED")
                    + marker_lines(replica.output, "REDIS_ZONE_STARTED")
                ),
                "readiness_markers": (
                    primary_vm_markers
                    + marker_lines(primary_wait.output, "REDIS_PRIMARY_READY")
                    + replica_vm_markers
                    + marker_lines(wait.output, "REDIS_REPLICA_READY")
                    + marker_lines(wait.output, "REDIS_SYNC_COMPLETE")
                    + marker_lines(wait.output, "REDIS_DEPLOYMENT_READY")
                ),
                "direct_guest_markers": [
                    console.event(direct_primary, primary.start_offset)[0][0].decode(errors="replace").strip(),
                    console.event(direct_replica, replica.start_offset)[0][0].decode(errors="replace").strip(),
                ],
                "raw_console": "vm/root-console.log",
            }
            write_json(args.output / "timestamps.json", timestamps)
            write_json(args.output / "validation.json", validation)
            metadata.update(
                status="passed",
                completed_utc=datetime.now(timezone.utc).isoformat(),
                deployment_startup_ns=timestamps["deployment_startup_ns"],
            )
            write_json(args.output / "metadata.json", metadata)
            print(json.dumps({
                "status": "passed",
                "variant": args.variant,
                "output": str(args.output),
                "timestamps": timestamps,
            }, indent=2))
    except BaseException as error:
        metadata.update(
            status="failed",
            completed_utc=datetime.now(timezone.utc).isoformat(),
            error=repr(error),
        )
        write_json(args.output / "metadata.json", metadata)
        raise


if __name__ == "__main__":
    main()
