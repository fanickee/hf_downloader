"""Resumable Hugging Face HTTP downloader with Rich console progress."""

from __future__ import annotations

import argparse
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import fnmatch
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import random
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
import uuid

import truststore

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress as RichProgress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text
from rich.table import Column

from hf_storage import PartialFile, Segment, write_all


BUFFER = 256 * 1024
CHECKPOINT_BYTES = 4 * 1024 * 1024
CHECKPOINT_SECONDS = 2
MIN_SPLIT_BYTES = 1024 * 1024


class DownloadError(Exception):
    """A permanent failure requiring user intervention."""


class Cancelled(Exception):
    pass


class ForcedCancel(Cancelled):
    """Workers did not stop; the CLI must bypass executor shutdown hooks."""


def windows_console(fd: int):
    """Return the native console handle, or None for redirected streams."""
    if sys.platform != "win32":
        return None
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    mode = wintypes.DWORD()
    return (kernel, handle) if kernel.GetConsoleMode(handle, ctypes.byref(mode)) else None


def write_unbuffered(fd: int, message: str):
    """Avoid Python stream locks without bypassing Windows Unicode handling."""
    console = windows_console(fd)
    if console is not None:
        from ctypes import wintypes

        kernel, handle = console
        kernel.WriteConsoleW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteConsoleW.restype = wintypes.BOOL
        data = message.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(data)
        offset, length = 0, len(data) // 2
        while offset < length:
            written = wintypes.DWORD()
            if not kernel.WriteConsoleW(handle, ctypes.addressof(buffer) + offset * 2,
                                        length - offset, ctypes.byref(written), None) or not written.value:
                raise ctypes.WinError(ctypes.get_last_error())
            offset += written.value
    else:
        # Pipes/log files use UTF-8, consistent with main()'s text streams.
        data = message.encode("utf-8")
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("取消提示输出失败")
            data = data[written:]


def cancellation_message(message: str):
    # Keep this outside the signal handler and independent of Rich/stream locks.
    try:
        write_unbuffered(2, "\n" + message + "\n")
    except OSError:
        pass


def restore_terminal_cursor():
    """Restore Rich's hidden cursor even when normal cleanup cannot run."""
    try:
        console = windows_console(1)
        if console is not None:
            from ctypes import wintypes

            class CursorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("visible", wintypes.BOOL)]

            kernel, handle = console
            kernel.GetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CursorInfo)]
            kernel.GetConsoleCursorInfo.restype = wintypes.BOOL
            kernel.SetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CursorInfo)]
            kernel.SetConsoleCursorInfo.restype = wintypes.BOOL
            cursor = CursorInfo()
            if kernel.GetConsoleCursorInfo(handle, ctypes.byref(cursor)):
                cursor.visible = True
                kernel.SetConsoleCursorInfo(handle, ctypes.byref(cursor))
        elif sys.stdout.isatty():
            write_unbuffered(1, "\x1b[?25h\n")
    except OSError:
        pass


def run_interruptibly(action, stop: threading.Event, interrupts, *, grace: float = 5):
    """Keep the CLI main thread responsive even during DNS/socket/pool waits."""
    finished = threading.Event()
    failures = []

    def worker():
        try:
            action()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    threading.Thread(target=worker, name="download-controller", daemon=True).start()
    deadline = None
    while True:
        count = interrupts()
        if count:
            stop.set()
            if deadline is None:
                deadline = time.monotonic() + grace
                cancellation_message("正在停止并保留下载进度；最多等待 5 秒，再按 Ctrl+C 可立即退出。")
            if count > 1:
                raise ForcedCancel()
        if finished.wait(0.1):
            if interrupts():
                stop.set()
                raise Cancelled()
            if failures:
                raise failures[0]
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise ForcedCancel()


class Retryable(Exception):
    def __init__(self, message: str, delay: float = 0):
        super().__init__(message)
        self.delay = delay


class NoRange(Exception):
    pass


