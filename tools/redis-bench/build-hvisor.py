#!/usr/bin/env python3
"""Build two isolated production AArch64 hvisor images without changing branches."""

import argparse
import datetime
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile


REPO = Path(__file__).resolve().parents[2]
BASELINE_REF = "d2a078c40979122a30999651553ecaca3b5371db"
VERIFIED_REF = "df547ceda31ec3d41d1f8f14fdff71ac7757e0e5"
TOOLCHAIN = "nightly-2024-05-05"
PER_CPU_SOURCE = "src/consts.rs"
ORIGINAL_PER_CPU_SIZE = 512 * 1024
ORIGINAL_PER_CPU_DECLARATION = b"pub const PER_CPU_SIZE: usize = 512 * 1024; // 512 KiB\n"
ELF = "target/aarch64-unknown-none/release/hvisor"
BIN = ELF + ".bin"
FLAGS = [
    "-Clink-arg=-Tplatform/aarch64/qemu-gicv3/linker.ld",
    "-Ctarget-feature=+a72,+v8a,+strict-align,-neon,-fp-armv8",
    "-Cforce-frame-pointers=yes",
]
CONFIG = {
    "ARCH": "aarch64", "BOARD": "qemu-gicv3", "MODE": "release", "LOG": "error",
    # Cargo merges array values from ancestor config files. A snapshot inside
    # this repository otherwise receives -T twice, corrupting its BSS layout.
    "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(FLAGS),
}
BUILD = [
    "cargo", "build", "--offline", "--locked", "--target", "aarch64-unknown-none",
    "-Z", "build-std=core,alloc", "-Z", "build-std-features=compiler-builtins-mem",
    "--release",
]
ARTIFACTS = [
    ELF, BIN, BIN + ".tmp", "Cargo.toml", "Cargo.lock", ".config", ".cargo/config.toml",
    "platform/aarch64/qemu-gicv3/board.rs", "platform/aarch64/qemu-gicv3/platform.mk",
    "platform/aarch64/qemu-gicv3/image/bootloader/u-boot-atf.bin",
]


def output(command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment(src):
    env = os.environ.copy()
    for name in list(env):
        if name in {
            "RUSTFLAGS", "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
            "CARGO_BUILD_RUSTFLAGS", "CARGO_BUILD_TARGET", "CARGO_ENCODED_RUSTFLAGS",
            "RUSTC_BOOTSTRAP", "FEATURES", "BID",
        } or name.startswith(("CARGO_PROFILE_", "CARGO_TARGET_AARCH64_")):
            del env[name]
    env.update(CONFIG)
    env.update(RUSTUP_TOOLCHAIN=TOOLCHAIN, CARGO_TARGET_DIR=str(src / "target"))
    return env


def archive(repo, commit):
    return subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=repo)


