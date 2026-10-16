#!/usr/bin/env python3
"""
imla - push-to-talk dictation for Windows.

Hit the hotkey, talk, hit it again. The recording gets transcribed and pasted
into whatever window has focus. Whisper runs locally on the GPU by default;
there's an OpenAI-compatible HTTP backend as an option.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import glob
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
import winsound
from ctypes import wintypes
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import sounddevice as sd

CONFIG_PATH = Path(os.environ["APPDATA"]) / "imla" / "config.json"
WORDS_PATH = CONFIG_PATH.parent / "words.txt"

WORDS_TEMPLATE = """\
# imla vocabulary - names and terms Whisper should know.
#
# Lines starting with # are ignored. Write your words BELOW the dashed line,
# one per line, without a leading #:
#
#     Word
#     Word = how Whisper misspells it, another misspelling
#
# The part after '=' is the safety net: imla replaces those spellings with
# the word, whatever their case. Read on every transcription, no restart.
# ---------------------------------------------------------------------------
"""

DEFAULT_CONFIG = {
    "backend": "local",                    # local | api

    "local_model": "large-v3",
    "local_device": "auto",                # auto | cuda | cpu
    # Same speed as float16, half the VRAM (~2.2 GB vs ~4 GB).
    "local_compute_type": "int8_float16",
    "local_beam_size": 5,

    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "api_model": "gpt-4o-transcribe",

    "language": "",
    "prompt": "",
    # Ctrl+Alt+Space is taken here, and Win+Space / Win+H / Win+Alt+D are Windows'.
    "hotkey": "ctrl+shift+space",
    "input_device": None,
    "max_seconds": 300,
    "cleanup": False,                      # api backend only
    "cleanup_model": "gpt-4o-mini",
    "paste": True,
    "sounds": True,
}

CLEANUP_SYSTEM_PROMPT = (
    "You clean up dictated speech. Remove filler words, false starts and "
    "repeated words. Fix punctuation and capitalisation. Do not answer, "
    "summarise, translate or add anything - return only the corrected text."
)


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


# A toast needs an AppID Windows already knows about, so borrow PowerShell's
# rather than registering one in the Start Menu.
TOAST_APP_ID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                r"\WindowsPowerShell\v1.0\powershell.exe")


def toast(title: str, body: str) -> None:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]>$null\n"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
        "ContentType=WindowsRuntime]>$null\n"
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml('<toast duration=\"short\"><visual><binding "
        f'template="ToastGeneric"><text>{esc(title)}</text>'
        f"<text>{esc(body)}</text></binding></visual></toast>')\n"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $x\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]"
        f"::CreateToastNotifier('{TOAST_APP_ID}').Show($t)\n"
        # Show() is async; exiting straight away drops the toast.
        "Start-Sleep -Milliseconds 600\n"
    )
    ps = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / \
        "v1.0" / "powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            [str(ps), "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"could not read {CONFIG_PATH}: {exc}")
    cfg["api_key"] = os.environ.get("OPENAI_API_KEY") or cfg["api_key"]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}
    CONFIG_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    if stored.get("api_key"):
        os.system(f'icacls "{CONFIG_PATH}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')


def load_words() -> tuple[list[str], list[tuple[re.Pattern, str]]]:
    """Terms to bias Whisper towards, and alias -> term fixes for afterwards."""
    try:
        lines = WORDS_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return [], []

    terms, fixes = [], []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        term, _, aliases = line.partition("=")
        term = term.strip()
        if not term:
            continue
        terms.append(term)
        for alias in aliases.split(","):
            alias = alias.strip()
            if alias:
                pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
                fixes.append((pattern, term))
    return terms, fixes


def apply_word_fixes(text: str, fixes: list[tuple[re.Pattern, str]]) -> str:
    for pattern, term in fixes:
        text = pattern.sub(term, text)
    return text


def add_word(entry: str) -> None:
    if not WORDS_PATH.exists():
        WORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WORDS_PATH.write_text(WORDS_TEMPLATE, encoding="utf-8")
    existing = WORDS_PATH.read_text(encoding="utf-8-sig")
    sep = "" if existing.endswith("\n") or not existing else "\n"
    with WORDS_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{entry.strip()}\n")


class Recorder:
    """Mono int16 capture into memory."""

    def __init__(self, device=None):
        self.device = device
        self._chunks: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.RawInputStream | None = None
        self.samplerate = 16000
        self.channels = 1

    def _open(self):
        # Fall back through looser formats until the device accepts one.
        info = sd.query_devices(self.device if self.device is not None else sd.default.device[0])
        native_sr = int(info["default_samplerate"])
        native_ch = int(info["max_input_channels"])
        attempts = [(16000, 1), (native_sr, 1), (native_sr, min(native_ch, 2))]

        last_error = None
        for sr, ch in attempts:
            try:
                stream = sd.RawInputStream(
                    samplerate=sr, channels=ch, dtype="int16",
                    device=self.device, blocksize=0, callback=self._callback,
                )
                stream.start()
                self.samplerate, self.channels = sr, ch
                return stream
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"could not open input device: {last_error}")

    def _callback(self, indata, frames, time_info, status):
        self._chunks.put(bytes(indata))

    def start(self) -> None:
        self._chunks = queue.Queue()
        self._stream = self._open()

    def stop(self) -> bytes:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        frames = []
        while True:
            try:
                frames.append(self._chunks.get_nowait())
            except queue.Empty:
                break

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.samplerate)
            wav.writeframes(b"".join(frames))
        return buf.getvalue()

    @staticmethod
    def duration(wav_bytes: bytes) -> float:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate())


def _register_cuda_dlls() -> list[str]:
    # CTranslate2 loads cuBLAS and cuDNN lazily with a plain LoadLibrary, which
    # ignores os.add_dll_directory, so PATH is the only thing that works.
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    dirs = glob.glob(os.path.join(base, "*", "bin"))
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ["PATH"]
    return dirs


class LocalTranscriber:
    """Whisper on this machine, via faster-whisper / CTranslate2."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        _register_cuda_dlls()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            die("faster-whisper is not installed. Run: pip install faster-whisper")

        device = cfg["local_device"]
        compute = cfg["local_compute_type"]
        if device == "auto":
            device, compute = self._detect_device(compute)

        try:
            self.model = WhisperModel(cfg["local_model"], device=device, compute_type=compute)
        except Exception as exc:
            if device == "cuda":
                print(f"  GPU init failed ({exc}); falling back to CPU", file=sys.stderr)
                device, compute = "cpu", "int8"
                self.model = WhisperModel(cfg["local_model"], device=device, compute_type=compute)
            else:
                raise
        self.device, self.compute = device, compute

    @staticmethod
    def _detect_device(preferred_compute: str) -> tuple[str, str]:
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", preferred_compute
        except Exception:
            pass
        return "cpu", "int8"

    def describe(self) -> str:
        return f"{self.cfg['local_model']} on {self.device} ({self.compute})"

    def transcribe(self, wav_bytes: bytes) -> str:
        terms, fixes = load_words()
        # hotwords reach every 30 s window; initial_prompt only the first.
        segments, _info = self.model.transcribe(
            io.BytesIO(wav_bytes),
            language=self.cfg["language"] or None,
            initial_prompt=self.cfg["prompt"] or None,
            hotwords=", ".join(terms) or None,
            beam_size=self.cfg["local_beam_size"],
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return apply_word_fixes(text, fixes)


class ApiTranscriber:
    """Any OpenAI-compatible /audio/transcriptions endpoint."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        if not cfg["api_key"]:
            die("the api backend needs a key. Set OPENAI_API_KEY or run --setup.")

    def describe(self) -> str:
        host = self.cfg["base_url"].split("//")[-1].split("/")[0]
        return f"{self.cfg['api_model']} via {host}"

    def transcribe(self, wav_bytes: bytes) -> str:
        terms, fixes = load_words()
        prompt = " ".join(p for p in (self.cfg["prompt"], ", ".join(terms)) if p)
        fields = {
            "model": self.cfg["api_model"],
            "response_format": "json",
            "language": self.cfg["language"],
            "prompt": prompt,
        }
        body, content_type = _multipart(fields, "audio.wav", wav_bytes)
        result = _post(
            f"{self.cfg['base_url'].rstrip('/')}/audio/transcriptions",
            body, content_type, self.cfg["api_key"],
        )
        return apply_word_fixes((result.get("text") or "").strip(), fixes)


def make_transcriber(cfg: dict):
    backend = cfg["backend"].lower()
    if backend == "local":
        return LocalTranscriber(cfg)
    if backend == "api":
        return ApiTranscriber(cfg)
    die(f"unknown backend {cfg['backend']!r}; expected 'local' or 'api'")


def _multipart(fields: dict[str, str], filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----imla" + os.urandom(12).hex()
    out = io.BytesIO()
    for name, value in fields.items():
        if value == "":
            continue
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(f"{value}\r\n".encode())
    out.write(f"--{boundary}\r\n".encode())
    out.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    out.write(b"Content-Type: audio/wav\r\n\r\n")
    out.write(payload)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _post(url: str, data: bytes, content_type: str, api_key: str, timeout: int = 120) -> dict:
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", content_type)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} from API: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def cleanup_text(text: str, cfg: dict) -> str:
    payload = json.dumps({
        "model": cfg["cleanup_model"],
        "messages": [
            {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }).encode("utf-8")
    result = _post(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        payload, "application/json", cfg["api_key"], timeout=60,
    )
    try:
        return result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return text


# clipboard and synthetic keystrokes, win32 through ctypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]


def _with_clipboard(fn):
    # Other apps hold the clipboard for a moment at a time, so retry.
    for _ in range(20):
        if user32.OpenClipboard(None):
            try:
                return fn()
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    raise RuntimeError("could not open the clipboard")


def clipboard_get() -> str | None:
    def read():
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    return _with_clipboard(read)


def clipboard_set(text: str) -> None:
    def write():
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        # The clipboard owns the handle now; don't free it.
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    _with_clipboard(write)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C


def _send_keys(events: list[tuple[int, bool]]) -> None:
    """events: (virtual_key, is_keyup) pairs."""
    array = (INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        array[i].type = INPUT_KEYBOARD
        array[i].ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(INPUT))


def _wait_for_modifier_release(timeout: float = 2.0) -> None:
    # Ctrl+V while the hotkey's Alt is still down comes out as Ctrl+Alt+V.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)):
            return
        time.sleep(0.02)


def paste_text(text: str) -> None:
    try:
        previous = clipboard_get()
    except RuntimeError:
        previous = None

    clipboard_set(text)
    _wait_for_modifier_release()
    time.sleep(0.05)
    _send_keys([(VK_CONTROL, False), (ord("V"), False),
                (ord("V"), True), (VK_CONTROL, True)])

    if previous is not None:
        def restore():
            time.sleep(0.6)  # give the target app time to read the clipboard
            try:
                clipboard_set(previous)
            except RuntimeError:
                pass
        threading.Thread(target=restore, daemon=True).start()


MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312

_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    mods, vk = 0, None
    for part in (p.strip().lower() for p in spec.split("+")):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif part in _NAMED_KEYS:
            vk = _NAMED_KEYS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        else:
            raise ValueError(f"unrecognised key in hotkey: {part!r}")
    if vk is None:
        raise ValueError(f"hotkey {spec!r} has no main key")
    if mods == 0:
        raise ValueError(f"hotkey {spec!r} needs at least one modifier")
    return mods | MOD_NOREPEAT, vk


class Imla:
    def __init__(self, cfg: dict, transcriber):
        self.cfg = cfg
        self.transcriber = transcriber
        self.recorder = Recorder(cfg["input_device"])
        self.recording = False
        self.busy = False
        self.lock = threading.Lock()
        self._auto_stop: threading.Timer | None = None

    def beep(self, kind: str) -> None:
        if not self.cfg["sounds"]:
            return
        tones = {
            "start": [(880, 80)], "stop": [(660, 80)],
            "done": [(1175, 60)], "error": [(320, 180), (240, 220)],
            # Rising pair, deliberately unlike "start": loading takes ~12s and
            # the window may be hidden, so this is the only cue.
            "ready": [(660, 90), (990, 120)],
        }
        for freq, ms in tones.get(kind, []):
            winsound.Beep(freq, ms)

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # A log line must never take down a transcription.
            enc = sys.stdout.encoding or "ascii"
            print(line.encode(enc, "replace").decode(enc), flush=True)

    def toggle(self) -> None:
        with self.lock:
            if self.busy:
                self.log("still transcribing the last clip, ignoring")
                return
            if self.recording:
                self._stop()
            else:
                self._start()

    def _start(self) -> None:
        try:
            self.recorder.start()
        except Exception as exc:
            self.log(f"could not start recording: {exc}")
            self.beep("error")
            return
        self.recording = True
        self.log(f"recording ({self.recorder.samplerate} Hz, {self.recorder.channels} ch)")
        self.beep("start")

        if self.cfg["max_seconds"]:
            self._auto_stop = threading.Timer(self.cfg["max_seconds"], self._auto_stop_fired)
            self._auto_stop.daemon = True
            self._auto_stop.start()

    def _auto_stop_fired(self) -> None:
        with self.lock:
            if self.recording:
                self.log(f"hit the {self.cfg['max_seconds']}s limit, stopping")
                self._stop()

    def _stop(self) -> None:
        if self._auto_stop is not None:
            self._auto_stop.cancel()
            self._auto_stop = None
        wav_bytes = self.recorder.stop()
        self.recording = False
        self.busy = True
        self.beep("stop")
        # Off the message loop thread, or the hotkey goes dead while we work.
        threading.Thread(target=self._process, args=(wav_bytes,), daemon=True).start()

    def _process(self, wav_bytes: bytes) -> None:
        try:
            seconds = Recorder.duration(wav_bytes)
            if seconds < 0.4:
                self.log(f"only {seconds:.2f}s of audio, discarding")
                self.beep("error")
                return

            started = time.time()
            text = self.transcriber.transcribe(wav_bytes)
            if not text:
                self.log("no speech detected")
                self.beep("error")
                return

            if self.cfg["cleanup"] and self.cfg["api_key"]:
                text = cleanup_text(text, self.cfg)

            self.log(f"{seconds:.1f}s audio -> {time.time() - started:.1f}s | {text}")

            if self.cfg["paste"]:
                paste_text(text)
            else:
                clipboard_set(text)
                self.log("copied to clipboard")
            self.beep("done")

        except Exception as exc:
            self.log(f"failed: {exc}")
            self.beep("error")
        finally:
            with self.lock:
                self.busy = False

    def run(self) -> None:
        mods, vk = parse_hotkey(self.cfg["hotkey"])
        if not user32.RegisterHotKey(None, 1, mods, vk):
            err = ctypes.get_last_error()
            hint = " (another program already owns it)" if err == 1409 else ""
            die(f"could not register hotkey {self.cfg['hotkey']!r}{hint}")

        device = self.cfg["input_device"]
        mic = sd.query_devices(device if device is not None else sd.default.device[0])["name"]
        print(f"imla ready - {self.cfg['hotkey']} to start and stop")
        print(f"microphone: {mic}")
        print(f"model: {self.transcriber.describe()}")
        print(f"language: {self.cfg['language'] or 'auto-detect'}   "
              f"paste: {'on' if self.cfg['paste'] else 'off'}")
        print("Ctrl+C in this window to quit.\n")

        self.beep("ready")
        toast("imla ready",
              f"{self.cfg['hotkey']} to start and stop  -  {mic}")

        msg = wintypes.MSG()
        user32.GetMessageW.restype = ctypes.c_int
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    self.toggle()
        except KeyboardInterrupt:
            pass
        finally:
            user32.UnregisterHotKey(None, 1)
            print("\nstopped.")


def cmd_devices() -> None:
    default = sd.default.device[0]
    print("Input devices (use the number as input_device):\n")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            mark = "  <- default" if i == default else ""
            print(f"  {i:3d}  [{api}]  {d['name']}{mark}")
    print("\nLeave input_device as null to follow the Windows default.")


def cmd_setup(cfg: dict) -> None:
    print("imla setup - press Enter to keep the current value.\n")

    entered = input(f"Backend, local or api [{cfg['backend']}]: ").strip().lower()
    if entered in ("local", "api"):
        cfg["backend"] = entered

    if cfg["backend"] == "local":
        entered = input(f"Whisper model [{cfg['local_model']}]: ").strip()
        if entered:
            cfg["local_model"] = entered
        entered = input(f"Device, auto/cuda/cpu [{cfg['local_device']}]: ").strip().lower()
        if entered in ("auto", "cuda", "cpu"):
            cfg["local_device"] = entered
    else:
        current = cfg["api_key"]
        shown = f"{current[:7]}...{current[-4:]}" if len(current) > 12 else ("set" if current else "not set")
        entered = input(f"API key [{shown}]: ").strip()
        if entered:
            cfg["api_key"] = entered
        entered = input(f"Base URL [{cfg['base_url']}]: ").strip()
        if entered:
            cfg["base_url"] = entered
        entered = input(f"Model [{cfg['api_model']}]: ").strip()
        if entered:
            cfg["api_model"] = entered

    entered = input(f"Hotkey [{cfg['hotkey']}]: ").strip()
    if entered:
        try:
            parse_hotkey(entered)
            cfg["hotkey"] = entered
        except ValueError as exc:
            print(f"  keeping the old hotkey: {exc}")

    entered = input(f"Language code, or 'auto' [{cfg['language'] or 'auto'}]: ").strip()
    if entered:
        cfg["language"] = "" if entered.lower() == "auto" else entered

    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}")


def cmd_test(cfg: dict, seconds: int) -> None:
    print("Loading model...", flush=True)
    transcriber = make_transcriber(cfg)
    print(f"Ready: {transcriber.describe()}")

    rec = Recorder(cfg["input_device"])
    rec.start()
    print(f"\nRecording for {seconds}s - say something...", flush=True)
    winsound.Beep(880, 80)
    time.sleep(seconds)
    wav_bytes = rec.stop()
    winsound.Beep(660, 80)

    print(f"Captured {Recorder.duration(wav_bytes):.1f}s at {rec.samplerate} Hz. Transcribing...")
    started = time.time()
    text = transcriber.transcribe(wav_bytes)
    if cfg["cleanup"] and cfg["api_key"] and text:
        text = cleanup_text(text, cfg)
    print(f"({time.time() - started:.1f}s)\n\n  {text or '(nothing detected)'}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="imla",
        description="Push-to-talk dictation that pastes into the focused window.",
    )
    parser.add_argument("--setup", action="store_true", help="configure backend, hotkey and model")
    parser.add_argument("--devices", action="store_true", help="list microphones and exit")
    parser.add_argument("--test", nargs="?", const=5, type=int, metavar="SECONDS",
                        help="record for N seconds and print the transcript (default 5)")
    parser.add_argument("--no-paste", action="store_true", help="copy to the clipboard instead of pasting")
    parser.add_argument("--hotkey", help="override the configured hotkey for this run")
    parser.add_argument("--model", help="override the model for this run")
    parser.add_argument("--add-word", metavar="TERM",
                        help="add a term to the vocabulary, e.g. \"Mecmua\" or \"Mecmua = Mejmua\"")
    parser.add_argument("--words", action="store_true", help="list the vocabulary and exit")
    args = parser.parse_args()

    # Redirected stdout is block-buffered, which leaves imla.log looking
    # empty all session under the .vbs launcher, and falls back to cp1252,
    # which can't hold Turkish text - printing one then kills the paste.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    cfg = load_config()
    if args.no_paste:
        cfg["paste"] = False
    if args.hotkey:
        cfg["hotkey"] = args.hotkey
    if args.model:
        cfg["local_model" if cfg["backend"] == "local" else "api_model"] = args.model

    if args.add_word:
        add_word(args.add_word)
        print(f"added to {WORDS_PATH}")
    elif args.words:
        terms, _fixes = load_words()
        print(f"{WORDS_PATH}\n")
        print("\n".join(f"  {t}" for t in terms) or "  (empty)")
    elif args.devices:
        cmd_devices()
    elif args.setup:
        cmd_setup(cfg)
    elif args.test is not None:
        cmd_test(cfg, args.test)
    else:
        print(f"Loading {cfg['local_model']}..." if cfg["backend"] == "local" else "Connecting...",
              flush=True)
        transcriber = make_transcriber(cfg)
        Imla(cfg, transcriber).run()


if __name__ == "__main__":
    main()
