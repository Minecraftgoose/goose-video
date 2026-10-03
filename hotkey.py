# -*- coding: utf-8 -*-
"""全局热键：WH_KEYBOARD_LL 低级键盘钩子（自实现，不依赖第三方库）。
绑定 {虚拟键码: 回调}，按下时触发一次，不吞键（继续传给其它程序）。"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

from win32util import PM_REMOVE, user32


class GlobalHotkey:
    def __init__(self):
        self._proc = None
        self._hook = None
        self._thread = None
        self._handlers = {}

    def start(self, bindings):
        self._handlers = bindings
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _run(self):
        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        )
        user32.CallNextHookEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_long

        pressed = set()

        @HOOKPROC
        def proc(code, wp, lp):
            vk = ctypes.cast(
                lp, ctypes.POINTER(wintypes.DWORD)
            ).contents.value & 0xFFFF
            if code >= 0:
                if wp == 0x0100:  # WM_KEYDOWN
                    if vk in self._handlers and vk not in pressed:
                        pressed.add(vk)
                        try:
                            self._handlers[vk]()
                        except Exception as e:
                            try:
                                sys.stderr.write(f"[hotkey] {e}\n")
                            except Exception:
                                pass
                elif wp == 0x0101:  # WM_KEYUP
                    pressed.discard(vk)
            return user32.CallNextHookEx(None, code, wp, lp)

        self._proc = proc
        self._hook = user32.SetWindowsHookExW(
            13, ctypes.cast(proc, ctypes.c_void_p), None, 0,
        )
        msg = wintypes.MSG()
        while self._hook:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)
