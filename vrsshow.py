import sys
import os
import time
import queue
import threading
import argparse
from datetime import datetime, timedelta

import tkinter as tk

import cv2 as cv
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

import psutil

from LogLibrary import Load_Config, Loguru_Logging

# ----------------------- Configuration Values -----------------------
Program_Name = "VRS_Show"        # Program name for identification and logging.
Program_Version = "1.8.0"        # Program version used for file naming and logging.
# ---------------------------------------------------------------------

# Force RTSP over TCP for every cv.VideoCapture in this process and bound the
# socket I/O so a dead/unreachable NVR can't block VideoCapture() forever.
# This must be set BEFORE any VideoCapture is created, so we set it once at
# import time instead of from inside a worker thread (which raced with the
# capture being opened in the previous version).
#   - stimeout is in microseconds (6 s) -> open()/read() abort instead of hanging,
#     which is what makes the loading-screen timeout actually cancel the connect.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;6000000",
)

default_config = {
    # --- Plaza NVR (channel based: cam/playback) ---
    "plaza_nvr_ip": "",
    "plaza_nvr_user": "",
    "plaza_nvr_password": "",
    "plaza_to_channel": {
        "DM35": 1,
        "DM36": 2,
        "TB21": 21,
        "TB22": 22
    },
    # --- DC1 NVR (same channel-based method as Plaza) ---
    "dc1_nvr_ip": "",
    "dc1_nvr_user": "",
    "dc1_nvr_password": "",
    "dc1_to_channel": {
        "DC101": 1,
        "DC102": 2
    },
    # --- DC2 NVR (UID based: recordstream) ---
    "dc2_nvr_ip": "",
    "dc2_lane_to_uid": {
        "AN01": "a1b2c3d4-e5f6-...",
        "AN02": "b2c3d4e5-f6a7-...",
        "TC01": "c3d4e5f6-a7b8-..."
    },
    # --- Playback timing (applies to all NVRs) ---
    "start_offset_seconds": 0,   # เริ่มเล่นก่อนเวลาที่ระบุกี่วินาที (ลบออกจากเวลาเริ่ม)
    "play_seconds": 300,         # เล่นไปกี่วินาที
    "log_Level": "DEBUG",
    "Log_Console": 1,      # Set to 1 to enable console logging.
    "log_Backup": 90,      # Log retention duration (number of backup days).
    "Log_Size": "10 MB"    # Maximum log file size before rotation.
}

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)


if getattr(sys, 'frozen', False):
    script_dir = os.path.dirname(sys.executable)
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))

# Define the lock file name in script_dir (must be after script_dir is defined)
LOCK_FILE = os.path.join(script_dir, f"{Program_Name}.lock")
FONT_PATH = os.path.join(script_dir, 'THSarabunNew.ttf')

FONT_WARNING_LOGGED = False

# Cache loaded fonts so we don't re-read THSarabunNew.ttf from disk on every frame.
_FONT_CACHE = {}


def _get_font(font_path, font_size):
    """Return a cached ImageFont, loading from disk only once per (path, size)."""
    key = (font_path, font_size)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(font_path, font_size)
        _FONT_CACHE[key] = font
    return font