def network_error_message(error: Exception) -> str:
    """Explain failures without exposing signed URLs, proxy passwords or tokens."""
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = getattr(reason, "verify_code", None)
        if code == 10:
            return "TLS 证书已过期（校验错误码 10），请检查系统时间、站点或 HTTPS 代理证书"
        if code == 9:
            return "TLS 证书尚未生效（校验错误码 9），请检查系统时间和证书有效期"
        suffix = f"（校验错误码 {code}）" if isinstance(code, int) else ""
        return f"TLS 证书验证失败{suffix}，请检查站点、代理证书或受信任证书配置"
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败，请检查网络、DNS 或代理设置"
    if isinstance(reason, TimeoutError):
        return "连接或读取超时"
    if isinstance(reason, ConnectionRefusedError):
        return "服务器或代理拒绝连接"
    if isinstance(reason, ConnectionResetError):
        return "连接被对端重置"
    if isinstance(reason, ssl.SSLError):
        return "TLS 握手或加密连接失败"
    if isinstance(reason, str) and reason.startswith("Tunnel connection failed:"):
        match = re.match(r"Tunnel connection failed:\s*(\d{3})\b", reason)
        return "代理隧道建立失败" + (f"（HTTP {match[1]}）" if match else "")
    return f"网络不可用（{type(reason).__name__}）"


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old, new = urlsplit(req.full_url), urlsplit(newurl)
            if old.scheme == "https" and new.scheme != "https":
                raise DownloadError("拒绝 HTTPS 降级重定向")
            if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                redirected.remove_header("Authorization")
        return redirected


def human_size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError


class DownloadDisplay(RichProgress):
    def get_renderables(self):
        yield from super().get_renderables()
        if self.tasks:
            # Render status separately so long filenames don't squeeze out the bar.
            yield Text(self.tasks[0].fields.get("status", ""), overflow="fold")


class ExactPercentageColumn(ProgressColumn):
    def render(self, task):
        if task.total is None:
            return Text("--%")
        value = 100 if task.completed >= task.total else math.floor(task.percentage * 100) / 100
        return Text(f"{value:.2f}%")


class RemainingColumn(ProgressColumn):
    def render(self, task):
        remaining = max(0, task.total - task.completed) if task.total is not None else None
        return Text("剩余 " + (human_size(remaining) if remaining is not None else "--"))


class FileCountColumn(ProgressColumn):
    def render(self, task):
        if "count" not in task.fields:
            return Text("")
        return Text(f"文件 {task.fields['finished']}/{task.fields['count']}")


class Progress:
    def __init__(self, quiet: bool = False, *, console: Console | None = None):
        self.lock = threading.Lock()
        self.done = self.total = self.network = self.finished = self.count = 0
        self.status = "读取模型信息"
        self.quiet = quiet
        self.console = console or Console()
        self.display = DownloadDisplay(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False, table_column=Column(max_width=30, overflow="ellipsis")),
            BarColumn(bar_width=None),
            ExactPercentageColumn(),
            DownloadColumn(binary_units=True),
            RemainingColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            FileCountColumn(),
            console=self.console,
            refresh_per_second=4,
            expand=True,
            redirect_stdout=False,
            redirect_stderr=False,
            disable=quiet or not self.console.is_terminal,
        )
        self.task_id = self.display.add_task("下载", total=None, finished=0, count=0, status=self.status)
        self.closed = threading.Event()
        self.thread = None

    def set_totals(self, total: int, count: int):
        with self.lock:
            self.total, self.count = total, count
            self.display.update(self.task_id, total=total, count=count)

    def file_finished(self):
        with self.lock:
            self.finished += 1
            self.display.update(self.task_id, finished=self.finished)

    def begin_phase(self, label: str, total: int):
        return self.display.add_task(label, total=total)

    def advance_phase(self, task_id, amount: int):
        self.display.update(task_id, advance=amount)

    def end_phase(self, task_id):
        self.display.remove_task(task_id)

    def add(self, size: int, network: bool = False):
        with self.lock:
            self.done += size
            if network:
                self.network += size
                self.display.update(self.task_id, completed=self.done)
            else:
                # Cached bytes and discarded corrupt chunks aren't network traffic.
                # Reset Rich's speed samples when the resume baseline changes.
                self.display.reset(self.task_id, total=self.total, completed=self.done,
                                   finished=self.finished, count=self.count, status=self.status)

    def note(self, message: str):
        with self.lock:
            self.status = message
            self.display.update(self.task_id, status=message)

    def log(self, message: str):
        self.console.print(Text(message))

    def start(self):
        if self.quiet:
            return
        if self.console.is_terminal:
            self.display.start()
        else:
            self.console.print(self.display.get_renderable())
            self.thread = threading.Thread(target=self._log_snapshots, daemon=True)
            self.thread.start()

    def _log_snapshots(self):
        # Rich owns rendering; redirected logs get occasional plain snapshots.
        while not self.closed.wait(10):
            self.console.print(self.display.get_renderable())

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join()
        if not self.quiet:
            if self.console.is_terminal:
                self.display.stop()
            else:
                self.console.print(self.display.get_renderable())


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    digest: str
    algorithm: str


