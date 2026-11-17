from contextlib import contextmanager
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, unquote

import pytest

import hf_downloader as hf
from rich.console import Console


COMMIT = "a" * 40
PAYLOAD = bytes(range(256)) * 12289


@pytest.fixture(autouse=True)
def bypass_proxy_for_local_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    original = hf.Downloader.retry_delay

    def bounded_retry(self, attempt, delay=0):
        if attempt > 20:
            raise AssertionError("Unexpected endless retry")
        return original(self, attempt, delay)

    monkeypatch.setattr(hf.Downloader, "retry_delay", bounded_retry)


@contextmanager
def hub(files=None, *, faults=(), no_range=False, corrupt=False, auth_error=False, metadata_stall=False):
    files = files if files is not None else {"weights.bin": PAYLOAD, "config.json": b'{"ok":true}', "empty": b""}
    state = {"faults": list(faults), "ranges": [], "resolve": 0, "active": 0,
             "max_active": 0, "cdn_auth": [], "metadata": 0,
             "stalled": threading.Event(), "release": threading.Event()}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, status, body=b"", headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/api/"):
                state["metadata"] += 1
                if metadata_stall:
                    state["stalled"].set()
                    state["release"].wait(30)
                    self.close_connection = True
                    return
                if auth_error:
                    self.send(401)
                    return
                siblings = []
                for name, data in files.items():
                    item = {"rfilename": name, "size": len(data)}
                    if name.endswith(".bin"):
                        item["lfs"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    else:
                        item["blobId"] = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
                    siblings.append(item)
                self.send(200, json.dumps({"sha": COMMIT, "siblings": siblings}).encode())
            elif "/resolve/" in path:
                with lock:
                    state["resolve"] += 1
                name = path.split(f"/resolve/{COMMIT}/", 1)[1]
                # Different hostname is intentional: a Hub token must not leak to CDN.
                self.send(302, headers={"Location": f"http://localhost:{self.server.server_port}/cdn/{name}"})
            elif path.startswith("/cdn/"):
                data = files[path.removeprefix("/cdn/")]
                with lock:
                    state["cdn_auth"].append(self.headers.get("Authorization"))
                    fault = state["faults"][0] if state["faults"] else ""
                    requested_range = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                    if (fault in ("disconnect", "body-stall", "trickle") and requested_range
                            and int(requested_range[2]) - int(requested_range[1]) < hf.BUFFER):
                        fault = ""  # A tiny final range cannot reproduce a mid-stream interruption.
                    elif fault:
                        state["faults"].pop(0)
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                try:
                    time.sleep(0.02)  # Make overlapping requests observable and deterministic.
                    if fault in ("expired", "rate-limit", "server-error"):
                        self.send({"expired": 403, "rate-limit": 429, "server-error": 503}[fault],
                                  headers={"Retry-After": "0"})
                        return
                    requested = self.headers.get("Range")
                    with lock:
                        state["ranges"].append(requested)
                    if no_range or not requested:
                        self.send(200, data)
                        return
                    start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", requested).groups())
                    body = data[start:end + 1]
                    if fault == "body-stall":
                        self.send_response(206)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                        self.end_headers()
                        self.wfile.write(body[:hf.BUFFER])
                        self.wfile.flush()
                        state["stalled"].set()
                        state["release"].wait(30)
                        self.close_connection = True
                        return
                    if fault == "trickle":
                        self.send_response(206)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                        self.end_headers()
                        try:
                            for position in range(0, min(len(body), 64 * 1024), 1024):
                                self.wfile.write(body[position:position + 1024])
                                self.wfile.flush()
                                time.sleep(0.03)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            pass
                        self.close_connection = True
                        return
                    if corrupt or fault == "corrupt":
                        body = bytes([body[0] ^ 255]) + body[1:]
                    if fault == "wrong-range":
                        self.send(206, body, {"Content-Range": f"bytes {start + 1}-{end}/{len(data)}"})
                        return
                    if fault == "disconnect":
                        self.send_response(206)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                        self.end_headers()
                        self.wfile.write(body[:12345])
                        self.wfile.flush()
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.send(206, body, {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
                finally:
                    with lock:
                        state["active"] -= 1
            else:
                self.send(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()
        thread.join()


def downloader(tmp_path, endpoint, extra=()):
    args = hf.parser().parse_args(["test/model", "-o", str(tmp_path), "--endpoint", endpoint,
                                   "--workers", "4", "--chunk-size", "1", "--timeout", "1",
                                   "--retry-base", "0.001", "--retry-max", "0.005", "--quiet", *extra])
    return hf.Downloader(args)


def test_download_multisegment_hash_and_empty(tmp_path):
    with hub() as (endpoint, state):
        task = downloader(tmp_path, endpoint, ["--token", "private-test-token"])
        task.run()
    assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
    assert (tmp_path / "config.json").read_bytes() == b'{"ok":true}'
    assert (tmp_path / "empty").read_bytes() == b""
    assert task.progress.done == task.progress.total
    assert task.progress.finished == 3
    assert len(state["ranges"]) >= 4
    assert state["max_active"] > 1
    assert all(token is None for token in state["cdn_auth"])


@pytest.mark.parametrize("fault", ["expired", "rate-limit", "server-error", "disconnect", "wrong-range", "corrupt"])
def test_recovers_from_network_and_integrity_faults(tmp_path, fault):
    with hub({"weights.bin": PAYLOAD}, faults=[fault]) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.run()
    assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
    assert state["resolve"] > 4
    assert task.progress.done == len(PAYLOAD)
    if fault == "disconnect":
        assert any(r and int(r.split("=")[1].split("-")[0]) % (1024 * 1024) == 12345 for r in state["ranges"])


def test_resume_partial_after_restart_and_skip_completed(tmp_path):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.state.mkdir(parents=True)
        file = task.metadata()[0]
        parts = task.part_dir(file)
        (parts / "00000000.part").write_bytes(PAYLOAD[:54321])
        task = downloader(tmp_path, endpoint)
        task.run()
        assert "bytes=54321-1048575" in state["ranges"]
        count = state["resolve"]
        task = downloader(tmp_path, endpoint)
        task.run()
        assert state["resolve"] == count
        assert state["metadata"] == 1
        assert task.progress.done == len(PAYLOAD)


def test_server_without_range_falls_back(tmp_path):
    with hub({"weights.bin": PAYLOAD}, no_range=True) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.run()
    assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
    assert None in state["ranges"]
    assert task.progress.done == len(PAYLOAD)


def test_permanent_corruption_fails_without_publishing(tmp_path):
    with hub({"weights.bin": b"abcdefg"}, corrupt=True) as (endpoint, state):
        with pytest.raises(hf.DownloadError, match="三次"):
            downloader(tmp_path, endpoint).run()
    assert not (tmp_path / "weights.bin").exists()


def test_permissions_fail_fast(tmp_path):
    with hub(auth_error=True) as (endpoint, state):
        with pytest.raises(hf.DownloadError, match="HF_TOKEN"):
            downloader(tmp_path, endpoint).run()
    assert state["metadata"] == 1


def test_filter_and_refresh(tmp_path):
    with hub() as (endpoint, state):
        downloader(tmp_path, endpoint, ["--include", "*.json"]).run()
        assert not (tmp_path / "weights.bin").exists()
        downloader(tmp_path, endpoint, ["--include", "*.json", "--refresh"]).run()
        assert state["metadata"] == 2


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "a\\b", ".hf-download/lock", "NUL", "x:ads"])
def test_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(hf.DownloadError):
        hf.safe_target(tmp_path, name)


def test_lock_prevents_concurrent_writers(tmp_path):
    with hf.directory_lock(tmp_path):
        with pytest.raises(hf.DownloadError, match="已有下载器"):
            with hf.directory_lock(tmp_path):
                pytest.fail("lock unexpectedly acquired")
    with hf.directory_lock(tmp_path):
        pass


def test_cancel_retry_preserves_partial_and_never_hibernates(tmp_path, monkeypatch):
    with hub({"weights.bin": PAYLOAD}, faults=["disconnect"]) as (endpoint, state):
        task = downloader(tmp_path, endpoint, ["--workers", "1"])

        def cancel(*args, **kwargs):
            task.stop.set()
            raise hf.Cancelled()

        monkeypatch.setattr(task, "retry_delay", cancel)
        with pytest.raises(hf.Cancelled):
            task.run()
        assert not (tmp_path / "weights.bin").exists()
        assert any(p.stat().st_size == 12345 for p in task.state.rglob("*.incomplete"))
        downloader(tmp_path, endpoint).run()
        assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD


def test_main_hibernates_only_after_success(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hf, "hibernate", lambda *a: calls.append("hibernate"))
    monkeypatch.setattr(hf.Downloader, "run", lambda self: calls.append("download"))
    assert hf.main(["test/model", "-o", str(tmp_path), "--hibernate"]) == 0
    assert calls == ["download", "hibernate"]
    calls.clear()

    def fail(self):
        raise hf.DownloadError("disk full")

    monkeypatch.setattr(hf.Downloader, "run", fail)
    assert hf.main(["test/model", "-o", str(tmp_path), "--hibernate"]) == 1
    assert not calls


def test_cancel_hibernate_countdown(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(hf.subprocess, "run", lambda *a, **k: pytest.fail("must not hibernate"))
    with pytest.raises(hf.Cancelled):
        hf.hibernate(stop, 1)


def test_initial_network_outage_retries_metadata(tmp_path, monkeypatch):
    with hub({"config.json": b"{}"}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        real_open = task.open
        attempts = []

        def flaky_open(url, headers=None):
            if len(attempts) < 2:
                attempts.append(url)
                raise hf.Retryable("network offline")
            return real_open(url, headers)

        monkeypatch.setattr(task, "open", flaky_open)
        task.run()
        assert len(attempts) == 2
        assert (tmp_path / "config.json").read_bytes() == b"{}"


def test_refresh_repairs_invalid_manifest(tmp_path):
    with hub({"config.json": b"{}"}) as (endpoint, state):
        task = downloader(tmp_path, endpoint, ["--refresh"])
        task.state.mkdir(parents=True)
        (task.state / "manifest.json").write_text("invalid", encoding="utf-8")
        task.run()
        assert (tmp_path / "config.json").read_bytes() == b"{}"


def test_cooperative_cancel_exits_promptly():
    stop = threading.Event()
    counts = [0]
    entered = threading.Event()

    def action():
        entered.set()
        stop.wait(10)
        raise hf.Cancelled()

    def interrupt():
        assert entered.wait(2)
        counts[0] = 1

    sender = threading.Thread(target=interrupt)
    sender.start()
    started = time.monotonic()
    try:
        with pytest.raises(hf.Cancelled):
            hf.run_interruptibly(action, stop, lambda: counts[0])
        assert time.monotonic() - started < 2
    finally:
        stop.set()
        sender.join()


def test_hibernate_countdown_accepts_signal(monkeypatch):
    monkeypatch.setattr(hf.subprocess, "run", lambda *a, **k: pytest.fail("must not hibernate"))
    with pytest.raises(hf.Cancelled):
        hf.hibernate(threading.Event(), 60, lambda: 1)


@pytest.mark.parametrize("stage,twice", [("metadata", False), ("body", False), ("body", True)])
def test_cli_ctrl_c_during_blocked_network(tmp_path, stage, twice):
    # Deliver a real Python SIGINT in a separate process, without sending a
    # Windows console broadcast to pytest/the user's other processes.
    script = '''
import _thread
from pathlib import Path
import signal
import sys
import threading
import hf_downloader as hf

def interrupt_reader():
    for line in sys.stdin:
        if line.strip() == "interrupt":
            _thread.interrupt_main(signal.SIGINT)

threading.Thread(target=interrupt_reader, daemon=True).start()
hf.hibernate = lambda *args: Path(sys.argv[1]).write_text("unexpected hibernate")
raise SystemExit(hf.main(sys.argv[2:]))
'''
    marker = tmp_path / "unexpected-hibernate"
    with hub({"weights.bin": PAYLOAD}, faults=["body-stall"] if stage == "body" else [],
             metadata_stall=stage == "metadata") as (endpoint, state):
        command = [sys.executable, "-u", "-c", script, str(marker), "test/model", "-o", str(tmp_path),
                   "--endpoint", endpoint, "--workers", "1", "--chunk-size", "1",
                   "--timeout", "60", "--hibernate"]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={**os.environ, "HF_TOKEN": "", "PYTHONUTF8": "1"})
        try:
            assert state["stalled"].wait(10), "child did not reach the blocking request"
            if stage == "body":
                deadline = time.monotonic() + 3
                while not any(p.stat().st_size >= hf.BUFFER for p in (tmp_path / ".hf-download").rglob("*.incomplete")):
                    assert time.monotonic() < deadline, "partial chunk was not saved"
                    time.sleep(0.05)
            started = time.monotonic()
            if twice:
                proc.stdin.write(b"interrupt\n")
                proc.stdin.flush()
                time.sleep(0.4)
            stdout, stderr = proc.communicate(b"interrupt\n", timeout=9)
            elapsed = time.monotonic() - started
            assert proc.returncode == 130, (stdout, stderr)
            assert elapsed < (3 if twice else 8)
            assert "正在停止" in stderr.decode("utf-8")
            assert "强制停止" in stderr.decode("utf-8")
            assert not marker.exists()
            if stage == "body":
                assert any(p.stat().st_size == hf.BUFFER for p in (tmp_path / ".hf-download").rglob("*.incomplete"))
                journal = next((tmp_path / ".hf-download").rglob("ranges.sqlite3"))
                with sqlite3.connect(journal) as db:
                    saved = db.execute("SELECT done FROM ranges WHERE start=0").fetchone()[0]
                assert 0 < saved <= hf.BUFFER
                # OS locks must be released and saved data must remain resumable.
                downloader(tmp_path, endpoint, ["--workers", "1"]).run()
                assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
                assert f"bytes={saved}-1048575" in state["ranges"]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


@pytest.mark.parametrize("fault,extra", [
    ("body-stall", ["--stall-timeout", "0.2"]),
    ("trickle", ["--slow-window", "0.1", "--min-speed", "128"]),
])
def test_slow_and_stalled_connections_are_replaced(tmp_path, fault, extra):
    with hub({"weights.bin": PAYLOAD}, faults=[fault]) as (endpoint, state):
        task = downloader(tmp_path, endpoint, ["--timeout", "10", "--workers", "1", *extra])
        started = time.monotonic()
        task.run()
        assert time.monotonic() - started < 6
        assert state["resolve"] > 4
        assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
        assert task.progress.done == task.progress.total


def test_tail_is_split_among_idle_workers(tmp_path):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint, ["--chunk-size", "4"])
        task.run()
    assert state["max_active"] > 1
    assert len(state["ranges"]) > 1
    assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
    assert not list(task.state.rglob("*.part"))
    assert not list(task.state.rglob("assembled.tmp"))
    assert not list(task.state.rglob("*.incomplete"))


def test_full_length_temporary_file_is_not_treated_as_complete(tmp_path):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.state.mkdir(parents=True)
        file = task.metadata()[0]
        directory = task.part_dir(file)
        partial = hf.PartialFile(directory, file.size, file.digest, 1024 * 1024)
        with partial.path.open("r+b") as stream:
            stream.truncate(file.size)
        partial.close()
        task.run()
    assert len(state["ranges"]) == 4
    assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD


def test_legacy_migration_survives_interruption_before_source_delete(tmp_path, monkeypatch):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.state.mkdir(parents=True)
        file = task.metadata()[0]
        directory = task.part_dir(file)
        legacy = directory / "00000000.part"
        legacy.write_bytes(PAYLOAD[:54321])
        original = hf.PartialFile.checkpoint

        def checkpoint_then_stop(*args):
            original(*args)
            raise hf.Cancelled()

        with monkeypatch.context() as patch:
            patch.setattr(hf.PartialFile, "checkpoint", checkpoint_then_stop)
            with pytest.raises(hf.Cancelled):
                task.run()
        assert legacy.exists()
        downloader(tmp_path, endpoint).run()
        assert not legacy.exists()
        assert "bytes=54321-1048575" in state["ranges"]
        assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD


def test_verification_has_separate_byte_progress(tmp_path, monkeypatch):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        phases, amounts = {}, {}
        original_begin = task.progress.begin_phase
        original_advance = task.progress.advance_phase

        def begin(label, total):
            phase = original_begin(label, total)
            phases[phase] = (label, total)
            amounts[phase] = 0
            return phase

        def advance(phase, amount):
            amounts[phase] += amount
            original_advance(phase, amount)

        monkeypatch.setattr(task.progress, "begin_phase", begin)
        monkeypatch.setattr(task.progress, "advance_phase", advance)
        task.run()
        verified = [key for key, (label, total) in phases.items() if label == "校验 weights.bin"]
        assert len(verified) == 1
        assert amounts[verified[0]] == len(PAYLOAD)


def test_progress_does_not_round_incomplete_download_up_to_100():
    output = io.StringIO()
    progress = hf.Progress(console=Console(file=output, width=160, force_terminal=False))
    progress.set_totals(100000, 1)
    progress.add(99999)
    progress.console.print(progress.display.get_renderable())
    text = output.getvalue()
    assert "99.99%" in text
    assert "100.00%" not in text
    assert "剩余 1.0 B" in text


def test_corrupt_resumed_range_is_redownloaded_without_losing_good_ranges(tmp_path):
    with hub({"weights.bin": PAYLOAD}) as (endpoint, state):
        task = downloader(tmp_path, endpoint)
        task.state.mkdir(parents=True)
        file = task.metadata()[0]
        partial = hf.PartialFile(task.part_dir(file), file.size, file.digest, 1024 * 1024)
        with partial.path.open("r+b", buffering=0) as stream:
            for segment in partial.segments[:2]:
                block = PAYLOAD[segment.start:segment.end]
                stream.seek(segment.start)
                hf.write_all(stream, block)
                partial.checkpoint(segment, stream, len(block), hashlib.sha256(block))
            stream.seek(0)
            hf.write_all(stream, b"corrupt")
        partial.close()
        task.run()
        assert "bytes=0-1048575" in state["ranges"]
        assert "bytes=1048576-2097151" not in state["ranges"]
        assert (tmp_path / "weights.bin").read_bytes() == PAYLOAD