def putText_Thai(image, text, position, font_path, font_size, color):
    """
    Draw Thai text on an OpenCV image using a Pillow-rendered TrueType font.
    Falls back to OpenCV's built-in font if the .ttf cannot be loaded.
    """
    global FONT_WARNING_LOGGED

    try:
        font = _get_font(font_path, font_size)
    except OSError:
        if not FONT_WARNING_LOGGED:
            logger.warning(
                f"Font file not found or unreadable: {font_path}. "
                f"Falling back to OpenCV text rendering."
            )
            FONT_WARNING_LOGGED = True
        cv.putText(image, text, position, cv.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv.LINE_AA)
        return image

    pil_image = Image.fromarray(cv.cvtColor(image, cv.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_image)
    draw.text(position, text, font=font, fill=color)
    return cv.cvtColor(np.array(pil_image), cv.COLOR_RGB2BGR)


def getplaytime(dt):
    """Convert local time to Zulu Time (UTC) for use in the DC NVR URL."""
    logger.debug(f"Converting playtime. Original (Local): {dt.strftime('%Y-%m-%d %H:%M:%S')}")
    dt_utc = dt - timedelta(hours=7)
    converted_date_zulu = dt_utc.strftime("%Y%m%dT%H%M%SZ")
    logger.debug(f"Converted to Zulu Time (UTC): {converted_date_zulu}")
    return converted_date_zulu


# ----------------------------- Threading helpers -----------------------------

def open_capture_async(rtsp_url):
    """
    Open a cv.VideoCapture in a background thread so the UI stays responsive.

    Returns:
        (thread, state) where `state` is a dict guarded by state['lock']:
          state['cap']       -> the opened VideoCapture, or None
          state['abandoned'] -> set True by the caller after a timeout/cancel

    If the caller abandons the attempt (timeout), the worker releases the
    capture as soon as it finishes opening, so a slow connect that completes
    after we gave up never leaks a VideoCapture handle.
    """
    state = {"cap": None, "abandoned": False, "lock": threading.Lock()}

    def task():
        cap = cv.VideoCapture(rtsp_url, cv.CAP_FFMPEG)
        # Keep the internal buffer tiny so we display the freshest frame and
        # don't accumulate latency over a long playback session.
        try:
            cap.set(cv.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        opened = cap.isOpened()
        with state["lock"]:
            if opened and not state["abandoned"]:
                state["cap"] = cap
                cap = None  # ownership handed to the caller
        if cap is not None:
            cap.release()  # failed to open, or the caller already gave up

    thread = threading.Thread(target=task, daemon=True)
    thread.start()
    return thread, state


def abandon_capture(state):
    """Give up on an async capture: release it now or as soon as it opens."""
    with state["lock"]:
        state["abandoned"] = True
        cap = state["cap"]
        state["cap"] = None
    if cap is not None:
        cap.release()


class ThreadedVideoStream:
    """
    Decouples frame decoding (network + ffmpeg) from rendering.

    A background thread continuously reads frames into a bounded queue while the
    main thread only does cv.imshow / waitKey. This keeps the display smooth
    even when a single read() stalls on the network, and bounded back-pressure
    prevents unbounded memory growth.
    """

    _SENTINEL = object()  # marks end-of-stream in the queue

    def __init__(self, cap, queue_size=16):
        self.cap = cap
        self.q = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _reader(self):
        # The reader thread is the SOLE owner of self.cap once started: it is
        # the only thread that calls read() and the only one that release()s it
        # (in finally). This avoids releasing the capture from stop() while a
        # read() is still in flight, which is undefined behaviour in FFmpeg.
        try:
            while not self._stop.is_set():
                ret, frame = self.cap.read()
                if not ret:
                    # Signal end-of-stream and exit the reader thread.
                    try:
                        self.q.put(self._SENTINEL, timeout=0.5)
                    except queue.Full:
                        pass
                    return
                # Block (with back-pressure) until there is room, but stay
                # responsive to stop requests.
                while not self._stop.is_set():
                    try:
                        self.q.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            self.cap.release()

    def read(self, timeout=2.0):
        """
        Return (ok, frame).
          ok=True  -> frame is a valid image
          ok=False -> stream ended (frame is None)
        Raises queue.Empty handling internally; on timeout returns (True, None)
        meaning "no frame yet, still alive".
        """
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return True, None
        if item is self._SENTINEL:
            return False, None
        return True, item

    def stop(self):
        # Only signal + join. The reader thread releases self.cap itself once it
        # leaves read(); the FFmpeg stimeout bounds how long that takes. We never
        # release the capture here to avoid a release/read race.
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=7.0)


# ------------------------------ Tkinter GUI player ------------------------------

# Custom dark theme palette (Catppuccin-inspired) for the player window.
_THEME = {
    "bg": "#1e1e2e",
    "panel": "#181825",
    "video_bg": "#11111b",
    "text": "#cdd6f4",
    "muted": "#9399b2",
    "pause": "#a6e3a1",   # green
    "reload": "#fab387",  # orange
    "stop": "#f38ba8",    # red
    "accent": "#89b4fa",  # blue
}

# Thai-capable UI font (Tahoma ships with Windows and renders Thai well).
_UI_FONT = "Tahoma"


def _lighten(hex_color, amount=0.18):
    """Blend a hex color toward white (for button hover feedback)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _make_loading_base():
    """Render the static Thai loading text once onto a black 16:9 frame."""
    base = np.zeros((480, 854, 3), dtype=np.uint8)
    return putText_Thai(
        image=base,
        text="กำลังโหลดข้อมูล กรุณารอสักครู่",
        position=(255, 255),
        font_path=FONT_PATH,
        font_size=30,
        color=(255, 255, 255),
    )


class VideoPlayerGUI:
    """
    Custom-styled Tkinter player with real on-screen Pause / Reload / Stop
    buttons (plus q/p/space/r keyboard shortcuts).

    Frame decoding stays on the ThreadedVideoStream background thread; the Tk
    main loop only pulls already-decoded frames via after() and repaints, so the
    UI never blocks on the network. A generation counter cancels stale after()
    callbacks across reloads. Closing the window or pressing Stop always tears
    down the capture (the RTSP connection is closed).

    run() returns 'ok' if it connected and played at least once, else 'failed'.
    """

    def __init__(self, rtsp_url, timeout, title, max_seconds=0):
        self.rtsp_url = rtsp_url
        self.timeout = timeout
        self.max_seconds = max(0, int(max_seconds or 0))  # 0 = unlimited

        self.conn_thread = None
        self.state = None
        self.stream = None
        self.delay = 40

        self.gen = 0                 # bumps each (re)connect; cancels old callbacks
        self.connect_deadline = 0.0
        self.spinner_angle = 0
        self._loading_base = None

        self.paused = False
        self.ended = False
        self.connected = False
        self.closing = False
        self.play_start = 0.0
        self._pause_started = 0.0
        self.last_frame = None
        self._imgtk = None
        self.result = "failed"

        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=_THEME["bg"])
        self.root.geometry("960x640")
        self.root.minsize(560, 420)
        self._build_ui(title)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<space>", lambda e: self._toggle_pause())
        self.root.bind("p", lambda e: self._toggle_pause())
        self.root.bind("r", lambda e: self._reload())
        self.root.bind("q", lambda e: self._on_close())
        self.root.bind("<Escape>", lambda e: self._on_close())

    # ---- UI construction ----
    def _build_ui(self, title):
        self.header = tk.Label(self.root, text=title, bg=_THEME["panel"],
                               fg=_THEME["text"], anchor="w", padx=14, pady=8,
                               font=(_UI_FONT, 12, "bold"))
        self.header.pack(side="top", fill="x")

        self.video = tk.Label(self.root, bg=_THEME["video_bg"])
        self.video.pack(side="top", fill="both", expand=True)

        bar = tk.Frame(self.root, bg=_THEME["panel"])
        bar.pack(side="bottom", fill="x")

        self.btn_pause = self._button(bar, "⏸  หยุดชั่วคราว", _THEME["pause"], self._toggle_pause)
        self.btn_reload = self._button(bar, "⟳  โหลดใหม่", _THEME["reload"], self._reload)
        self.btn_stop = self._button(bar, "■  หยุด/ปิด", _THEME["stop"], self._on_close)
        self.btn_pause.pack(side="left", padx=(14, 6), pady=10)
        self.btn_reload.pack(side="left", padx=6, pady=10)
        self.btn_stop.pack(side="left", padx=6, pady=10)

        self.status = tk.Label(bar, text="กำลังเชื่อมต่อ...", bg=_THEME["panel"],
                               fg=_THEME["muted"], font=(_UI_FONT, 11))
        self.status.pack(side="right", padx=16)

    def _button(self, parent, text, color, command):
        btn = tk.Button(parent, text=text, command=command,
                        bg=color, fg=_THEME["bg"], activebackground=_lighten(color),
                        activeforeground=_THEME["bg"], relief="flat", bd=0,
                        font=(_UI_FONT, 11, "bold"), padx=16, pady=8,
                        cursor="hand2", highlightthickness=0)
        btn.bind("<Enter>", lambda e: btn.configure(bg=_lighten(color)))
        btn.bind("<Leave>", lambda e: btn.configure(bg=color))
        return btn

    # ---- lifecycle ----
    def run(self):
        self._start_connect()
        self.root.mainloop()
        return self.result

    def _start_connect(self):
        self.gen += 1
        g = self.gen
        self.connected = False
        self.ended = False
        self.paused = False
        try:
            self.btn_pause.configure(text="⏸  หยุดชั่วคราว")
        except tk.TclError:
            return
        self._set_status("กำลังเชื่อมต่อ...")
        if self._loading_base is None:
            self._loading_base = _make_loading_base()
        self.conn_thread, self.state = open_capture_async(self.rtsp_url)
        self.connect_deadline = time.time() + self.timeout
        self._poll_connect(g)

    def _poll_connect(self, g):
        if self.closing or g != self.gen:
            return
        if not self.conn_thread.is_alive():
            with self.state["lock"]:
                cap = self.state["cap"]
                self.state["cap"] = None
            if cap is not None:
                self._begin_playback(cap, g)
            else:
                self._fail("เชื่อมต่อไม่สำเร็จ")
            return
        if time.time() > self.connect_deadline:
            abandon_capture(self.state)
            self._fail(f"หมดเวลาเชื่อมต่อ ({int(self.timeout)} วินาที)")
            return

        frame = self._loading_base.copy()
        cv.ellipse(frame, (427, 200), (26, 26), 0, self.spinner_angle,
                   self.spinner_angle + 270, (200, 200, 200), 4)
        self.spinner_angle = (self.spinner_angle + 20) % 360
        self._render(frame)
        self.root.after(80, lambda: self._poll_connect(g))

    def _begin_playback(self, cap, g):
        fps = cap.get(cv.CAP_PROP_FPS)
        # RTSP often reports 0 or garbage; fall back to 25 fps so recorded
        # playback is paced to real time instead of fast-forwarding.
        if not (1 < fps <= 120):
            fps = 25.0
        self.delay = max(1, int(1000 / fps))
        self.stream = ThreadedVideoStream(cap).start()  # cap released inside it
        self.connected = True
        self.result = "ok"
        self.play_start = time.time()
        logger.info(f"GUI playback started (fps={fps:.1f}, delay={self.delay} ms).")
        self._set_status("กำลังเล่น")
        self._tick(g)

    def _tick(self, g):
        if self.closing or g != self.gen:
            return
        if self.paused:
            if self.last_frame is not None:
                self._render(self.last_frame, overlay="PAUSED")
            self.root.after(80, lambda: self._tick(g))
            return
        if self.ended:
            self.root.after(120, lambda: self._tick(g))
            return

        # Auto-stop after the configured play length. DC2 (recordstream) has no
        # end time in the URL, so the GUI counts elapsed play time itself and
        # closes the connection here; for channel-based NVRs this is a backstop.
        if self.max_seconds and (time.time() - self.play_start) >= self.max_seconds:
            self.ended = True
            logger.info(f"GUI: reached play limit ({self.max_seconds}s) — closing connection.")
            self._teardown_stream()  # release cap -> RTSP connection closed
            self._set_status(f"เล่นครบ {int(self.max_seconds)} วินาที — ปิดการเชื่อมต่อแล้ว (กด ⟳ เริ่มใหม่)")
            self.root.after(120, lambda: self._tick(g))
            return

        ok, frame = self.stream.read(timeout=0.01)
        if not ok:
            self.ended = True
            self._set_status("จบการเล่น — กด ⟳ โหลดใหม่ เพื่อเริ่มต้นใหม่")
            logger.info("GUI: stream ended.")
            self.root.after(120, lambda: self._tick(g))
            return
        if frame is not None:
            self.last_frame = frame
            self._render(frame)
            self._update_elapsed()
        self.root.after(self.delay, lambda: self._tick(g))

    # ---- rendering ----
    def _render(self, frame_bgr, overlay=None):
        if self.closing:
            return
        tw = self.video.winfo_width()
        th = self.video.winfo_height()
        if tw <= 1 or th <= 1:
            tw, th = 944, 540
        h, w = frame_bgr.shape[:2]
        scale = min(tw / w, th / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

        rgb = cv.cvtColor(frame_bgr, cv.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        if (nw, nh) != (w, h):
            img = img.resize((nw, nh))
        if overlay:
            draw = ImageDraw.Draw(img)
            try:
                draw.text((20, 14), overlay, font=_get_font(FONT_PATH, 42), fill=(243, 139, 168))
            except OSError:
                draw.text((20, 14), overlay, fill=(243, 139, 168))

        imgtk = ImageTk.PhotoImage(img)
        self._imgtk = imgtk  # keep a reference so Tk doesn't garbage-collect it
        try:
            self.video.configure(image=imgtk)
        except tk.TclError:
            pass

    def _update_elapsed(self):
        secs = int(time.time() - self.play_start)
        m, s = divmod(secs, 60)
        self._set_status(f"กำลังเล่น   {m:02d}:{s:02d}")

    def _set_status(self, text):
        try:
            self.status.configure(text=text)
        except tk.TclError:
            pass

    # ---- controls ----
    def _toggle_pause(self):
        if not self.connected or self.ended:
            return
        self.paused = not self.paused
        # Keep the play-time counter (for max_seconds / elapsed) honest by not
        # counting time spent paused.
        if self.paused:
            self._pause_started = time.time()
        elif self._pause_started:
            self.play_start += time.time() - self._pause_started
            self._pause_started = 0.0
        try:
            self.btn_pause.configure(text="▶  เล่นต่อ" if self.paused else "⏸  หยุดชั่วคราว")
        except tk.TclError:
            pass
        self._set_status("⏸ หยุดชั่วคราว (ค้างเฟรมล่าสุด)" if self.paused else "กำลังเล่น")
        logger.info("GUI paused (frozen on latest frame)." if self.paused else "GUI resumed.")

    def _reload(self):
        if self.closing:
            return
        logger.info("GUI reload: restarting from the beginning.")
        self._teardown_stream()
        self.last_frame = None
        self._start_connect()

    def _fail(self, msg):
        self._set_status(msg)
        logger.error(f"GUI: {msg}")
        # Show the message briefly, then close so the NVR chain can continue.
        self.root.after(700, self._on_close)

    def _teardown_stream(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        if self.state is not None:
            abandon_capture(self.state)

    def _on_close(self):
        if self.closing:
            return
        self.closing = True
        self.gen += 1  # cancel any pending after() callbacks
        logger.info("GUI closing — tearing down connection.")
        self._teardown_stream()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ----------------------------- NVR dispatch helpers -----------------------------
#
# Each handler returns a tri-state:
#   None  -> this NVR does not serve the requested name  (try the next NVR)
#   True  -> connected and played successfully           (stop the chain)
#   False -> this NVR owns the name but failed/cancelled (try the next NVR)
#
# The fallback order is: Plaza NVR -> DC1 NVR -> DC2 NVR.
# The chain stops only on success (True); any failure falls through.

def parse_cli_args():
    parser = argparse.ArgumentParser(description="NVR Playback Tool")
    parser.add_argument("name", help="Plaza/DC1 channel name or DC2 lane name (e.g., DM35, AN01)")
    parser.add_argument("time", help="Start time 'DDMMYYYYHHMMSS'")
    parser.add_argument("duration", nargs='?', type=int, default=None,
                        help="Optional: play length in seconds (overrides config 'play_seconds')")
    return parser.parse_args()


def _connect_and_play(rtsp_url, timeout, video_window, max_seconds=0):
    """Open + play inside the custom Tkinter player.

    Connect (async), loading screen, playback, pause, reload and teardown are
    all handled by VideoPlayerGUI. `max_seconds` auto-stops playback after that
    many seconds of actual playing (0 = unlimited). Returns True if it connected
    and played at least once, otherwise False (so the NVR chain can fall through).
    """
    player = VideoPlayerGUI(rtsp_url, timeout, video_window, max_seconds=max_seconds)
    status = player.run()
    logger.info("================ Program End ==================")
    return status == "ok"


def play_channel_nvr(args, ip, user, password, channel_map, label, offset, play_seconds):
    """Playback from a channel-based NVR (Plaza / DC1 style: cam/playback).

    Start time = requested time minus `offset` seconds; end time = start +
    `play_seconds`. Both come from the config (see run_playback).
    """
    channel_no = channel_map.get(args.name.upper())
    if channel_no is None:
        logger.debug(f"{label}: name '{args.name}' not found in its channel map.")
        return None  # not served here -> try next NVR

    logger.info(f"🔄 {label} serves '{args.name}' (CH: {channel_no}).")
    if not ip:
        logger.error(f"{label}: NVR IP is not configured.")
        return False

    try:
        base_obj = datetime.strptime(args.time, "%d%m%Y%H%M%S")
        start_obj = base_obj - timedelta(seconds=offset)
        end_obj = start_obj + timedelta(seconds=play_seconds)
        starttime_str = start_obj.strftime("%Y_%m_%d_%H_%M_%S")
        endtime_str = end_obj.strftime("%Y_%m_%d_%H_%M_%S")
    except ValueError:
        logger.error("Error: Invalid time format. Please use 'DDMMYYYYHHMMSS'.")
        return False

    logger.info(f"{label}: start -{offset}s @ {starttime_str}, play {play_seconds}s -> {endtime_str}")
    rtsp_url = (
        f"rtsp://{user}:{password}@{ip}:554/cam/playback?"
        f"channel={channel_no}&starttime={starttime_str}&endtime={endtime_str}"
    )
    logger.info(f"Connecting to {label} (CH: {channel_no})...")
    logger.debug(f"URL: {rtsp_url}")

    return _connect_and_play(
        rtsp_url,
        timeout=5.0,
        video_window=f"Playback - {label} {args.name.upper()} (CH {channel_no})",
        max_seconds=play_seconds,
    )


def play_uid_nvr(args, ip, uid_map, label, offset, play_seconds):
    """Playback from a UID-based NVR (DC2 style: recordstream).

    Playtime = requested time minus `offset` seconds. recordstream has no end
    parameter, so the GUI auto-stops after `play_seconds` (max_seconds).
    """
    uid = None
    for key in (args.name, args.name.upper()):
        if key in uid_map:
            uid = uid_map[key]
            break
    if uid is None:
        logger.debug(f"{label}: name '{args.name}' not found in its UID map.")
        return None  # not served here -> try next NVR

    if not str(uid).strip():
        logger.error(f"{label}: UID for '{args.name}' is empty in config.")
        return False

    logger.info(f"🔄 {label} serves '{args.name}' (UID: {uid}).")
    if not ip:
        logger.error(f"{label}: NVR IP is not configured.")
        return False

    try:
        playtime_obj = datetime.strptime(args.time, "%d%m%Y%H%M%S") - timedelta(seconds=offset)
    except ValueError:
        logger.error("Error: Invalid time format. Please use 'DDMMYYYYHHMMSS'.")
        return False
    playtime_str = getplaytime(playtime_obj)

    logger.info(f"{label}: start -{offset}s, auto-stop after {play_seconds}s")
    rtsp_url = f"rtsp://{ip}/recordstream?streamid={uid}&playtime={playtime_str}"
    logger.info(f"Connecting to {label} (UID: {uid})...")
    logger.debug(f"URL: {rtsp_url}")

    return _connect_and_play(
        rtsp_url,
        timeout=10.0,
        video_window=f"Playback - {label} {args.name}",
        max_seconds=play_seconds,
    )


def run_playback(config):
    """Locate the requested name across Plaza -> DC1 -> DC2 and play it."""
    args = parse_cli_args()

    # Playback timing from config. The optional CLI `duration` overrides
    # play_seconds when provided. Both are clamped to sane non-negative values.
    offset = max(0, int(config.get("start_offset_seconds", 0) or 0))
    cfg_play = int(config.get("play_seconds", 300) or 300)
    play_seconds = args.duration if args.duration is not None else cfg_play
    play_seconds = max(1, int(play_seconds))
    logger.info(f"Playback timing: start_offset=-{offset}s, play_seconds={play_seconds}s")

    # 1) Plaza NVR (channel based)
    result = play_channel_nvr(
        args,
        config.get("plaza_nvr_ip"),
        config.get("plaza_nvr_user"),
        config.get("plaza_nvr_password"),
        config.get("plaza_to_channel", {}),
        "Plaza NVR",
        offset, play_seconds,
    )

    # 2) DC1 NVR (same channel-based method as Plaza)
    if not result:
        result = play_channel_nvr(
            args,
            config.get("dc1_nvr_ip"),
            config.get("dc1_nvr_user"),
            config.get("dc1_nvr_password"),
            config.get("dc1_to_channel", {}),
            "DC1 NVR",
            offset, play_seconds,
        )

    # 3) DC2 NVR (UID based). Falls back to legacy "dc_nvr_ip"/"lane_to_uid" keys.
    #    An empty IP must stay empty (NOT default to 127.0.0.1) so the
    #    "NVR IP is not configured" guard in play_uid_nvr can fire.
    if not result:
        dc2_ip = config.get("dc2_nvr_ip") or config.get("dc_nvr_ip", "")
        dc2_map = config.get("dc2_lane_to_uid") or config.get("lane_to_uid", {})
        result = play_uid_nvr(args, dc2_ip, dc2_map, "DC2 NVR", offset, play_seconds)

    if not result:
        logger.error(f"Could not play '{args.name}' from any NVR (Plaza / DC1 / DC2).")

    return result


# ------------------------------- Singleton lock -------------------------------

def _is_our_process(p):
    """True if process `p` looks like another instance of this program.

    Guards against PID reuse: a stale lock may point at a PID the OS has since
    handed to an unrelated process, which we must NOT kill.
    """
    try:
        tokens = [p.name() or ""] + list(p.cmdline() or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    haystack = " ".join(tokens).lower()
    return Program_Name.lower() in haystack or "vrsshow" in haystack


def handle_singleton_lock():
    """Ensure only one instance runs; kill a stale/hanging instance if found."""
    logger.info("Checking for duplicate instances...")

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())

            if psutil.pid_exists(old_pid):
                try:
                    p = psutil.Process(old_pid)
                    if old_pid == os.getpid() or not _is_our_process(p):
                        # PID reused by something unrelated (or it's us) -> do not kill.
                        logger.info(f"PID {old_pid} is not a {Program_Name} instance; leaving it alone.")
                    else:
                        logger.warning(f"Found a hanging instance of {Program_Name} (PID: {old_pid}). Terminating it...")
                        p.kill()
                        p.wait(timeout=5)
                        logger.info(f"Old instance (PID: {old_pid}) has been terminated.")
                except psutil.NoSuchProcess:
                    logger.info("Old instance terminated itself before it could be killed.")
                except Exception as e:
                    logger.error(f"An error occurred while terminating the old instance: {e}")
            else:
                logger.info("Found a stale lock file, but the process is not running.")

        except (ValueError, FileNotFoundError):
            logger.warning("Lock file is corrupted or could not be read. A new one will be created.")

        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

    # Create the lock atomically so two instances launched at the same moment
    # can't both believe they won the race. Returns True only if WE own the lock.
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        logger.info(f"Lock file created successfully (PID: {os.getpid()}).")
        return True
    except FileExistsError:
        logger.warning("Another instance acquired the lock concurrently; not overwriting it.")
        return False
    except Exception as e:
        logger.error(f"Could not create lock file: {e}")
        return False


if __name__ == "__main__":
    lock_owned = False
    try:
        lock_owned = handle_singleton_lock()
        if not lock_owned:
            logger.error("Another instance is already running. Exiting.")
        else:
            run_playback(config)

    except Exception as e:
        logger.error(f"Main Program Error : {e}")

    finally:
        # Only remove the lock if WE own it, so we never delete another
        # instance's lock after losing the startup race.
        if lock_owned and os.path.exists(LOCK_FILE):
            try:
                os.remove(LOCK_FILE)
                logger.info("Lock file removed. Program has exited cleanly.")
            except OSError:
                pass