def safe_target(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if (not parts or name.startswith("/") or "\\" in name
            or any(p in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', p)
                   or p.endswith((" ", ".")) or p.split(".")[0].upper() in reserved for p in parts)
            or parts[0].lower() == ".hf-download"):
        raise DownloadError(f"不安全或不兼容的文件路径：{name!r}")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise DownloadError(f"文件路径越界：{name!r}")
    return target


def atomic_json(path: Path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def directory_lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a+b") as stream:
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DownloadError("此目录已有下载器运行，请使用其他输出目录") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Downloader:
    def __init__(self, args, stop: threading.Event | None = None):
        self.args = args
        self.root = Path(args.output or f"models/{args.repo}").resolve()
        self.state = self.root / ".hf-download"
        self.stop = stop or threading.Event()
        self.progress = Progress(args.quiet)
        self.local = threading.local()
        self.commit = ""

    def check_stop(self):
        if self.stop.is_set():
            raise Cancelled()

    def wait_retry(self, attempt: int, reason: str, delay: float = 0):
        self.check_stop()
        seconds = self.retry_delay(attempt, delay)
        self.progress.note(f"{reason}，{seconds:.1f}s 后重试")
        if self.stop.wait(seconds):
            raise Cancelled()

    def retry_delay(self, attempt: int, delay: float = 0):
        seconds = max(delay, min(self.args.retry_max, self.args.retry_base * 2 ** min(attempt, 20)))
        seconds += random.uniform(0, min(1, seconds / 10))
        return seconds

    def open(self, url: str, headers: dict | None = None):
        self.check_stop()
        if not hasattr(self.local, "opener"):
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.local.opener = build_opener(SafeRedirect(), HTTPSHandler(context=context))
        request_headers = {"User-Agent": "hf-resilient-downloader/0.1", "Accept-Encoding": "identity",
                           "Cache-Control": "no-cache"}
        if self.args.token:
            request_headers["Authorization"] = f"Bearer {self.args.token}"
        request_headers.update(headers or {})
        try:
            return self.local.opener.open(Request(url, headers=request_headers),
                                          timeout=min(self.args.timeout, self.args.stall_timeout))
        except HTTPError as exc:
            status = exc.code
            final_origin = urlsplit(exc.url).netloc
            endpoint_origin = urlsplit(self.args.endpoint).netloc
            error_code = exc.headers.get("X-Error-Code", "")
            retry_after = exc.headers.get("Retry-After", "0")
            exc.close()
            hub_response = (final_origin == endpoint_origin
                            and urlsplit(exc.url).path == urlsplit(url).path)
            if status in (401, 403, 404) and hub_response:
                if status in (401, 403):
                    raise DownloadError(f"HTTP {status}：请检查 HF_TOKEN、仓库访问权限及模型协议授权") from None
                raise DownloadError("HTTP 404：仓库、版本或文件不存在，或无权访问私有仓库") from None
            if error_code in ("GatedRepo", "RepoNotFound", "RevisionNotFound", "EntryNotFound"):
                raise DownloadError(f"Hugging Face 拒绝请求：{error_code}") from None
            if status in (401, 403, 404, 408, 409, 416, 425, 429) or status >= 500:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        delay = 0
                raise Retryable(f"HTTP {status}，刷新下载链接", max(0, delay)) from None
            raise DownloadError(f"请求失败：HTTP {status}") from None
        except (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException) as exc:
            raise Retryable(network_error_message(exc)) from None

    def metadata(self) -> list[RemoteFile]:
        identity = {"repo": self.args.repo, "revision": self.args.revision, "endpoint": self.args.endpoint}
        manifest = self.state / "manifest.json"
        cached = None
        if manifest.exists():
            try:
                cached = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                if not self.args.refresh:
                    raise DownloadError("下载清单损坏，请使用 --refresh 重新获取") from None
            if cached and cached.get("identity") != identity and not self.args.refresh:
                raise DownloadError("此目录属于其他仓库或版本，请换目录或使用 --refresh")
        if not cached or self.args.refresh:
            url = (f"{self.args.endpoint}/api/models/{quote(self.args.repo, safe='/')}/revision/"
                   f"{quote(self.args.revision, safe='')}?blobs=true")
            attempt = 0
            while True:
                try:
                    with self.open(url) as response:
                        info = json.load(response)
                    break
                except (Retryable, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                    self.wait_retry(attempt, str(exc) if isinstance(exc, Retryable) else "元数据连接中断",
                                    getattr(exc, "delay", 0))
                    attempt += 1
                except (ValueError, UnicodeError):
                    self.wait_retry(attempt, "元数据响应无效")
                    attempt += 1
            cached = {"identity": identity, "info": info}
            # Validate before replacing a usable cached snapshot.
            self.parse_info(info)
            atomic_json(manifest, cached)
        info = cached["info"]
        files = self.parse_info(info)
        self.commit = info["sha"]
        selected = [f for f in files
                    if (not self.args.include or any(fnmatch.fnmatchcase(f.name, p) for p in self.args.include))
                    and not any(fnmatch.fnmatchcase(f.name, p) for p in self.args.exclude)]
        if not selected:
            raise DownloadError("没有匹配的文件，请检查 --include / --exclude")
        return selected

    def parse_info(self, info) -> list[RemoteFile]:
        if not isinstance(info, dict) or not re.fullmatch(r"[0-9a-f]{40,64}", str(info.get("sha", ""))):
            raise DownloadError("Hub 未返回有效的固定版本 SHA")
        result, names = [], set()
        for item in info.get("siblings", []):
            name = item.get("rfilename", "")
            safe_target(self.root, name)
            collision_key = os.path.normcase(name)
            if collision_key in names:
                raise DownloadError(f"重复或大小写冲突的路径：{name}")
            names.add(collision_key)
            lfs = item.get("lfs")
            size = lfs.get("size", item.get("size")) if lfs else item.get("size")
            digest = lfs.get("sha256", "") if lfs else item.get("blobId", "")
            algorithm = "sha256" if lfs else "sha1"
            length = 64 if lfs else 40
            if not isinstance(size, int) or size < 0 or not re.fullmatch(f"[0-9a-f]{{{length}}}", digest):
                raise DownloadError(f"{name} 缺少大小或校验信息；请检查端点是否支持 blobs=true")
            result.append(RemoteFile(name, size, digest, algorithm))
        return result

    def resolve_url(self, file: RemoteFile) -> str:
        # Always re-enter the stable resolve endpoint. A unique query bypasses cached expired redirects.
        return (f"{self.args.endpoint}/{quote(self.args.repo, safe='/')}/resolve/{self.commit}/"
                f"{quote(file.name, safe='/')}?download=true&_hf_retry={uuid.uuid4().hex}")

    def hash_file(self, path: Path, file: RemoteFile) -> bool:
        if not path.is_file() or path.stat().st_size != file.size:
            return False
        task_id = self.progress.begin_phase(f"校验 {file.name}", file.size)
        digest = hashlib.new(file.algorithm)
        if file.algorithm == "sha1":
            digest.update(f"blob {file.size}\0".encode())
        try:
            with path.open("rb") as stream:
                while block := stream.read(BUFFER):
                    self.check_stop()
                    digest.update(block)
                    self.progress.advance_phase(task_id, len(block))
            return digest.hexdigest() == file.digest
        finally:
            self.progress.end_phase(task_id)

    def file_state_dir(self, file: RemoteFile) -> Path:
        # Stable identity for the temporary file and its range journal.
        identity = json.dumps([self.args.repo, self.commit, file.name, file.digest, self.args.chunk_size])
        path = self.state / hashlib.sha256(identity.encode()).hexdigest()
        path.mkdir(exist_ok=True)
        return path

    def download_segment(self, file: RemoteFile, partial: PartialFile, segment: Segment,
                         cancelled: threading.Event):
        """One connection attempt; the scheduler owns backoff and tail splitting."""
        self.check_stop()
        if cancelled.is_set():
            raise Cancelled()
        if not segment.remaining:
            return
        if not partial.ranged and segment.done:
            self.progress.add(-segment.done)
            partial.forget(segment)
        offset = segment.done
        hasher = segment.hasher.copy()
        headers = {"Range": f"bytes={segment.start + offset}-{segment.end - 1}"} if partial.ranged else {}
        try:
            with self.open(self.resolve_url(file), headers) as response:
                if partial.ranged and response.status == 200:
                    raise NoRange()
                if partial.ranged:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (response.status != 206 or not match or
                            tuple(map(int, match.groups())) != (segment.start + offset, segment.end - 1, file.size)):
                        raise Retryable("服务器返回错误的分块范围")
                elif response.status != 200:
                    raise Retryable("服务器未返回完整文件")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise DownloadError("服务器返回压缩数据，无法可靠按字节续传")
                self.progress.note(f"下载 {file.name}")
                with partial.path.open("r+b", buffering=0) as stream:
                    stream.seek(segment.start + offset)
                    checkpoint_time = window_start = time.monotonic()
                    window_bytes = 0
                    try:
                        while offset < segment.length:
                            self.check_stop()
                            if cancelled.is_set():
                                raise Cancelled()
                            # read1 returns available bytes rather than waiting
                            # for a full 256 KiB buffer from a trickling connection.
                            block = response.read1(min(BUFFER, segment.length - offset))
                            if not block:
                                raise Retryable("连接提前断开，保留已下载数据")
                            write_all(stream, block)
                            hasher.update(block)
                            offset += len(block)
                            window_bytes += len(block)
                            self.progress.add(len(block), network=True)
                            now = time.monotonic()
                            if (segment.done == 0 or offset - segment.done >= CHECKPOINT_BYTES
                                    or now - checkpoint_time >= CHECKPOINT_SECONDS):
                                partial.checkpoint(segment, stream, offset, hasher)
                                checkpoint_time = now
                            elapsed = now - window_start
                            if elapsed >= self.args.slow_window:
                                if (offset < segment.length and self.args.min_speed
                                        and window_bytes / elapsed < self.args.min_speed * 1024):
                                    raise Retryable("连接持续低速，保存进度并刷新链接")
                                window_start, window_bytes = now, 0
                    finally:
                        if offset != segment.done:
                            partial.checkpoint(segment, stream, offset, hasher)
        except (TimeoutError, ConnectionError, http.client.HTTPException, URLError) as exc:
            raise Retryable(f"连接无进展或中断 ({type(exc).__name__})") from None

    def download_ranges(self, file: RemoteFile, partial: PartialFile, pool: ThreadPoolExecutor):
        # Entries are (segment, ready_at, attempts). Limit queued requests so idle
        # tails can be split before being handed to a worker.
        pending = [(s, 0.0, 0) for s in partial.segments if s.remaining]
        active = {}
        cancelled = threading.Event()
        try:
            while pending or active:
                self.check_stop()
                while partial.ranged and len(pending) + len(active) < self.args.workers:
                    candidates = [entry for entry in pending if entry[0].remaining >= 2 * MIN_SPLIT_BYTES]
                    if not candidates:
                        break
                    segment, ready_at, attempts = max(candidates, key=lambda entry: entry[0].remaining)
                    pending.append((partial.split(segment), ready_at, attempts))
                pending.sort(key=lambda entry: entry[1])
                while pending and len(active) < self.args.workers and pending[0][1] <= time.monotonic():
                    segment, _, attempts = pending.pop(0)
                    before = segment.done
                    future = pool.submit(self.download_segment, file, partial, segment, cancelled)
                    active[future] = (segment, attempts, before)
                if not active:
                    if self.stop.wait(min(0.2, max(0, pending[0][1] - time.monotonic()))):
                        raise Cancelled()
                    continue
                completed, _ = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in completed:
                    segment, attempts, before = active.pop(future)
                    try:
                        future.result()
                    except Retryable as exc:
                        self.check_stop()
                        if segment.done > before:
                            attempts = 0
                        seconds = self.retry_delay(attempts, exc.delay)
                        self.progress.note(f"{exc}，{seconds:.1f}s 后重试")
                        pending.append((segment, time.monotonic() + seconds, attempts + 1))
        finally:
            cancelled.set()
            for future in active:
                future.cancel()
            # Never reuse an interval, publish a file, or close its journal while
            # another thread still owns a write handle.
            cleanup_failure = None
            for future in active:
                try:
                    future.result()
                except (Cancelled, FutureCancelledError, NoRange, Retryable):
                    pass
                except BaseException as exc:
                    cleanup_failure = exc
            if cleanup_failure is not None:
                raise cleanup_failure

    def download_file(self, file: RemoteFile, pool: ThreadPoolExecutor):
        self.check_stop()
        target = safe_target(self.root, file.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.progress.note(f"检查 {file.name}")
        if self.hash_file(target, file):
            self.progress.add(file.size)
            self.progress.file_finished()
            return
        directory = self.file_state_dir(file)
        partial = PartialFile(directory, file.size, file.digest, self.args.chunk_size * 1024 * 1024)
        try:
            phase = self.progress.begin_phase(f"恢复校验 {file.name}", sum(s.done for s in partial.segments))
            try:
                partial.restore(self.check_stop, lambda n: self.progress.advance_phase(phase, n))
            finally:
                self.progress.end_phase(phase)
            # A pre-existing incomplete file may have a stale longer tail. Only
            # journaled prefixes count towards progress, never its logical size.
            with partial.path.open("r+b") as stream:
                if stream.seek(0, os.SEEK_END) > file.size:
                    stream.truncate(file.size)
            self.progress.add(sum(s.done for s in partial.segments))
            for integrity_attempt in range(3):
                self.check_stop()
                try:
                    self.download_ranges(file, partial, pool)
                except NoRange:
                    self.progress.note(f"{file.name} 不支持 Range，改为完整文件下载")
                    self.progress.add(-sum(s.done for s in partial.segments))
                    partial.reset(full=True)
                    self.download_ranges(file, partial, pool)
                self.check_stop()
                self.progress.note(f"完整校验 {file.name}")
                if self.hash_file(partial.path, file):
                    partial.path.replace(target)
                    self.progress.file_finished()
                    return
                self.progress.add(-sum(s.done for s in partial.segments))
                partial.reset()
                self.progress.log(f"校验失败，重新下载：{file.name}（{integrity_attempt + 1}/3）")
            raise DownloadError(f"{file.name} 连续三次校验失败，已停止；不会执行睡眠或休眠")
        finally:
            partial.close()

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with directory_lock(self.state):
            self.progress.start()
            try:
                files = self.metadata()
                self.progress.set_totals(sum(f.size for f in files), len(files))
                self.progress.log(f"仓库：{self.args.repo} | 版本：{self.commit}\n"
                                  f"文件：{len(files)} | 总大小：{human_size(self.progress.total)} | 输出：{self.root}")
                with ThreadPoolExecutor(max_workers=self.args.workers) as segments:
                    with ThreadPoolExecutor(max_workers=min(4, self.args.workers)) as file_pool:
                        futures = [file_pool.submit(self.download_file, file, segments) for file in files]
                        try:
                            for future in as_completed(futures):
                                future.result()
                        except BaseException:
                            self.stop.set()
                            for future in futures:
                                future.cancel()
                            raise
                self.check_stop()
                self.progress.note("下载完成，所有文件已通过校验")
                self.progress.log(f"下载完成：{len(files)} 个文件均已通过内容校验。")
            finally:
                self.progress.close()


@contextmanager
def keep_awake():
    """Prevent Windows idle sleep while downloading; restore the previous state."""
    previous = 0
    if sys.platform == "win32":
        previous = ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        yield
    finally:
        if previous:
            ctypes.windll.kernel32.SetThreadExecutionState(previous)


def power_countdown(stop: threading.Event, seconds: int, action: str, interrupts=lambda: 0):
    print(f"全部下载完成，{seconds} 秒后系统{action}；按 Ctrl+C 可取消。", flush=True)
    deadline = time.monotonic() + seconds
    while True:
        if stop.is_set() or interrupts():
            stop.set()
            raise Cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Finite waits let Python dispatch console signals on Windows.
        time.sleep(min(0.1, remaining))


def windows_sleep():
    """Request standby with the process's existing shutdown privilege, then restore it."""
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        # Exactly one LUID_AND_ATTRIBUTES entry is needed.
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", LUID),
                    ("Attributes", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    power = ctypes.WinDLL("powrprof", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                               ctypes.POINTER(LUID)]
    security.LookupPrivilegeValueW.restype = wintypes.BOOL
    security.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                               ctypes.POINTER(TOKEN_PRIVILEGES), wintypes.DWORD,
                                               ctypes.POINTER(TOKEN_PRIVILEGES),
                                               ctypes.POINTER(wintypes.DWORD)]
    security.AdjustTokenPrivileges.restype = wintypes.BOOL
    power.SetSuspendState.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
    power.SetSuspendState.restype = ctypes.c_ubyte
    token = wintypes.HANDLE()
    # TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY; this does not elevate the process.
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    previous = TOKEN_PRIVILEGES()
    try:
        requested = TOKEN_PRIVILEGES()
        requested.PrivilegeCount = 1
        requested.Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        if not security.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(requested.Luid)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        ctypes.set_last_error(0)
        if not security.AdjustTokenPrivileges(token, False, ctypes.byref(requested),
                                               ctypes.sizeof(previous), ctypes.byref(previous),
                                               ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)  # Includes ERROR_NOT_ALL_ASSIGNED (1300).
        # FALSE means sleep, not hibernation; keep wake events enabled.
        if not power.SetSuspendState(False, False, False):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        try:
            if previous.PrivilegeCount:
                if not security.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(token)


def sleep_system(stop: threading.Event, seconds: int, interrupts=lambda: 0):
    power_countdown(stop, seconds, "睡眠", interrupts)
    if sys.platform == "win32":
        windows_sleep()
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "suspend"], check=True)
    else:
        raise DownloadError("此系统暂不支持自动睡眠")


def hibernate(stop: threading.Event, seconds: int, interrupts=lambda: 0):
    power_countdown(stop, seconds, "休眠", interrupts)
    if sys.platform == "win32":
        subprocess.run(["shutdown.exe", "/h"], check=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "hibernate"], check=True)
    else:
        raise DownloadError("此系统暂不支持自动休眠")


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须为有限正数")
    return number


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须为有限非负数")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Hugging Face 模型下载器：多线程、断点续传、断网持续重试")
    result.add_argument("repo", help="模型仓库，例如 Qwen/Qwen2.5-0.5B-Instruct")
    result.add_argument("-o", "--output", help="输出目录，默认 models/<仓库名>")
    result.add_argument("-w", "--workers", type=positive_int, default=8, help="全局下载线程数，默认 8")
    result.add_argument("--chunk-size", type=positive_int, default=64, help="分块大小 MiB，默认 64；续传时保持一致")
    result.add_argument("--revision", default="main", help="分支、标签或 commit SHA，默认 main")
    result.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"), help="Hub 地址")
    result.add_argument("--token", default=os.environ.get("HF_TOKEN"), help="访问令牌，推荐通过 HF_TOKEN 环境变量设置")
    result.add_argument("--include", nargs="+", default=[], help="只下载匹配的路径，例如 '*.safetensors' '*.json'")
    result.add_argument("--exclude", nargs="+", default=[], help="排除匹配的路径，例如 '*.bin'")
    result.add_argument("--timeout", type=positive_float, default=30, help="每次网络阻塞超时秒数，默认 30")
    result.add_argument("--stall-timeout", type=positive_float, default=30, help="无数据到达的超时秒数，与 timeout 取较小值，默认 30")
    result.add_argument("--slow-window", type=positive_float, default=30, help="持续低速的检测窗口秒数，默认 30")
    result.add_argument("--min-speed", type=nonnegative_float, default=16, help="每连接最低平均速度 KiB/s，低于此值重试；0 禁用，默认 16")
    result.add_argument("--retry-base", type=positive_float, default=2, help="初始重试间隔秒数，默认 2")
    result.add_argument("--retry-max", type=positive_float, default=60, help="重试退避上限秒数，默认 60")
    result.add_argument("--refresh", action="store_true", help="刷新固定版本；不加此参数会继续下载清单记录的版本")
    power = result.add_mutually_exclusive_group()
    power.add_argument("--sleep", action="store_true", help="所有文件校验通过后自动睡眠（Windows/Linux）")
    power.add_argument("--hibernate", action="store_true", help="所有文件校验通过后自动休眠（Windows/Linux）")
    result.add_argument("--sleep-delay", type=positive_int, default=60, help="睡眠倒计时秒数，默认 60")
    result.add_argument("--hibernate-delay", type=positive_int, default=60, help="休眠倒计时秒数，默认 60")
    result.add_argument("--quiet", action="store_true", help="隐藏动态进度，保留结果和错误")
    return result


