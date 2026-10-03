# -*- coding: utf-8 -*-
"""
小鹅录屏 - 零广告屏幕录制小工具（入口）
自研录制引擎：mss 抓帧 + Windows Media Foundation (MSMF) H.264 编码。
输出为标准 H.264 MP4 —— 浏览器(Chrome/Edge/Firefox)和本地播放器通吃。
录制时同步采集系统扬声器声音（WASAPI loopback），停止后自动合并音轨。

模块结构:
    rec_tool.py   入口：托盘 UI、菜单、录制控制流、热键绑定
    recorder.py   RecorderEngine：抓帧 + H.264 编码 + 暂停
    cursor.py     CursorRenderer：鼠标光标合成
    indicator.py  RegionIndicator：选区指示器（Win32 透明窗）
    hotkey.py     GlobalHotkey：全局热键（LL 钩子）
    audio.py      AudioRecorder：系统声音采集 + ffmpeg 合并
    win32util.py  公共 Win32 结构

用法:
    py -3.11 rec_tool.py

托盘菜单:
    开始录制 (F9)       开始录屏（全屏或自定义区域，视当前设置）
    停止录制 (F9)       停止并保存 MP4 到 recordings 文件夹
    暂停/继续 (F10)     暂停或继续录制（暂停时长不计入视频）
    录制区域            全屏 / 自定义区域...(拖拽框选，Esc 取消)
    帧率                10 / 15 / 20 / 30 fps 勾选切换
    打开输出文件夹      打开保存目录
    退出                退出程序（若正在录制会自动保存）

全局快捷键:
    F9    开始 / 停止录制
    F10   暂停 / 继续录制

备注: 自定义区域基于主屏幕坐标；多显示器场景请把目标屏设为主屏。
"""
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import mss
import pystray
from PIL import Image, ImageDraw

from audio import AudioRecorder, mux_video_audio
from hotkey import GlobalHotkey
from indicator import RegionIndicator
from notify import Notifier
from recorder import RecorderEngine

