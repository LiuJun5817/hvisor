#!/usr/bin/env python3
"""Describe a completed local Redis build without changing its binaries."""

import argparse
import hashlib
import json
import platform
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SOURCES = [
    ("downloads/redis-7.2.5.tar.gz", "https://download.redis.io/releases/redis-7.2.5.tar.gz",
     "5981179706f8391f03be91d951acafaeda91af7fac56beffb2701963103e423d"),
    ("downloads/musl-1.2.5.tar.gz", "https://musl.libc.org/releases/musl-1.2.5.tar.gz",
     "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4"),
    ("downloads/compiler-rt-14.0.6.src.tar.xz",
     "https://github.com/llvm/llvm-project/releases/download/llvmorg-14.0.6/compiler-rt-14.0.6.src.tar.xz",
     "88df303840ca8fbff944e15e61c141226fe79f5d2b8e89fb024264d77841a02e"),
]
MODULES = {
    "ExtendPath": "37c372634b9797e8986e36ad6f2b4ee5fbb7ca85f7d210bcea8c432515f94061",
    "SetPlatformToolchainTools": "a1e12387e6b1a8a0113e8d4ac4d981935105562c8f9655b331364e0d2cfdd1a3",
    "HandleCompilerRT": "624227b8653743723f8e1213fa37ff0d25dd1c495add6f751da770fdd44eff11",
}
CLIENT_SOURCES = [
    ("downloads/libevent-2.1.12-stable.tar.gz",
     "https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz",
     "92e6de1be9ec176428fd2367677e61ceffc2ee1cb119035037a27d346b0403bb"),
    ("downloads/memtier_benchmark-2.5.1.tar.gz",
     "https://github.com/redis/memtier_benchmark/archive/refs/tags/2.5.1.tar.gz",
     "9b34e17a0d1d7e70b152eb442c6362161b5b764ce2ea98e97b7c74815bdd90b7"),
]
PACKAGES = {
    "autoconf": "96b528889794c4134015a63c75050f93d8aecdf5e3f2a20993c1433f4c61b80e",
    "automake": "59e3890fc8407bcf8ccc9f709d6513156346d5c942e8c624dc90435e58f6f978",
    "libtool": "fb994bc0152f4e77a791b51bc54fd5e38963aa6473e60c45369ef373b6119124",
    "libpcre3-dev": "79762f3fcd3a29ee9c9c0faf61c0d0292b83b9d07e0bd37566401f0e2109ca87",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(command):
    return subprocess.check_output(command, text=True).strip()


def checked_source(root, relative, url, expected):
    actual = sha256(root / relative)
    if actual != expected:
        raise ValueError(f"Source checksum mismatch: {relative}")
    return {"file": relative, "url": url, "sha256": actual}


def binary_record(root, relative):
    path = root / relative
    record = {"file": relative, "sha256": sha256(path), "size_bytes": path.stat().st_size}
    if relative.startswith("bin/aarch64/"):
        data = path.read_bytes()
        if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
            raise ValueError(f"Expected a little-endian AArch64 ELF64 binary: {relative}")
        offset = struct.unpack_from("<Q", data, 32)[0]
        size, count = struct.unpack_from("<HH", data, 54)
        headers = [struct.unpack_from("<IIQQQQQQ", data, offset + i * size) for i in range(count)]
        record["has_interpreter"] = any(header[0] == 3 for header in headers)
        record["dt_needed_count"] = sum(
            struct.unpack_from("<qQ", data, pos)[0] == 1
            for header in headers if header[0] == 2
            for pos in range(header[2], header[2] + header[5], 16)
        )
        if record["has_interpreter"] or record["dt_needed_count"]:
            raise ValueError(f"ARM binary unexpectedly requires guest shared libraries: {relative}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_dir", type=Path)
    parser.add_argument("--memtier", action="store_true")
    parser.add_argument("--output", type=Path, help="Alternate output, useful for auditing an existing build")
    args = parser.parse_args()
    root = args.build_dir.resolve()
    sources = [checked_source(root, *source) for source in SOURCES]
    for name, expected in MODULES.items():
        sources.append(checked_source(
            root, f"cmake/Modules/{name}.cmake",
            f"https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-14.0.6/cmake/Modules/{name}.cmake",
            expected,
        ))
    components = {"redis": "7.2.5", "musl": "1.2.5", "compiler_rt": "14.0.6"}
    binaries = [binary_record(root, f"bin/{arch}/{name}")
                for arch in ("aarch64", "native")
                for name in ("redis-server", "redis-cli", "redis-benchmark")]
    versions = {"redis_native": output([str(root / "bin/native/redis-server"), "--version"])}
    if "v=7.2.5 " not in versions["redis_native"]:
        raise ValueError("Native Redis binary is not the pinned version")
    packages = []
    if args.memtier:
        sources.extend(checked_source(root, *source) for source in CLIENT_SOURCES)
        components.update(memtier_benchmark="2.5.1", libevent="2.1.12-stable")
        binaries.append(binary_record(root, "bin/native/memtier_benchmark"))
        versions["memtier"] = output([str(root / "bin/native/memtier_benchmark"), "--version"])
        if "v=2.5.1 " not in versions["memtier"]:
            raise ValueError("memtier binary is not the pinned version with the upstream timing fix")
        for package, expected in PACKAGES.items():
            matches = [path for path in (root / "downloads").glob(f"{package}_*.deb")
                       if sha256(path) == expected]
            if not matches:
                raise ValueError(f"Missing verified local build package: {package}")
            path = sorted(matches)[0]
            packages.append({"file": str(path.relative_to(root)), "sha256": expected,
                             "metadata": output(["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"])})
    settings = {}
    for relative in ("redis-native/src/.make-settings", "redis-arm/src/.make-settings", "musl-src/config.mak"):
        path = root / relative
        settings[relative] = {"sha256": sha256(path), "text": path.read_text()}
    if args.memtier:
        path = root / "memtier-2.5.1-src/Makefile"
        names = {"CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"}
        settings["memtier-2.5.1-src/Makefile"] = {
            "sha256": sha256(path),
            "compiler_flags": [line for line in path.read_text().splitlines()
                               if " = " in line and line.split(" = ", 1)[0] in names],
        }
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "host": platform.platform(), "components": components, "sources": sources,
        "localized_ubuntu_packages": packages, "binaries": binaries, "binary_versions": versions,
        "toolchains": {name: output([command, "--version"]).splitlines()[0]
                       for name, command in (("clang", "clang"), ("gcc", "gcc"),
                                             ("lld", "ld.lld"), ("cmake", "cmake"))},
        "build_flags": {
            "redis_common": ["MALLOC=libc", "BUILD_TLS=no", "USE_SYSTEMD=no", "OPTIMIZATION=-O2"],
            "redis_arm": ["CC=aarch64-musl-clang", "AR=llvm-ar", "RANLIB=llvm-ranlib",
                          "uname_M=aarch64", "LDFLAGS=-static"],
            "generated_settings": settings,
        },
        "validation": ["Pinned source SHA256 values verified", "Native Redis version verified",
                       "AArch64 Redis ELF64 architecture, absent PT_INTERP and absent DT_NEEDED verified"],
        "caveats": [
            "Upstream release-header scripts may find the enclosing hvisor Git repository. Embedded Git SHA values are not authoritative Redis/memtier commits; use pinned source archive hashes.",
            "Redis -rdynamic exports dynamic symbols; an ARM ELF may be described as dynamically linked by file(1) despite having no interpreter or shared-library dependencies.",
            "Both hvisor variants must run the identical AArch64 Redis binary; the native server is for harness checks.",
        ],
    }
    if args.memtier:
        manifest["build_flags"]["memtier"] = ["--disable-tls", "static libevent 2.1.12",
                                                "system zlib/libstdc++/glibc"]
        manifest["validation"].append("memtier version 2.5.1 verified")
        manifest["caveats"].append("memtier --rate-limiting is per connection: aggregate cap = threads * clients * rate.")
        manifest["timing_fix"] = {
            "release_url": "https://github.com/redis/memtier_benchmark/releases/tag/2.4.4",
            "source_url": "https://github.com/redis/memtier_benchmark/blob/2.5.1/run_stats.cpp#L108",
            "description": "Upstream changed independent floating-point epoch second/microsecond averaging to int64 total-microsecond averaging. Earlier versions can add or lose whole seconds in aggregate throughput denominators.",
        }
    destination = args.output or root / "build-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
