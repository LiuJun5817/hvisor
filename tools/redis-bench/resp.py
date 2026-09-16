"""Small RESP2 client for readiness, correctness and out-of-band setup."""

import socket
import time


def encode(*parts):
    data = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
    return b"*%d\r\n" % len(data) + b"".join(b"$%d\r\n" % len(p) + p + b"\r\n" for p in data)


class Redis:
    def __init__(self, port, timeout=5):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.file = self.socket.makefile("rb")

    def read(self):
        line = self.file.readline()
        if not line.endswith(b"\r\n"):
            raise ConnectionError("incomplete Redis response")
        kind, value = line[:1], line[1:-2]
        if kind == b"-":
            raise RuntimeError("Redis: " + value.decode(errors="replace"))
        if kind == b"+":
            return value
        if kind == b":":
            return int(value)
        if kind == b"$":
            length = int(value)
            if length == -1:
                return None
            value = self.file.read(length + 2)
            if len(value) != length + 2 or value[-2:] != b"\r\n":
                raise ConnectionError("incomplete Redis bulk response")
            return value[:-2]
        if kind == b"*":
            length = int(value)
            return None if length == -1 else [self.read() for _ in range(length)]
        raise ValueError(f"unsupported Redis response: {line!r}")

    def command(self, *parts):
        self.socket.sendall(encode(*parts))
        return self.read()

    def info(self):
        text = self.command("INFO", "all").decode()
        return {line.split(":", 1)[0]: line.split(":", 1)[1]
                for line in text.splitlines() if line and not line.startswith("#")}

    def close(self):
        self.file.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def wait_ready(port, start_ns, timeout=120, poll_ms=1, health_check=None):
    deadline = time.monotonic() + timeout
    probes = 0
    while time.monotonic() < deadline:
        if health_check is not None:
            health_check()
        probes += 1
        client = None
        try:
            client = Redis(port, timeout=0.05)
            if client.command("PING") != b"PONG":
                raise RuntimeError("unexpected PING reply")
            ready_ns = time.monotonic_ns()
        except (OSError, ConnectionError):
            if client is not None:
                client.close()
            time.sleep(poll_ms / 1000)
            continue
        try:
            client.socket.settimeout(5)
            if client.command("SET", "readiness-check", "ok") != b"OK":
                raise RuntimeError("readiness SET validation failed")
            if client.command("GET", "readiness-check") != b"ok":
                raise RuntimeError("readiness GET validation failed")
            client.command("DEL", "readiness-check")
        finally:
            client.close()
        return {"startup_ns": ready_ns - start_ns, "probes": probes,
                "poll_ms": poll_ms, "socket_timeout_ms": 50,
                "validation": "set_get", "clock": "host CLOCK_MONOTONIC"}
    raise TimeoutError("Redis did not become ready")


def preload(port, keys, value_size):
    value = b"x" * value_size
    with Redis(port, timeout=120) as client:
        client.command("FLUSHDB")
        for first in range(1, keys + 1, 256):
            last = min(first + 256, keys + 1)
            client.socket.sendall(b"".join(encode("SET", f"bench:{key}", value) for key in range(first, last)))
            for _ in range(first, last):
                if client.read() != b"OK":
                    raise RuntimeError("preload failed")
        if client.command("DBSIZE") != keys:
            raise RuntimeError("preload key count mismatch")
        if client.command("GET", "bench:1") != value or client.command("GET", f"bench:{keys}") != value:
            raise RuntimeError("preload value mismatch")
        return client.info()