def source_patch(data, per_cpu_size):
    """Describe the sole supported control patch from the original archive bytes."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        try:
            member = tar.getmember(PER_CPU_SOURCE)
        except KeyError as error:
            raise RuntimeError(f"Unsupported source archive: missing {PER_CPU_SOURCE}") from error
        if not member.isfile():
            raise RuntimeError(f"Expected a regular source file: {PER_CPU_SOURCE}")
        original = tar.extractfile(member).read()
    if original.count(ORIGINAL_PER_CPU_DECLARATION) != 1:
        raise RuntimeError(f"Unsupported source archive: expected exactly one original 512 KiB "
                           f"PER_CPU_SIZE declaration in {PER_CPU_SOURCE}")
    if per_cpu_size is None or per_cpu_size == ORIGINAL_PER_CPU_SIZE:
        return {}, []
    declaration = (f"pub const PER_CPU_SIZE: usize = {per_cpu_size}; "
                   f"// {per_cpu_size // 1024} KiB (Redis benchmark control)\n").encode()
    patched = original.replace(ORIGINAL_PER_CPU_DECLARATION, declaration, 1)
    patch = {
        "name": "per-cpu-size-control",
        "path": PER_CPU_SOURCE,
        "original_bytes": ORIGINAL_PER_CPU_SIZE,
        "requested_bytes": per_cpu_size,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "diff": "".join(difflib.unified_diff(
            original.decode().splitlines(keepends=True), patched.decode().splitlines(keepends=True),
            fromfile="a/" + PER_CPU_SOURCE, tofile="b/" + PER_CPU_SOURCE)),
    }
    return {PER_CPU_SOURCE: patched}, [patch]


def check_source(src, data, overrides=None):
    """Check every tracked file against the commit plus the explicit control patch."""
    overrides = overrides or {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            path = src / member.name
            if member.isfile():
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"Missing or replaced source file: {path}")
                expected = overrides.get(member.name, tar.extractfile(member).read())
                if path.read_bytes() != expected:
                    raise RuntimeError(f"Source differs from selected commit plus declared patches: {path}")
            elif member.issym():
                if not path.is_symlink() or os.readlink(path) != member.linkname:
                    raise RuntimeError(f"Source symlink differs from selected commit: {path}")


def verified_revision(src):
    for block in (src / "Cargo.lock").read_text().split("[[package]]"):
        if re.search(r'^name = "verified-hv-mem"$', block, re.MULTILINE):
            source = re.search(r'^source = "([^"]+)"$', block, re.MULTILINE)
            if source and "#" in source[1]:
                return source[1].rsplit("#", 1)[1]
            raise RuntimeError("verified-hv-mem must be pinned to a git commit in Cargo.lock")
    return None


def check_linker(src, env):
    symbols = {}
    for line in output(["rust-nm", ELF], cwd=src, env=env).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] in {"stext", "etext", "sbss", "ebss", "__core_end"}:
            symbols[parts[2]] = int(parts[0], 16)
    try:
        valid = (symbols["stext"] == 0x40400000
                 and symbols["stext"] < symbols["etext"] <= symbols["sbss"]
                 < symbols["ebss"] <= symbols["__core_end"])
    except KeyError as error:
        raise RuntimeError(f"Missing linker symbol: {error}") from error
    if not valid:
        raise RuntimeError(f"Invalid image layout (possible duplicate linker script): {symbols}")
    return {name: hex(value) for name, value in symbols.items()}


def check_flags(src):
    fingerprints = list((src / "target/aarch64-unknown-none/release/.fingerprint")
                        .glob("hvisor-*/bin-hvisor.json"))
    if not fingerprints:
        raise RuntimeError(f"Missing Cargo production-binary fingerprint: {src}")
    for path in fingerprints:
        data = json.loads(path.read_text())
        if data.get("rustflags") == FLAGS and data.get("features") == "[]":
            return
    raise RuntimeError(f"No production build with the expected isolated rustflags: {src}")


def reuse(directory, commit, data, per_cpu_size=None):
    meta = json.loads((directory / "metadata.json").read_text())
    src = directory / "src"
    env = environment(src)
    overrides, patches = source_patch(data, per_cpu_size)
    if (meta.get("per_cpu_size_bytes", ORIGINAL_PER_CPU_SIZE)
            != (per_cpu_size or ORIGINAL_PER_CPU_SIZE)
            or meta.get("patches", []) != patches):
        raise RuntimeError(f"Existing build has a different per-CPU size or source patch: {directory}")
    if meta.get("commit") != commit or meta.get("toolchain") != TOOLCHAIN:
        raise RuntimeError(f"Existing build has a different commit/toolchain: {directory}")
    if any(meta.get("configuration", {}).get(key) != value for key, value in CONFIG.items()):
        raise RuntimeError(f"Existing build has a different configuration: {directory}")
    if meta.get("commands", {}).get("build") != BUILD:
        raise RuntimeError(f"Existing build used different Cargo arguments: {directory}")
    if meta.get("rustc_vv") != output(["rustc", "-vV"], cwd=src, env=env):
        raise RuntimeError(f"Existing build used a different rustc: {directory}")
    for name in ARTIFACTS:
        path = src / name
        recorded = meta.get("artifacts", {}).get(name, {})
        if (not path.is_file() or recorded.get("path") != str(path)
                or recorded.get("size") != path.stat().st_size
                or recorded.get("sha256") != sha256(path)):
            raise RuntimeError(f"Existing artifact is missing or changed: {path}")
    check_source(src, data, overrides)
    if meta.get("verified_hv_mem_commit") != verified_revision(src):
        raise RuntimeError(f"Verified dependency revision differs: {directory}")
    check_flags(src)
    if meta.get("validated_linker_symbols") != check_linker(src, env):
        raise RuntimeError(f"Existing image has unexpected linker symbols: {directory}")
    print(f"Reused verified artifacts: {src / BIN}", flush=True)


def run_logged(commands, logfile, src, env):
    with logfile.open("w") as log:
        for command in commands:
            log.write("$ " + shlex.join(command) + "\n")
            log.flush()
            try:
                subprocess.run(command, cwd=src, env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True)
            except subprocess.CalledProcessError as error:
                raise RuntimeError(f"Command failed; see {logfile}") from error


def build(repo, directory, label, commit, data, kconfig_python, per_cpu_size=None):
    overrides, patches = source_patch(data, per_cpu_size)
    directory.mkdir(parents=True, exist_ok=False)
    src = directory / "src"
    src.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(src, filter="data")
    for name, content in overrides.items():
        (src / name).write_bytes(content)
    check_source(src, data, overrides)
    (src / "target").mkdir(exist_ok=True)
    env = environment(src)
    commands = {
        "configure": ["make", "--no-print-directory", "-j1", "defconfig", "gen_cargo_config",
                      "kconfig_python=" + kconfig_python],
        "build": BUILD,
        "package_and_checks": [
            ["rust-objcopy", "--binary-architecture=aarch64", ELF, "--strip-all", "-O", "binary", BIN + ".tmp"],
            ["mkimage", "-n", "hvisor_img", "-A", "arm64", "-O", "linux", "-C", "none",
             "-T", "kernel", "-a", "0x40400000", "-e", "0x40400000", "-d", BIN + ".tmp", BIN],
            [sys.executable, "tools/check_hv_mem_overlap.py", ELF, "platform/aarch64/qemu-gicv3/board.rs"],
            ["readelf", "-h", ELF], ["readelf", "-p", ".comment", ELF],
        ],
    }
    meta = {
        "label": label, "source_repository": str(repo), "commit": commit,
        "snapshot_method": "git archive --format=tar " + commit,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "toolchain": TOOLCHAIN, "configuration": CONFIG,
        "build_environment": {key: env[key] for key in [*CONFIG, "RUSTUP_TOOLCHAIN", "CARGO_TARGET_DIR"]},
        "verified_hv_mem_commit": verified_revision(src), "commands": commands,
        "working_directory": str(src), "patches": patches,
        "per_cpu_size_bytes": per_cpu_size or ORIGINAL_PER_CPU_SIZE,
        "rustc_vv": output(["rustc", "-vV"], cwd=src, env=env),
        "cargo_version": output(["cargo", "-V"], cwd=src, env=env).strip(),
    }
    metadata = directory / "metadata.json"
    metadata.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Building {label} at {commit}; logs: {directory}", flush=True)
    run_logged([commands["configure"]], directory / "configure.log", src, env)
    run_logged([commands["build"]], directory / "build.log", src, env)
    check_flags(src)
    meta["validated_linker_symbols"] = check_linker(src, env)
    run_logged(commands["package_and_checks"], directory / "package.log", src, env)
    check_source(src, data, overrides)
    meta["artifacts"] = {
        name: {"path": str(src / name), "sha256": sha256(src / name), "size": (src / name).stat().st_size}
        for name in ARTIFACTS
    }
    meta["build_completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    metadata.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Built {src / BIN}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO, help="Git source repository")
    parser.add_argument("--baseline-ref", default=BASELINE_REF, help="Baseline commit, tag or branch")
    parser.add_argument("--verified-ref", default=VERIFIED_REF, help="Integrated hvisor commit, tag or branch")
    parser.add_argument("--output", type=Path, default=REPO / "target/redis-bench/build")
    parser.add_argument("--reuse", action="store_true", help="Reuse only matching, fully validated existing builds")
    parser.add_argument("--per-cpu-size", type=lambda value: int(value, 0), metavar="BYTES",
                        help="Apply the same explicit per-CPU data/stack size to both snapshots "
                             "(power of two, at least 4096 bytes; default: unchanged 524288)")
    parser.add_argument("--kconfig-python", help="Python executable with kconfiglib (default: repository Kconfig venv)")
    args = parser.parse_args()
    if args.per_cpu_size is not None and (args.per_cpu_size < 4096
                                         or args.per_cpu_size & (args.per_cpu_size - 1)):
        parser.error("--per-cpu-size must be a power of two of at least 4096 bytes")
    repo = args.repo.resolve()
    destination = args.output.resolve()
    plans = []
    for label, ref in [("baseline", args.baseline_ref), ("verified", args.verified_ref)]:
        commit = output(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], cwd=repo).strip()
        directory = destination / label
        if directory.exists() and not args.reuse:
            raise RuntimeError(f"Output exists: {directory}; use --reuse to validate it or choose a new --output")
        plans.append((label, directory, commit, archive(repo, commit)))
    # Reject an incompatible source declaration before creating either snapshot.
    for _, _, _, data in plans:
        source_patch(data, args.per_cpu_size)
    # Validate all existing outputs before creating any new snapshot.
    for _, directory, commit, data in plans:
        if directory.exists():
            reuse(directory, commit, data, args.per_cpu_size)
    pending = [plan for plan in plans if not plan[1].exists()]
    if not pending:
        return
    kconfig_python = args.kconfig_python or str(repo / "tools/kconfig/.venv/bin/python")
    if not shutil.which(kconfig_python):
        raise RuntimeError("Kconfig Python not found; prepare tools/kconfig/.venv or pass --kconfig-python")
    subprocess.run([kconfig_python, "-c", "import kconfiglib"], check=True)
    for program in ["make", "cargo", "rustc", "rust-objcopy", "rust-nm", "mkimage", "readelf"]:
        if not shutil.which(program):
            raise RuntimeError(f"Required build tool not found: {program}")
    for label, directory, commit, data in pending:
        build(repo, directory, label, commit, data, kconfig_python, args.per_cpu_size)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"build-hvisor: {error}", file=sys.stderr)
        sys.exit(1)
