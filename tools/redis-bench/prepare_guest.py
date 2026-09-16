#!/usr/bin/env python3
"""Create a Redis guest disk copy without mounting or changing the source image."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = ROOT / "target/redis-bench"
URL = "https://github.com/CHonghaohao/hvisor_env_img/releases/download/v2025.04.11"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=BASE / "assets/rootfs1.ext4")
    parser.add_argument("--output", type=Path, default=BASE / "assets/rootfs-redis.ext4")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}; choose a fresh --output")
    binaries = BASE / "redis-build/bin/aarch64"
    files = {"redis-server": binaries / "redis-server", "redis-cli": binaries / "redis-cli",
             "guest_startup": binaries / "guest-startup", "guest_net": binaries / "guest_net",
             "redis.conf": HERE / "redis.conf", "guest_init.sh": HERE / "guest_init.sh"}
    for file in [args.source, *files.values()]:
        if not file.is_file():
            parser.error(f"missing input: {file}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", str(args.source), str(args.output)], check=True)
    # debugfs has its own quoting rules. All paths come from this known local tree.
    def quote(path):
        value = str(Path(path).resolve())
        if any(c in value for c in '\n\r"\\'):
            raise ValueError("unsupported character in debugfs path")
        return '"' + value + '"'
    commands = ["mkdir /redis-bench"]
    for name, file in files.items():
        commands += [f"write {quote(file)} /redis-bench/{name}",
                     f"set_inode_field /redis-bench/{name} mode " + ("0100644" if name == "redis.conf" else "0100755")]
    commands += ["ls -l /redis-bench"]
    command_file = args.output.with_suffix(".debugfs.txt")
    command_file.write_text("\n".join(commands) + "\n")
    result = subprocess.run(["debugfs", "-w", "-f", str(command_file), str(args.output)],
                            capture_output=True, text=True, check=True)
    output = result.stdout + result.stderr
    args.output.with_suffix(".debugfs.log").write_text(output)
    if re.search(r"not found|not a directory|no space|file exists|usage:|error|invalid", output, re.I):
        raise RuntimeError("debugfs reported an error; see preparation log")
    dts = (ROOT / "platform/aarch64/qemu-gicv3/image/dts/zone0.dts").read_text()
    dts, count = re.subn(r'(bootargs\s*=\s*"[^"]*)";', r'\1 init=/redis-bench/guest_init.sh";', dts)
    if count != 1:
        raise ValueError("expected one root zone bootargs property")
    dts_path = args.output.parent / "zone0-redis.dts"
    dts_path.write_text(dts)
    dtb_path = args.output.parent / "zone0-redis.dtb"
    with args.output.with_suffix(".dtc.log").open("w") as log:
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", str(dtb_path), str(dts_path)],
                       stdout=log, stderr=log, check=True)
    manifest = {"guest": "root Linux zone0", "source_release": URL,
                "source_image": str(args.source.resolve()), "source_sha256": sha256(args.source),
                "disk": str(args.output.resolve()), "disk_sha256": sha256(args.output),
                "dtb": str(dtb_path), "dtb_sha256": sha256(dtb_path),
                "files": {name: {"source": str(path), "sha256": sha256(path)} for name, path in files.items()},
                "network": "virtio MMIO 0xa003a00 IRQ77, 10.0.2.15/24; localhost-only QEMU host forwarding",
                "redis_server_cpu": 0, "persistence": "disabled", "transparent_hugepages": "disabled if supported"}
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
