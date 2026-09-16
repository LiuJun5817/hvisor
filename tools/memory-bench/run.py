#!/usr/bin/env python3
"""Build and run the host memory benchmarks with a recorded environment."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import time


TOOLCHAIN = "1.95.0"
CRITERION_VERSION = "0.8.2"
BENCHMARK = "region_zone_ops"
CRATE = Path(__file__).resolve().parent
REPOSITORY = CRATE.parent.parent
TARGET = CRATE / "target"


def capture(command, cwd=REPOSITORY):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{shlex.join(command)} failed: {detail}")
    return result.stdout.strip()


def workload_environment():
    legacy = [
        name for name in ("REGIONS", "ZONE_REGIONS", "ZONE_REGION_PAGES")
        if name in os.environ
    ]
    if legacy:
        raise ValueError(
            f"{', '.join(legacy)} no longer supported; unset these variables. "
            "Region cases now time one operation on a prefilled set: use "
            "PREFILL_REGIONS for the background region count, PREFILL_REGION_PAGES "
            "for background region size, and REGION_PAGES for target size. "
            "Zone cases always create/remove empty zones."
        )
    values = {}
    for name, default, maximum in (
        ("PREFILL_REGIONS", "32", 4096),
        ("PREFILL_REGION_PAGES", "1024", 32768),
        ("REGION_PAGES", "1024", 32768),
    ):
        value = os.environ.get(name, default)
        if not re.fullmatch(r"[1-9][0-9]{0,4}", value) or int(value) > maximum:
            raise ValueError(f"{name} must be an integer from 1 to {maximum}; got {value!r}")
        values[name] = int(value)
    stride = max(values["PREFILL_REGION_PAGES"], values["REGION_PAGES"])
    if values["PREFILL_REGIONS"] * stride + values["REGION_PAGES"] > 65536:
        raise ValueError(
            "PREFILL_REGIONS * max(PREFILL_REGION_PAGES, REGION_PAGES) + "
            "REGION_PAGES must not exceed 65536 pages (256 MiB address range)"
        )
    return values


def benchmark_affinity():
    requested = os.environ.get("BENCH_CPU")
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        if requested is not None:
            raise ValueError("BENCH_CPU requires sched_getaffinity/sched_setaffinity support")
        return {"supported": False, "allowed_cpus": None, "selected_cpu": None}
    allowed = sorted(os.sched_getaffinity(0))
    if not allowed:
        raise ValueError("the current process has no allowed CPUs")
    if requested is None:
        selected = allowed[0]
    else:
        if not re.fullmatch(r"[0-9]+", requested):
            raise ValueError(f"BENCH_CPU must be a nonnegative CPU number; got {requested!r}")
        selected = int(requested)
        if selected not in allowed:
            raise ValueError(f"BENCH_CPU={selected} is outside the allowed CPUs: {allowed}")
    return {"supported": True, "allowed_cpus": allowed, "selected_cpu": selected}


def source_metadata():
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "."],
        cwd=REPOSITORY, check=True, capture_output=True,
    ).stdout
    untracked = capture([
        "git", "ls-files", "--others", "--exclude-standard", "-z", "--",
        "tools/memory-bench",
    ]).split("\0")
    digest = hashlib.sha256()
    files = {}
    for name in sorted(filter(None, untracked)):
        path = REPOSITORY / name
        if "target" in path.relative_to(CRATE).parts:
            continue
        contents = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        file_hash = hashlib.sha256(contents).hexdigest()
        files[name] = file_hash
        digest.update(name.encode() + b"\0" + file_hash.encode() + b"\0")
    return {
        "branch": capture(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "revision": capture(["git", "rev-parse", "HEAD"]),
        "status": capture(["git", "status", "--porcelain=v1"]),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked_benchmark_sha256": digest.hexdigest(),
        "untracked_benchmark_files": files,
    }


def stream_process(command, log, environment, cpu=None, cargo_json=False):
    # This runner is single-threaded. Only the benchmark child changes affinity;
    # Cargo and the user's shell keep their existing CPU masks.
    def pin_child():
        os.sched_setaffinity(0, {cpu})

    executable = None
    process = subprocess.Popen(
        command, cwd=CRATE, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
        preexec_fn=pin_child if cpu is not None else None,
    )
    try:
        for line in process.stdout:
            log.write(line)
            log.flush()
            if cargo_json:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    print(line, end="", flush=True)
                    continue
                if message.get("reason") == "compiler-artifact":
                    if message.get("target", {}).get("name") == BENCHMARK:
                        executable = message.get("executable") or executable
                elif message.get("reason") == "compiler-message":
                    rendered = message.get("message", {}).get("rendered")
                    if rendered:
                        print(rendered, end="", flush=True)
            else:
                print(line, end="", flush=True)
        return process.wait(), executable
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        process.stdout.close()


def operation_counts(workload):
    region_case = (
        f"prefill_{workload['PREFILL_REGIONS']}_regions_"
        f"{workload['PREFILL_REGION_PAGES']}_pages/"
        f"target_{workload['REGION_PAGES']}_pages"
    )
    return {
        f"region/insert/{region_case}": 1,
        f"region/remove/after_insert/{region_case}": 1,
        "zone_memory/create/empty": 1,
        "zone_memory/remove/empty": 1,
    }


def normalized_summary(started_ns, counts, status):
    results = []
    for path in sorted((TARGET / "criterion").glob("**/new/estimates.json")):
        if started_ns is None or path.stat().st_mtime_ns < started_ns:
            continue
        benchmark = json.loads((path.parent / "benchmark.json").read_text())
        benchmark_id = benchmark["full_id"]
        if benchmark_id not in counts:
            continue
        divisor = counts[benchmark_id]
        if divisor != 1 or benchmark.get("throughput") != {"Elements": 1}:
            raise ValueError(
                f"{benchmark_id}: expected one operation per iteration with "
                f"throughput Elements(1); got {benchmark.get('throughput')!r}"
            )
        mean = json.loads(path.read_text())["mean"]
        interval = mean["confidence_interval"]
        results.append({
            "benchmark": benchmark_id,
            "operations_per_iteration": divisor,
            "mean_ns_per_iteration": mean["point_estimate"],
            "mean_ns_per_op": mean["point_estimate"] / divisor,
            "confidence_interval_ns_per_op": {
                "confidence_level": interval["confidence_level"],
                "lower_bound": interval["lower_bound"] / divisor,
                "upper_bound": interval["upper_bound"] / divisor,
            },
            "estimates_file": str(path),
        })
    summary = {"status": status, "benchmarks": results}
    (TARGET / "membench-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return results


def main(arguments):
    workload = workload_environment()
    affinity = benchmark_affinity()
    manifest = CRATE / "Cargo.toml"
    if not manifest.is_file():
        raise RuntimeError(f"benchmark manifest is missing: {manifest}")
    lock = (CRATE / "Cargo.lock").read_text()
    version = re.search(r'\[\[package\]\]\s+name = "criterion"\s+version = "([^"]+)"', lock)
    if version is None or version.group(1) != CRITERION_VERSION:
        raise RuntimeError(f"Cargo.lock must pin criterion {CRITERION_VERSION}")
    rustc = capture(["rustc", f"+{TOOLCHAIN}", "-vV"], cwd=CRATE)
    host = next((line[6:] for line in rustc.splitlines() if line.startswith("host: ")), None)
    if not host:
        raise RuntimeError("rustc -vV did not report a host target")
    command = [
        "cargo", f"+{TOOLCHAIN}", "bench", "--manifest-path", str(manifest),
        "--bench", BENCHMARK, "--no-run", "--message-format=json", "--locked",
        "--target", host, "--target-dir", str(TARGET),
    ]
    environment = os.environ.copy()
    environment.update({name: str(value) for name, value in workload.items()})
    environment["CRITERION_HOME"] = str(TARGET / "criterion")
    counts = operation_counts(workload)
    cpuinfo_path = Path("/proc/cpuinfo")
    metadata = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": source_metadata(), "rustc": rustc,
        "criterion_version": CRITERION_VERSION, "host_target": host,
        "build_command": command, "criterion_arguments": arguments,
        "uname": platform.uname()._asdict(),
        "cpuinfo": cpuinfo_path.read_text() if cpuinfo_path.is_file() else None,
        "workload": workload, "affinity": affinity,
        "operations_per_iteration": counts,
        "criterion_output_directory": environment["CRITERION_HOME"],
        "build_environment": {
            key: value for key, value in environment.items()
            if key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET", "RUSTC_WRAPPER")
        },
        "status": "building",
    }
    TARGET.mkdir(parents=True, exist_ok=True)
    metadata_path = TARGET / "membench-environment.json"
    log_path = TARGET / "membench-criterion.log"

    def save_metadata():
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

    save_metadata()
    normalized_summary(None, counts, "not_run")
    print(f"Workload: {workload}", flush=True)
    print(f"Benchmark CPU: {affinity['selected_cpu']}; host target: {host}", flush=True)
    print(f"Environment: {metadata_path}\nLog: {log_path}", flush=True)
    with log_path.open("w") as log:
        log.write(f"Build: {shlex.join(command)}\n")
        status, executable = stream_process(command, log, environment, cargo_json=True)
        metadata["build_exit_code"] = status
        if status:
            metadata["status"] = "build_failed"
        elif executable is None:
            metadata["status"] = "missing_benchmark_executable"
            status = 1
            print("Cargo did not report a benchmark executable", file=sys.stderr)
        else:
            run_command = [executable, *([] if "--bench" in arguments else ["--bench"]), *arguments]
            metadata["benchmark_command"] = run_command
            metadata["status"] = "running"
            save_metadata()
            log.write(f"Benchmark: {shlex.join(run_command)}\n")
            started_ns = time.time_ns()
            metadata["benchmark_started_ns"] = started_ns
            status, _ = stream_process(
                run_command, log, environment, cpu=affinity["selected_cpu"],
            )
            metadata["benchmark_exit_code"] = status
            metadata["status"] = "completed" if status == 0 else "benchmark_failed"
            results = normalized_summary(started_ns, counts, metadata["status"])
            for result in results:
                interval = result["confidence_interval_ns_per_op"]
                line = (
                    f"{result['benchmark']}: mean {result['mean_ns_per_op']:.3f} ns/op, "
                    f"{interval['confidence_level'] * 100:g}% CI "
                    f"[{interval['lower_bound']:.3f}, {interval['upper_bound']:.3f}] ns/op "
                    f"({result['operations_per_iteration']} operations/iteration)"
                )
                print(line, flush=True)
                log.write(line + "\n")
            if not results:
                line = "No new Criterion estimates were produced; the summary contains no measurements."
                print(line, flush=True)
                log.write(line + "\n")
        if "benchmark_exit_code" not in metadata:
            normalized_summary(None, counts, metadata["status"])
    metadata["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_metadata()
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Memory benchmark: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Memory benchmark interrupted", file=sys.stderr)
        sys.exit(130)
