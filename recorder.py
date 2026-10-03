# -*- coding: utf-8 -*-
"""录屏引擎：mss 抓帧 + Windows Media Foundation (MSMF) H.264 编码。
支持全屏/自定义区域/暂停（暂停时长不计入视频）。"""
import threading
import time

import cv2
import mss
import numpy as np

from cursor import CursorRenderer


class RecorderEngine:
    def __init__(self):
        self.running = False
        self.paused = False
        self._pause_evt = threading.Event()
        self.thread = None
        self._ready = None
        self._error = None

    def start(self, path, fps, region=None):
        if self.running:
            raise RuntimeError("已经在录制了")
        self.running = True
        self._ready = threading.Event()
        self._error = None
        self.thread = threading.Thread(
            target=self._run, args=(path, fps, region), daemon=True
        )
        self.thread.start()
        if not self._ready.wait(timeout=5):
            self.running = False
            raise RuntimeError("录制引擎启动超时")
        if self._error:
            self.running = False
            raise RuntimeError(self._error)

    def _run(self, path, fps, region):
        try:
            with mss.mss() as sct:
                mon = dict(region) if region else dict(sct.monitors[0])
                # opencv 编码器要求偶数宽高
                mon["width"] = mon["width"] // 2 * 2
                mon["height"] = mon["height"] // 2 * 2
                writer = cv2.VideoWriter(
                    path,
                    cv2.CAP_MSMF,
                    cv2.VideoWriter_fourcc(*"H264"),
                    fps,
                    (mon["width"], mon["height"]),
                )
                if not writer.isOpened():
                    raise RuntimeError("H.264 编码器初始化失败（MSMF 不可用）")
                self._ready.set()
                interval = 1.0 / fps
                cursor = CursorRenderer()
                while self.running:
                    if self.paused:
                        # 暂停：不抓帧不写帧，暂停时长不计入视频
                        self._pause_evt.wait(0.1)
                        continue
                    t0 = time.perf_counter()
                    frame = cv2.cvtColor(
                        np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR
                    )
                    cursor.paint(frame, mon)
                    writer.write(frame)
                    wait = interval - (time.perf_counter() - t0)
                    if wait > 0:
                        time.sleep(wait)
                writer.release()
        except Exception as e:
            self._error = str(e)
            self._ready.set()

    def pause(self):
        """暂停录制（不写帧，暂停时长不计入视频）。"""
        if self.running and not self.paused:
            self.paused = True
            self._pause_evt.clear()

    def resume(self):
        """继续录制。"""
        if self.running and self.paused:
            self.paused = False
            self._pause_evt.set()

    def stop(self):
        self.running = False
        self.paused = False
        self._pause_evt.set()  # 唤醒可能暂停中的循环
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
