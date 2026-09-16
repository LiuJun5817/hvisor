#!/usr/bin/env python3
"""Run paired root-VM startup, guest Redis startup and Redis request experiments."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import re
import resource
import signal
import subprocess
import time

from prepare_guest import sha256
from qemu import ARTIFACTS, REPO, Console, qemu_command
from resp import Redis, preload, wait_ready

VARIANTS = ("baseline", "verified")
WORKLOADS = {"get": "0:1", "set": "1:0", "mixed_90get_10set": "1:9"}


class VMStartupError(RuntimeError):
    pass


class GuestConfigurationError(VMStartupError):
    def __init__(self, online, measurement):
        super().__init__(f"expected online guest CPUs 0-1, observed {online!r}")
        self.online = online
        self.measurement = measurement


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def append_json(path, value):
    with Path(path).open("a") as file:
        file.write(json.dumps(value) + "\n")


def progress(message):
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def info(port):
    with Redis(port) as client:
        return client.info()


def boot_once(args, variant, directory):
    console = Console(qemu_command(variant, args.disk, args.port, args.qemu_cpus,
                                  prepared=True, build_root=args.build_root), directory)
    try:
        start_ns = console.start_boot()
        def health_check():
            match = re.search(rb"EL2 Exception:[^\r\n]*|Kernel panic[^\r\n]*|panicked at[^\r\n]*", console.data)
            if match:
                raise VMStartupError(match[0].decode(errors="replace"))
            if console.process.poll() is not None:
                raise VMStartupError(f"QEMU exited during guest startup: {console.process.returncode}")
        measurement = wait_ready(args.port, start_ns, health_check=health_check)
        console.expect(rb"REDIS_BENCH_GUEST_READY", 30)
        output = console.command("cat /sys/devices/system/cpu/online")
        online = [line.strip() for line in output.splitlines() if re.fullmatch(r"[0-9,-]+", line.strip())]
        if online != ["0-1"]:
            raise GuestConfigurationError(online, measurement)
        measurement["online_guest_cpus"] = online[0]
        return console, measurement
    except BaseException:
        console.close()
        raise


def boot(args, variant, directory):
    # A degraded one-CPU VM is not a comparable two-vCPU performance sample.
    # Retain and count every such failure; report healthy-boot timing conditionally.
    for attempt in range(args.boot_attempts):
        attempt_directory = directory / f"attempt-{attempt:02d}"
        try:
            console, measurement = boot_once(args, variant, attempt_directory)
            measurement["boot_attempt"] = attempt
            write_json(attempt_directory / "boot-attempt.json", dict(measurement, status="healthy"))
            return console, measurement
        except (VMStartupError, TimeoutError) as error:
            failure = {"metric": "boot_attempt_failure", "variant": variant,
                       "directory": str(attempt_directory), "attempt": attempt,
                       "reason": str(error), "online_guest_cpus": getattr(error, "online", None),
                       "ready_measurement": getattr(error, "measurement", None)}
            write_json(attempt_directory / "boot-attempt.json", failure)
            append_json(args.output / "samples.jsonl", failure)
            progress(f"BOOT FAILURE {variant}: {error}; raw={attempt_directory}")
            if attempt == args.boot_attempts - 1:
                raise


def launch_process_samples(args, console, variant, directory, run, count):
    # The boot server belongs to this private VM, which has a disposable disk overlay.
    console.command("redis-cli shutdown nosave")
    output = console.command(
        "/redis-bench/guest_startup --redis /redis-bench/redis-server "
        "--config /redis-bench/redis.conf --port 6379 "
        f"--repeats {count} --poll-us 1000 --timeout-ms 10000 "
        "--server-cpu 0 --observer-cpu 1", timeout=max(120, args.process_runs * 11))
    rows = []
    for line in output.splitlines():
        if line.startswith('{"'):
            row = json.loads(line)
            if row.get("status") != "ok":
                raise RuntimeError(f"Redis process startup failed: {row}")
            row.update(variant=variant, metric="redis_process_startup", run=run,
                       cache_state="warm executable; boot server already ran")
            rows.append(row)
    if len(rows) != count:
        raise RuntimeError(f"expected {count} startup samples, got {len(rows)}")
    write_json(directory / "redis-process-startup.json", rows)
    for row in rows:
        append_json(args.output / "samples.jsonl", row)


def run_load(args, variant, phase, run, workload, rate, directory, seconds, warmup=False):
    directory.mkdir(parents=True, exist_ok=False)
    command = ["taskset", "-c", args.client_cpus, str(args.memtier),
               "--server=127.0.0.1", f"--port={args.port}", "--protocol=redis",
               "--threads=1", f"--clients={args.clients}", "--pipeline=1",
               f"--test-time={seconds}", "--run-count=1", f"--ratio={WORKLOADS[workload]}",
               "--key-prefix=bench:", "--key-minimum=1", f"--key-maximum={args.keys}",
               "--key-pattern=R:R", f"--data-size={args.value_size}",
               "--print-percentiles=50,95,99", "--hide-histogram",
               f"--json-out-file={directory / 'memtier.json'}"]
    if rate is not None:
        command.append(f"--rate-limiting={rate}")  # Requests/second/connection, not total RPS.
    before = info(args.port)
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.monotonic()
    with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=seconds + 120)
    duration = time.monotonic() - start
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    after = info(args.port)
    write_json(directory / "command.json", command)
    write_json(directory / "redis-before.json", before)
    write_json(directory / "redis-after.json", after)
    if result.returncode != 0:
        raise RuntimeError(f"memtier failed ({result.returncode}); see {directory}")
    data = json.loads((directory / "memtier.json").read_text())
    totals = data["ALL STATS"]["Totals"]
    runtime = data["ALL STATS"]["Runtime"]
    if str(runtime.get("Interrupted")).lower() != "false":
        raise RuntimeError(f"memtier run was interrupted or lacks interruption status: {runtime}")
    if totals["Connection Errors"] != 0:
        raise RuntimeError(f"memtier reported connection errors: {totals['Connection Errors']}")
    if runtime["Time unit"] != "MILLISECONDS" or runtime["Total duration"] < seconds * 950:
        raise RuntimeError(f"incomplete memtier duration: {runtime}")
    if totals["Count"] <= 0 or totals["Ops/sec"] <= 0:
        raise RuntimeError("memtier completed no requests")
    client_log = (directory / "stderr.log").read_text()
    if re.search(r"\b(errors?|failed|failures?|aborted|disconnect(?:ed)?|dropped|restart|exception)\b",
                 client_log, re.I):
        raise RuntimeError(f"memtier reported a client error; see {directory}")
    misses = int(after["keyspace_misses"]) - int(before["keyspace_misses"])
    errors = int(after.get("total_error_replies", 0)) - int(before.get("total_error_replies", 0))
    evictions = int(after["evicted_keys"]) - int(before["evicted_keys"])
    if misses or errors or evictions:
        raise RuntimeError(f"invalid workload: misses={misses}, errors={errors}, evictions={evictions}")
    if int(after["db0"].split(",")[0].split("=")[1]) != args.keys:
        raise RuntimeError("dataset size changed during overwrite workload")
    row = {"variant": variant, "metric": "requests", "phase": phase, "run": run,
           "workload": workload, "seconds": seconds, "warmup": warmup,
           "clients": args.clients, "pipeline": 1, "value_size": args.value_size,
           "keys": args.keys, "target_rps": rate * args.clients if rate is not None else None,
           "ops_per_second": totals["Ops/sec"], "memtier_totals": totals,
           "keyspace_misses": misses, "redis_errors": errors, "evictions": evictions,
           "client_cpu_seconds": (after_usage.ru_utime + after_usage.ru_stime -
                                  before_usage.ru_utime - before_usage.ru_stime),
           "client_wall_seconds": duration, "raw_directory": str(directory)}
    if rate is not None:
        row["achieved_target_ratio"] = row["ops_per_second"] / row["target_rps"]
        # Short smoke runs have a larger boundary effect; final 30s runs use 5%.
        tolerance = 0.05 if seconds >= 10 else 0.2
        if not warmup and abs(row["achieved_target_ratio"] - 1) > tolerance:
            write_json(directory / "measurement.json", dict(row, valid=False))
            raise RuntimeError(f"offered load not sustained: {row['achieved_target_ratio']:.3f} of target")
    write_json(directory / "measurement.json", row)
    if not warmup:
        append_json(args.output / "samples.jsonl", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, default=ARTIFACTS / "build")
    parser.add_argument("--disk", type=Path, default=ARTIFACTS / "assets/rootfs-redis-final.ext4")
    parser.add_argument("--memtier", type=Path, default=ARTIFACTS / "redis-build/bin/native/memtier_benchmark")
    parser.add_argument("--startup-runs", type=int, default=30)
    parser.add_argument("--process-runs", type=int, default=30)
    parser.add_argument("--capacity-runs", type=int, default=3)
    parser.add_argument("--capacity-seconds", type=int, default=10)
    parser.add_argument("--request-runs", type=int, default=5)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--keys", type=int, default=65536)
    parser.add_argument("--value-size", type=int, default=1024)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--port", type=int, default=16379)
    parser.add_argument("--boot-attempts", type=int, default=5)
    parser.add_argument("--qemu-cpus", default="0-5")
    parser.add_argument("--client-cpus", default="6-7")
    args = parser.parse_args()
    for name in ("startup_runs", "process_runs", "capacity_runs", "capacity_seconds",
                 "request_runs", "seconds", "warmup", "keys", "value_size", "clients", "boot_attempts"):
        if getattr(args, name) <= 0:
            parser.error(f"{name} must be positive")
    args.output = args.output.resolve()
    args.build_root = args.build_root.resolve()
    args.disk = args.disk.resolve()
    args.memtier = args.memtier.resolve()
    memtier_version = subprocess.check_output([str(args.memtier), "--version"], text=True)
    version_match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", memtier_version)
    if not version_match or tuple(map(int, version_match.groups())) < (2, 4, 4):
        raise RuntimeError("memtier >= 2.4.4 is required to avoid the timestamp-averaging statistics bug")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = args.disk.with_suffix(".manifest.json")
    prepared = json.loads(manifest.read_text())
    if sha256(args.disk) != prepared["disk_sha256"]:
        raise RuntimeError("prepared disk checksum mismatch")
    if sha256(args.disk.parent / "zone0-redis.dtb") != prepared["dtb_sha256"]:
        raise RuntimeError("prepared DTB checksum mismatch")
    metadata = {"started_utc": datetime.now(timezone.utc).isoformat(),
                "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "environment": platform.uname()._asdict(), "guest_manifest": prepared,
                "boot_metric": "host send bootm (hvisor + root Linux zone0) to first successful PING",
                "boot_failure_policy": f"record every failed/degraded-CPU boot; at most {args.boot_attempts} attempts per scheduled VM; report healthy-boot latency separately from failures",
                "acceleration": "QEMU AArch64 TCG on x86_64; no hardware-performance claim",
                "qemu_version": subprocess.check_output(["qemu-system-aarch64", "--version"], text=True),
                "memtier_version": memtier_version,
                "memtier_sha256": sha256(args.memtier),
                "scripts": {str(p.relative_to(REPO)): sha256(p) for p in Path(__file__).parent.iterdir() if p.is_file()},
                "builds": {v: json.loads((args.build_root / v / "metadata.json").read_text()) for v in VARIANTS}}
    metadata["redis_build"] = json.loads((ARTIFACTS / "redis-build/build-manifest.json").read_text())
    metadata["shared_assets"] = {str(path): sha256(path) for path in [ARTIFACTS / "assets/Image",
        REPO / "platform/aarch64/qemu-gicv3/image/bootloader/u-boot-atf.bin"]}
    for variant in VARIANTS:
        artifact = metadata["builds"][variant]["artifacts"]["target/aarch64-unknown-none/release/hvisor.bin"]
        expected = args.build_root / variant / "src/target/aarch64-unknown-none/release/hvisor.bin"
        if Path(artifact["path"]).resolve() != expected:
            raise RuntimeError(f"{variant} metadata does not describe the selected QEMU image")
        if sha256(artifact["path"]) != artifact["sha256"]:
            raise RuntimeError(f"{variant} hvisor checksum mismatch")
    source_copy = args.output / "runner-source"
    source_copy.mkdir()
    for path in Path(__file__).parent.iterdir():
        if path.is_file():
            (source_copy / path.name).write_bytes(path.read_bytes())
    write_json(args.output / "metadata.json", metadata)
    try:
        # Alternate AB/BA each pair; each sample boots a fresh QEMU and disk overlay.
        for run in range(args.startup_runs):
            for variant in VARIANTS if run % 2 == 0 else VARIANTS[::-1]:
                directory = args.output / f"startup/{run:02d}-{variant}"
                console, measurement = boot(args, variant, directory)
                with console:
                    row = dict(measurement, variant=variant, run=run, metric="vm_redis_startup")
                    append_json(args.output / "samples.jsonl", row)
                    write_json(directory / "startup.json", row)
                    progress(f"startup {run + 1}/{args.startup_runs} {variant}: {row['startup_ns'] / 1e6:.3f} ms")
                    count = args.process_runs // args.startup_runs + (run < args.process_runs % args.startup_runs)
                    if count:
                        write_json(directory / "guest-state.json", info(args.port))
                        launch_process_samples(args, console, variant, directory, run, count)
                        progress(f"guest Redis startup {variant}, VM {run + 1}: {count} sample(s) complete")
        capacity = {workload: {variant: [] for variant in VARIANTS} for workload in WORKLOADS}
        rates = {}
        for phase, count, seconds in (("capacity", args.capacity_runs, args.capacity_seconds),
                                      ("fixed_rate", args.request_runs, args.seconds)):
            if phase == "fixed_rate":
                # Use the lower observed capacity, with a 50% margin and integer per-client rate.
                for workload in WORKLOADS:
                    values = [min(capacity[workload][v]) for v in VARIANTS]
                    rates[workload] = max(1, math.floor(min(values) * 0.5 / args.clients))
                write_json(args.output / "fixed-rates.json", rates)
            for run in range(count):
                for variant in VARIANTS if run % 2 == 0 else VARIANTS[::-1]:
                    directory = args.output / f"{phase}/{run:02d}-{variant}"
                    console, _ = boot(args, variant, directory / "vm")
                    with console:
                        write_json(directory / "preload.json", preload(args.port, args.keys, args.value_size))
                        workloads = list(WORKLOADS)
                        workloads = workloads[run % 3:] + workloads[:run % 3]
                        for workload in workloads:
                            rate = rates.get(workload) if phase == "fixed_rate" else None
                            run_load(args, variant, phase, run, workload, rate,
                                     directory / f"{workload}-warmup", args.warmup, warmup=True)
                            row = run_load(args, variant, phase, run, workload, rate,
                                           directory / workload, seconds)
                            if phase == "capacity":
                                capacity[workload][variant].append(row["ops_per_second"])
                            progress(f"{phase} {run + 1}/{count} {variant} {workload}: {row['ops_per_second']:.1f} ops/s")
        metadata["completed_utc"] = datetime.now(timezone.utc).isoformat()
        metadata["status"] = "complete"
        write_json(args.output / "metadata.json", metadata)
    except BaseException as error:
        metadata["status"] = "failed"
        metadata["error"] = repr(error)
        write_json(args.output / "metadata.json", metadata)
        raise


if __name__ == "__main__":
    def terminate(_signal, _frame):
        raise SystemExit("terminated; closing the owned VM")
    signal.signal(signal.SIGTERM, terminate)
    main()
