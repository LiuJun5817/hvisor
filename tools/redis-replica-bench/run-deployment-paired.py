#!/usr/bin/env python3
"""Run alternating native/integrated Redis deployment-startup pairs."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent.parent
DEFAULT_ROOT_DISK = (
    REPO / "platform/aarch64/qemu-gicv3-redis/image/virtdisk/redis-root.ext4"
)
METRICS = (
    "primary_vm_startup_ns",
    "primary_redis_startup_ns",
    "replica_vm_startup_ns",
    "replica_redis_startup_ns",
    "deployment_startup_ns",
)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def percentile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def compare_pairs(rows, metric, resamples, seed):
    complete = [row for row in rows if row.get("native") and row.get("integrated")]
    if not complete:
        return {"complete_pairs": 0}
    native = [row["native"][metric] for row in complete]
    integrated = [row["integrated"][metric] for row in complete]
    native_mean = statistics.mean(native)
    integrated_mean = statistics.mean(integrated)
    result = {
        "complete_pairs": len(complete),
        "native_mean_ns": native_mean,
        "native_median_ns": statistics.median(native),
        "integrated_mean_ns": integrated_mean,
        "integrated_median_ns": statistics.median(integrated),
        "mean_paired_difference_ns": statistics.mean(
            after - before for before, after in zip(native, integrated)
        ),
        "ratio_of_means": integrated_mean / native_mean,
        "relative_change": integrated_mean / native_mean - 1,
    }
    if len(complete) < 2:
        result["ci_unavailable_reason"] = "At least two complete pairs are required."
        return result

    rng = random.Random(seed)
    ratios = []
    for _ in range(resamples):
        draw = [rng.randrange(len(complete)) for _ in complete]
        before = statistics.mean(native[index] for index in draw)
        after = statistics.mean(integrated[index] for index in draw)
        ratios.append(after / before - 1)
    result["relative_change_95ci"] = [
        percentile(ratios, 0.025), percentile(ratios, 0.975)
    ]
    return result


def deployment_cell(sample):
    return "--" if sample is None else f"{sample['deployment_startup_ns'] / 1e9:.6f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=5,
                        help="number of complete pairs (default: 5)")
    parser.add_argument("--output", type=Path,
                        help="new result directory (default: timestamped under target)")
    parser.add_argument("--root-disk", type=Path, default=DEFAULT_ROOT_DISK)
    parser.add_argument("--qemu-cpus", default="0-5")
    parser.add_argument("--native-hvisor-dir", type=Path)
    parser.add_argument("--integrated-hvisor-dir", type=Path)
    parser.add_argument("--native-hvisor-binary", type=Path)
    parser.add_argument("--integrated-hvisor-binary", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--keep-going", action="store_true",
                        help="attempt remaining samples after a failure (default: stop)")
    args = parser.parse_args()
    if args.pairs <= 0:
        parser.error("--pairs must be positive")
    if args.bootstrap_samples < 1000:
        parser.error("--bootstrap-samples must be at least 1000")
    if not args.root_disk.is_file():
        parser.error(
            f"missing root disk: {args.root_disk}; run image-build/build-images.sh first"
        )
    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.output = REPO / "target/redis-replica-bench" / f"deployment-paired-{stamp}"
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error("output directory already exists")
    args.output.mkdir(parents=True)

    metadata = {
        "status": "running",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "pairs": args.pairs,
        "qemu_cpus": args.qemu_cpus,
        "root_disk": str(args.root_disk.resolve()),
        "bootstrap": {
            "method": "paired percentile bootstrap of ratio of means",
            "samples": args.bootstrap_samples,
            "seed": args.seed,
            "confidence": 0.95,
        },
        "samples": [],
    }
    write_json(args.output / "metadata.json", metadata)
    pair_rows = []
    failures = []
    sample_number = 0
    stopped_early = False

    for pair in range(1, args.pairs + 1):
        order = ["native", "integrated"] if pair % 2 else ["integrated", "native"]
        pair_row = {"pair": pair, "order": order}
        for position, variant in enumerate(order, 1):
            sample_number += 1
            sample_dir = args.output / f"pair-{pair:02d}-{position}-{variant}"
            command = [
                sys.executable, str(SCRIPT_DIR / "run-deployment.py"),
                "--variant", variant,
                "--output", str(sample_dir),
                "--root-disk", str(args.root_disk.resolve()),
                "--qemu-cpus", args.qemu_cpus,
            ]
            override = (
                args.native_hvisor_dir if variant == "native"
                else args.integrated_hvisor_dir
            )
            if override is not None:
                command += ["--hvisor-dir", str(override.resolve())]
            binary = (args.native_hvisor_binary if variant == "native"
                      else args.integrated_hvisor_binary)
            if binary is not None:
                command += ["--hvisor-binary", str(binary.resolve())]
            print(
                f"[{sample_number}/{args.pairs * 2}] pair {pair} {variant}: {sample_dir}",
                flush=True,
            )
            with (args.output / f"pair-{pair:02d}-{position}-{variant}.log").open("w") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            sample_record = {
                "pair": pair,
                "position": position,
                "variant": variant,
                "directory": str(sample_dir),
                "returncode": result.returncode,
            }
            metadata["samples"].append(sample_record)
            if result.returncode:
                failures.append(sample_record)
                action = "continuing" if args.keep_going else "stopping"
                print(f"  FAILED (exit {result.returncode}); {action}", flush=True)
                if not args.keep_going:
                    stopped_early = True
                    break
                continue
            timestamps = json.loads((sample_dir / "timestamps.json").read_text())
            pair_row[variant] = {metric: timestamps[metric] for metric in METRICS}
            print(
                f"  passed: deployment={timestamps['deployment_startup_ns'] / 1e9:.6f}s",
                flush=True,
            )
        pair_rows.append(pair_row)
        write_json(args.output / "pairs.json", pair_rows)
        write_json(args.output / "metadata.json", metadata)
        if stopped_early:
            break

    comparisons = {
        metric: compare_pairs(pair_rows, metric, args.bootstrap_samples, args.seed)
        for metric in METRICS
    }
    attempted = len(metadata["samples"])
    successful = attempted - len(failures)
    metadata.update(
        status=("passed" if not failures else
                "failed" if stopped_early else "completed_with_failures"),
        completed_utc=datetime.now(timezone.utc).isoformat(),
        attempted_samples=attempted,
        successful_samples=successful,
        failures=failures,
    )
    write_json(args.output / "metadata.json", metadata)
    write_json(args.output / "comparisons.json", comparisons)

    # Render explicitly so absent samples remain readable.
    report = args.output / "report.md"
    lines = [
        "# Redis primary-replica startup comparison", "",
        f"Status: **{metadata['status']}**. Scheduled pairs: {args.pairs}; "
        f"successful samples: {successful}/{attempted} attempted "
        f"({args.pairs * 2} scheduled).", "",
        "| Metric | Pairs | Native mean (s) | Integrated mean (s) | Relative change (95% paired bootstrap CI) |",
        "|---|---:|---:|---:|---:|",
    ]
    for metric in METRICS:
        item = comparisons[metric]
        if not item.get("complete_pairs"):
            lines.append(f"| `{metric}` | 0 | -- | -- | -- |")
            continue
        change = f"{item['relative_change'] * 100:+.3f}%"
        if "relative_change_95ci" in item:
            lower, upper = item["relative_change_95ci"]
            change += f" ({lower * 100:+.3f}%, {upper * 100:+.3f}%)"
        lines.append(
            f"| `{metric}` | {item['complete_pairs']} | "
            f"{item['native_mean_ns'] / 1e9:.6f} | "
            f"{item['integrated_mean_ns'] / 1e9:.6f} | {change} |"
        )
    lines += ["", "| Pair | Order | Native deployment (s) | Integrated deployment (s) |",
              "|---:|---|---:|---:|"]
    for row in pair_rows:
        lines.append(
            f"| {row['pair']} | {' → '.join(row['order'])} | "
            f"{deployment_cell(row.get('native'))} | "
            f"{deployment_cell(row.get('integrated'))} |"
        )
    lines += ["", "Intervals are descriptive because the experiment contains only a small "
              "number of pairs. The comparison measures the application-visible impact of "
              "the complete verified-memory-manager integration.", ""]
    report.write_text("\n".join(lines))
    print(f"report: {report}", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
