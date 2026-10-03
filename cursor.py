# -*- coding: utf-8 -*-
"""鼠标光标合成：mss/GDI 抓屏默认不含光标，把系统光标每帧画回画面。"""
import ctypes

import numpy as np

from win32util import (
    BITMAP, BITMAPINFO, BITMAPINFOHEADER, CURSORINFO, ICONINFO,
    BI_RGB, DIB_RGB_COLORS, gdi32, user32,
)


class CursorRenderer:
    """把系统鼠标光标合成到抓帧上。
    光标位图按 hCursor 句柄缓存，帧内仅做小区域 alpha 混合，开销可忽略。"""

    def __init__(self):
        self._key = None
        self._img = None  # (h, w, 4) BGRA
        self._hot = (0, 0)

    def paint(self, frame, mon):
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & 1):
            return
        if ci.hCursor != self._key:
            self._key = ci.hCursor
            self._img = self._extract(ci.hCursor)
        if self._img is None:
            return
        h, w = self._img.shape[:2]
        hx, hy = self._hot
        fx = ci.ptScreenPos.x - mon["left"] - hx
        fy = ci.ptScreenPos.y - mon["top"] - hy
        fh, fw = frame.shape[:2]
        if fx >= fw or fy >= fh or fx + w <= 0 or fy + h <= 0:
            return
        x0, y0 = max(0, fx), max(0, fy)
        x1, y1 = min(fw, fx + w), min(fh, fy + h)
        if x1 <= x0 or y1 <= y0:
            return
        cur = self._img[y0 - fy:y1 - fy, x0 - fx:x1 - fx]
        roi = frame[y0:y1, x0:x1]
        a = cur[:, :, 3:4].astype(np.float32) / 255.0
        roi[:] = (
            roi.astype(np.float32) * (1 - a)
            + cur[:, :, :3].astype(np.float32) * a
        ).astype(np.uint8)

    def _extract(self, hcur):
        hc = user32.CopyIcon(hcur)
        if not hc:
            return None
        ii = ICONINFO()
        if not user32.GetIconInfo(hc, ctypes.byref(ii)):
            user32.DestroyIcon(hc)
            return None
        hbmC, hbmM = ii.hbmColor, ii.hbmMask
        try:
            self._hot = (ii.xHotspot, ii.yHotspot)
            if hbmC:
                img = self._bitmap_to_bgra(hbmC)
                h, w = img.shape[:2]
                andm = self._mono_mask(hbmM, h)  # 1=透明
                img[:, :, 3] = (1 - andm) * 255
                return img
            # 单色光标：AND mask 定形状，XOR mask 定黑白
            andm, xorm = self._mono_both(hbmM)
            img = np.zeros((andm.shape[0], andm.shape[1], 4), np.uint8)
            gray = np.where(xorm == 1, 0, 255)
            img[:, :, 0] = img[:, :, 1] = img[:, :, 2] = gray
            img[:, :, 3] = (1 - andm) * 255
            return img
        finally:
            if hbmC:
                gdi32.DeleteObject(hbmC)
            if hbmM:
                gdi32.DeleteObject(hbmM)
            user32.DestroyIcon(hc)

    def _bitmap_to_bgra(self, hbm):
        bmp = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bmp))
        w, h = bmp.bmWidth, bmp.bmHeight
        hdc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        got = gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        gdi32.DeleteDC(hdc)
        if got != h:
            return np.zeros((h, w, 4), np.uint8)
        return np.frombuffer(buf, np.uint8).reshape(h, w, 4).copy()

    def _read_1bpp(self, hbm):
        bmp = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bmp))
        w, hh = bmp.bmWidth, bmp.bmHeight
        row_bytes = ((w + 31) // 32) * 4
        hdc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = hh  # bottom-up
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 1
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiColors[0] = 0x00000000  # 调色板: 黑
        bmi.bmiColors[1] = 0x00FFFFFF  # 白
        buf = (ctypes.c_ubyte * (row_bytes * hh))()
        got = gdi32.GetDIBits(hdc, hbm, 0, hh, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        gdi32.DeleteDC(hdc)
        if got != hh:
            return None
        raw = np.frombuffer(buf, np.uint8).reshape(hh, row_bytes).copy()
        raw = np.flipud(raw)  # bottom-up -> top-down
        out = np.zeros((hh, w), np.uint8)
        for y in range(hh):
            row = raw[y]
            for x in range(w):
                out[y, x] = (row[x // 8] >> (7 - (x % 8))) & 1
        return out

    def _mono_mask(self, hbm, h):
        bits = self._read_1bpp(hbm)
        if bits is None:
            return np.zeros((h, h), np.uint8)
        return bits[:h]

    def _mono_both(self, hbm):
        bits = self._read_1bpp(hbm)
        if bits is None:
            h = 32
            return np.zeros((h, h), np.uint8), np.zeros((h, h), np.uint8)
        h = bits.shape[0] // 2
        return bits[:h], bits[h:]
