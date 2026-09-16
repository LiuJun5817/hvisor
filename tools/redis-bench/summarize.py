#!/usr/bin/env python3
"""Summarize Redis run.py output without changing samples or other raw artifacts."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import sys


VARIANTS = ("baseline", "verified")
WORKLOADS = ("get", "set", "mixed_90get_10set")


def validate_memtier(metadata):
    version_text = metadata.get("memtier_version", "")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_text)
    version = tuple(int(part) for part in match.groups()) if match else None
    valid = version is not None and version >= (2, 4, 4)
    if version is None:
        reason = "The memtier version is missing or unrecognized; request statistics cannot be validated."
    elif not valid:
        reason = (f"memtier {'.'.join(map(str, version))} predates 2.4.4 and has the timestamp-averaging bug "
                  "that corrupts JSON duration and throughput. Request records are diagnostic only.")
    else:
        reason = None
    return {"version": list(version) if version else None, "minimum_version": "2.4.4",
            "valid": valid, "invalid_reason": reason}


def number(value, label, positive=False):
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"Invalid {label}: {value!r}")
    return result


def describe(values):
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None,
                "max": None, "sample_sd": None}
    return {"n": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values), "min": min(values), "max": max(values),
            "sample_sd": statistics.stdev(values) if len(values) > 1 else None}


def quantile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def paired_ratio(baseline, verified, repetitions, rng):
    paired_ids = sorted(baseline.keys() & verified.keys(), key=str)
    result = {"paired_n": len(paired_ids), "paired_ids": paired_ids,
              "unpaired_baseline_ids": sorted(baseline.keys() - verified.keys(), key=str),
              "unpaired_verified_ids": sorted(verified.keys() - baseline.keys(), key=str),
              "verified_over_baseline": None, "ratio_ci95": None,
              "change_percent": None, "change_percent_ci95": None}
    if not paired_ids:
        return result
    # Lists represent repeated process starts in one VM. Resample entire paired
    # VM clusters while retaining each cluster's sample count and sum.
    pairs = []
    for key in paired_ids:
        base = baseline[key] if isinstance(baseline[key], list) else [baseline[key]]
        value = verified[key] if isinstance(verified[key], list) else [verified[key]]
        pairs.append((sum(base), len(base), sum(value), len(value)))
    base_sum = sum(item[0] for item in pairs)
    if base_sum <= 0:
        result["ci_unavailable_reason"] = "Baseline mean is zero."
        return result
    def ratio_of_means(draw):
        return ((sum(item[2] for item in draw) / sum(item[3] for item in draw))
                / (sum(item[0] for item in draw) / sum(item[1] for item in draw)))

    ratio = ratio_of_means(pairs)
    result.update(verified_over_baseline=ratio, change_percent=(ratio - 1) * 100)
    if len(pairs) < 2:
        result["ci_unavailable_reason"] = "At least two complete pairs are required."
        return result
    ratios = []
    for _ in range(repetitions):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        denominator = sum(item[0] for item in draw)
        if denominator == 0:
            result["ci_unavailable_reason"] = "A bootstrap draw has a zero baseline mean."
            return result
        ratios.append(ratio_of_means(draw))
    ratios.sort()
    ci = [quantile(ratios, 0.025), quantile(ratios, 0.975)]
    result.update(ratio_ci95=ci, change_percent_ci95=[(bound - 1) * 100 for bound in ci])
    return result


def percentile(totals, requested):
    candidates = []
    for key, value in totals.get("Percentile Latencies", {}).items():
        match = re.fullmatch(r"p?(\d+(?:\.\d+)?)", key, re.IGNORECASE)
        if match:
            candidates.append((abs(float(match[1]) - requested), key, float(match[1]), value))
    if not candidates:
        raise ValueError(f"Missing percentile latencies for P{requested}")
    distance, key, actual, value = min(candidates)
    if distance > 0.001:
        raise ValueError(f"P{requested} not reported; nearest percentile is {key}")
    return number(value, key), {"key": key, "percentile": actual, "unit": "ms"}


def make_group(rows, identity, fields, repetitions, rng):
    group = {"pairing_key": identity, "variants": {}, "comparisons": {}}
    indexes = {variant: {} for variant in VARIANTS}
    for row in rows:
        variant, key = row["variant"], row[identity]
        if key in indexes[variant]:
            raise ValueError(f"Duplicate {variant} {identity}={key} in one measurement group")
        indexes[variant][key] = row
    for variant in VARIANTS:
        group["variants"][variant] = {
            field: describe([number(row[field], field) for row in indexes[variant].values()])
            for field in fields
        }
    for field in fields:
        group["comparisons"][field] = paired_ratio(
            {key: number(row[field], field) for key, row in indexes["baseline"].items()},
            {key: number(row[field], field) for key, row in indexes["verified"].items()},
            repetitions, rng)
    return group


def process_group(rows, repetitions, rng):
    grouped = {variant: defaultdict(dict) for variant in VARIANTS}
    has_run = ["run" in row for row in rows]
    if any(has_run) and not all(has_run):
        raise ValueError("Process samples mix the old and new VM pairing schemas")
    for row in rows:
        run = row.get("run", "legacy_single_vm")
        repeats = grouped[row["variant"]][run]
        if row["repeat"] in repeats:
            raise ValueError(f"Duplicate process sample: {row['variant']}/{run}/{row['repeat']}")
        repeats[row["repeat"]] = number(row["startup_ns"], "startup_ns", positive=True)
    clusters = {variant: {} for variant in VARIANTS}
    unpaired = {variant: [] for variant in VARIANTS}
    for run in grouped["baseline"].keys() | grouped["verified"].keys():
        base = grouped["baseline"].get(run, {})
        verified = grouped["verified"].get(run, {})
        common = sorted(base.keys() & verified.keys())
        if common:
            clusters["baseline"][run] = [base[key] for key in common]
            clusters["verified"][run] = [verified[key] for key in common]
        for variant, own, other in [("baseline", base, verified), ("verified", verified, base)]:
            unpaired[variant].extend([[run, key] for key in sorted(own.keys() - other.keys())])
    comparison = paired_ratio(clusters["baseline"], clusters["verified"], repetitions, rng)
    comparison.update(unpaired_baseline_ids=unpaired["baseline"], unpaired_verified_ids=unpaired["verified"],
                      paired_process_starts=sum(len(values) for values in clusters["baseline"].values()))
    return {
        "pairing_key": "run (VM cluster); repeats matched within each run",
        "variants": {variant: {"startup_ns": describe([value for repeats in grouped[variant].values()
                                                       for value in repeats.values()])} for variant in VARIANTS},
        "comparisons": {"startup_ns": comparison},
        "independent_vms_per_variant": {variant: len(grouped[variant]) for variant in VARIANTS},
        "sampling_limit": "Starts within a VM are correlated; bootstrap resamples whole paired VM clusters. No across-VM CI is available with only one VM per variant.",
        "cache_state": "Warm executable: the boot Redis server already ran.",
    }


def boot_identity(directory):
    parts = Path(directory).parts
    for index, part in enumerate(parts):
        match = re.fullmatch(r"\d+-(baseline|verified)", part)
        if match and index > 0 and parts[index - 1] in {"startup", "capacity", "fixed_rate"}:
            return "/".join(parts[index - 1:]), match[1], parts[index - 1]
    raise ValueError(f"Cannot identify boot attempt phase and variant: {directory}")


def read_boot_records(directory):
    records = []
    for path in sorted(directory.rglob("attempt-*")):
        if not path.is_dir():
            continue
        source = path / "boot-attempt.json"
        if source.is_file():
            content = source.read_bytes()
            record = json.loads(content)
            record["record_file"] = str(source.relative_to(directory))
            record["record_sha256"] = hashlib.sha256(content).hexdigest()
        elif (path / "command.json").is_file():
            record = {"status": "unclassified", "reason": "Attempt was launched but has no final boot-attempt.json record."}
        else:
            continue
        record["directory"] = str(path.relative_to(directory))
        records.append(record)
    return records


def summarize_boot_attempts(failures, records, metadata):
    attempts = {}
    for record in [*failures, *records]:
        key, variant, phase = boot_identity(record["directory"])
        if record.get("variant", variant) != variant:
            raise ValueError(f"Boot attempt variant does not match directory: {key}")
        status = ("failed" if record.get("metric") == "boot_attempt_failure"
                  else record.get("status", "unclassified"))
        if status == "healthy" and record.get("online_guest_cpus") != "0-1":
            raise ValueError(f"Healthy boot record does not have both guest CPUs online: {key}")
        if status not in {"healthy", "failed", "unclassified"}:
            raise ValueError(f"Unknown boot attempt status: {status}")
        current = dict(record, directory=key, variant=variant, phase=phase, status=status)
        if key in attempts and attempts[key]["status"] != status:
            raise ValueError(f"Conflicting boot attempt records: {key}")
        attempts[key] = dict(attempts.get(key, {}), **current)
    instrumented = bool(attempts or metadata.get("boot_failure_policy"))
    counts = {}
    for variant in VARIANTS:
        own = [item for item in attempts.values() if item["variant"] == variant]
        total = len(own)
        counts[variant] = {"total": total, **{status: sum(item["status"] == status for item in own)
                          for status in ["healthy", "failed", "unclassified"]}}
        counts[variant]["recorded_failure_fraction"] = counts[variant]["failed"] / total if total else None
    return {"instrumented": instrumented, "scope": "All startup, capacity and fixed-rate phases, including retries",
            "variants": counts, "records": [attempts[key] for key in sorted(attempts)],
            "policy": metadata.get("boot_failure_policy", "Boot-attempt policy was not recorded"),
            "startup_condition": "Performance timings include only accepted boots with online guest CPUs 0-1; failed attempts are excluded from timing averages and counted separately."}


def summarize(rows, metadata, repetitions, seed, boot_records=None):
    rng = random.Random(seed)
    bins = defaultdict(list)
    warnings = []
    tool_validation = validate_memtier(metadata)
    if not tool_validation["valid"]:
        warnings.append(tool_validation["invalid_reason"] + " Startup uses independent timers and is still summarized.")
    excluded_warmups = 0
    boot_failures = []
    for raw in rows:
        row = dict(raw)
        if row.get("warmup", False):
            excluded_warmups += 1
            continue
        if row.get("variant") not in VARIANTS:
            raise ValueError(f"Unknown variant: {row.get('variant')}")
        if row.get("metric") == "boot_attempt_failure":
            boot_failures.append(row)
            continue
        if row.get("valid") is False or row.get("status", "ok") != "ok":
            raise ValueError("Failed/invalid measurements must not be included as successful samples")
        metric = row.get("metric")
        if metric in {"vm_redis_startup", "redis_process_startup"}:
            row["startup_ns"] = number(row["startup_ns"], "startup_ns", positive=True)
            bins[metric].append(row)
        elif metric == "requests":
            phase, workload = row["phase"], row["workload"]
            if phase not in {"capacity", "fixed_rate"} or workload not in WORKLOADS:
                raise ValueError(f"Unknown request group: {phase}/{workload}")
            row["ops_per_second"] = number(row["ops_per_second"], "ops_per_second", positive=True)
            row["percentile_keys"] = {}
            for p in [50, 95, 99]:
                row[f"p{p}_ms"], row["percentile_keys"][str(p)] = percentile(row["memtier_totals"], p)
            for name in ["keyspace_misses", "redis_errors", "evictions"]:
                if row.get(name, 0):
                    warnings.append(f"{phase}/{workload}/{row['variant']}/{row['run']}: {name}={row[name]}")
            bins[f"requests/{phase}/{workload}"].append(row)
        else:
            raise ValueError(f"Unknown metric: {metric}")
    groups = {}
    expected = metadata.get("arguments", {})
    required = {"vm_redis_startup": expected.get("startup_runs"),
                "redis_process_startup": expected.get("process_runs")}
    for phase, count in [("capacity", "capacity_runs"), ("fixed_rate", "request_runs")]:
        for workload in WORKLOADS:
            required[f"requests/{phase}/{workload}"] = expected.get(count)
    for name in required:
        group_rows = bins[name]
        if name.startswith("requests/"):
            fields = ["ops_per_second", "p50_ms", "p95_ms", "p99_ms"]
            group = make_group(group_rows, "run", fields, repetitions, rng)
            group["latency_aggregation"] = "Mean of each run's percentile; not a pooled request percentile."
            group["percentile_keys"] = [row["percentile_keys"] for row in group_rows]
            group["primary_metric"] = "ops_per_second" if "/capacity/" in name else "p99_ms"
            group["request_statistics_valid"] = tool_validation["valid"]
            group["workload_valid"] = not any(row.get(key, 0) for row in group_rows
                                                for key in ["keyspace_misses", "redis_errors", "evictions"])
            if "/fixed_rate/" in name:
                rate_checks = {}
                targets = set()
                sustained = all(any(row["variant"] == variant for row in group_rows) for variant in VARIANTS)
                for variant in VARIANTS:
                    ratios, failed = [], []
                    for row in group_rows:
                        if row["variant"] != variant:
                            continue
                        target = number(row["target_rps"], "target_rps", positive=True)
                        targets.add(target)
                        ratio = row["ops_per_second"] / target
                        ratios.append(ratio)
                        tolerance = 0.05 if row["seconds"] >= 10 else 0.2
                        if abs(ratio - 1) > tolerance:
                            failed.append(row["run"])
                            sustained = False
                    rate_checks[variant] = {"achieved_target_ratio": describe(ratios), "failed_run_ids": failed}
                group["fixed_rate"] = {"target_rps_values": sorted(targets), "checks": rate_checks,
                                       "matched_sustained_load": sustained and len(targets) == 1}
                if group_rows and not group["fixed_rate"]["matched_sustained_load"]:
                    warnings.append(f"{name}: target load differs or was not sustained; latency comparison is not equivalent-load evidence.")
            if not tool_validation["valid"]:
                group["diagnostic_only_reason"] = tool_validation["invalid_reason"]
                for comparison in group["comparisons"].values():
                    comparison.update(verified_over_baseline=None, ratio_ci95=None,
                                      change_percent=None, change_percent_ci95=None,
                                      invalid_reason=tool_validation["invalid_reason"])
                if "fixed_rate" in group:
                    group["fixed_rate"]["matched_sustained_load"] = False
        else:
            group = (process_group(group_rows, repetitions, rng) if name == "redis_process_startup"
                     else make_group(group_rows, "run", ["startup_ns"], repetitions, rng))
            group["primary_metric"] = "startup_ns"
        group["expected_samples_per_variant"] = required[name]
        for variant in VARIANTS:
            count = group["variants"][variant][group["primary_metric"]]["n"]
            if required[name] is not None and count != required[name]:
                warnings.append(f"{name}/{variant}: {count} samples; expected {required[name]}.")
        comparison = group["comparisons"][group["primary_metric"]]
        if comparison["unpaired_baseline_ids"] or comparison["unpaired_verified_ids"]:
            warnings.append(f"{name}: ratio and CI use complete pairs only; unpaired samples remain in descriptive statistics.")
        groups[name] = group
    status = metadata.get("status", "running_or_unknown")
    if status != "complete":
        warnings.insert(0, f"Run status is {status}; these results are provisional/incomplete.")
    boot_attempts = summarize_boot_attempts(boot_failures, boot_records or [], metadata)
    failed_attempts = sum(counts["failed"] for counts in boot_attempts["variants"].values())
    if failed_attempts:
        warnings.append(f"{failed_attempts} failed/degraded boot attempts are retained separately. Startup times are conditional on healthy two-vCPU boots, not all attempted launches.")
    if any(counts["unclassified"] for counts in boot_attempts["variants"].values()):
        warnings.append("Some launched boot attempts have no final status; recorded failure fractions do not establish overall boot reliability.")
    return {"schema_version": 1, "generated_utc": datetime.now(timezone.utc).isoformat(),
            "run_status": status, "raw_sample_count": len(rows), "excluded_warmups": excluded_warmups,
            "request_tool_validation": tool_validation,
            "boot_attempts": boot_attempts,
            "bootstrap": {"method": "paired percentile bootstrap of ratio of means",
                          "resamples": repetitions, "seed": seed, "confidence": 0.95,
                          "pairing": "run IDs for VM startup and request runs; whole paired VM clusters for process startup, with repeats matched inside each VM"},
            "groups": groups, "warnings": warnings}


def fmt(value, digits=3):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def change_text(comparison):
    estimate, ci = comparison["change_percent"], comparison["change_percent_ci95"]
    if estimate is None:
        return "n/a"
    return f"{estimate:+.2f}%" + (f" [{ci[0]:+.2f}%, {ci[1]:+.2f}%]" if ci else " [CI unavailable]")


def build_provenance(metadata):
    lines, patches = [], {}
    for variant, build in metadata.get("builds", {}).items():
        text = f"- {variant}: hvisor `{build.get('commit', 'unknown')}`"
        if build.get("verified_hv_mem_commit"):
            text += f", pinned verified-hv-mem `{build['verified_hv_mem_commit']}`"
        size = build.get("per_cpu_size_bytes")
        text += (f"; `PER_CPU_SIZE` {size:,} bytes ({size / 1024:g} KiB)"
                 if size is not None else "; `PER_CPU_SIZE` not recorded in this run's metadata")
        lines.append(text + ".")
        for patch in build.get("patches", []):
            key = json.dumps(patch, sort_keys=True)
            patches.setdefault(key, {"patch": patch, "variants": []})["variants"].append(variant)
    lines += ["", "Full build settings, image SHA-256 values and source patch records are embedded in "
              "[metadata.json](metadata.json) under `builds`.", ""]
    if patches:
        lines += ["These measurements apply to the patched benchmark builds described below.", ""]
        for record in patches.values():
            patch = record["patch"]
            variants = ", ".join(record["variants"])
            lines += [f"Source patch `{patch.get('name', 'unnamed')}` on {variants}: "
                      f"`{patch.get('path', 'path not recorded')}`.", ""]
            if patch.get("diff"):
                lines += ["```diff", patch["diff"].rstrip("\n"), "```", ""]
            else:
                lines += ["The patch diff was not recorded; see the source patch record in metadata.", ""]
    else:
        lines += ["Recorded source patches: none.", ""]
    return lines


def markdown(summary, metadata):
    args = metadata.get("arguments", {})
    lines = ["# Redis application comparison", "", f"Run status: **{summary['run_status']}**.", "",
             "The VM startup metric starts when the host sends U-Boot `bootm` and ends at the first successful Redis PING. "
             "It includes hvisor and root Linux zone0 startup; it excludes the preceding QEMU/U-Boot startup and is not a dynamic zone1 launch.", "",
             "These are AArch64 QEMU TCG measurements on an x86_64 host, not physical AArch64 performance measurements. "
             "Redis traffic uses identical virtio-mmio networking; the PCI/SMMU network data path is not evaluated.", "",
             "## Configuration", ""]
    lines += build_provenance(metadata)
    lines += [f"- Dataset: {args.get('keys', 'unknown')} keys, {args.get('value_size', 'unknown')} bytes/value; "
              f"{args.get('clients', 'unknown')} clients, pipeline 1; persistence disabled.",
              f"- QEMU CPU affinity: `{args.get('qemu_cpus', 'unknown')}`; client affinity: `{args.get('client_cpus', 'unknown')}`.", "",
              "## Boot reliability", ""]
    attempts = summary["boot_attempts"]
    if attempts["instrumented"]:
        lines += ["Counts include every recorded attempt across startup, capacity and fixed-rate phases, including retries.", "",
                  "| Variant | Failed / total attempts | Healthy | Unclassified / unfinished | Recorded failure fraction |",
                  "|---|---:|---:|---:|---:|"]
        for variant in VARIANTS:
            values = attempts["variants"][variant]
            fraction = values["recorded_failure_fraction"]
            rate = f"{fraction * 100:.2f}%" if fraction is not None else "n/a"
            lines.append(f"| {variant} | {values['failed']} / {values['total']} | {values['healthy']} | {values['unclassified']} | {rate} |")
        lines += ["", "Failed or degraded boots are excluded from performance averages. The startup tables below are conditional on "
                  "successful two-vCPU boots; they do not establish that all attempted launches were reliable. "
                  "They also exclude time spent on failed attempts before a successful retry. "
                  "An unclassified attempt has a QEMU command record but no final boot status.", ""]
    else:
        lines += ["This older run did not record complete per-attempt boot health. Boot reliability cannot be inferred from its successful samples.", ""]
    lines += ["## Startup", "", "| Measurement | Variant | N | Mean (ms) | Median (ms) | Min (ms) | Max (ms) | Sample SD (ms) |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name, title in [("vm_redis_startup", "hvisor + root VM → Redis"),
                        ("redis_process_startup", "Redis process in running VM")]:
        group = summary["groups"][name]
        for variant in VARIANTS:
            values = group["variants"][variant]["startup_ns"]
            columns = [fmt(values[key] / 1e6) if values[key] is not None else "n/a"
                       for key in ["mean", "median", "min", "max", "sample_sd"]]
            lines.append(f"| {title} | {variant} | {values['n']} | " + " | ".join(columns) + " |")
    lines += ["", "| Startup comparison | Complete pairs | Verified/baseline time change (95% CI) |",
              "|---|---:|---:|"]
    for name in ["vm_redis_startup", "redis_process_startup"]:
        comp = summary["groups"][name]["comparisons"]["startup_ns"]
        lines.append(f"| {name} | {comp['paired_n']} | {change_text(comp)} |")
    vm_counts = summary["groups"]["redis_process_startup"]["independent_vms_per_variant"]
    lines += ["", f"Process startup samples span {vm_counts['baseline']} baseline VMs and {vm_counts['verified']} verified VMs. "
              "Repeated starts within a VM are kept together when bootstrapping; a single VM pair cannot provide an across-VM interval. "
              "These are warm-executable starts after the boot Redis server already ran. "
              "Both startup metrics end at PING; SET/GET correctness checks follow outside the timer.", "",
              "## Requests", "", "Capacity means observed throughput at the configured client count and pipeline, not a search for maximum server capacity.", "",
              "| Phase | Workload | Variant | N | Mean ops/s | Median ops/s | SD ops/s | Mean run P50 (ms) | Mean run P95 (ms) | Mean run P99 (ms) |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for phase in ["capacity", "fixed_rate"]:
        for workload in WORKLOADS:
            group = summary["groups"][f"requests/{phase}/{workload}"]
            for variant in VARIANTS:
                values = group["variants"][variant]
                qps = values["ops_per_second"]
                lines.append(f"| {phase} | {workload} | {variant} | {qps['n']} | {fmt(qps['mean'], 1)} | "
                             f"{fmt(qps['median'], 1)} | {fmt(qps['sample_sd'], 1)} | "
                             + " | ".join(fmt(values[f"p{p}_ms"]["mean"]) for p in [50, 95, 99]) + " |")
    lines += ["", "| Phase | Workload | Primary metric | Complete pairs | Verified/baseline change (95% CI) |",
              "|---|---|---|---:|---:|"]
    for phase in ["capacity", "fixed_rate"]:
        for workload in WORKLOADS:
            group = summary["groups"][f"requests/{phase}/{workload}"]
            field = group["primary_metric"]
            comp = group["comparisons"][field]
            lines.append(f"| {phase} | {workload} | {field} | {comp['paired_n']} | {change_text(comp)} |")
    lines += ["", "| Fixed-rate workload | Target ops/s | Baseline achieved/target mean [min, max] | Verified achieved/target mean [min, max] | Matched and sustained |",
              "|---|---:|---:|---:|---|"]
    for workload in WORKLOADS:
        fixed = summary["groups"][f"requests/fixed_rate/{workload}"]["fixed_rate"]
        columns = []
        for variant in VARIANTS:
            ratio = fixed["checks"][variant]["achieved_target_ratio"]
            columns.append(f"{fmt(ratio['mean'])} [{fmt(ratio['min'])}, {fmt(ratio['max'])}]")
        target = ", ".join(fmt(value, 1) for value in fixed["target_rps_values"]) or "n/a"
        lines.append(f"| {workload} | {target} | " + " | ".join(columns)
                     + f" | {'yes' if fixed['matched_sustained_load'] else 'no'} |")
    lines += ["", "The fixed-rate check uses ±5% of target for runs of at least 10 seconds, and ±20% for shorter smoke runs. "
              "P50/P95/P99 values above are means of per-run percentiles, not percentiles of pooled requests. "
              "Latency values from memtier are in milliseconds. Full per-metric min/max, median and sample SD are in `summary.json`.", "",
              ]
    if not summary["request_tool_validation"]["valid"]:
        # Preserve raw/diagnostic JSON, but do not display invalid client numbers
        # in performance tables or allow their ratios to support a conclusion.
        start = lines.index("## Requests")
        lines = lines[:start + 1] + ["", "**Request results withheld.** "
                + summary["request_tool_validation"]["invalid_reason"], "",
                "Raw request records and diagnostic descriptive values in `summary.json` are retained, "
                "but request ratios and confidence intervals are invalid and omitted. Startup statistics above use our own timers.", ""]
    lines += [
              "## Statistical interpretation", "",
              f"Ratios compare means of complete matched pairs. The 95% intervals use {summary['bootstrap']['resamples']:,} "
              f"paired bootstrap resamples (seed {summary['bootstrap']['seed']}). VM startup and request pairs are matched by run number. "
              "Process starts are paired by run and repeat number, then resampled in whole VM clusters. "
              "Each resample recomputes the ratio of means, retaining the number of starts in each sampled cluster.", "",
              "Changes are `(verified / baseline − 1) × 100%`: a positive startup/latency change is slower; a positive throughput change is faster. "
              "An interval containing zero does not establish equivalence or prove negligible overhead. "
              "A practical equivalence margin and sufficient independent repetitions would be needed for that claim. "
              "No confidence interval is reported for fewer than two pairs.", ""]
    if summary["warnings"]:
        lines += ["## Measurement warnings", ""] + ["- " + warning for warning in summary["warnings"]] + [""]
    lines += ["Raw `samples.jsonl`, `metadata.json`, guest logs, memtier output and per-run measurements are retained unchanged.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path, help="Directory containing samples.jsonl and metadata.json")
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260916)
    args = parser.parse_args()
    if args.bootstrap_samples < 1000:
        parser.error("--bootstrap-samples must be at least 1000")
    directory = args.result_dir.resolve()
    sources = {name: (directory / name).read_bytes() for name in ["samples.jsonl", "metadata.json"]}
    rows = [json.loads(line) for line in sources["samples.jsonl"].decode().splitlines() if line.strip()]
    metadata = json.loads(sources["metadata.json"])
    result = summarize(rows, metadata, args.bootstrap_samples, args.seed, read_boot_records(directory))
    result["inputs"] = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                        for name, data in sources.items()}
    (directory / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    (directory / "results.md").write_text(markdown(result, metadata))
    print(f"Summarized {len(rows)} samples: {directory / 'results.md'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"summarize: {error}", file=sys.stderr)
        sys.exit(1)
