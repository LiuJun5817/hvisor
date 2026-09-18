#!/usr/bin/env python3
"""Run one steady-state Redis workload sample."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from qemu import Console, PLATFORM, REPO, qemu_command


VARIANT_DIRS = {
    "native": Path(os.environ.get("REDIS_NATIVE_HVISOR_DIR", "/tmp/redis-hvisor-worktrees/native")),
    "integrated": Path(os.environ.get("REDIS_INTEGRATED_HVISOR_DIR", "/tmp/redis-hvisor-worktrees/integrated")),
}
DEFAULT_ROOT_DISK = REPO / "platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4"


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
    parser.add_argument("--variant", choices=VARIANT_DIRS, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hvisor-dir", type=Path)
    parser.add_argument("--hvisor-binary", type=Path)
    parser.add_argument("--hvisor-commit")
    parser.add_argument("--root-disk", type=Path, default=DEFAULT_ROOT_DISK)
    parser.add_argument("--key-count", type=int, default=1000)
    parser.add_argument("--value-size", type=int, default=64)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--get-set-ratio", default="7:3")
    parser.add_argument("--platform-dir", type=Path, default=PLATFORM)
    parser.add_argument("--firmware", type=Path,
                        default=REPO / "platform/aarch64/qemu-gicv3/image/bootloader/u-boot-atf.bin")
    parser.add_argument("--kernel", type=Path,
                        default=REPO / "platform/aarch64/qemu-gicv3/image/kernel/Image")
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--qemu-cpus", default="0-5")
    args = parser.parse_args()

    if args.key_count <= 0 or args.value_size <= 0 or args.duration <= 0:
        parser.error("key count, value size, and duration must be positive")
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", args.get_set_ratio):
        parser.error("--get-set-ratio must be two positive integers, for example 7:3")

    args.hvisor_dir = (args.hvisor_dir or VARIANT_DIRS[args.variant]).resolve()
    args.hvisor_binary = (args.hvisor_binary or
                          args.hvisor_dir / "target/aarch64-unknown-none/release/hvisor.bin").resolve()
    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.output = REPO / "target/redis-replica-bench" / f"workload-sample-{args.variant}-{stamp}"
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error("output directory already exists")

    paths = {
        "hvisor": args.hvisor_binary,
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
    elif (args.hvisor_dir / ".redis-build-info").is_file():
        info = (args.hvisor_dir / ".redis-build-info").read_text()
        hvisor_commit = next((line.split("=", 1)[1] for line in info.splitlines()
                              if line.startswith("hvisor_commit=")), "unknown")
    elif (args.hvisor_binary.parent / "build-info.txt").is_file():
        info = (args.hvisor_binary.parent / "build-info.txt").read_text()
        hvisor_commit = next((line.split("=", 1)[1] for line in info.splitlines()
                              if line.startswith("hvisor_commit=")), "unknown")
    elif (args.hvisor_dir / ".git").exists():
        hvisor_commit = subprocess.check_output(
            ["git", "-C", str(args.hvisor_dir), "rev-parse", "HEAD"], text=True
        ).strip()
    else:
        hvisor_commit = "unknown"

    args.output.mkdir(parents=True)
    metadata = {
        "status": "running", "scope": "steady-state workload with populated dataset",
        "started_utc": datetime.now(timezone.utc).isoformat(), "variant": args.variant,
        "key_count": args.key_count, "value_size": args.value_size,
        "duration_sec": args.duration, "get_set_ratio": args.get_set_ratio,
        "qemu_cpus": args.qemu_cpus, "hvisor_commit": hvisor_commit,
        "hvisor_sha256": sha256(paths["hvisor"]),
        "artifacts": {name: {"path": str(path), "bytes": path.stat().st_size}
                      for name, path in paths.items()},
        "qemu_version": subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
        "stage_timings_sec": {},
    }
    write_json(args.output / "metadata.json", metadata)
    print(f"starting {args.variant} workload sample; results: {args.output}", flush=True)

    try:
        with Console(qemu_command(paths["hvisor"], paths["root_disk"], paths["root_dtb"],
                                  paths["firmware"], paths["kernel"], args.qemu, args.qemu_cpus),
                     args.output / "vm") as console:
            boot_ns = console.start_boot()
            try:
                _, _ = console.expect(rb"root@redis-root[^\r\n]*#", timeout=240,
                                      failure_patterns=(
                                          rb"Timed out waiting for device[^\r\n]*/dev/ttyAMA0",
                                          rb"Dependency failed for[^\r\n]*Serial Getty on ttyAMA0",
                                      ))
            finally:
                metadata["stage_timings_sec"]["root_boot"] = round((time.monotonic_ns() - boot_ns) / 1e9, 3)
                write_json(args.output / "metadata.json", metadata)

            def stage(name, command, timeout):
                started = time.monotonic()
                try:
                    return console.command(command, timeout=timeout)
                finally:
                    metadata["stage_timings_sec"][name] = round(time.monotonic() - started, 3)
                    write_json(args.output / "metadata.json", metadata)

            cpu = stage("cpu_check", "cat /sys/devices/system/cpu/online", 120)
            require_success(cpu, "root CPU check")
            online = re.findall(r"(?:\r|\n)([0-9]+(?:[-,][0-9]+)*)(?=\r|\n)", cpu.output)
            if not online or online[-1] != "0-1":
                raise RuntimeError(f"expected root CPUs 0-1, got: {cpu.output!r}")
            prepare = stage("prepare", "/eval/bin/prepare-redis.sh", 120)
            require_success(prepare, "deployment preparation")
            primary = stage("primary_start", "/eval/bin/start-zone.sh primary", 120)
            require_success(primary, "primary start")
            ready = stage("primary_ready", "EXPECTED_PRIMARY_KEYS=0 /eval/bin/wait-primary.sh", 360)
            require_success(ready, "primary readiness")
            populate = stage("populate", f"KEY_COUNT={args.key_count} VALUE_SIZE={args.value_size} /eval/bin/populate-dataset.sh", 600)
            require_success(populate, "dataset population")
            replica = stage("replica_start", "/eval/bin/start-zone.sh replica", 120)
            require_success(replica, "replica start")
            deployment = stage("deployment_ready", "/eval/bin/wait-deployment.sh", 600)
            require_success(deployment, "deployment readiness")
            workload = stage(
                "workload",
                f"DURATION_SEC={args.duration} KEY_COUNT={args.key_count} VALUE_SIZE={args.value_size} "
                f"GET_SET_RATIO={args.get_set_ratio} OUTPUT_DIR=/tmp /eval/bin/run-workload.sh",
                args.duration + 300,
            )
            require_success(workload, "workload execution")
            markers = marker_lines(workload.output, "REDIS_WORKLOAD_COMPLETE")
            if not markers:
                raise RuntimeError("workload completion marker is missing")
            metrics = {}
            for key, value in re.findall(r"(\w+)=([^\s]+)", markers[0]):
                try:
                    metrics[key] = float(value)
                except ValueError:
                    metrics[key] = value
            (args.output / "workload.log").write_text(console.command("cat /tmp/workload.log", timeout=30).output)
            (args.output / "replication.log").write_text(console.command("cat /tmp/replication.log", timeout=30).output)
            fatal = re.search(rb"EL2 Exception|Kernel panic|panicked at|Out of memory:", console.data, re.I)
            if fatal:
                raise RuntimeError(f"fatal console signature: {fatal[0].decode(errors='replace')}")
            validation = {"status": "passed", "workload_markers": markers,
                          "workload_metrics": metrics, "raw_console": "vm/root-console.log"}
            write_json(args.output / "validation.json", validation)
            metadata.update(status="passed", completed_utc=datetime.now(timezone.utc).isoformat(),
                            workload_metrics=metrics)
            write_json(args.output / "metadata.json", metadata)
            print(json.dumps({"status": "passed", "variant": args.variant,
                              "output": str(args.output), "metrics": metrics}, indent=2))
    except BaseException as error:
        metadata.update(status="failed", completed_utc=datetime.now(timezone.utc).isoformat(), error=repr(error))
        write_json(args.output / "metadata.json", metadata)
        raise


if __name__ == "__main__":
    main()
