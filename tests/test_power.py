from contextlib import contextmanager
import ctypes
import sys
import threading
from unittest.mock import Mock

import pytest

import hf_downloader as hf


@pytest.fixture(autouse=True)
def block_real_power_changes(monkeypatch):
    monkeypatch.setattr(hf.subprocess, "run", Mock(side_effect=AssertionError("unexpected system command")))
    original = getattr(ctypes, "WinDLL", None)

    def safe_dll(name, *args, **kwargs):
        if "powrprof" in name.lower() or original is None:
            raise AssertionError("unexpected native power API")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(hf.ctypes, "WinDLL", safe_dll, raising=False)
    monkeypatch.setattr(hf, "keep_awake", lambda: harmless_context())


@contextmanager
def harmless_context():
    yield


@pytest.mark.parametrize("outcome,expected", [("success", 0), ("failure", 1), ("cancel", 130)])
def test_sleep_only_after_success_and_awake_guard_released(tmp_path, monkeypatch, outcome, expected):
    calls = []

    @contextmanager
    def awake():
        calls.append("awake")
        try:
            yield
        finally:
            calls.append("released")

    def download(self):
        calls.append("download")
        if outcome == "failure":
            raise hf.DownloadError("verification failed")
        if outcome == "cancel":
            raise hf.Cancelled()

    monkeypatch.setattr(hf, "keep_awake", awake)
    monkeypatch.setattr(hf.Downloader, "run", download)
    monkeypatch.setattr(hf, "sleep_system", lambda stop, seconds, interrupts: calls.append(("sleep", seconds)))
    assert hf.main(["test/model", "-o", str(tmp_path), "--sleep", "--sleep-delay", "7"]) == expected
    assert calls == ["awake", "download", "released"] + ([("sleep", 7)] if outcome == "success" else [])


def test_power_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit) as caught:
        hf.parser().parse_args(["test/model", "--sleep", "--hibernate"])
    assert caught.value.code == 2


@pytest.mark.parametrize("delay", ["0", "-1"])
def test_sleep_delay_must_be_positive(delay):
    with pytest.raises(SystemExit):
        hf.parser().parse_args(["test/model", "--sleep-delay", delay])


@pytest.mark.parametrize("by_signal", [False, True])
def test_sleep_countdown_cancellation(monkeypatch, by_signal):
    stop = threading.Event()
    clock = iter([0, 0, 0.1])
    interrupted = [0]
    monkeypatch.setattr(hf.time, "monotonic", lambda: next(clock))

    def interrupt(_):
        if by_signal:
            interrupted[0] = 1
        else:
            stop.set()

    monkeypatch.setattr(hf.time, "sleep", interrupt)
    with pytest.raises(hf.Cancelled):
        hf.sleep_system(stop, 60, lambda: interrupted[0])
    assert stop.is_set()


def test_linux_sleep_uses_suspend(monkeypatch):
    monkeypatch.setattr(hf.sys, "platform", "linux")
    run = Mock()
    monkeypatch.setattr(hf.subprocess, "run", run)
    hf.sleep_system(threading.Event(), 0)
    run.assert_called_once_with(["systemctl", "suspend"], check=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes error API")
@pytest.mark.parametrize("failure", [None, "permission", "suspend"])
def test_windows_sleep_arguments_errors_and_token_cleanup(monkeypatch, failure):
    kernel, security, power = Mock(), Mock(), Mock()
    monkeypatch.setattr(hf.ctypes, "WinDLL", lambda name, **kw: {
        "kernel32": kernel, "advapi32": security, "powrprof": power}[name])
    restored = []

    def open_token(process, access, token):
        token._obj.value = 123
        return True

    def adjust(token, disable_all, requested, size, previous, returned_size):
        if previous is not None:
            previous._obj.PrivilegeCount = 0 if failure == "permission" else 1
            previous._obj.Attributes = 0
            ctypes.set_last_error(1300 if failure == "permission" else 0)
        else:
            restored.append(requested._obj.Attributes)
        return True

    def suspend(*args):
        if failure == "suspend":
            ctypes.set_last_error(5)
            return False
        return True

    security.OpenProcessToken.side_effect = open_token
    security.AdjustTokenPrivileges.side_effect = adjust
    power.SetSuspendState.side_effect = suspend
    if failure:
        with pytest.raises(OSError) as caught:
            hf.sleep_system(threading.Event(), 0)
        assert caught.value.winerror == (1300 if failure == "permission" else 5)
    else:
        hf.sleep_system(threading.Event(), 0)
    if failure == "permission":
        power.SetSuspendState.assert_not_called()
        assert restored == []
    else:
        power.SetSuspendState.assert_called_once_with(False, False, False)
        assert restored == [0]
    kernel.CloseHandle.assert_called_once()
    assert kernel.CloseHandle.call_args.args[0].value == 123


def test_sleep_system_failure_reports_failure_but_keeps_download(tmp_path, monkeypatch):
    completed = tmp_path / "model.bin"
    monkeypatch.setattr(hf.Downloader, "run", lambda self: completed.write_bytes(b"downloaded"))
    monkeypatch.setattr(hf, "sleep_system", Mock(side_effect=OSError("sleep denied")))
    assert hf.main(["test/model", "-o", str(tmp_path), "--sleep"]) == 1
    assert completed.read_bytes() == b"downloaded"