# ===== 可调配置 =====
DEFAULT_FPS = 30  # 默认帧率，可在托盘菜单里切换
# exe(frozen) 运行时以 exe 所在目录为基准，脚本运行时以脚本目录为基准
BASE_DIR = (Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_DIR = BASE_DIR / "recordings"


def load_config():
    """读取配置文件（自定义输出目录等）。"""
    try:
        import json
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    """保存配置文件。"""
    try:
        import json
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass


_cfg = load_config()
# 归档目录：录制仍写默认 recordings，停止后代码自动把文件剪切到这里（未设置则留在原处）
ARCHIVE_DIR = Path(_cfg["output_dir"]) if _cfg.get("output_dir") else None

# ===== 全局状态 =====
recording = False
output_path = None
FPS = DEFAULT_FPS
REGION = None  # None=全屏; dict(left,top,width,height)=自定义区域

engine = RecorderEngine()
indicator = RegionIndicator()
hotkey = GlobalHotkey()
audio = AudioRecorder()
notifier = Notifier()


def now_tag():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _find_icon():
    """查找图标文件：exe 内置(_MEIPASS)优先，其次 exe/脚本旁。"""
    if getattr(sys, "frozen", False):
        mp = Path(getattr(sys, "_MEIPASS", ""))
        if mp.is_dir():
            for n in ("icon.ico", "icon.png"):
                p = mp / n
                if p.exists():
                    return p
    for n in ("icon.ico", "icon.png"):
        p = BASE_DIR / n
        if p.exists():
            return p
    return None


def make_icon():
    """托盘图标：优先使用内置 icon.ico/icon.png，缺失时退回自画红点。"""
    icon_path = _find_icon()
    if icon_path is not None:
        try:
            img = Image.open(icon_path).convert("RGBA")
            img = img.resize((64, 64), Image.LANCZOS)
            return img
        except Exception:
            pass
    # fallback：自画红点
    img = Image.new("RGB", (64, 64), (36, 36, 36))
    d = ImageDraw.Draw(img)
    d.ellipse([14, 14, 50, 50], fill=(217, 48, 45))
    d.ellipse([25, 25, 39, 39], fill=(255, 255, 255))
    return img


def _set_title(icon, text):
    """跨线程设置托盘标题（异常时静默，避免拖垮录制流程）。"""
    try:
        icon.title = text
    except Exception:
        pass


# ===== 录制操作 =====
def _start(icon):
    global recording, output_path
    if recording:
        notifier.notify("已在录制")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUTPUT_DIR / f"rec_{now_tag()}"
    output_path = str(base) + ".mp4"           # 最终文件
    video_tmp = str(base) + ".video.tmp.mp4"   # 临时视频（无音轨）
    wav_tmp = str(base) + ".audio.tmp.wav"     # 临时音频
    try:
        engine.start(video_tmp, FPS, REGION)
        ok_audio = audio.start(wav_tmp)
        recording = True
        # 指示器红框画在区域外沿，不进画面，录制中持续显示区域
        _set_title(icon, "小鹅录屏 - 录制中")
        if ok_audio:
            notifier.notify("开始录制")
        else:
            notifier.notify("开始录制（无声音）")
    except Exception as e:
        sys.stderr.write(f"[rec] 启动录制失败: {e}\n")
        notifier.notify("录制启动失败")


def _log(msg):
    """诊断日志（exe 无控制台，写到 exe 旁的 rec.log）。"""
    try:
        with open(BASE_DIR / "rec.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _stop(icon, silent_if_idle=True):
    global recording
    if not recording:
        if not silent_if_idle:
            notifier.notify("未在录制")
        return
    final = output_path
    vtmp = output_path[:-4] + ".video.tmp.mp4"
    wtmp = output_path[:-4] + ".audio.tmp.wav"
    _log("stop 开始")
    try:
        engine.stop()
        _log("engine.stop 完成")
        audio.stop()
        _log("audio.stop 完成")
    except Exception as e:
        _log(f"停止异常: {e}")
        notifier.notify("停止录制出错")
    finally:
        recording = False
        if REGION is not None:
            indicator.set_region(REGION)  # 停止录制后恢复选区指示器
        _set_title(icon, "小鹅录屏")

    # 音视频合并：有音频且合并成功 -> 最终带声音文件；否则保留无音轨视频
    if audio.has_data and mux_video_audio(vtmp, wtmp, final):
        _log("合并成功")
        for f in (vtmp, wtmp):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        saved = "已保存"
    else:
        _log("合并失败/无音频，走 fallback")
        if os.path.exists(vtmp):
            try:
                os.replace(vtmp, final)
            except OSError:
                pass
        if os.path.exists(wtmp):
            try:
                os.remove(wtmp)
            except OSError:
                pass
        saved = "已保存（无声音）"

    # 归档：录完自动剪切到指定目录（未设置则留在原处）
    if ARCHIVE_DIR and os.path.exists(final):
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            dest = str(ARCHIVE_DIR / Path(final).name)
            if os.path.abspath(dest) != os.path.abspath(final):
                import shutil
                shutil.move(final, dest)
                notifier.notify(f"{saved}并归档")
                return
        except Exception as e:
            notifier.notify(f"{saved}（归档失败）")
            return
    notifier.notify(saved)


def on_start(icon, item):
    _start(icon)


def on_stop(icon, item):
    _stop(icon, silent_if_idle=False)


def _toggle_pause(icon):
    """暂停 / 继续录制（画面与声音同步暂停）。暂停时长不计入视频。"""
    if not recording:
        notifier.notify("未在录制")
        return
    if engine.paused:
        engine.resume()
        audio.resume()
        _set_title(icon, "小鹅录屏 - 录制中")
        notifier.notify("继续录制")
    else:
        engine.pause()
        audio.pause()
        _set_title(icon, "小鹅录屏 - 已暂停")
        notifier.notify("已暂停录制")
    try:
        icon.update_menu()
    except Exception:
        pass


def on_pause(icon, item):
    _toggle_pause(icon)


def on_open(icon, item):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(OUTPUT_DIR))  # Windows 专用
    except Exception as e:
        notifier.notify("打开文件夹失败")


def _tk_icon(root):
    """给 tkinter 窗口挂上程序图标（避免默认 Tk 羽毛笔图标）。"""
    ico = _find_icon()
    if ico is not None and ico.suffix.lower() == ".ico":
        try:
            root.iconbitmap(str(ico))
        except Exception:
            pass


def on_change_dir(icon, item):
    """设置归档目录：录制停止后代码自动把文件剪切到这里（记忆到 config.json）。"""
    global ARCHIVE_DIR
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    _tk_icon(root)
    try:
        d = filedialog.askdirectory(
            title="选择归档目录（录完自动剪切到这里）",
            initialdir=str(ARCHIVE_DIR) if ARCHIVE_DIR else str(OUTPUT_DIR),
        )
    finally:
        root.destroy()
    if not d:
        notifier.notify("已取消")
        return
    try:
        ARCHIVE_DIR = Path(d)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        notifier.notify("目录不可用")
        return
    save_config({"output_dir": str(ARCHIVE_DIR)})
    notifier.notify("已设置归档目录")
    try:
        icon.update_menu()
    except Exception:
        pass


def on_quit(icon, item):
    _stop(icon, silent_if_idle=True)
    hotkey.close()
    notifier.close()
    indicator.close()
    icon.stop()


# ===== 录制区域 =====
def on_region_full(icon, item):
    global REGION
    if recording:
        notifier.notify("先停止再改区域")
        return
    REGION = None
    indicator.set_region(None)
    notifier.notify("区域: 全屏")


def pick_region(icon):
    """全屏半透明框选器，返回 (left, top, width, height) 或 None(取消)。"""
    import tkinter as tk
    from tkinter import messagebox

    try:
        with mss.mss() as sct:
            m = sct.monitors[1]  # 主屏
            sw, sh = m["width"], m["height"]
    except Exception:
        return None

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-alpha", 0.30)
    root.geometry(f"{sw}x{sh}+0+0")
    root.attributes("-topmost", True)
    _tk_icon(root)

    cv = tk.Canvas(root, bg="black", cursor="cross", highlightthickness=0)
    cv.pack(fill="both", expand=True)

    state = {"x0": 0, "y0": 0, "result": None, "done": False}

    def on_press(e):
        state["x0"], state["y0"] = e.x, e.y
        cv.delete("sel")
        cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#d9302d", width=2, tags="sel")

    def on_drag(e):
        cv.delete("sel")
        cv.create_rectangle(state["x0"], state["y0"], e.x, e.y,
                            outline="#d9302d", width=2, tags="sel")

    def on_release(e):
        x0, y0, x1, y1 = state["x0"], state["y0"], e.x, e.y
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 20 or h < 20:
            messagebox.showwarning("太小了", "框得太小了，至少 20x20 像素，重新框",
                                   parent=root)
            cv.delete("sel")
            return
        # opencv 编码器要求偶数宽高
        left, top = min(x0, x1), min(y0, y1)
        state["result"] = (left, top, w // 2 * 2, h // 2 * 2)
        state["done"] = True
        root.destroy()

    def on_esc(e):
        state["done"] = True
        root.destroy()

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_drag)
    cv.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_esc)
    root.mainloop()

    return state["result"] if state["done"] else None


