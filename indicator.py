# -*- coding: utf-8 -*-
"""选区指示器：Win32 per-pixel alpha 置顶透明窗。
中间 100% 透明（无遮罩），红框围绕录制区域外沿 + 顶部尺寸角标，
创建时即带 WS_EX_TRANSPARENT 原生鼠标穿透。独立线程 + 命令队列驱动。"""
import ctypes
import queue
import threading
import time
from ctypes import wintypes

import numpy as np

from win32util import (
    BITMAPINFO, BITMAPINFOHEADER, BLENDFUNCTION, POINT, SIZE,
    WNDCLASSEXW, WNDPROC,
    AC_SRC_ALPHA, AC_SRC_OVER, BI_RGB, DIB_RGB_COLORS, PM_REMOVE,
    SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA, WS_EX_LAYERED,
    WS_EX_NOACTIVATE, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP,
    gdi32, user32,
)


class RegionIndicator:
    """选区指示器：红框画在录制区域外沿（录制时天然不进画面），
    尺寸角标浮在区域正上方的角标条里。"""

    def __init__(self):
        self._q = queue.Queue()
        self._thread = None
        self._hwnd = None

    def get_hwnd(self):
        return self._hwnd

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_region(self, region):
        self._q.put(("set", region))

    def close(self):
        self._q.put(("close", None))
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self):
        # 让窗口坐标与 mss 抓屏一致（物理像素，避免 DPI 缩放偏移）
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls_name = "XiaoeRecIndicator"

        @WNDPROC
        def wndproc(hwnd, msg, wp, lp):
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
        wc.hInstance = hInstance
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            cls_name, "region_indicator", WS_POPUP,
            0, 0, 1, 1, None, None, hInstance, None,
        )
        self._hwnd = hwnd

        while True:
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                cmd, arg = self._q.get_nowait()
            except queue.Empty:
                cmd, arg = None, None
            if cmd == "set":
                if arg is None:
                    user32.ShowWindow(hwnd, SW_HIDE)
                else:
                    self._paint(hwnd, arg)
                    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            elif cmd == "close":
                user32.DestroyWindow(hwnd)
                user32.UnregisterClassW(cls_name, hInstance)
                break
            time.sleep(0.03)

    def _paint(self, hwnd, region):
        m = 4     # 红框外扩边距：红框画在录制区域外沿 1-4px 处
        badge = 28  # 顶部角标条高度：尺寸角标画在区域正上方，绝不被录制
        left, top = region["left"] - m, region["top"] - m - badge
        w, h = region["width"], region["height"]
        W, H = w + 2 * m, h + 2 * m + badge
        user32.MoveWindow(hwnd, left, top, W, H, True)

        hdcScreen = user32.GetDC(None)
        hdcMem = gdi32.CreateCompatibleDC(hdcScreen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H  # top-down，与屏幕坐标一致
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(
            hdcMem, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        old = gdi32.SelectObject(hdcMem, hbm)

        # BGRA 像素：中间全透明；红框围绕区域外沿；角标在顶部条（区域上方）
        px = np.zeros((H, W, 4), dtype=np.uint8)
        RED = (0x30, 0x3B, 0xFF, 255)  # B,G,R,A -> #ff3b30
        px[badge:badge + 3, :] = RED    # 顶部红框（区域上方 1-4px）
        px[H - m:H - m + 3, :] = RED    # 底部红框
        px[:, 0:3] = RED                # 左红框
        px[:, W - 3:W] = RED            # 右红框

        label = f"{w} x {h}"
        bw = len(label) * 11 + 10  # 角标黑底宽度（估）
        px[4:28, 6:6 + bw] = (0x11, 0x11, 0x11, 255)  # 角标黑底（顶部条内）

        ctypes.memmove(bits, px.ctypes.data, px.nbytes)

        gdi32.SetBkMode(hdcMem, 1)  # TRANSPARENT
        gdi32.SetTextColor(hdcMem, 0x00FFFFFF)  # 白字
        hFont = gdi32.CreateFontW(
            -16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI",
        )
        oldf = gdi32.SelectObject(hdcMem, hFont)
        gdi32.TextOutW(hdcMem, 12, 8, label, len(label))

        ptDst = POINT(left, top)
        size = SIZE(W, H)
        ptSrc = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            hwnd, hdcScreen,
            ctypes.byref(ptDst), ctypes.byref(size),
            hdcMem, ctypes.byref(ptSrc),
            0, ctypes.byref(blend), ULW_ALPHA,
        )

        gdi32.SelectObject(hdcMem, oldf)
        gdi32.SelectObject(hdcMem, old)
        gdi32.DeleteObject(hFont)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdcMem)
        user32.ReleaseDC(None, hdcScreen)
