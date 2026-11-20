#!/usr/bin/env python3
"""Voice-activated push-to-talk for League of Legends voice chat.

Listens to your microphone and, while you are actually speaking, holds a
push-to-talk key down for you.  When you stop talking it releases the key.

The detector is not a simple volume meter:

  * rumble/DC is removed and the level is measured only inside the voice band
    (150 Hz - 4 kHz), so fans, hum and low-frequency noise barely register;
  * the gate threshold adapts to your room's noise floor instead of relying on
    a fixed value that breaks when your mic gain changes;
  * a block only counts as speech if most of its energy is in the voice band,
    which rejects broadband noise (keyboard clatter, desk bumps, air hiss);
  * the gate needs a few consistent blocks to open (attack) and keeps the key
    held briefly after you stop (release) so words are not chopped.

Only sends synthetic keystrokes (SendInput with hardware scan codes).  It does
not read game memory, inject DLLs or touch the network.

Usage:
    python voice_ptt.py                # run
    python voice_ptt.py --list-devices # show microphones
    python voice_ptt.py --selftest     # check the detector logic, no mic needed
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import math
import queue
import re
import sys
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "push_to_talk_key": "V",
    "input_device": None,
    "sample_rate": 0,
    "block_ms": 20,
    "only_when_league_focused": True,
    "focus_processes": ["League of Legends.exe", "LeagueClientUx.exe", "LeagueClient.exe"],
    "calibration_seconds": 3.0,
    "verify_seconds": 1.2,
    "open_offset_db": 8.0,
    "close_hysteresis_db": 5.0,
    "absolute_floor_db": -55.0,
    "attack_ms": 60,
    "release_ms": 320,
    "highpass_hz": 120.0,
    "voice_band_low_hz": 150.0,
    "voice_band_high_hz": 4000.0,
    "min_voice_ratio": 0.55,
    "max_flatness": 0.45,
    "min_spectral_bins": 6,
    "min_voicing": 0.3,
    "over_subtraction": 1.0,
    "noise_floor_adapt_rate": 0.02,
    "detector": "auto",
    "vad_threshold": 0.6,
    "vad_release_threshold": 0.35,
    "vad_model": "",
    "voice_commands": True,
    "command_model": "",
    "toggle_hotkey": "F9",
    "recalibrate_hotkey": "F10",
    "show_meter": True,
    "log_file": "",
}

try:
    import numpy as np
except ImportError:  # pragma: no cover - dependency check
    sys.exit(
        "Missing dependency: numpy.\n"
        "Install the requirements first:  pip install -r requirements.txt"
    )

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - dependency check
    sys.exit(
        "Missing dependency: sounddevice.\n"
        "Install the requirements first:  pip install -r requirements.txt"
    )


# --------------------------------------------------------------------------- #
# Win32 key injection
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

CONSOLE_CTRL_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = (CONSOLE_CTRL_HANDLER, wintypes.BOOL)
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL


def _send_inputs(inputs):
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


NAMED_KEYS = {
    "SPACE": 0x20,
    "SPACEBAR": 0x20,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "BACKSPACE": 0x08,
    "SHIFT": 0xA0,
    "LSHIFT": 0xA0,
    "RSHIFT": 0xA1,
    "CTRL": 0xA2,
    "CONTROL": 0xA2,
    "LCTRL": 0xA2,
    "RCTRL": 0xA3,
    "ALT": 0xA4,
    "LALT": 0xA4,
    "RALT": 0xA5,
    "WIN": 0x5B,
    "CAPSLOCK": 0x14,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PGUP": 0x21,
    "PAGEDOWN": 0x22,
    "PGDN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "`": 0xC0,
    "TILDE": 0xC0,
    "GRAVE": 0xC0,
    "-": 0xBD,
    "MINUS": 0xBD,
    "=": 0xBB,
    "EQUALS": 0xBB,
    "[": 0xDB,
    "LBRACKET": 0xDB,
    "]": 0xDD,
    "RBRACKET": 0xDD,
    "\\": 0xDC,
    "BACKSLASH": 0xDC,
    ";": 0xBA,
    "SEMICOLON": 0xBA,
    "'": 0xDE,
    "APOSTROPHE": 0xDE,
    ",": 0xBC,
    "COMMA": 0xBC,
    ".": 0xBE,
    "PERIOD": 0xBE,
    "/": 0xBF,
    "SLASH": 0xBF,
}

# Keys that live in the "extended" scan-code block (the 0xE0 prefix).
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
    0xA3, 0xA5, 0x5B, 0x5C, 0x5D, 0x90, 0x6F, 0x2C,
}

MOUSE_BUTTONS = {
    "MOUSE4": (XBUTTON1, VK_XBUTTON1),
    "MOUSEBUTTON4": (XBUTTON1, VK_XBUTTON1),
    "MBUTTON4": (XBUTTON1, VK_XBUTTON1),
    "XBUTTON1": (XBUTTON1, VK_XBUTTON1),
    "MOUSE5": (XBUTTON2, VK_XBUTTON2),
    "MOUSEBUTTON5": (XBUTTON2, VK_XBUTTON2),
    "MBUTTON5": (XBUTTON2, VK_XBUTTON2),
    "XBUTTON2": (XBUTTON2, VK_XBUTTON2),
}

# Sending these would actually click in game, so they are refused on purpose.
BLOCKED_MOUSE = {
    "MOUSE1", "MOUSE2", "MOUSE3", "LEFTCLICK", "RIGHTCLICK", "MIDDLECLICK", "CLICK",
    "MOUSEBUTTON1", "MOUSEBUTTON2", "MOUSEBUTTON3", "LMB", "RMB", "MMB",
}


def vk_from_name(name):
    """Translate a friendly key name into a Windows virtual-key code."""
    key = name.strip().upper().replace(" ", "").replace("_", "")
    if not key:
        raise ValueError("empty key name")
    if len(key) == 1 and key.isalnum():
        return ord(key)
    if key in NAMED_KEYS:
        return NAMED_KEYS[key]
    if key.startswith("F") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 24:
            return 0x6F + number
    if key.startswith("NUM") and key[3:].isdigit():
        number = int(key[3:])
        if 0 <= number <= 9:
            return 0x60 + number
    raise ValueError(
        f"unknown key {name!r}. Use a letter/number, a name like SPACE/TAB/CAPSLOCK, "
        "F1-F24, or MOUSE4/MOUSE5."
    )


class PushToTalkKey:
    """The synthetic key we hold while the user speaks."""

    def __init__(self, name):
        self.name = name
        upper = name.strip().upper().replace(" ", "").replace("_", "")
        self.mouse_button = None
        self.vk = None
        self.scan = 0
        self.extended = False
        self._down = False

        if upper in BLOCKED_MOUSE:
            raise ValueError(
                f"{name!r} is a real mouse button; simulating it would click in game. "
                "Rebind voice chat to a keyboard key or MOUSE4/MOUSE5."
            )
        if upper in MOUSE_BUTTONS:
            self.mouse_button, self.vk = MOUSE_BUTTONS[upper]
            return

        self.vk = vk_from_name(name)
        self.scan = user32.MapVirtualKeyW(self.vk, 0)  # MAPVK_VK_TO_VSC
        if not self.scan:
            raise ValueError(f"key {name!r} has no hardware scan code and cannot be simulated")
        self.extended = self.vk in EXTENDED_VKS

    # -- state ------------------------------------------------------------

    def is_physically_down(self):
        """True when the user is already holding this key themselves."""
        return bool(user32.GetAsyncKeyState(self.vk) & 0x8000)

    @property
    def is_down(self):
        return self._down

    # -- actions ----------------------------------------------------------

    def press(self):
        if self._down:
            return
        if self.mouse_button is not None:
            _send_inputs(
                [
                    INPUT(
                        type=INPUT_MOUSE,
                        u=_INPUTUNION(
                            mi=MOUSEINPUT(
                                dx=0,
                                dy=0,
                                mouseData=self.mouse_button,
                                dwFlags=MOUSEEVENTF_XDOWN,
                                time=0,
                                dwExtraInfo=0,
                            )
                        ),
                    )
                ]
            )
        else:
            flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if self.extended else 0)
            _send_inputs(
                [
                    INPUT(
                        type=INPUT_KEYBOARD,
                        u=_INPUTUNION(
                            ki=KEYBDINPUT(
                                wVk=0,
                                wScan=self.scan,
                                dwFlags=flags,
                                time=0,
                                dwExtraInfo=0,
                            )
                        ),
                    )
                ]
            )
        self._down = True

    def release(self):
        if not self._down:
            return
        if self.mouse_button is not None:
            _send_inputs(
                [
                    INPUT(
                        type=INPUT_MOUSE,
                        u=_INPUTUNION(
                            mi=MOUSEINPUT(
                                dx=0,
                                dy=0,
                                mouseData=self.mouse_button,
                                dwFlags=MOUSEEVENTF_XUP,
                                time=0,
                                dwExtraInfo=0,
                            )
                        ),
                    )
                ]
            )
        else:
            flags = (
                KEYEVENTF_SCANCODE
                | KEYEVENTF_KEYUP
                | (KEYEVENTF_EXTENDEDKEY if self.extended else 0)
            )
            _send_inputs(
                [
                    INPUT(
                        type=INPUT_KEYBOARD,
                        u=_INPUTUNION(
                            ki=KEYBDINPUT(
                                wVk=0,
                                wScan=self.scan,
                                dwFlags=flags,
                                time=0,
                                dwExtraInfo=0,
                            )
                        ),
                    )
                ]
            )
        self._down = False


def install_abort_release(get_key):
    """Release the held key if the window is closed or the PC logs off.

    Windows keeps an injected key logically down until a matching key-up is
    sent, so a hard kill would leave voice chat transmitting forever.  Takes a
    callable so the key can be swapped at runtime from the UI.
    """

    def handler(event):
        try:
            key = get_key() if callable(get_key) else get_key
            if key is not None and key.is_down:
                key.release()
        except Exception:
            pass
        # Ctrl+C / Ctrl+Break still go through Python's normal shutdown.
        return event not in (CTRL_C_EVENT, CTRL_BREAK_EVENT)

    callback = CONSOLE_CTRL_HANDLER(handler)
    kernel32.SetConsoleCtrlHandler(callback, True)
    return callback  # keep the reference alive


def foreground_process_name():
    """Executable name of the window that currently has focus, or ''."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.replace("/", "\\").rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# Voice activity detection
