# -*- coding: utf-8 -*-
"""自研通知：右下角矩形卡片，3 秒后自动淡出。
不依赖系统 balloon 通知（Windows 通知中心对重复通知会抽风闪烁）。
消息队列驱动：显示中来新消息则合并刷新并重置计时，不叠加不闪烁。
文字用 PIL 抗锯齿渲染（微软雅黑），无圆角。"""
import ctypes
import queue
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from win32util import (
    BITMAPINFO, BITMAPINFOHEADER, BLENDFUNCTION, POINT, SIZE,
    WNDCLASSEXW, WNDPROC,
    AC_SRC_ALPHA, AC_SRC_OVER, BI_RGB, DIB_RGB_COLORS, PM_REMOVE,
    SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA, WS_EX_LAYERED,
    WS_EX_NOACTIVATE, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP,
    gdi32, user32,
)

SHOW_SECONDS = 3.0   # 显示时长
FADE_STEP = 35       # 淡出每帧减小的透明度
FADE_INTERVAL = 0.03  # 主循环间隔（秒）

user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
]

_FONT = None  # 惰性加载微软雅黑


def _get_font():
    global _FONT
    if _FONT is None:
        try:
            _FONT = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 15)
        except Exception:
            _FONT = ImageFont.load_default()
    return _FONT


class Notifier:
    """自研通知：右下角矩形卡片，消息排队、显示中刷新合并、3 秒淡出。"""

    def __init__(self):
        self._q = queue.Queue()
        self._thread = None
        self._hwnd = None
        self._text = ""
        self._state = "idle"  # idle / showing / fading
        self._show_until = 0.0
        self._alpha = 255

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def notify(self, text):
        self._q.put(text)

    def close(self):
        self._q.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    # ===== 窗口与绘制 =====
    def _paint(self, text):
        font = _get_font()
        # 用 PIL 测量文本尺寸
        probe = Image.new("RGBA", (8, 8))
        bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        w = min(480, max(140, tw + 44))
        h = max(46, th + 28)

        # 右下角（工作区内，不遮任务栏）
        rect = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        left = rect.right - w - 16
        top = rect.bottom - h - 16

        hdcScreen = user32.GetDC(None)
        hdcMem = gdi32.CreateCompatibleDC(hdcScreen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(
            hdcMem, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        old = gdi32.SelectObject(hdcMem, hbm)

        # 矩形背景（无圆角）+ 顶部红条
        px = np.zeros((h, w, 4), np.uint8)
        px[:, :, 0] = 20
        px[:, :, 1] = 20
        px[:, :, 2] = 20
        px[:, :, 3] = 225
        px[0:3, :, 0] = 0x30
        px[0:3, :, 1] = 0x3B
        px[0:3, :, 2] = 0xFF
        px[0:3, :, 3] = 255

        # 文字用 PIL 渲染（抗锯齿），合成进 BGRA 像素
        txt = Image.new("RGBA", (tw + 8, th + 8), (0, 0, 0, 0))
        ImageDraw.Draw(txt).text(
            (4 - bbox[0], 4 - bbox[1]), text, font=font,
            fill=(255, 255, 255, 255),
        )
        ta = np.array(txt)  # RGBA
        x0 = 14
        y0 = max(10, (h - ta.shape[0]) // 2)
        # 文字超过卡片宽度时裁剪（不换行，超长截断显示）
        th_cut = min(ta.shape[0], h - y0)
        tw_cut = min(ta.shape[1], w - x0)
        ta = ta[:th_cut, :tw_cut]
        roi = px[y0:y0 + th_cut, x0:x0 + tw_cut]
        a = ta[:, :, 3:4].astype(np.float32) / 255.0
        # 只合成 RGB（BGR 序），alpha 取最大值
        roi[:, :, :3] = (
            roi[:, :, :3].astype(np.float32) * (1 - a)
            + ta[:, :, :3][:, :, ::-1].astype(np.float32) * a  # RGB -> BGR
        ).astype(np.uint8)
        roi[:, :, 3] = np.maximum(roi[:, :, 3], ta[:, :, 3])

        ctypes.memmove(bits, px.ctypes.data, px.nbytes)

        ptDst = POINT(left, top)
        size = SIZE(w, h)
        ptSrc = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, self._alpha, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self._hwnd, hdcScreen,
            ctypes.byref(ptDst), ctypes.byref(size),
            hdcMem, ctypes.byref(ptSrc),
            0, ctypes.byref(blend), ULW_ALPHA,
        )

        gdi32.SelectObject(hdcMem, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdcMem)
        user32.ReleaseDC(None, hdcScreen)

    def _show(self):
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)

    def _hide(self):
        user32.ShowWindow(self._hwnd, SW_HIDE)

    # ===== 主循环 =====
    def _run(self):
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls_name = "XiaoeRecNotifier"

        @WNDPROC
        def wndproc(hwnd, msg, wp, lp):
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
        wc.hInstance = hInstance
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        self._hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            cls_name, "notifier", WS_POPUP,
            0, 0, 1, 1, None, None, hInstance, None,
        )

        while True:
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            # 取最新消息（旧的直接丢弃合并，避免叠加闪烁）
            got = None
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is None:  # close
                    user32.DestroyWindow(self._hwnd)
                    user32.UnregisterClassW(cls_name, hInstance)
                    return
                got = item

            if got is not None:
                self._text = got
                self._alpha = 255
                self._state = "showing"
                self._show_until = time.monotonic() + SHOW_SECONDS
                self._paint(got)
                self._show()

            now = time.monotonic()
            if self._state == "showing" and now >= self._show_until:
                self._state = "fading"
            elif self._state == "fading":
                self._alpha -= FADE_STEP
                if self._alpha <= 0:
                    self._alpha = 0
                    self._hide()
                    self._state = "idle"
                else:
                    self._paint(self._text)
                    self._show()

            time.sleep(FADE_INTERVAL)
