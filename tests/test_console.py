import os
import subprocess
import sys

import pytest

import hf_downloader as hf


def test_redirected_cancellation_is_utf8_even_with_short_writes(monkeypatch):
    output = bytearray()
    monkeypatch.setattr(hf, "windows_console", lambda fd: None)

    def write(fd, data):
        assert fd == 2
        output.extend(data[:3])
        return min(3, len(data))

    monkeypatch.setattr(hf.os, "write", write)
    hf.cancellation_message("已停止，进度已保留。")
    assert output.decode("utf-8") == "\n已停止，进度已保留。\n"


def test_closed_stderr_does_not_prevent_cancellation(monkeypatch):
    def closed(*args):
        raise OSError("closed")

    monkeypatch.setattr(hf, "write_unbuffered", closed)
    hf.cancellation_message("停止")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native console regression")
@pytest.mark.parametrize("codepage", [936, 65001])
def test_native_console_chinese_and_cursor_without_changing_user_console(codepage):
    # Use a separate hidden console, not the user's console/code page. stdout and
    # stderr are captured pipes; a private screen buffer exercises the real API.
    script = r'''
import ctypes
from ctypes import wintypes
import msvcrt
import os
import hf_downloader as hf

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.SetConsoleOutputCP.argtypes = [wintypes.UINT]
kernel.SetConsoleOutputCP.restype = wintypes.BOOL
assert kernel.SetConsoleOutputCP(CODEPAGE), ctypes.get_last_error()
kernel.CreateConsoleScreenBuffer.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.c_void_p]
kernel.CreateConsoleScreenBuffer.restype = wintypes.HANDLE
handle = kernel.CreateConsoleScreenBuffer(0xC0000000, 3, None, 1, None)
assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
fd = msvcrt.open_osfhandle(handle, os.O_RDWR)

class Coord(ctypes.Structure):
    _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

class CursorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("visible", wintypes.BOOL)]

kernel.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                               Coord, ctypes.POINTER(wintypes.DWORD)]
kernel.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
kernel.GetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CursorInfo)]
kernel.GetConsoleCursorInfo.restype = wintypes.BOOL
kernel.SetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CursorInfo)]
kernel.SetConsoleCursorInfo.restype = wintypes.BOOL

message = "已停止，进度已保留。Ctrl+C"
hf.write_unbuffered(fd, message)
buffer = ctypes.create_unicode_buffer(100)
read = wintypes.DWORD()
assert kernel.ReadConsoleOutputCharacterW(handle, buffer, 99, Coord(0, 0), ctypes.byref(read))
assert buffer.value.rstrip() == message, repr(buffer.value)

cursor = CursorInfo()
assert kernel.GetConsoleCursorInfo(handle, ctypes.byref(cursor))
cursor.visible = False
assert kernel.SetConsoleCursorInfo(handle, ctypes.byref(cursor))
os.dup2(fd, 1)
hf.restore_terminal_cursor()
assert kernel.GetConsoleCursorInfo(handle, ctypes.byref(cursor))
assert cursor.visible
assert kernel.ReadConsoleOutputCharacterW(handle, buffer, 99, Coord(0, 0), ctypes.byref(read))
assert buffer.value.rstrip() == message, repr(buffer.value)
os.close(fd)
'''.replace("CODEPAGE", str(codepage))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
                            env={**os.environ, "PYTHONUTF8": "1"}, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