# --------------------------------------------------------------------------- #


class GateResult:
    __slots__ = (
        "level_db",
        "threshold_db",
        "ratio",
        "speaking",
        "changed",
        "calibrating",
        "snr_db",
        "voicing",
        "flatness",
        "vad",
        "detector",
    )

    def __init__(
        self,
        level_db,
        threshold_db,
        ratio,
        speaking,
        changed,
        calibrating,
        snr_db=0.0,
        voicing=0.0,
        flatness=0.0,
        vad=0.0,
        detector="",
    ):
        self.level_db = level_db
        self.threshold_db = threshold_db
        self.ratio = ratio
        self.speaking = speaking
        self.changed = changed
        self.calibrating = calibrating
        self.snr_db = snr_db
        self.voicing = voicing
        self.flatness = flatness
        self.vad = vad
        self.detector = detector


# --------------------------------------------------------------------------- #
# Neural voice activity detection (Silero VAD v5, ONNX)
# --------------------------------------------------------------------------- #

VAD_MODEL_PATH = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"


class StreamResampler:
    """Resample a stream of audio blocks to 16 kHz without gaps.

    The neural detector only accepts 16 kHz audio while most microphones run
    at 44.1 or 48 kHz.  A windowed-sinc low pass removes everything above the
    new Nyquist frequency first, so decimating cannot fold fan whine or hiss
    back down into the speech band.
    """

    TAPS = 63

    def __init__(self, in_rate, out_rate=16000):
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.passthrough = self.in_rate == self.out_rate
        self.step = self.in_rate / float(self.out_rate)
        self._pos = 0.0
        self._buffer = np.zeros(0, dtype=np.float64)
        self._tail = np.zeros(self.TAPS - 1, dtype=np.float64)
        self.kernel = None
        if self.passthrough:
            return
        cutoff = 0.42 * self.out_rate / self.in_rate
        taps = np.arange(self.TAPS) - (self.TAPS - 1) / 2.0
        kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * taps) * np.hamming(self.TAPS)
        total = float(kernel.sum())
        self.kernel = kernel / total if total else kernel

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float64)
        if self.passthrough:
            return x.astype(np.float32)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        joined = np.concatenate((self._tail, x))
        filtered = np.convolve(joined, self.kernel, mode="valid")
        self._tail = joined[-(self.TAPS - 1) :]
        self._buffer = np.concatenate((self._buffer, filtered))

        out = []
        while True:
            base = int(math.floor(self._pos))
            if base + 1 >= self._buffer.size:
                break
            frac = self._pos - base
            out.append(self._buffer[base] * (1.0 - frac) + self._buffer[base + 1] * frac)
            self._pos += self.step

        consumed = int(math.floor(self._pos))
        if consumed > 0:
            self._buffer = self._buffer[consumed:]
            self._pos -= consumed
        return np.asarray(out, dtype=np.float32)


class HighpassFilter:
    """Streaming biquad high pass (RBJ cookbook), cascaded for a steeper slope.

    Mains hum is a harmonic series (60/120/180 Hz) that reads as a very low,
    buzzy voice to any detector.  Removing it before the model is cheaper and
    safer than second-guessing the model afterwards, and speech loses nothing
    measurable: the fundamental is the only part below the corner.
    """

    def __init__(self, sample_rate, cutoff_hz, stages=2, q=0.7071):
        self.sample_rate = int(sample_rate)
        self.cutoff_hz = float(cutoff_hz)
        self.stages = max(1, int(stages))
        self._z = np.zeros((self.stages, 2), dtype=np.float64)

        w0 = 2.0 * math.pi * self.cutoff_hz / self.sample_rate
        cos_w, sin_w = math.cos(w0), math.sin(w0)
        alpha = sin_w / (2.0 * q)
        a0 = 1.0 + alpha
        self._b0 = (1.0 + cos_w) / 2.0 / a0
        self._b1 = -(1.0 + cos_w) / a0
        self._b2 = self._b0
        self._a1 = -2.0 * cos_w / a0
        self._a2 = (1.0 - alpha) / a0

    def reset(self):
        self._z[:] = 0.0

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        b0, b1, b2 = self._b0, self._b1, self._b2
        a1, a2 = self._a1, self._a2
        for stage in range(self.stages):
            z1, z2 = self._z[stage]
            out = np.empty_like(x)
            for index in range(x.size):
                sample = x[index]
                value = b0 * sample + z1
                z1 = b1 * sample - a1 * value + z2
                z2 = b2 * sample - a2 * value
                out[index] = value
            self._z[stage, 0] = z1
            self._z[stage, 1] = z2
            x = out
        return x.astype(np.float32)


class SileroVad:
    """Silero VAD v5: a small neural net that scores "is this speech".

    Unlike a volume threshold it does not care how loud the room is, and unlike
    spectral heuristics it was trained on real speech, so fan noise, mains hum,
    keyboard clatter and hiss score near zero while quiet speech still scores
    near one.  The model takes 512 samples at 16 kHz plus 64 samples of carried
    context, which is how the reference wrapper drives it.
    """

    SAMPLE_RATE = 16000
    CHUNK = 512
    CONTEXT = 64

    def __init__(self, model_path):
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("onnxruntime is not installed") from exc
        if not Path(model_path).is_file():
            raise RuntimeError(f"model file not found: {model_path}")

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        names = {item.name for item in self.session.get_inputs()}
        missing = {"input", "state", "sr"} - names
        if missing:
            raise RuntimeError(f"unexpected model inputs, missing {sorted(missing)}")
        self.reset()

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self._buffer = np.zeros(0, dtype=np.float32)
        self.probability = 0.0
        self.frames = 0

    def push(self, samples):
        """Feed 16 kHz samples; returns the score of the newest full chunk."""
        if samples.size:
            self._buffer = np.concatenate(
                (self._buffer, np.asarray(samples, dtype=np.float32))
            )
        offset = 0
        while self._buffer.size - offset >= self.CHUNK:
            chunk = self._buffer[offset : offset + self.CHUNK].reshape(1, -1)
            offset += self.CHUNK
            fed = np.concatenate((self._context, chunk), axis=1)
            out, state = self.session.run(
                None,
                {
                    "input": fed,
                    "state": self._state,
                    "sr": np.array(self.SAMPLE_RATE, dtype=np.int64),
                },
            )
            self._state = np.asarray(state, dtype=np.float32)
            self._context = fed[:, -self.CONTEXT :]
            self.probability = float(out[0][0])
            self.frames += 1
        if offset:
            self._buffer = self._buffer[offset:]
        elif self._buffer.size > self.CHUNK * 8:
            self._buffer = self._buffer[-self.CHUNK :]
        return self.probability


COMMAND_MODEL_PATH = Path(__file__).resolve().parent / "models" / "vosk-model-small-en-us-0.15"
# Limited vocabularies keep the decoder roughly 60x faster than real time and
# stop it inventing unrelated words.  "un mute" is the two-token form the small
# model produces for "unmute".
COMMAND_GRAMMAR = '["mute", "un mute", "[unk]"]'

_voice_model_cache = {}


def voice_command_issue(model_path=None):
    """Why voice commands cannot run, or "" when they can."""
    try:
        import vosk  # noqa: F401
    except ImportError:
        return "vosk is not installed (pip install vosk)"
    path = Path(model_path) if model_path else COMMAND_MODEL_PATH
    if not path.is_dir():
        return f"speech model folder not found: {path}"
    return ""


def load_voice_model(path=None):
    """Load (and cache) the Vosk model; loading takes about a second."""
    from vosk import Model, SetLogLevel

    key = str(Path(path) if path else COMMAND_MODEL_PATH)
    if key not in _voice_model_cache:
        SetLogLevel(-1)
        _voice_model_cache[key] = Model(key)
    return _voice_model_cache[key]


