#!/usr/bin/env python3.11
"""Build and measure both native memory APIs with one checked benchmark harness.

Requires Python 3.11+, rustup/Cargo, and Linux CPU affinity support. Results are
created in a new directory; an incomplete or failed run never produces a report.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11+ is required; run: python3.11 tools/memory-bench/compare.py")


ROOT = Path(__file__).resolve().parents[2]
NAMES = ("hvisor", "verified-hv-mem")
IDS = (
    "allocator/alloc/100",
    "allocator/dealloc/100",
    "page_table/map_page/100",
    "page_table/unmap_page/100",
    "page_table/query/100",
)
CRITERION_ARGS = [
    "--sample-size", "100", "--warm-up-time", "3", "--measurement-time", "10",
    "--confidence-level", "0.95", "--nresamples", "100000",
    "--noise-threshold", "0.01", "--significance-level", "0.05",
]
BUILD_ENV = {
    "RUSTFLAGS": "",
    "RUSTC_WRAPPER": "",
    "RUSTC_WORKSPACE_WRAPPER": "",
    "CARGO_PROFILE_BENCH_OPT_LEVEL": "3",
    "CARGO_PROFILE_BENCH_LTO": "thin",
    "CARGO_PROFILE_BENCH_CODEGEN_UNITS": "1",
    "CARGO_PROFILE_BENCH_DEBUG": "false",
    "CARGO_PROFILE_BENCH_DEBUG_ASSERTIONS": "false",
    "CARGO_PROFILE_BENCH_OVERFLOW_CHECKS": "false",
    "CARGO_PROFILE_BENCH_INCREMENTAL": "false",
    "CARGO_INCREMENTAL": "0",
    "CARGO_TERM_COLOR": "never",
    "RAYON_NUM_THREADS": "1",
}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def checked(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command!r}\n{result.stderr}")
    return result.stdout


def clean_environment() -> tuple[dict[str, str], list[str]]:
    exact = {
        "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER", "RUSTUP_TOOLCHAIN", "RUSTC_BOOTSTRAP",
        "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET", "CARGO_BUILD_RUSTFLAGS",
        "CARGO_BUILD_RUSTC", "CARGO_BUILD_RUSTC_WRAPPER",
        "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER", "CARGO_ENCODED_RUSTDOCFLAGS",
        "RUSTDOCFLAGS", "CARGO_INCREMENTAL", "CRITERION_HOME",
        "CARGO_CRITERION_PORT", "RAYON_NUM_THREADS",
    }
    removed = sorted(key for key in os.environ if key in exact
                     or key.startswith(("CARGO_PROFILE_", "CARGO_TARGET_")))
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(BUILD_ENV)
    return env, removed


def criterion_closure(lockfile: Path) -> list[dict]:
    """Compare resolved packages and edges, including target-specific dependencies."""
    packages = tomllib.loads(lockfile.read_text())["package"]
    roots = [item for item in packages if item["name"] == "criterion"]
    if len(roots) != 1 or roots[0]["version"] != "0.8.2":
        raise ValueError(f"{lockfile}: exactly Criterion 0.8.2 is required")

    def resolve(dependency: str) -> dict:
        match = re.fullmatch(r"([^ ]+)(?: ([^ ]+))?(?: \((.+)\))?", dependency)
        if match is None:
            raise ValueError(f"Unrecognized lockfile dependency: {dependency}")
        name, version, source = match.groups()
        matches = [item for item in packages if item["name"] == name
                   and (version is None or item["version"] == version)
                   and (source is None or item.get("source") == source)]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous/missing dependency in {lockfile}: {dependency}")
        return matches[0]

    def identity(item: dict) -> str:
        return " ".join((item["name"], item["version"], item.get("source", "local")))

    pending = roots[:]
    seen = {}
    while pending:
        item = pending.pop()
        key = identity(item)
        if key in seen:
            continue
        dependencies = [resolve(dep) for dep in item.get("dependencies", [])]
        seen[key] = {
            "id": key, "checksum": item.get("checksum"),
            "dependencies": sorted(identity(dep) for dep in dependencies),
        }
        pending.extend(dependencies)
    return [seen[key] for key in sorted(seen)]


def source_files(repo: Path, crate: Path) -> list[Path]:
    files = set((repo / "src").rglob("*.rs"))
    files.update((crate / "src").rglob("*.rs"))
    files.update((crate / "benches").rglob("*.rs"))
    for directory in {repo, crate}:
        files.update(directory / name for name in ("Cargo.toml", "Cargo.lock", "build.rs")
                     if (directory / name).is_file())
    if repo == ROOT:
        files.add(Path(__file__).resolve())
    return sorted(files)


def source_hashes(repo: Path, crate: Path) -> dict[str, str]:
    return {str(path.relative_to(repo)): sha256(path) for path in source_files(repo, crate)}


def log_command(command: list[str], *, label: str, cwd: Path, env: dict[str, str],
                output: Path, metadata: dict, cpu: int | None = None) -> Path:
    log = output / "logs" / f"{label}.log"
    entry = {"label": label, "command": command, "cwd": str(cwd), "cpu": cpu,
             "started_utc": stamp(), "log": str(log.relative_to(output)),
             "environment": {key: env[key] for key in sorted(BUILD_ENV.keys() | {
                 "CARGO_TARGET_DIR", "CRITERION_HOME", "RUSTUP_TOOLCHAIN", "RUSTC"
             }) if key in env}}
    metadata["commands"].append(entry)
    write_json(output / "metadata.json", metadata)
    print(f"[{stamp()}] {label}; log: {log}", flush=True)
    started = time.monotonic()
    with log.open("w") as stream:
        child = subprocess.Popen(command, cwd=cwd, env=env, text=True,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 preexec_fn=(lambda: os.sched_setaffinity(0, {cpu}))
                                 if cpu is not None else None)
        try:
            while True:
                try:
                    code = child.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    print(f"[{stamp()}] {label} still running ({time.monotonic() - started:.0f}s)",
                          flush=True)
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
    entry.update(returncode=code, finished_utc=stamp(), seconds=time.monotonic() - started)
    write_json(output / "metadata.json", metadata)
    if code:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
        raise RuntimeError(f"{label} exited {code}; see {log}\n{tail}")
    return log


def build_artifacts(log: Path) -> tuple[Path, dict]:
    artifacts = []
    criterion = []
    for line in log.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("reason") != "compiler-artifact":
            continue
        if item["target"]["name"] == "memory_ops" and item.get("executable"):
            artifacts.append(item)
        if item["target"]["name"] == "criterion":
            criterion.append(item)
    if len(artifacts) != 1 or len(criterion) != 1:
        raise ValueError(f"Expected one memory_ops executable and Criterion artifact in {log}")
    artifact = artifacts[0]
    profile = artifact["profile"]
    if profile["opt_level"] != "3" or profile["debug_assertions"] or profile["overflow_checks"]:
        raise ValueError(f"Unexpected benchmark compiler profile: {profile}")
    executable = Path(artifact["executable"])
    if not executable.is_file():
        raise ValueError(f"Missing built executable: {executable}")
    return executable, {"profile": profile, "criterion_features": criterion[0]["features"],
                        "sha256": sha256(executable)}


def read_results(raw: Path, baseline: str) -> dict[str, dict]:
    actual = {str(path.parent.parent.relative_to(raw))
              for path in raw.glob(f"**/{baseline}/benchmark.json")}
    if actual != set(IDS):
        raise ValueError(f"{raw}/{baseline}: benchmark IDs differ: {sorted(actual)}")
    results = {}
    for benchmark_id in IDS:
        directory = raw / benchmark_id / baseline
        benchmark = json.loads((directory / "benchmark.json").read_text())
        sample = json.loads((directory / "sample.json").read_text())
        estimates = json.loads((directory / "estimates.json").read_text())
        if benchmark.get("full_id") != benchmark_id or benchmark.get("throughput") != {"Elements": 100}:
            raise ValueError(f"Invalid benchmark ID/batch size: {directory}")
        if len(sample["iters"]) != 100 or len(sample["times"]) != 100:
            raise ValueError(f"Expected exactly 100 samples: {directory}")
        if not all(math.isfinite(value) and value > 0 for key in ("iters", "times")
                   for value in sample[key]):
            raise ValueError(f"Invalid sample values: {directory}")
        mean = estimates["mean"]
        interval = mean["confidence_interval"]
        point, lower, upper = mean["point_estimate"], interval["lower_bound"], interval["upper_bound"]
        if interval["confidence_level"] != 0.95 or not all(
                math.isfinite(value) and value > 0 for value in (point, lower, upper)) or not lower <= point <= upper:
            raise ValueError(f"Invalid 95% mean confidence interval: {directory}")
        results[benchmark_id] = {"mean_ns": point / 100, "ci_lower_ns": lower / 100,
                                 "ci_upper_ns": upper / 100,
                                 "sampling_mode": sample["sampling_mode"]}
    return results


def write_report(output: Path, metadata: dict, pairs: list[dict]) -> None:
    rows = []
    for pair in pairs:
        for benchmark_id in IDS:
            hvisor = pair["results"][NAMES[0]][benchmark_id]
            verified = pair["results"][NAMES[1]][benchmark_id]
            row = {"pair": pair["pair"], "order": " -> ".join(pair["order"]),
                   "benchmark": benchmark_id}
            for name, values in (("hvisor", hvisor), ("verified", verified)):
                row.update({f"{name}_{key}": value for key, value in values.items()})
            row["hvisor_over_verified"] = hvisor["mean_ns"] / verified["mean_ns"]
            rows.append(row)
    with (output / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# Matched memory operation benchmarks", "",
        f"Completed: {metadata['finished_utc']}. Logical CPU: {metadata['cpu']}. "
        f"Target: `{metadata['target']}`. Toolchain: `{metadata['toolchain']}`.", "",
        f"Compiler: `{metadata['rustc'].splitlines()[0]}`.", "",
        f"Identical `memory_ops.rs` SHA-256: `{metadata['harness_sha256']}`.", "",
        "Both builds use identical Criterion 0.8.2 dependency resolutions and features, "
        "opt-level 3, thin LTO, one codegen unit, and disabled debug assertions, overflow "
        "checks and incremental compilation. Each case has 100 samples, 3 seconds of "
        "warmup and a 10-second measurement target; preparation and teardown add wall time.", "",
        "Values are Criterion **mean divided by 100**, in ns per operation, with the "
        "corresponding 95% confidence interval divided by 100. Ratios compare the means "
        "within each pair; they have no inferred confidence interval. Runs are shown "
        "separately; samples and confidence intervals are not pooled.", "",
        "| Pair / execution order | Operation | hvisor mean [95% CI], ns | "
        "VeriHyMem mean [95% CI], ns | hvisor / VeriHyMem |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['pair']}: {row['order']} | `{row['benchmark']}` | "
            f"{row['hvisor_mean_ns']:.3f} [{row['hvisor_ci_lower_ns']:.3f}, {row['hvisor_ci_upper_ns']:.3f}] | "
            f"{row['verified_mean_ns']:.3f} [{row['verified_ci_lower_ns']:.3f}, {row['verified_ci_upper_ns']:.3f}] | "
            f"{row['hvisor_over_verified']:.3f}x |"
        )
    lines.extend([
        "", "These are native API costs under the same host workload. Native ownership "
        "types, locking and page-table algorithms remain part of each implementation. "
        "hvisor allocates and clears intermediate tables during map and retains them "
        "until table destruction; VeriHyMem clears and reclaims empty tables during "
        "unmap. Consequently, map and unmap do not perform identical reclamation work. "
        "They are software timings without hardware TLB invalidation.", "",
        "Sequential ascending batches on a warmed pool do not describe cold, sparse or "
        "contended workloads. CPU affinity and alternating execution order reduce "
        "scheduling/order variation, but cannot remove host/VM frequency and load changes.", "",
        "[Run metadata and commands](metadata.json) · [Machine-readable results](results.csv) · "
        "[Raw Criterion files](raw/) · [Source snapshots](sources/) · [Build/run logs](logs/)", "",
    ])
    (output / "report.md").write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=ROOT.parent / "verified-hv-mem")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu", type=int)
    parser.add_argument("--toolchain", default="1.95.0")
    parser.add_argument("--pairs", type=int, default=2, help="alternating AB/BA pairs (default: 2)")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    if not hasattr(os, "sched_getaffinity"):
        parser.error("Linux CPU affinity support is required")
    allowed = sorted(os.sched_getaffinity(0))
    cpu = min(allowed) if args.cpu is None else args.cpu
    if cpu not in allowed:
        parser.error(f"--cpu must be in the allowed CPU set: {allowed}")
    repos = {NAMES[0]: ROOT, NAMES[1]: args.reference.resolve()}
    crates = {NAMES[0]: ROOT / "tools/memory-bench", NAMES[1]: args.reference.resolve()}
    benches = {name: crate / "benches/memory_ops.rs" for name, crate in crates.items()}
    if benches[NAMES[0]].read_bytes() != benches[NAMES[1]].read_bytes():
        raise ValueError("Both benches/memory_ops.rs files must be byte-identical before comparison")
    closures = {name: criterion_closure(crate / "Cargo.lock") for name, crate in crates.items()}
    if closures[NAMES[0]] != closures[NAMES[1]]:
        left = {item["id"]: item for item in closures[NAMES[0]]}
        right = {item["id"]: item for item in closures[NAMES[1]]}
        differing = sorted(key for key in left.keys() | right.keys() if left.get(key) != right.get(key))
        raise ValueError("Criterion dependency resolutions/checksums differ; align Cargo.lock files:\n"
                         + "\n".join(differing))
    profiles = {name: tomllib.loads((crate / "Cargo.toml").read_text()).get("profile", {})
                for name, crate in crates.items()}
    if profiles[NAMES[0]] != profiles[NAMES[1]]:
        raise ValueError("The two benchmark manifests must have identical [profile.*] settings")
    output = (args.output or ROOT / "target/memory-bench" /
              datetime.now(timezone.utc).strftime("comparison-%Y%m%dT%H%M%S.%fZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    env, removed = clean_environment()
    metadata = {"status": "running", "started_utc": stamp(), "toolchain": args.toolchain,
                "cpu": cpu, "original_allowed_cpus": allowed, "platform": platform.platform(),
                "uname": list(platform.uname()), "python": sys.version,
                "removed_environment_keys": removed, "effective_build_environment": BUILD_ENV,
                "harness_sha256": sha256(benches[NAMES[0]]), "criterion_dependencies": closures[NAMES[0]],
                "criterion_arguments": CRITERION_ARGS, "pairs": args.pairs, "commands": [],
                "repositories": {}, "binaries": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="memory-bench-cargo-", dir="/tmp") as temporary:
            neutral = Path(temporary)
            metadata["rustc"] = checked(["rustup", "run", args.toolchain, "rustc", "-vV"], cwd=neutral, env=env)
            metadata["cargo"] = checked(["rustup", "run", args.toolchain, "cargo", "-vV"], cwd=neutral, env=env)
            rustc_path = Path(checked(["rustup", "which", "--toolchain", args.toolchain, "rustc"],
                                     cwd=neutral, env=env).strip())
            metadata["rustc_path"] = str(rustc_path)
            metadata["rustc_sha256"] = sha256(rustc_path)
            # Pin the executable as well as the rustup selection; user Cargo
            # configuration must not replace rustc behind the version check.
            env["RUSTC"] = str(rustc_path)
            env["RUSTUP_TOOLCHAIN"] = args.toolchain
            target = next(line.removeprefix("host: ") for line in metadata["rustc"].splitlines()
                          if line.startswith("host: "))
            metadata["target"] = target
            for filename in ("/proc/cpuinfo", "/proc/version"):
                if Path(filename).is_file():
                    (output / Path(filename).name).write_bytes(Path(filename).read_bytes())
            for name in NAMES:
                repo, crate = repos[name], crates[name]
                snapshot = output / "sources" / name
                snapshot.mkdir(parents=True)
                hashes = source_hashes(repo, crate)
                metadata["repositories"][name] = {
                    "path": str(repo), "crate": str(crate), "source_sha256": hashes,
                    "commit": checked(["git", "-C", str(repo), "rev-parse", "HEAD"], cwd=neutral, env=env).strip(),
                    "status": checked(["git", "-C", str(repo), "status", "--porcelain"], cwd=neutral, env=env),
                }
                diff = checked(["git", "-C", str(repo), "diff", "--binary", "HEAD", "--", "src", "benches",
                                "tools/memory-bench", "Cargo.toml", "Cargo.lock", "build.rs"], cwd=neutral, env=env)
                (snapshot / "tracked.diff").write_text(diff)
                write_json(snapshot / "sha256.json", hashes)
                for path in source_files(repo, crate):
                    destination = snapshot / path.relative_to(repo)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
            write_json(output / "metadata.json", metadata)
            executables = {}
            environments = {}
            for name in NAMES:
                current_env = dict(env, CARGO_TARGET_DIR=str(output / "build" / name),
                                   CRITERION_HOME=str(output / "raw" / name))
                environments[name] = current_env
                command = ["rustup", "run", args.toolchain, "cargo", "bench", "--locked", "--offline",
                           "--manifest-path", str(crates[name] / "Cargo.toml"), "--target", target,
                           "--bench", "memory_ops", "--no-run", "--message-format=json"]
                log = log_command(command, label=f"build-{name}", cwd=neutral, env=current_env,
                                  output=output, metadata=metadata)
                executables[name], artifact = build_artifacts(log)
                metadata["binaries"][name] = {"path": str(executables[name]), **artifact}
            if metadata["binaries"][NAMES[0]]["criterion_features"] != metadata["binaries"][NAMES[1]]["criterion_features"]:
                raise ValueError("Criterion compiled features differ between implementations")
            for name in NAMES:
                log_command([str(executables[name]), "--test"], label=f"smoke-{name}", cwd=neutral,
                            env=environments[name], output=output, metadata=metadata, cpu=cpu)
            pairs = []
            for number in range(1, args.pairs + 1):
                order = list(NAMES if number % 2 else reversed(NAMES))
                pair = {"pair": number, "order": order, "results": {}}
                baseline = f"pair-{number}"
                for name in order:
                    log_command([str(executables[name]), "--bench", "--save-baseline", baseline, *CRITERION_ARGS],
                                label=f"{baseline}-{name}", cwd=neutral, env=environments[name],
                                output=output, metadata=metadata, cpu=cpu)
                    pair["results"][name] = read_results(output / "raw" / name, baseline)
                pairs.append(pair)
                write_json(output / "pairs.json", pairs)
            for name in NAMES:
                if source_hashes(repos[name], crates[name]) != metadata["repositories"][name]["source_sha256"]:
                    raise ValueError(f"{name} source files changed during comparison; refusing a final report")
            if sha256(rustc_path) != metadata["rustc_sha256"]:
                raise ValueError("Compiler changed during comparison; refusing a final report")
            metadata.update(status="complete", finished_utc=stamp())
            write_report(output, metadata, pairs)
            write_json(output / "metadata.json", metadata)
            print(f"Completed comparison: {output / 'report.md'}", flush=True)
    except BaseException as error:
        metadata.update(status="failed", finished_utc=stamp(), error=str(error))
        write_json(output / "metadata.json", metadata)
        raise
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f"Comparison failed: {error}")