def on_region_custom(icon, item):
    global REGION
    if recording:
        notifier.notify("先停止再改区域")
        return
    result = pick_region(icon)
    if result is None:
        notifier.notify("已取消")
        return
    left, top, w, h = result
    REGION = {"left": left, "top": top, "width": w, "height": h}
    indicator.set_region(REGION)
    notifier.notify(f"区域: {w}x{h}")


# ===== 帧率 =====
def make_set_fps(fps):
    def _set(icon, item):
        global FPS
        FPS = fps
        notifier.notify(f"{fps} fps")
    return _set


def make_menu():
    return pystray.Menu(
        pystray.MenuItem("开始录制 (F9)", on_start, default=True),
        pystray.MenuItem("停止录制 (F9)", on_stop),
        pystray.MenuItem(
            lambda item: "继续录制 (F10)" if engine.paused else "暂停录制 (F10)",
            on_pause,
            enabled=lambda item: recording,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("录制区域", pystray.Menu(
            pystray.MenuItem("全屏", on_region_full,
                             checked=lambda item: REGION is None),
            pystray.MenuItem("自定义区域...", on_region_custom,
                             checked=lambda item: REGION is not None),
        )),
        pystray.MenuItem("帧率", pystray.Menu(
            *[pystray.MenuItem(f"{f} fps", make_set_fps(f),
                               checked=lambda item, f=f: FPS == f)
              for f in (10, 15, 20, 30)]
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("归档目录", pystray.Menu(
            pystray.MenuItem(
                lambda item: f"当前: {ARCHIVE_DIR}" if ARCHIVE_DIR else "未设置（留在 recordings）",
                None, enabled=False),
            pystray.MenuItem("录完自动剪切到...", on_change_dir),
        )),
        pystray.MenuItem("打开输出文件夹", on_open),
        pystray.MenuItem("退出", on_quit),
    )


def main():
    indicator.start()
    notifier.start()
    icon = pystray.Icon("xiaoe_recorder", make_icon(), "小鹅录屏", make_menu())

    # 全局快捷键：F9(0x78) 开始/停止录制，F10(0x79) 暂停/继续。
    # 钩子回调只投递指令，由独立 worker 线程执行——
    # 在低级键盘钩子回调上下文里直接做录制操作会导致 MSMF 初始化失败。
    hotkey_actions = queue.Queue()

    def hotkey_worker():
        while True:
            fn = hotkey_actions.get()
            try:
                fn()
            except Exception:
                sys.stderr.write("[rec] 热键动作异常:\n")
                import traceback
                traceback.print_exc()

    threading.Thread(target=hotkey_worker, daemon=True).start()

    # 防抖：keybd_event/系统偶发重复 down 事件，500ms 内忽略。
    # F9/F10 各自独立计时，互不吞键。
    debounce = {"rec": 0.0, "pause": 0.0}

    def debounced(key, fn):
        def wrapper():
            now = time.time()
            if now - debounce[key] < 0.5:
                return
            debounce[key] = now
            fn()
        return wrapper

    def hotkey_record():
        hotkey_actions.put(
            lambda: _stop(icon, silent_if_idle=False) if recording else _start(icon)
        )

    def hotkey_pause():
        hotkey_actions.put(lambda: recording and _toggle_pause(icon))

    hotkey.start({
        0x78: debounced("rec", hotkey_record),
        0x79: debounced("pause", hotkey_pause),
    })

    icon.run()


if __name__ == "__main__":
    main()