class VoiceCommandListener:
    """Listens for the words "mute" and "unmute" in the microphone stream.

    Fed the same 16 kHz audio the neural detector uses.  A *partial* transcript
    only fires when it is exactly the command (so "I muted my phone" cannot
    trip it later, once the rest of the sentence arrives), and a *final*
    transcript may contain at most one unrecognised word.  "unmute" is matched
    both as one token and as "un"/"mute", because the shipped model does not
    have the compound word in its vocabulary.
    """

    BLOCK = 320  # 20 ms at 16 kHz
    COOLDOWN = 1.2

    def __init__(self, model_path=None):
        from vosk import KaldiRecognizer

        self.model = load_voice_model(model_path)
        self.recognizer = KaldiRecognizer(self.model, 16000, COMMAND_GRAMMAR)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_fire = -1e9
        self.last_text = ""

    def reset(self):
        from vosk import KaldiRecognizer

        self.recognizer = KaldiRecognizer(self.model, 16000, COMMAND_GRAMMAR)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_fire = -1e9
        self.last_text = ""

    @staticmethod
    def _match(text, final):
        words = re.findall(r"[a-z']+", text.lower())
        if not words:
            return None

        if final:
            if text.count("[unk]") > 1:
                return None
            for index, word in enumerate(words):
                if word == "unmute":
                    return "unmute"
                if word == "mute":
                    return "unmute" if index and words[index - 1] == "un" else "mute"
            return None

        if words == ["mute"]:
            return "mute"
        if words in (["un", "mute"], ["unmute"]):
            return "unmute"
        return None

    def feed(self, samples, now=None):
        """Feed 16 kHz audio; returns "mute", "unmute" or None."""
        if samples is None:
            return None
        block = np.asarray(samples, dtype=np.float32)
        if block.size == 0:
            return None
        self._buffer = np.concatenate((self._buffer, block))

        command = None
        while self._buffer.size >= self.BLOCK:
            chunk = self._buffer[: self.BLOCK]
            self._buffer = self._buffer[self.BLOCK :]
            pcm = np.clip(chunk * 32767.0, -32768.0, 32767.0).astype(np.int16).tobytes()
            if self.recognizer.AcceptWaveform(pcm):
                text = json.loads(self.recognizer.Result()).get("text", "")
                self.last_text = text
                found = self._match(text, final=True)
            else:
                text = json.loads(self.recognizer.PartialResult()).get("partial", "")
                if text:
                    self.last_text = text
                found = self._match(text, final=False)
            if found:
                command = found

        if command is None:
            return None
        stamp = time.monotonic() if now is None else now
        if stamp - self._last_fire < self.COOLDOWN:
            return None
        self._last_fire = stamp
        return command