def main(argv=None) -> int:
    # Windows pipes otherwise use the legacy code page, garbling redirected Chinese logs.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cli = parser()
    args = cli.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)?", args.repo) or any(p in (".", "..") for p in args.repo.split("/")):
        cli.error("repo 应为仓库 ID，例如 Qwen/Qwen2.5-0.5B-Instruct")
    args.endpoint = args.endpoint.rstrip("/")
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme not in ("http", "https") or not endpoint.netloc or endpoint.query or endpoint.fragment or endpoint.username:
        cli.error("endpoint 必须是有效的 HTTP(S) 地址")
    if (args.sleep or args.hibernate) and sys.platform != "win32" and not sys.platform.startswith("linux"):
        cli.error("自动睡眠/休眠目前仅支持 Windows/Linux")
    stop = threading.Event()
    previous = {}
    interrupt_count = 0
    forced = False

    def interrupted(signum, frame):
        # Only change Python state here: no I/O or locks in the signal handler.
        nonlocal interrupt_count
        interrupt_count += 1

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for signum in signals:
        previous[signum] = signal.signal(signum, interrupted)
    try:
        with keep_awake():
            task = Downloader(args, stop)
            run_interruptibly(task.run, stop, lambda: interrupt_count)
        if interrupt_count or stop.is_set():
            raise Cancelled()
        if args.hibernate:
            hibernate(stop, args.hibernate_delay, lambda: interrupt_count)
        elif args.sleep:
            sleep_system(stop, args.sleep_delay, lambda: interrupt_count)
        return 0
    except ForcedCancel:
        forced = True
        cancellation_message("网络线程未及时退出，正在强制停止。已写入的分块保留，下次使用相同命令续传。")
        # os._exit bypasses Rich's normal Live.stop(), so restore its hidden cursor
        # without waiting for any renderer locks held by a stuck worker.
        restore_terminal_cursor()
    except (Cancelled, KeyboardInterrupt):
        cancellation_message("已停止，进度已保留。使用相同命令继续下载。")
        return 130
    except (DownloadError, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if forced:
        # A normal sys.exit still joins ThreadPoolExecutor workers at interpreter
        # shutdown. Partial files use unbuffered writes and final files are only
        # published after verification. Windows idle-sleep state is restored above.
        os._exit(130)
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
