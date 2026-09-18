#!/usr/bin/env python3
"""QEMU serial control shared by the Redis primary--replica runners."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


REPO = Path(__file__).resolve().parents[2]
PLATFORM = REPO / "platform/aarch64/qemu-gicv3-redis"


@dataclass
class CommandResult:
    output: str
    returncode: int
    sent_ns: int
    completed_ns: int
    start_offset: int
    end_offset: int


class Console:
    def __init__(self, command, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "qemu-command.json").write_text(
            json.dumps(command, indent=2) + "\n"
        )
        temporary = self.directory / "qemu-tmp"
        temporary.mkdir()
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary.resolve())
        self.log = (self.directory / "root-console.log").open("wb")
        self.data = bytearray()
        self.boundaries = []
        self.condition = threading.Condition()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
            env=environment,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            chunk = self.process.stdout.read(4096)
            if not chunk:
                break
            observed_ns = time.monotonic_ns()
            with self.condition:
                self.log.write(chunk)
                self.log.flush()
                self.data.extend(chunk)
                self.boundaries.append((len(self.data), observed_ns))
                self.condition.notify_all()
        with self.condition:
            self.condition.notify_all()

    def send(self, command):
        sent_ns = time.monotonic_ns()
        self.process.stdin.write(command.encode() + b"\r")
        self.process.stdin.flush()
        return sent_ns

    def _observed_ns(self, end_offset):
        for boundary, observed_ns in self.boundaries:
            if boundary >= end_offset:
                return observed_ns
        raise RuntimeError("console timestamp boundary is missing")

    def expect(self, pattern, timeout=120, offset=0, failure_patterns=()):
        regex = re.compile(pattern, re.MULTILINE)
        failure_regexes = [re.compile(item, re.MULTILINE) for item in failure_patterns]
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                match = regex.search(self.data, offset)
                if match:
                    return match, self._observed_ns(match.end())
                for failure_regex in failure_regexes:
                    failure = failure_regex.search(self.data, offset)
                    if failure:
                        observed = bytes(failure[0]).decode(errors="replace")
                        raise RuntimeError(
                            f"console failure while waiting for {pattern!r}: {observed}; "
                            f"see {self.directory}"
                        )
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited ({self.process.returncode}); see {self.directory}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"console pattern {pattern!r}; see {self.directory}"
                    )
                self.condition.wait(min(remaining, 0.25))

    def command(self, command, timeout=120):
        marker = "REDIS_BENCH_DONE_" + uuid.uuid4().hex
        with self.condition:
            offset = len(self.data)
        sent_ns = self.send(command + "; printf '\\n" + marker + ":%s\\n' \"$?\"")
        match, completed_ns = self.expect(
            rb"^" + marker.encode() + rb":(\d+)\r?$", timeout, offset
        )
        output = bytes(self.data[offset:match.start()]).decode(errors="replace")
        return CommandResult(
            output=output,
            returncode=int(match[1]),
            sent_ns=sent_ns,
            completed_ns=completed_ns,
            start_offset=offset,
            end_offset=match.end(),
        )

    def event(self, pattern, offset=0):
        regex = re.compile(pattern, re.MULTILINE)
        with self.condition:
            match = regex.search(self.data, offset)
            if not match:
                raise RuntimeError(f"missing console event {pattern!r}")
            return match, self._observed_ns(match.end())

    def start_boot(self, timeout=120):
        self.expect(rb"Hit any key to stop autoboot:", timeout)
        self.send("")
        self.expect(rb"=> ", 10)
        return self.send("bootm 0x40400000 - $fdtcontroladdr")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def qemu_command(hvisor, root_disk, root_dtb, firmware, kernel,
                 qemu="qemu-system-aarch64", qemu_cpus=None):
    command = []
    if qemu_cpus:
        command.extend(["taskset", "-c", qemu_cpus])
    command.extend([
        qemu,
        "-machine", "virt-9.0,secure=on,gic-version=3,virtualization=on,its=off",
        "-accel", "tcg,thread=multi",
        "-cpu", "cortex-a72",
        "-smp", "4",
        "-m", "2G",
        "-nic", "none",
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-bios", str(Path(firmware).resolve()),
        "-device", f"loader,file={Path(hvisor).resolve()},addr=0x40400000,force-raw=on",
        "-device", f"loader,file={Path(kernel).resolve()},addr=0xa0400000,force-raw=on",
        "-device", f"loader,file={Path(root_dtb).resolve()},addr=0xa0000000,force-raw=on",
        "-drive", f"if=none,file={Path(root_disk).resolve()},id=rootfs,format=raw,snapshot=on",
        "-device", "virtio-blk-device,drive=rootfs,bus=virtio-mmio-bus.31",
    ])
    return command
