"""Win32 API を ctypes で直接叩き、前面ウィンドウ情報とアイドル秒数を取得する。

pywin32 等のサードパーティは使わない。標準ライブラリ（ctypes）のみで完結させる。
Windows 以外の環境では例外を投げず、空の情報を返す（is_supported() で判定可能）。
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
from dataclasses import dataclass


@dataclass
class ForegroundInfo:
    """前面ウィンドウの情報。取得に失敗した場合は process='' になる。"""

    process: str
    window_title: str
    pid: int


def is_supported() -> bool:
    """Windows 上で動作しているか。"""
    return sys.platform == "win32"


def get_host() -> str:
    """このマシンのホスト名（取得できなければ空文字）。"""
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _empty_foreground_info() -> ForegroundInfo:
    return ForegroundInfo(process="", window_title="", pid=0)


if not is_supported():
    # ------------------------------------------------------------------
    # Windows 以外向けのフォールバック実装。例外は投げず空情報を返す。
    # ------------------------------------------------------------------

    def get_foreground_info() -> ForegroundInfo:
        return _empty_foreground_info()

    def get_idle_sec() -> int:
        return 0

else:
    # ------------------------------------------------------------------
    # Windows 実装。user32 / kernel32 を ctypes 経由で直接呼び出す。
    # ------------------------------------------------------------------

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PATH_BUFFER_SIZE = 1024  # QueryFullProcessImageNameW 用（長いパスにも余裕を持たせる）

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []

    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    user32.GetLastInputInfo.restype = wintypes.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]

    kernel32.GetTickCount.restype = wintypes.DWORD
    kernel32.GetTickCount.argtypes = []

    def _get_window_title(hwnd: int) -> str:
        """ウィンドウタイトルを取得する（無ければ空文字）。"""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _get_process_name(pid: int) -> str:
        """PID から実行ファイル名（basename）を取得する。失敗時は空文字。"""
        if pid <= 0:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(_PATH_BUFFER_SIZE)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            if not ok:
                return ""
            return os.path.basename(buffer.value)
        finally:
            # ハンドルは必ず閉じる
            kernel32.CloseHandle(handle)

    def get_foreground_info() -> ForegroundInfo:
        """前面ウィンドウの process / window_title / pid を取得する。"""
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return _empty_foreground_info()
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = _get_window_title(hwnd)
            process = _get_process_name(pid.value)
            return ForegroundInfo(process=process, window_title=title, pid=int(pid.value))
        except OSError:
            # 収集ループを止めないよう、取得失敗時は空情報にフォールバック
            return _empty_foreground_info()

    def get_idle_sec() -> int:
        """最終入力からの経過秒数。GetTickCount の 32bit ラップアラウンドに対応。"""
        try:
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not user32.GetLastInputInfo(ctypes.byref(info)):
                return 0
            tick_now = kernel32.GetTickCount()
            # どちらも 32bit unsigned のティック値なので、差分を 32bit マスクで
            # 取れば tick_now が info.dwTime よりラップアラウンドしていても
            # 正しい非負の経過ミリ秒が求まる。
            idle_ms = (tick_now - info.dwTime) & 0xFFFFFFFF
            return max(0, idle_ms // 1000)
        except OSError:
            return 0