def neural_detector_issue(model_path=None):
    """Why the neural detector cannot be used, or "" when it can."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return "onnxruntime is not installed (pip install onnxruntime)"
    path = Path(model_path) if model_path else VAD_MODEL_PATH
    if not path.is_file():
        return f"model file not found: {path}"
    return ""


class VoiceGate:
    """Multi-feature voice activity detector.

    Instead of asking "is this loud?", it asks "how much of this sticks out
    above the noise this microphone actually produces, per frequency, and does
    it look like a human voice?".

    * Calibration learns a **per-bin noise profile** (the average spectrum of
      the room), so a fan, hum or air conditioning is subtracted rather than
      compared against.
    * The decision uses **excess energy over that profile** (spectral
      subtraction) mapped to an SNR in dB.
    * A frame must also look like voice: most energy in the speech band, not
      spectrally flat (hiss/clatter), and periodic in the human pitch range
      (voicing from autocorrelation).
    * Calibration ends with a short **verification** pass that measures the
      residual noise SNR and raises the threshold above it automatically.
    * A **stationarity guard** closes the gate on a perfectly steady tone
      (a fan starting up, a beep) which no human voice produces for a second.
    """

    SNR_SMOOTH = 0.55
    VAD_SILENCE_DB = -70.0
    VAD_IDLE_RESET_MS = 1000.0
    STEADY_DELTA = 0.05
    STEADY_LIMIT_MS = 1000.0
    PITCH_MIN_HZ = 70.0
    PITCH_MAX_HZ = 350.0

    def __init__(self, config, sample_rate, block_size):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.block_ms = 1000.0 * block_size / sample_rate

        self.open_snr_db = float(config["open_offset_db"])
        self.hysteresis_db = float(config["close_hysteresis_db"])
        self.absolute_floor_db = float(config["absolute_floor_db"])
        self.min_ratio = float(config["min_voice_ratio"])
        self.max_flatness = float(config["max_flatness"])
        self.min_voicing = float(config["min_voicing"])
        self.over_subtraction = float(config["over_subtraction"])
        self.adapt_rate = float(config["noise_floor_adapt_rate"])
        self.hold_ratio = self.min_ratio * 0.7
        self.min_bins = int(config.get("min_spectral_bins", 6))

        self.attack_blocks = max(1, int(round(float(config["attack_ms"]) / self.block_ms)))
        self.release_blocks = max(
            1, int(round(float(config["release_ms"]) / self.block_ms))
        )
        self.calibration_blocks = max(
            20, int(round(float(config["calibration_seconds"]) * 1000.0 / self.block_ms))
        )
        self.verify_blocks = max(
            5, int(round(float(config["verify_seconds"]) * 1000.0 / self.block_ms))
        )

        self.detector_mode = str(config.get("detector", "auto") or "auto").strip().lower()
        self.vad_threshold = float(config.get("vad_threshold", 0.5))
        self.vad_release = float(config.get("vad_release_threshold", 0.35))
        self.vad = None
        self.vad_error = ""
        self._resampler = None
        self._vad_filter = None
        self._analysis_audio = np.zeros(0, dtype=np.float32)
        self._vad_probability = 0.0
        self._vad_idle_blocks = 0
        self._configure_detector(config, strict=True)

        self.window = np.hanning(block_size)
        self.window_energy = float(np.sum(self.window**2))
        freqs = np.fft.rfftfreq(block_size, 1.0 / sample_rate)

        self.band_mask = (freqs >= float(config["voice_band_low_hz"])) & (
            freqs <= float(config["voice_band_high_hz"])
        )
        self.passband_mask = freqs >= float(config["highpass_hz"])
        if self.band_mask.sum() < 4:
            raise ValueError(
                "voice band has too few FFT bins; raise block_ms or sample_rate in config.json"
            )

        # rfft drops the mirrored half of the spectrum; weight it back so the
        # level is in real dBFS.
        weight = np.full(freqs.shape, 2.0)
        weight[0] = 1.0
        if block_size % 2 == 0:
            weight[-1] = 1.0
        self.weight = weight

        # Voicing needs a couple of pitch periods, so it looks at a longer
        # rolling window than a single block.
        voicing_len = max(block_size, int(round(sample_rate * 0.040)))
        self._history = np.zeros(voicing_len)
        self._voicing_window = np.hanning(voicing_len)
        min_lag = max(2, int(sample_rate / self.PITCH_MAX_HZ))
        max_lag = min(voicing_len - 2, int(sample_rate / self.PITCH_MIN_HZ))
        self._lag_slice = slice(min_lag, max_lag) if max_lag > min_lag else None

        # Learned state
        self.noise_profile = None
        self.noise_floor_db = -60.0
        self._open_snr_effective = self.open_snr_db
        self._noise_ceiling_snr = None
        self.threshold_db = self.absolute_floor_db
        self.close_db = self.threshold_db - self.hysteresis_db

        # Running state
        self.level_db = -120.0
        self.snr_db = 0.0
        self.ratio = 0.0
        self.flatness = 0.0
        self.voicing = 0.0
        self.density = 0
        self.speaking = False
        self._hits = 0
        self._quiet_blocks = 0
        self._snr_smooth = 0.0
        self._prev_log_magnitude = None
        self._steady_blocks = 0

        self._phase = "calibrating"
        self._cal_spectra = []
        self._cal_levels = []
        self._verify_snrs = []
        self._cal_stability_db = 0.0
        self._cal_discarded = 0
        self.calibration_completed = False
        self.calibration_report = None

    @property
    def detector_name(self):
        return "neural" if self.vad is not None else "spectral"

    @property
    def analysis_audio(self):
        """The 16 kHz high-passed block the detector just looked at."""
        return self._analysis_audio

    def _configure_detector(self, config, strict=False):
        """Load (or drop) the neural detector without disturbing the profile."""
        model = str(config.get("vad_model") or "").strip()
        path = Path(model).expanduser() if model else VAD_MODEL_PATH

        # The 16 kHz high-passed stream feeds the neural detector *and* voice
        # commands, so it is built even when the spectral detector is selected.
        if self._resampler is None:
            cutoff = min(300.0, max(60.0, float(config.get("highpass_hz", 120.0))))
            self._resampler = StreamResampler(self.sample_rate)
            self._vad_filter = HighpassFilter(SileroVad.SAMPLE_RATE, cutoff)

        if self.detector_mode == "spectral":
            self.vad = None
            self.vad_error = ""
            return

        problem = neural_detector_issue(path)
        if not problem:
            try:
                self.vad = SileroVad(path)
                self.vad_error = ""
                self._vad_probability = 0.0
                return
            except Exception as exc:  # present but unusable
                problem = str(exc)

        self.vad = None
        self.vad_error = problem
        if strict and self.detector_mode == "neural":
            raise ValueError(
                f"Neural detector unavailable: {problem}. Fix that, or set "
                '"detector": "spectral" in config.json to use the classic detector.'
            )

    # -- calibration ------------------------------------------------------

    def start_calibration(self):
        self._phase = "calibrating"
        self._cal_spectra = []
        self._cal_levels = []
        self._verify_snrs = []
        self._noise_ceiling_snr = None
        self._cal_stability_db = 0.0
        self._cal_discarded = 0
        self.calibration_completed = False
        self.calibration_report = None
        self.speaking = False
        self._hits = 0
        self._quiet_blocks = 0
        self._snr_smooth = 0.0
        self._steady_blocks = 0
        self._prev_log_magnitude = None
        self._open_snr_effective = self.open_snr_db
        if self._vad_filter is not None:
            self._vad_filter.reset()
        if self.vad is not None:
            self.vad.reset()
        self._vad_probability = 0.0
        self._vad_idle_blocks = 0

    @property
    def calibrating(self):
        return self._phase in ("calibrating", "verifying")

    @property
    def calibration_progress(self):
        if self._phase == "calibrating":
            done = len(self._cal_levels) / float(max(1, self.calibration_blocks))
            return 0.85 * min(1.0, done)
        if self._phase == "verifying":
            done = len(self._verify_snrs) / float(max(1, self.verify_blocks))
            return 0.85 + 0.15 * min(1.0, done)
        return 1.0

    def _build_profile(self):
        spectra = np.asarray(self._cal_spectra)
        levels = np.asarray(self._cal_levels)
        self._cal_spectra = []

        median_level = float(np.median(levels)) if levels.size else -120.0
        # Drop frames polluted by a cough, a door or somebody talking: anything
        # well above the median does not belong in the noise profile.
        keep = (levels > median_level - 14.0) & (levels < median_level + 7.0)
        self._cal_discarded = int(np.count_nonzero(~keep))
        retained = spectra[keep] if int(np.count_nonzero(keep)) >= 5 else spectra
        retained_levels = levels[keep] if int(np.count_nonzero(keep)) >= 5 else levels

        self.noise_profile = retained.mean(axis=0)
        band_noise = float(self.noise_profile[self.band_mask].sum())
        self.noise_floor_db = 10.0 * math.log10(
            band_noise / (self.block_size * self.window_energy) + 1e-15
        )
        if retained_levels.size >= 4:
            self._cal_stability_db = float(
                np.percentile(retained_levels, 75) - np.percentile(retained_levels, 25)
            )
        else:
            self._cal_stability_db = 0.0

        self._recompute_snr_threshold()
        self._phase = "verifying"
        self._verify_snrs = []
        self._prev_log_magnitude = None

    def _finish_calibration(self):
        snrs = [value for value in self._verify_snrs if math.isfinite(value)]
        self._verify_snrs = []
        ceiling = float(np.percentile(snrs, 95)) if snrs else 0.0
        self._noise_ceiling_snr = ceiling
        self._recompute_snr_threshold()
        self._snr_smooth = max(0.0, min(ceiling, self._open_snr_effective))

        note = "ok"
        if self.noise_floor_db <= -90.0:
            note = "no-signal"
        elif self._cal_stability_db > 6.0:
            note = "unstable"
        elif self._open_snr_effective > self.open_snr_db + 0.5:
            note = "raised"

        self.calibration_report = {
            "note": note,
            "floor_db": self.noise_floor_db,
            "stability_db": self._cal_stability_db,
            "noise_ceiling_snr": ceiling,
            "open_snr": self._open_snr_effective,
            "discarded_frames": self._cal_discarded,
            "profile_ready": self.noise_profile is not None,
        }
        self._phase = "running"
        self.calibration_completed = True

    def _recompute_snr_threshold(self):
        effective = self.open_snr_db
        if self._noise_ceiling_snr is not None:
            effective = max(effective, self._noise_ceiling_snr + 4.0)
            effective = min(effective, self.open_snr_db + 8.0)
        self._open_snr_effective = effective
        self.threshold_db = self.noise_floor_db + effective
        self.close_db = self.threshold_db - self.hysteresis_db

    def update_settings(self, config):
        """Apply new tuning values without losing the learned noise profile."""
        self.open_snr_db = float(config["open_offset_db"])
        self.hysteresis_db = float(config["close_hysteresis_db"])
        self.absolute_floor_db = float(config["absolute_floor_db"])
        self.min_ratio = float(config["min_voice_ratio"])
        self.max_flatness = float(config["max_flatness"])
        self.min_voicing = float(config["min_voicing"])
        self.over_subtraction = float(config["over_subtraction"])
        self.adapt_rate = float(config["noise_floor_adapt_rate"])
        self.min_bins = int(config.get("min_spectral_bins", 6))
        self.hold_ratio = self.min_ratio * 0.7
        self.attack_blocks = max(
            1, int(round(float(config["attack_ms"]) / self.block_ms))
        )
        self.release_blocks = max(
            1, int(round(float(config["release_ms"]) / self.block_ms))
        )
        self.vad_threshold = float(config.get("vad_threshold", 0.5))
        self.vad_release = float(config.get("vad_release_threshold", 0.35))
        mode = str(config.get("detector", "auto") or "auto").strip().lower()
        if mode != self.detector_mode:
            self.detector_mode = mode
            self._configure_detector(config)
        self._recompute_snr_threshold()

    # -- analysis helpers -------------------------------------------------

    def _flatness(self, band_power):
        """Wiener entropy: ~1.0 for hiss, near 0 for tonal/voice-like sound."""
        if band_power.size < 4:
            return 1.0
        total = float(band_power.sum())
        if total <= 1e-18:
            return 1.0
        safe = np.maximum(band_power, 1e-18)
        geometric = math.exp(float(np.mean(np.log(safe))))
        arithmetic = float(np.mean(band_power))
        if arithmetic <= 0.0:
            return 1.0
        return float(min(1.0, geometric / arithmetic))

    def _spectral_density(self, band_power):
        """How many band bins carry real energy.

        A pure tone or a mains hum lives in two or three bins; a voice spreads
        harmonics across dozens. This is what separates a beep from a vowel.
        """
        if band_power.size == 0:
            return 0
        peak = float(band_power.max())
        if peak <= 0.0:
            return 0
        return int(np.count_nonzero(band_power >= peak * 0.03))

    def _voicing(self):
        """Normalized autocorrelation peak in the human pitch range."""
        if self._lag_slice is None:
            return 0.0
        data = self._history * self._voicing_window
        if not np.any(data):
            return 0.0
        size = 1 << int(math.ceil(math.log2(data.size * 2)))
        spectrum = np.fft.rfft(data, size)
        power = spectrum.real**2 + spectrum.imag**2
        autocorrelation = np.fft.irfft(power, size)
        if autocorrelation[0] <= 0.0:
            return 0.0
        normalized = autocorrelation / autocorrelation[0]
        peak = float(np.max(normalized[self._lag_slice]))
        return max(0.0, min(1.0, peak))

    def _snr_db(self, power):
        """Energy that sticks out above the learned noise, in dB."""
        if self.noise_profile is None:
            return 0.0
        band = power[self.band_mask]
        noise = self.noise_profile[self.band_mask]
        noise_sum = float(noise.sum())
        band_sum = float(band.sum())
        if noise_sum <= 1e-18:
            return 30.0 if band_sum > 1e-14 else 0.0
        excess = np.maximum(band - noise * self.over_subtraction, 0.0)
        return 10.0 * math.log10(float(excess.sum()) / noise_sum + 1e-12)

    def _spectral_delta(self, band_power):
        """How much the spectrum changed since the previous block."""
        log_magnitude = np.log(np.sqrt(np.maximum(band_power, 1e-18)))
        previous = self._prev_log_magnitude
        self._prev_log_magnitude = log_magnitude
        if previous is None or previous.shape != log_magnitude.shape:
            return 1.0
        return float(np.mean(np.abs(log_magnitude - previous)))

    # -- per-block processing ---------------------------------------------

    def process(self, block):
        x = np.asarray(block, dtype=np.float64)
        if x.size != self.block_size:
            if x.size > self.block_size:
                x = x[: self.block_size]
            else:
                x = np.pad(x, (0, self.block_size - x.size))

        x = x - x.mean()
        self._history = np.concatenate((self._history[x.size :], x))

        if self._resampler is not None:
            resampled = self._resampler.process(x)
            if resampled.size:
                self._analysis_audio = self._vad_filter.process(resampled)
                if self.vad is not None:
                    self._vad_probability = self.vad.push(self._analysis_audio)
                    if not self.speaking and self._vad_probability < self.vad_release:
                        self._vad_idle_blocks += 1
                        if self._vad_idle_blocks * self.block_ms >= self.VAD_IDLE_RESET_MS:
                            # The model's recurrent state only describes the
                            # recent past.  After a second of non-speech it is
                            # stale, and a loud hiss left in it can make the next
                            # steady sound (a fridge switching on) read as speech.
                            self.vad.reset()
                            self._vad_idle_blocks = 0
                            self._vad_probability = 0.0
                    else:
                        self._vad_idle_blocks = 0

        spectrum = np.fft.rfft(x * self.window)
        power = (spectrum.real**2 + spectrum.imag**2) * self.weight
        band_power = power[self.band_mask]
        band_sum = float(band_power.sum())
        pass_sum = float(power[self.passband_mask].sum())

        self.level_db = 10.0 * math.log10(
            band_sum / (self.block_size * self.window_energy) + 1e-15
        )
        self.ratio = band_sum / pass_sum if pass_sum > 1e-15 else 0.0
        self.flatness = self._flatness(band_power)
        self.voicing = self._voicing()
        self.density = self._spectral_density(band_power)
        delta = self._spectral_delta(band_power)
        if delta < self.STEADY_DELTA:
            self._steady_blocks += 1
        else:
            self._steady_blocks = 0

        if self._phase == "calibrating":
            self._cal_spectra.append(power)
            self._cal_levels.append(self.level_db)
            if len(self._cal_levels) >= self.calibration_blocks:
                self._build_profile()
            return self._result(False, False, True)

        if self._phase == "verifying":
            snr = self._snr_db(power)
            self.snr_db = snr
            self._verify_snrs.append(snr)
            if len(self._verify_snrs) >= self.verify_blocks:
                self._finish_calibration()
            return self._result(False, False, True)

        snr = self._snr_db(power)
        self.snr_db = snr
        self._snr_smooth = (
            self.SNR_SMOOTH * self._snr_smooth + (1.0 - self.SNR_SMOOTH) * snr
        )

        speaking, changed = self._decide()
        self._adapt_profile(power)
        return self._result(speaking, changed, False)

    def _result(self, speaking, changed, calibrating):
        return GateResult(
            self.level_db,
            self.threshold_db,
            self.ratio,
            speaking,
            changed,
            calibrating,
            self.snr_db,
            self.voicing,
            self.flatness,
            self._vad_probability,
            self.detector_name,
        )

    def _decide(self):
        if self.vad is not None:
            return self._decide_neural()
        return self._decide_spectral()

    def _decide_neural(self):
        """Trust the speech model, with hysteresis and a dead-silence guard.

        The level check is only there so a muted or unplugged microphone whose
        stream is digital silence can never hold the key down; the model itself
        is level independent, which is what makes quiet speech work.
        """
        probability = self._vad_probability
        silent = self.level_db < self.VAD_SILENCE_DB

        if self.speaking:
            if not silent and probability >= self.vad_release:
                self._quiet_blocks = 0
                return True, False
            self._quiet_blocks += 1
            if self._quiet_blocks >= self.release_blocks:
                self.speaking = False
                self._quiet_blocks = 0
                self._hits = 0
                return False, True
            return True, False

        if silent or probability < self.vad_threshold:
            # Strictly consecutive: a device switching on, or a door slam, can
            # score 0.4-0.7 for a moment, but never holds it for the whole attack.
            self._hits = 0
            return False, False

        self._hits += 1
        if self._hits >= self.attack_blocks:
            self.speaking = True
            self._hits = 0
            self._quiet_blocks = 0
            return True, True
        return False, False

    def _decide_spectral(self):
        if self.noise_profile is None:
            return False, False

        snr = self._snr_smooth
        level_ok = self.level_db >= self.absolute_floor_db
        steady = self._steady_blocks * self.block_ms >= self.STEADY_LIMIT_MS

        if self.speaking:
            holds = (
                snr >= self._open_snr_effective - self.hysteresis_db
                and self.ratio >= self.hold_ratio
                and self.level_db >= self.absolute_floor_db - 6.0
                and not steady
            )
            if holds:
                self._quiet_blocks = 0
                return True, False
            self._quiet_blocks += 1
            if self._quiet_blocks >= self.release_blocks:
                self.speaking = False
                self._quiet_blocks = 0
                self._hits = 0
                return False, True
            return True, False

        if steady:
            self._hits = 0
            return False, False

        # A real voice is periodic in the pitch range and spreads harmonics
        # over many bins; hum, beeps and fan whine fail at least one of these.
        voiced = self.voicing >= self.min_voicing
        candidate = (
            snr >= self._open_snr_effective
            and level_ok
            and self.ratio >= self.min_ratio
            and self.flatness <= self.max_flatness
            and self.density >= self.min_bins
            and voiced
        )
        if candidate:
            self._hits += 1
            if self._hits >= self.attack_blocks:
                self.speaking = True
                self._hits = 0
                self._quiet_blocks = 0
                return True, True
        else:
            self._hits = max(0, self._hits - 1)
        return False, False

    def _adapt_profile(self, power):
        """Follow slow changes in the room (a fan speeding up, a new hum).

        Uses the *instantaneous* SNR, not the smoothed one: a keyboard click or
        a cough must never be learned as background noise, otherwise the floor
        creeps up until quiet speech stops working.
        """
        if self.speaking or self.noise_profile is None:
            return
        near_floor = self.level_db < self.noise_floor_db + 15.0
        calm = (
            self.snr_db < 6.0
            and near_floor
            and self.ratio < self.min_ratio
            and self.level_db >= -95.0
        )
        # Steady background noise that is clearly not voice (a fan spinning up)
        # can also be absorbed, but only while it is still close to the floor.
        steady = self._steady_blocks * self.block_ms >= self.STEADY_LIMIT_MS
        absorb = (
            steady
            and near_floor
            and self.snr_db < 20.0
            and self.ratio < self.min_ratio
        )
        if not (calm or absorb):
            return
        self.noise_profile = (
            (1.0 - self.adapt_rate) * self.noise_profile + self.adapt_rate * power
        )
        band_noise = float(self.noise_profile[self.band_mask].sum())
        self.noise_floor_db = 10.0 * math.log10(
            band_noise / (self.block_size * self.window_energy) + 1e-15
        )
        self._recompute_snr_threshold()

# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #

DIM = "\x1b[2m"
RESET = "\x1b[0m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
RED = "\x1b[91m"
CYAN = "\x1b[96m"


def enable_ansi():
    try:
        handle = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


class Console:
    def __init__(self):
        self.color = enable_ansi()
        self._line_len = 0
        self._lock = threading.Lock()
        try:
            # Only draw the live meter on a real terminal. Writing carriage
            # returns into a redirected pipe can block the audio worker.
            self.interactive = bool(sys.stdout.isatty())
        except Exception:
            self.interactive = False

    def paint(self, text, color):
        if not self.color:
            return text
        return f"{color}{text}{RESET}"

    def info(self, message=""):
        with self._lock:
            self._erase()
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    def meter(self, text, shown=None):
        """Draw the live meter on one line, padded using the plain text width."""
        if not self.interactive:
            return
        with self._lock:
            padding = max(0, self._line_len - len(text))
            sys.stdout.write("\r" + (shown if shown is not None else text) + " " * padding)
            sys.stdout.flush()
            self._line_len = len(text)

    def _erase(self):
        if self._line_len:
            sys.stdout.write("\r" + " " * self._line_len + "\r")
            self._line_len = 0

    def close(self):
        with self._lock:
            self._erase()


class EventLog:
    def __init__(self, path):
        self.handle = None
        if path:
            try:
                self.handle = open(path, "a", encoding="utf-8")
            except OSError as exc:
                print(f"Could not open log file {path}: {exc}")

    def write(self, message):
        if not self.handle:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.handle.write(f"{stamp}  {message}\n")
        self.handle.flush()

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


# --------------------------------------------------------------------------- #
# Global hotkeys
# --------------------------------------------------------------------------- #

HOTKEY_MODS = {
    "CTRL": MOD_CONTROL,
    "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT,
    "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN,
    "META": MOD_WIN,
}


def parse_hotkey(spec):
    mods = MOD_NOREPEAT
    parts = [part.strip() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    for part in parts[:-1]:
        upper = part.upper()
        if upper not in HOTKEY_MODS:
            raise ValueError(f"unknown hotkey modifier {part!r}")
        mods |= HOTKEY_MODS[upper]
    return mods, vk_from_name(parts[-1])


class GlobalHotkeys(threading.Thread):
    def __init__(self, bindings, on_error):
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings  # list of (spec, callback, label)
        self.on_error = on_error
        self._thread_id = 0
        self._ids = {}
        self._ready = threading.Event()
        self.failed = []

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        try:
            for index, (spec, callback, label) in enumerate(self.bindings):
                hotkey_id = 0xB000 + index
                try:
                    mods, vk = parse_hotkey(spec)
                except ValueError as exc:
                    self.failed.append(f"{label}: {exc}")
                    continue
                if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
                    self.failed.append(
                        f"{label}: {spec} is already used by another program "
                        f"(error {ctypes.get_last_error()})"
                    )
                    continue
                self._ids[hotkey_id] = callback
                registered.append(hotkey_id)
        finally:
            self._ready.set()

        msg = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                callback = self._ids.get(int(msg.wParam))
                if callback:
                    try:
                        callback()
                    except Exception as exc:  # keep the listener alive
                        self.on_error(f"Hotkey error: {exc}")
        for hotkey_id in registered:
            user32.UnregisterHotKey(None, hotkey_id)

    def wait_ready(self, timeout=2.0):
        self._ready.wait(timeout)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #


class VoiceEngine:
    """Microphone -> voice gate -> push-to-talk key.  No UI assumptions.

    Both the console front-end and the graphical front-end drive this class,
    so the detection and key handling live in exactly one place.
    """

    def __init__(self, config, device=None, check_focus=None, on_event=None, use_hotkeys=True):
        self.config = config
        self.on_event = on_event or (lambda message, kind="info": None)
        self.on_tick = None
        self.log = EventLog(config.get("log_file") or "")
        self.use_hotkeys = use_hotkeys

        self.key = PushToTalkKey(config["push_to_talk_key"])
        self.device_request = device
        self.device_index = None
        self.gate = None
        self.stream = None
        self.sample_rate = 0
        self.block_size = 0
        self.channels = 1
        self.device_label = "Windows default microphone"

        self.audio_queue = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.worker = None
        self.hotkeys = None
        self._stream_lock = threading.Lock()

        self.enabled = True
        self.commands = None
        self._commands_note = ""
        self._commands_key = None
        self.talking = False
        self.external_hold = False
        self.focus_ok = True
        self._focus_checked = 0.0
        self._audio_status = ""
        self._send_error_logged = False
        self.last_result = None

        self.check_focus = (
            bool(config["only_when_league_focused"])
            if check_focus is None
            else bool(check_focus)
        )
        self.focus_names = [name.lower() for name in config["focus_processes"]]

        self._ctrl_handler = install_abort_release(lambda: self.key)
        atexit.register(self.safe_release)

    # -- reporting --------------------------------------------------------

    def emit(self, message, kind="info"):
        self.log.write(message)
        self.on_event(message, kind)

    def snapshot(self):
        gate = self.gate
        result = self.last_result
        return {
            "level_db": result.level_db if result else -120.0,
            "threshold_db": result.threshold_db if result else (
                gate.threshold_db if gate else -48.0
            ),
            "ratio": result.ratio if result else 0.0,
            "snr_db": result.snr_db if result else 0.0,
            "voicing": result.voicing if result else 0.0,
            "flatness": result.flatness if result else 0.0,
            "vad": result.vad if result else 0.0,
            "detector": result.detector if result else (
                gate.detector_name if gate else "spectral"
            ),
            "voice_commands": self.commands is not None,
            "floor_db": gate.noise_floor_db if gate else -60.0,
            "speaking": self.talking,
            "enabled": self.enabled,
            "calibrating": bool(gate is not None and gate.calibrating),
            "calibration_progress": gate.calibration_progress if gate else 0.0,
            "focus_ok": self.focus_ok,
            "device_label": self.device_label,
            "device_index": self.device_index,
            "sample_rate": self.sample_rate,
            "key_name": self.key.name,
            "running": self.stream is not None,
        }

    # -- audio ------------------------------------------------------------

    def _close_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            for action in (stream.stop, stream.close):
                try:
                    action()
                except Exception:
                    pass

    def open_stream(self):
        """Open the configured input device (falling back to the default)."""
        with self._stream_lock:
            self._close_stream()
            while True:
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    break

            requested = self.device_request
            if requested is None:
                requested = self.config.get("input_device")
            candidates = [requested, None] if requested is not None else [None]

            last_error = None
            for device in candidates:
                try:
                    info = sd.query_devices(device, "input")
                except Exception as exc:
                    last_error = exc
                    continue

                channels = 2 if int(info.get("max_input_channels", 1)) >= 2 else 1
                sample_rate = int(self.config.get("sample_rate") or 0)
                if sample_rate <= 0:
                    sample_rate = int(round(float(info.get("default_samplerate") or 48000)))
                block_size = max(
                    64, int(round(sample_rate * float(self.config["block_ms"]) / 1000.0))
                )

                try:
                    stream = sd.InputStream(
                        device=device,
                        channels=channels,
                        samplerate=sample_rate,
                        blocksize=block_size,
                        dtype="float32",
                        callback=self._audio_callback,
                    )
                    stream.start()
                except Exception as exc:
                    last_error = exc
                    continue

                self.stream = stream
                self.channels = channels
                self.sample_rate = sample_rate
                self.block_size = block_size
                self.device_index = int(info.get("index", -1))
                self.device_label = str(info.get("name", device))
                self.gate = VoiceGate(self.config, sample_rate, block_size)
                self.last_result = None
                self._refresh_commands()
                if requested is not None and device is None:
                    self.emit(
                        "Configured microphone could not be opened; using the system "
                        "default instead.",
                        "warn",
                    )
                return

            raise RuntimeError(
                f"Could not open a microphone: {last_error}\n"
                "Pick a different input device in the app."
            )

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            self._audio_status = str(status)
        samples = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        try:
            self.audio_queue.put_nowait(samples.copy())
        except queue.Full:
            pass  # drop work rather than block the audio thread

    def check_health(self):
        """Reopen the microphone if the OS dropped it (unplugged, sleep, etc.)."""
        if self.stop_event.is_set():
            return
        with self._stream_lock:
            healthy = self.stream is not None and self.stream.active
        if healthy:
            return
        self.emit("Microphone stream stopped; reconnecting...", "warn")
        try:
            self.open_stream()
        except RuntimeError as exc:
            self.emit(str(exc), "error")

    # -- key handling -----------------------------------------------------

    def _press(self):
        if self.key.is_physically_down():
            # The user is holding the key themselves; do not fight them.
            self.external_hold = True
            self.talking = True
            return
        try:
            self.key.press()
            self.talking = True
            self.external_hold = False
            self.log.write("PTT DOWN")
        except OSError as exc:
            if not self._send_error_logged:
                self._send_error_logged = True
                self.emit(
                    f"Could not send the push-to-talk key: {exc}. "
                    "If League runs elevated, start this app as administrator too.",
                    "error",
                )

    def _release(self):
        if self.talking and not self.external_hold:
            try:
                self.key.release()
                self.log.write("PTT UP")
            except OSError as exc:
                if not self._send_error_logged:
                    self._send_error_logged = True
                    self.emit(f"Could not release the key: {exc}", "error")
        self.talking = False
        self.external_hold = False

    def safe_release(self):
        try:
            if self.key.is_down:
                self.key.release()
        except Exception:
            pass
        self.talking = False

    def _foreground_ok(self):
        if not self.check_focus:
            return True
        now = time.monotonic()
        if now - self._focus_checked >= 0.2:
            self._focus_checked = now
            name = foreground_process_name().lower()
            self.focus_ok = bool(name) and any(f in name for f in self.focus_names)
        return self.focus_ok

    # -- control ----------------------------------------------------------

    def set_enabled(self, value, announce=True):
        self.enabled = bool(value)
        if not self.enabled:
            self._release()
        if announce:
            self.emit(
                "Macro resumed" if self.enabled else "Macro paused",
                "good" if self.enabled else "warn",
            )

    def _refresh_commands(self):
        """Load or drop the "mute"/"unmute" listener to match the config."""
        wanted = bool(self.config.get("voice_commands", True))
        model = str(self.config.get("command_model") or "").strip() or None
        # Sliders call apply_settings constantly; only rebuild on a real change
        # so a half-heard word is never cut off by a fresh recogniser.
        key = (wanted, model or "")
        if key == self._commands_key:
            return
        self._commands_key = key

        if not wanted:
            self.commands = None
            return
        problem = voice_command_issue(model)
        if problem:
            self.commands = None
            if problem != self._commands_note:
                self._commands_note = problem
                self.emit(f"Voice commands off: {problem}", "warn")
            return
        try:
            self.commands = VoiceCommandListener(model)
            self._commands_note = ""
        except Exception as exc:
            self.commands = None
            self.emit(f"Voice commands off: {exc}", "warn")

    def _on_voice_command(self, command):
        if command == "mute":
            if self.enabled:
                self.set_enabled(False, announce=False)
                self.emit(
                    'Heard "mute" - push-to-talk is off. Say "unmute" to resume.',
                    "warn",
                )
            return
        if command == "unmute" and not self.enabled:
            self.set_enabled(True, announce=False)
            self.emit('Heard "unmute" - push-to-talk is back on.', "good")

    def toggle(self):
        self.set_enabled(not self.enabled)

    def recalibrate(self):
        self._release()
        if self.gate is not None:
            self.gate.start_calibration()
        self.emit(
            f"Re-measuring your room's noise floor "
            f"({self.config['calibration_seconds']:.1f}s) - stay quiet...",
            "info",
        )

    def set_key(self, name):
        new_key = PushToTalkKey(name)  # raises ValueError on a bad name
        if self.talking:
            self._release()
        self.key = new_key
        self.config["push_to_talk_key"] = new_key.name
        self._send_error_logged = False
        self.emit(f"Push-to-talk key set to {new_key.name}", "good")

    def set_device(self, index):
        index = None if index is None else int(index)
        self._release()
        self.device_request = index
        self.config["input_device"] = index
        self.open_stream()
        self.emit(f"Microphone: {self.device_label}", "good")

    def apply_settings(self):
        self.check_focus = bool(self.config["only_when_league_focused"])
        self.focus_names = [name.lower() for name in self.config["focus_processes"]]
        if not self.check_focus:
            self.focus_ok = True
        if self.gate is not None:
            self.gate.update_settings(self.config)
        self._refresh_commands()
        wanted = str(self.config["push_to_talk_key"]).strip().upper()
        if self.key.name.strip().upper() != wanted:
            try:
                self.set_key(self.config["push_to_talk_key"])
            except ValueError as exc:
                self.emit(str(exc), "error")

    # -- hotkeys ----------------------------------------------------------

    def install_hotkeys(self):
        if self.hotkeys is not None:
            return
        self.hotkeys = GlobalHotkeys(
            [
                (self.config["toggle_hotkey"], self.toggle, "toggle hotkey"),
                (self.config["recalibrate_hotkey"], self.recalibrate, "recalibrate hotkey"),
            ],
            lambda message: self.emit(message, "error"),
        )
        self.hotkeys.start()
        self.hotkeys.wait_ready()
        for failure in self.hotkeys.failed:
            self.emit(failure, "error")

    # -- worker -----------------------------------------------------------

    def _worker_loop(self):
        while not self.stop_event.is_set():
            if self._audio_status:
                note, self._audio_status = self._audio_status, ""
                self.emit(f"Audio warning: {note}", "warn")

            try:
                block = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            gate = self.gate
            if gate is None:
                continue

            was_calibrating = gate.calibrating
            result = gate.process(block)
            self.last_result = result

            if self.commands is not None:
                heard = self.commands.feed(gate.analysis_audio)
                if heard:
                    self._on_voice_command(heard)

            if was_calibrating and not gate.calibrating:
                report = gate.calibration_report or {}
                note = report.get("note")
                floor = gate.noise_floor_db
                stability = report.get("stability_db", 0.0)
                opens = report.get("open_snr", gate.open_snr_db)
                if note == "no-signal":
                    self.emit(
                        "This microphone is delivering almost no signal. If it is not "
                        "muted, pick your real mic in the app.",
                        "warn",
                    )
                elif note == "unstable":
                    self.emit(
                        f"Noise floor {floor:.1f} dB, but the room was noisy while "
                        f"measuring (stability {stability:.1f} dB). Recalibrate "
                        "somewhere quiet for better accuracy.",
                        "warn",
                    )
                elif note == "raised":
                    self.emit(
                        f"Calibrated: floor {floor:.1f} dB, residual noise "
                        f"{report.get('noise_ceiling_snr', 0.0):.1f} dB -> opening at "
                        f"{opens:.1f} dB SNR (raised automatically for accuracy).",
                        "good",
                    )
                else:
                    self.emit(
                        f"Calibrated: floor {floor:.1f} dB, opens at {opens:.1f} dB "
                        "SNR. Speak normally to test it.",
                        "good",
                    )

            if result.calibrating:
                if self.on_tick is not None:
                    self.on_tick(result)
                continue

            want = self.enabled and result.speaking and self._foreground_ok()
            if want and not self.talking:
                self._press()
            elif not want and self.talking:
                self._release()

            if self.on_tick is not None:
                self.on_tick(result)

    def start_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True, name="gate")
        self.worker.start()

    def start(self):
        self.open_stream()
        self.start_worker()
        if self.use_hotkeys:
            self.install_hotkeys()

    def shutdown(self):
        self.stop_event.set()
        self.safe_release()
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
            self.hotkeys = None
        with self._stream_lock:
            self._close_stream()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=1.0)
        self.log.close()


class ConsoleApp:
    """Text front-end (the original console UI) wrapped around the engine."""

    def __init__(self, config, args):
        self.config = config
        self.console = Console()
        self.engine = VoiceEngine(
            config,
            device=args.device,
            check_focus=False if args.no_focus_check else None,
            on_event=self._event,
            use_hotkeys=True,
        )
        self.engine.on_tick = self._draw_meter
        self._meter_time = 0.0

    def _event(self, message, kind="info"):
        color = {"good": GREEN, "warn": YELLOW, "error": RED}.get(kind, DIM)
        self.console.info("  " + self.console.paint(message, color))

    def _draw_meter(self, result):
        now = time.monotonic()
        if now - self._meter_time < 0.08:
            return
        self._meter_time = now

        engine = self.engine
        level = result.level_db
        fraction = min(1.0, max(0.0, (level + 70.0) / 60.0))
        filled = int(round(fraction * 22.0))
        bar = "#" * filled + "." * (22 - filled)

        if result.calibrating:
            progress = int(engine.gate.calibration_progress * 100) if engine.gate else 0
            status, color = f"CALIBRATING {progress}%", DIM
        elif not engine.enabled:
            status, color = "MUTED", YELLOW
        elif engine.talking:
            status, color = "TALKING", GREEN
        elif not engine.focus_ok:
            status, color = "game not focused", DIM
        else:
            status, color = "idle", DIM

        if result.detector == "neural":
            metric = f"vad   {result.vad:0.2f}  "
        else:
            metric = f"voice {result.voicing:0.2f}"
        plain = (
            f"  [{bar}] {level:7.1f} dB | snr {result.snr_db:5.1f} | "
            f"open {result.threshold_db:6.1f} | {metric} | {status}"
        )
        self.console.meter(plain, plain.replace(status, self.console.paint(status, color)))

    def run(self):
        self._print_banner()
        self.engine.start()
        self.console.info(f"  Microphone   : {self.engine.device_label}")
        total = float(self.config["calibration_seconds"]) + float(
            self.config.get("verify_seconds", 0.0)
        )
        self.console.info(
            self.console.paint(
                f"  Stay quiet for {total:.1f}s while your mic's noise floor is "
                "measured and verified...",
                DIM,
            )
        )
        try:
            while not self.engine.stop_event.is_set():
                self.engine.check_health()
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.console.info("")
            self.console.info("Stopping...")
        finally:
            self.engine.shutdown()
            self.console.close()

    def _print_banner(self):
        c = self.console
        c.info("")
        c.info(
            c.paint("  League Voice Chat Macro", CYAN)
            + c.paint("  -  hands-free push-to-talk", DIM)
        )
        c.info(c.paint("  " + "-" * 58, DIM))
        c.info(f"  PTT key      : {self.engine.key.name}")
        c.info(
            "  Focus check  : "
            + ("only while League is focused" if self.engine.check_focus else "off")
        )
        c.info(
            f"  Hotkeys      : {self.config['toggle_hotkey']} on/off   "
            f"{self.config['recalibrate_hotkey']} recalibrate   Ctrl+C quit"
        )
        c.info("")


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #


def load_config(path=CONFIG_PATH):
    config = dict(DEFAULTS)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("config.json must contain a JSON object")
            for key, value in loaded.items():
                if key not in DEFAULTS:
                    print(f"Note: ignoring unknown config key {key!r}")
                    continue
                config[key] = value
        except (OSError, ValueError) as exc:
            sys.exit(f"Could not read {path}: {exc}")
    else:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(DEFAULTS, handle, indent=2)
            handle.write("\n")
        print(f"Wrote a default configuration to {path}")
    return config


def save_config(config, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


CANONICAL_KEY_NAMES = {
    0x20: "SPACE",
    0x09: "TAB",
    0x0D: "ENTER",
    0x1B: "ESC",
    0x08: "BACKSPACE",
    0xA0: "LSHIFT",
    0xA1: "RSHIFT",
    0xA2: "LCTRL",
    0xA3: "RCTRL",
    0xA4: "LALT",
    0xA5: "RALT",
    0x14: "CAPSLOCK",
    0x2D: "INSERT",
    0x2E: "DELETE",
    0x24: "HOME",
    0x23: "END",
    0x21: "PAGEUP",
    0x22: "PAGEDOWN",
    0x26: "UP",
    0x28: "DOWN",
    0x25: "LEFT",
    0x27: "RIGHT",
    0x5B: "WIN",
    0xC0: "`",
    0xBD: "-",
    0xBB: "=",
    0xDB: "[",
    0xDD: "]",
    0xDC: "\\",
    0xBA: ";",
    0xDE: "'",
    0xBC: ",",
    0xBE: ".",
    0xBF: "/",
}

CAPTURE_IGNORED_VKS = {0x01, 0x02, 0x03, 0x04}  # mouse 1-3 would click in game


def key_name_from_vk(vk):
    """Friendly config name for a virtual-key code, or None if unusable."""
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"NUM{vk - 0x60}"
    if vk == VK_XBUTTON1:
        return "MOUSE4"
    if vk == VK_XBUTTON2:
        return "MOUSE5"
    return CANONICAL_KEY_NAMES.get(vk)


def capture_key(timeout=8.0):
    """Wait for the next key press and return (name, error)."""
    candidates = [vk for vk in range(0x05, 0xFF)]
    already_down = {vk for vk in candidates if user32.GetAsyncKeyState(vk) & 0x8000}
    deadline = time.monotonic() + timeout
    saw_mouse = False

    while time.monotonic() < deadline:
        for vk in candidates:
            if vk in already_down or not (user32.GetAsyncKeyState(vk) & 0x8000):
                continue
            already_down.add(vk)
            if vk in CAPTURE_IGNORED_VKS:
                saw_mouse = True
                continue
            name = key_name_from_vk(vk)
            if name:
                return name, None
        time.sleep(0.015)

    if saw_mouse:
        return None, "Mouse buttons 1-3 cannot be used - pick a keyboard key or Mouse 4/5."
    return None, "No key press detected. Try again and press the key once."


def list_devices():
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0]
    except Exception as exc:
        sys.exit(f"Could not query audio devices: {exc}")
    print("\nInput devices:\n")
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) < 1:
            continue
        marker = " (default)" if index == default_in else ""
        print(f"  [{index}] {device['name']}  -  {int(device['max_input_channels'])} ch, "
              f"{int(device['default_samplerate'])} Hz{marker}")
    print("\nSet \"input_device\": <index> in config.json, or use --device <index>.\n")


def test_key(name):
    key = PushToTalkKey(name)
    print(f"Pressing {key.name} for 1 second - watch for it in a text editor or in game...")
    key.press()
    try:
        time.sleep(1.0)
    finally:
        key.release()
    print("Released. If nothing happened, try a different key or run as administrator.")


def _speech_like(size, sample_rate, amplitude):
    """Synthetic speech: gliding pitch, sweeping formant, syllable envelope.

    Deliberately not stationary - real speech never holds a frozen spectrum,
    and the detector is allowed to reject things that do.
    """
    t = np.arange(size) / float(sample_rate)
    pitch = 145.0 + 25.0 * np.sin(2 * math.pi * 1.1 * t)
    phase = 2 * math.pi * np.cumsum(pitch) / float(sample_rate)
    formant = 700.0 + 300.0 * np.sin(2 * math.pi * 0.7 * t)

    signal = np.zeros(size)
    for harmonic in range(1, 13):
        frequency = harmonic * pitch
        weight = (1.0 / harmonic) * np.exp(
            -0.5 * ((frequency - formant) / 900.0) ** 2 + 0.4
        )
        signal += weight * np.sin(harmonic * phase)

    envelope = np.clip(np.sin(2 * math.pi * 3.2 * t) + 0.25, 0.0, 1.0) ** 1.3
    signal = signal * envelope
    peak = float(np.max(np.abs(signal))) or 1.0
    return (amplitude * signal / peak).astype(np.float64)


def run_selftest(config):
    sample_rate = 16000
    block = max(64, int(round(sample_rate * float(config["block_ms"]) / 1000.0)))
    # These checks cover the classic detector; the neural one is exercised
    # separately below with real speech.
    spectral = dict(config)
    spectral["detector"] = "spectral"
    gate = VoiceGate(spectral, sample_rate, block)
    rng = np.random.default_rng(20261120)

    def noise(size, amplitude):
        return amplitude * rng.standard_normal(size) / 6.0

    def hum(size, amplitude, base=60.0):
        t = np.arange(size) / float(sample_rate)
        signal = sum(np.sin(2 * math.pi * base * k * t) / k for k in (1, 2, 3, 4))
        return amplitude * signal / float(np.max(np.abs(signal)))

    def tone(size, amplitude, frequency):
        t = np.arange(size) / float(sample_rate)
        return amplitude * np.sin(2 * math.pi * frequency * t)

    def feed(signal, blocks=None):
        steps = blocks if blocks is not None else int(math.ceil(len(signal) / block))
        spoke = False
        result = None
        for index in range(steps):
            chunk = signal[index * block : (index + 1) * block]
            if chunk.size < block:
                chunk = np.pad(chunk, (0, block - chunk.size))
            result = gate.process(chunk)
            spoke = spoke or result.speaking
        return spoke, result

    def check(name, condition, detail=""):
        checks.append((name, condition))
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}  {detail}")

    checks = []

    calibration_blocks = gate.calibration_blocks + gate.verify_blocks
    feed(noise(block * calibration_blocks, 0.0009), calibration_blocks)
    if gate.calibrating:
        print("FAIL: calibration never finished")
        return 1

    report = gate.calibration_report or {}
    print(
        f"Noise floor after calibration : {gate.noise_floor_db:.1f} dB\n"
        f"Room stability                : {report.get('stability_db', 0.0):.1f} dB\n"
        f"Residual noise (95th pct)     : {report.get('noise_ceiling_snr', 0.0):.1f} dB SNR\n"
        f"Gate opens at                 : {gate._open_snr_effective:.1f} dB SNR "
        f"({gate.threshold_db:.1f} dB level)\n"
    )

    def describe(result):
        steady_ms = gate._steady_blocks * gate.block_ms
        return (
            f"(lvl {result.level_db:6.1f} snr {result.snr_db:5.1f} "
            f"ratio {result.ratio:0.2f} flat {result.flatness:0.2f} "
            f"voice {result.voicing:0.2f} bins {gate.density:2d} "
            f"steady {steady_ms:4.0f}ms)"
        )

    spoke, result = feed(np.zeros(block * 40))
    check("silence stays closed", not spoke, describe(result))

    spoke, result = feed(noise(block * 70, 0.0009))
    check("room noise stays closed", not spoke, describe(result))

    clicks = np.zeros(block * 70)
    for start in range(0, clicks.size - block, block * 14):
        clicks[start : start + 30] = noise(30, 0.5)
    spoke, result = feed(clicks)
    check("keyboard clicks stay closed", not spoke, describe(result))

    spoke, result = feed(noise(block * 45, 0.45))
    check("loud white noise stays closed", not spoke, describe(result))

    spoke, result = feed(hum(block * 70, 0.3))
    check("mains hum stays closed", not result.speaking, describe(result))

    spoke, result = feed(tone(block * 70, 0.3, 250.0))
    check("steady tone is dropped", not result.speaking, describe(result))

    spoke, result = feed(_speech_like(block * 32, sample_rate, 0.25))
    check("speech opens the gate", result.speaking, describe(result))

    spoke, result = feed(np.zeros(block * 6))
    check("held through a short pause", result.speaking, describe(result))

    spoke, result = feed(np.zeros(block * 40))
    check("released after silence", not result.speaking, describe(result))

    fan = noise(block * 70, 0.03)
    spoke, result = feed(fan)
    check("fan noise alone stays closed", not spoke, describe(result))

    speech = _speech_like(block * 34, sample_rate, 0.18)
    mixed = noise(block * 34, 0.03)
    mixed[: speech.size] += speech
    spoke, result = feed(mixed)
    check("speech over fan noise opens", spoke, describe(result))

    # Recalibrate: the fan from the previous test is not part of this room.
    gate.start_calibration()
    feed(noise(block * calibration_blocks, 0.0009), calibration_blocks)
    feed(np.zeros(block * 30))
    spoke, result = feed(_speech_like(block * 34, sample_rate, 0.02))
    check("quiet speech opens", spoke, describe(result))

    # -- neural detector (Silero VAD) --------------------------------------
    fixture = Path(__file__).resolve().parent / "tests" / "speech_16k.wav"
    problem = neural_detector_issue()
    if problem:
        print(f"  [SKIP] neural detector: {problem}")
    elif not fixture.is_file():
        print(f"  [SKIP] neural detector: no speech fixture at {fixture}")
    else:
        neural_cfg = dict(config)
        neural_cfg["detector"] = "neural"
        try:
            neural_gate = VoiceGate(neural_cfg, sample_rate, block)
        except ValueError as exc:
            print(f"  [SKIP] neural detector: {exc}")
        else:
            with wave.open(str(fixture)) as handle:
                raw = handle.readframes(handle.getnframes())
            voice = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            def run_neural(signal):
                spoke = False
                last = None
                peak = 0.0
                for start in range(0, len(signal) - block + 1, block):
                    last = neural_gate.process(signal[start : start + block])
                    peak = max(peak, last.vad)
                    spoke = spoke or last.speaking
                return spoke, last, peak

            def neural_detail(result, peak):
                if result is None:
                    return f"(peak {peak:0.2f})"
                return f"(peak {peak:0.2f} level {result.level_db:6.1f})"

            warmup = neural_gate.calibration_blocks + neural_gate.verify_blocks
            run_neural(noise(block * warmup, 0.0009))

            spoke, last, peak = run_neural(voice)
            check("neural: real speech opens", spoke, neural_detail(last, peak))

            spoke, last, peak = run_neural(voice * 0.15)
            check("neural: quiet speech opens", spoke, neural_detail(last, peak))

            run_neural(np.zeros(block * 40))
            for label, signal in (
                ("hiss", noise(block * 60, 0.35)),
                ("mains hum", hum(block * 60, 0.3)),
                ("fan noise", noise(block * 60, 0.03)),
            ):
                spoke, last, peak = run_neural(signal)
                check(f"neural: {label} stays closed", not spoke, neural_detail(last, peak))

    # -- voice commands ("mute" / "unmute") --------------------------------
    fixtures = Path(__file__).resolve().parent / "tests" / "commands"
    problem = voice_command_issue()
    if problem:
        print(f"  [SKIP] voice commands: {problem}")
    elif not fixtures.is_dir():
        print(f"  [SKIP] voice commands: no fixtures at {fixtures}")
    else:
        try:
            listener = VoiceCommandListener()
        except Exception as exc:
            print(f"  [SKIP] voice commands: {exc}")
        else:
            expected = (
                ("mute", "mute"),
                ("unmute", "unmute"),
                ("unmute_me", "unmute"),
                ("mute_phrase", "mute"),
                ("muted_control", None),
                ("unrelated", None),
            )
            for name, want in expected:
                path = fixtures / f"{name}.wav"
                if not path.is_file():
                    continue
                with wave.open(str(path)) as handle:
                    raw = handle.readframes(handle.getnframes())
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                listener.reset()
                heard = None
                for start in range(0, audio.size - listener.BLOCK + 1, listener.BLOCK):
                    heard = heard or listener.feed(
                        audio[start : start + listener.BLOCK], now=0.0
                    )
                check(
                    f'command: "{name}" -> {want or "nothing"}',
                    heard == want,
                    f"(heard {heard!r})",
                )

    key_errors = []
    for name in ("V", "T", "SPACE", "CAPSLOCK", "F8", "MOUSE4", "MOUSE5"):
        try:
            PushToTalkKey(name)
        except Exception as exc:
            key_errors.append(f"{name}: {exc}")
    check("key names parse", not key_errors, "" if not key_errors else str(key_errors))

    failed = [name for name, ok in checks if not ok]
    print("")
    if failed:
        print(f"{len(failed)} of {len(checks)} checks FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(checks)} detector checks passed.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Voice-activated push-to-talk for League of Legends voice chat.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to config.json")
    parser.add_argument("--device", type=int, default=None, help="input device index")
    parser.add_argument("--key", default=None, help="override the push-to-talk key")
    parser.add_argument(
        "--no-focus-check",
        action="store_true",
        help="send the key regardless of which window is focused",
    )
    parser.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    parser.add_argument("--test-key", action="store_true", help="press and release the PTT key once")
    parser.add_argument("--selftest", action="store_true", help="check the detector without a mic")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_devices:
        list_devices()
        return 0

    config = load_config(Path(args.config))
    if args.key:
        config["push_to_talk_key"] = args.key

    if args.selftest:
        return run_selftest(config)

    if args.test_key:
        try:
            test_key(config["push_to_talk_key"])
        except ValueError as exc:
            sys.exit(f"Invalid key: {exc}")
        return 0

    try:
        macro = ConsoleApp(config, args)
    except ValueError as exc:
        sys.exit(f"Configuration problem: {exc}")

    try:
        macro.run()
    except RuntimeError as exc:
        sys.exit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
