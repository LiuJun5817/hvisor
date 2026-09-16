#!/usr/bin/env python3
"""Bounded integration checks for guest_startup.c, without a Redis dependency.

The fake Redis implements only the RESP commands exercised by this runner.
Set REDIS_STARTUP_RUNNER to test an existing native build instead of compiling.
These checks validate measurement boundaries, failure handling and ownership;
they do not produce Redis performance measurements.
"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest


FAKE_REDIS = r'''#!/usr/bin/env python3
import json
import os
import socket
import sys
import time

config = json.load(open(sys.argv[1]))
args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
assert args["--save"] == ""
assert args["--appendonly"] == "no"
assert args["--daemonize"] == "no"
assert args["--pidfile"] == ""
assert os.path.isdir(args["--dir"])
assert not os.listdir(args["--dir"])
trace = config["trace"]
def record(event):
    with open(trace, "a") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "event": event}) + "\n")
record("spawn")
if config.get("mode") == "timeout":
    time.sleep(60)
time.sleep(config.get("delay", 0))
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(args["--port"])))
server.listen(1)
record("listen")
conn, _ = server.accept()
reader = conn.makefile("rb")
stored = {}
def bulk(value):
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"
while True:
    line = reader.readline()
    if not line:
        break
    assert line.startswith(b"*")
    words = []
    for _ in range(int(line[1:])):
        size = reader.readline()
        assert size.startswith(b"$")
        words.append(reader.read(int(size[1:])))
        assert reader.read(2) == b"\r\n"
    command = words[0].decode()
    record(command)
    if command == "PING":
        conn.sendall(b"+PONG\r\n")
    elif command == "INFO":
        pid = os.getpid() + (1 if config.get("mode") == "wrong_pid" else 0)
        conn.sendall(bulk(("# Server\r\nprocess_id:%d\r\n" % pid).encode()))
    elif command == "SET":
        stored[words[1]] = words[2]
        conn.sendall(b"+OK\r\n")
    elif command == "GET":
        value = b"wrong" if config.get("mode") == "wrong_get" else stored[words[1]]
        conn.sendall(bulk(value))
conn.close()
server.close()
time.sleep(60)
'''


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory(prefix="redis-startup-test-build-")
        cls.runner = os.environ.get("REDIS_STARTUP_RUNNER")
        if not cls.runner:
            cls.runner = str(Path(cls.build_dir.name) / "guest-startup")
            subprocess.run(
                [os.environ.get("CC", "gcc"), "-std=c11", "-O2", "-Wall", "-Wextra",
                 "-Werror", str(Path(__file__).with_name("guest_startup.c")),
                 "-o", cls.runner], check=True,
            )

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="redis-startup-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fake = self.root / "fake-redis"
        self.fake.write_text(FAKE_REDIS)
        self.fake.chmod(0o755)
        self.trace = self.root / "trace.jsonl"
        self.config = self.root / "config.json"
        self.port = unused_port()

    def command(self, mode="ok", delay=0, repeats=1, timeout=1000):
        self.config.write_text(json.dumps({
            "trace": str(self.trace), "mode": mode, "delay": delay,
        }))
        return [self.runner, "--redis", str(self.fake), "--config", str(self.config),
                "--port", str(self.port), "--repeats", str(repeats),
                "--timeout-ms", str(timeout), "--poll-us", "1000"]

    def run_case(self, **options):
        result = subprocess.run(self.command(**options), capture_output=True,
                                text=True, timeout=6)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(records, result.stderr)
        for record in records:
            if record["pid"] > 0:
                with self.assertRaises(ProcessLookupError):
                    os.kill(record["pid"], 0)
        return result, records

    def events(self):
        return [json.loads(line)["event"] for line in self.trace.read_text().splitlines()]

    def test_repeated_success_and_boundaries(self):
        result, records = self.run_case(repeats=3, delay=0.03)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(records), 3)
        for index, record in enumerate(records, 1):
            self.assertEqual(record["status"], "ok")
            self.assertEqual(record["repeat"], index)
            self.assertEqual(record["validation"], "set_get")
            self.assertGreaterEqual(record["exec_to_ready_ns"], 30_000_000)
            self.assertGreater(record["spawn_to_exec_ns"], 0)
            self.assertEqual(record["startup_ns"],
                             record["exec_to_ready_ns"] + record["spawn_to_exec_ns"])
        self.assertEqual(self.events(), ["spawn", "listen", "PING", "INFO", "SET", "GET"] * 3)

    def test_timeout_reaps_child(self):
        result, records = self.run_case(mode="timeout", timeout=80)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("timed out", records[0]["error"])
        self.assertNotIn("startup_ns", records[0])

    def test_identity_check_precedes_mutation(self):
        result, records = self.run_case(mode="wrong_pid")
        self.assertEqual(result.returncode, 1)
        self.assertIn("process_id", records[0]["error"])
        self.assertNotIn("SET", self.events())

    def test_wrong_value_is_not_successful_sample(self):
        result, records = self.run_case(mode="wrong_get")
        self.assertEqual(result.returncode, 1)
        self.assertIn("wrong value", records[0]["error"])
        self.assertNotIn("startup_ns", records[0])

    def test_exec_error(self):
        command = self.command()
        self.fake.write_text("invalid executable without shebang\n")
        result = subprocess.run(command, capture_output=True, text=True, timeout=6)
        record = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exec failed", record["error"])
        with self.assertRaises(ProcessLookupError):
            os.kill(record["pid"], 0)

    def test_existing_listener_is_not_touched(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.port = listener.getsockname()[1]
            result, records = self.run_case()
            self.assertEqual(result.returncode, 1)
            self.assertEqual(records[0]["pid"], -1)
            self.assertIn("unavailable", records[0]["error"])
            listener.settimeout(0.05)
            with self.assertRaises(TimeoutError):
                listener.accept()

    def test_interruption_reaps_owned_child(self):
        process = subprocess.Popen(self.command(mode="timeout"), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not self.trace.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(self.trace.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
            record = json.loads(stdout)
            self.assertIn("interrupted", record["error"])
            with self.assertRaises(ProcessLookupError):
                os.kill(record["pid"], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
