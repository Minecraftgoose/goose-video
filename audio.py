# -*- coding: utf-8 -*-
"""音频采集：WASAPI loopback 录制系统扬声器输出（网页声音/音乐/游戏音效），
写入 WAV；配合 ffmpeg 把音视频合并为带 AAC 音轨的 MP4。"""
import os
import subprocess
import sys
import threading
import wave


class AudioRecorder:
    """WASAPI loopback 采集扬声器声音，写入 WAV。支持暂停（暂停期不写帧）。"""

    def __init__(self):
        self._thread = None
        self._running = False
        self._paused = False
        self._wav = None
        self._stream = None
        self._pa = None
        self._rate = 48000
        self.path = None
        self.has_data = False
        self.error = None

    def start(self, wav_path):
        if self._running:
            return
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            self.error = f"音频库不可用: {e}"
            return False
        pa = pyaudio.PyAudio()
        stream = None
        wav = None
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loopback = None
            if default.get("isLoopbackDevice", False):
                loopback = default
            else:
                for i in range(pa.get_device_count()):
                    dev = pa.get_device_info_by_index(i)
                    # loopback 设备名 = 默认设备名 + " [Loopback]"
                    if (dev.get("isLoopbackDevice", False)
                            and default.get("name") in dev.get("name", "")):
                        loopback = dev
                        break
            if loopback is None:
                raise RuntimeError("未找到 WASAPI 回环设备")
            channels = max(1, int(loopback.get("maxInputChannels", 2)))
            rate = int(loopback.get("defaultSampleRate", 48000))
            wav = wave.open(wav_path, "wb")
            wav.setnchannels(channels)
            wav.setsampwidth(2)  # paInt16
            wav.setframerate(rate)
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=loopback["index"],
                frames_per_buffer=max(512, rate // 20),
            )
        except Exception as e:
            if stream:
                stream.close()
            if wav:
                wav.close()
            pa.terminate()
            self.error = f"音频初始化失败: {e}"
            return False

        self._pa = pa
        self._stream = stream
        self._wav = wav
        self._rate = rate
        self.path = wav_path
        self.has_data = False
        self.error = None
        self._paused = False
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        chunk = max(512, self._rate // 20)
        try:
            while self._running:
                data = self._stream.read(chunk, exception_on_overflow=False)
                if not self._running:
                    break
                if self._paused:
                    continue
                self._wav.writeframes(data)
                self.has_data = True
        except Exception as e:
            sys.stderr.write(f"[audio] {e}\n")
            self.error = str(e)

    def pause(self):
        if self._running and not self._paused:
            self._paused = True

    def resume(self):
        if self._running and self._paused:
            self._paused = False

    def stop(self):
        self._running = False
        # 先 stop_stream 解除 read 阻塞，等采集线程退出，再关闭资源
        # （顺序错了会崩：线程还阻塞在 read() 时 close 流 -> 访问已关闭对象）
        if self._stream:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._wav:
            try:
                self._wav.close()
            except Exception:
                pass
            self._wav = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


def mux_video_audio(video_path, wav_path, out_path):
    """用 ffmpeg 把视频轨 + 音频轨合并为最终 MP4（AAC + faststart）。"""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return False
    if not os.path.exists(video_path) or not os.path.exists(wav_path):
        return False
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-i", wav_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        # CREATE_NO_WINDOW：ffmpeg 是控制台程序，隐藏黑窗口（Windows）
        creationflags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, timeout=180,
                           creationflags=creationflags)
        ok = (r.returncode == 0
              and os.path.exists(out_path)
              and os.path.getsize(out_path) > 0)
        if not ok and r.stderr:
            sys.stderr.write(f"[audio] ffmpeg: {r.stderr.decode('utf-8', 'ignore')[:300]}\n")
        return ok
    except Exception as e:
        sys.stderr.write(f"[audio] ffmpeg 调用失败: {e}\n")
        return False
