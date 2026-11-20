#!/usr/bin/env python3
"""Graphical control panel for the League voice chat push-to-talk macro.

Everything the console version can do, with widgets: pick the microphone, press
a key to rebind, drag the sensitivity until the meter behaves, watch the live
level, start/stop, recalibrate.

Run with:  pythonw voice_ptt_gui.py    (or run.bat)
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import voice_ptt as core

BG = "#14161c"
CARD = "#1c2028"
CARD2 = "#252b36"
LINE = "#2e3542"
TEXT = "#e8ecf3"
DIM = "#8b94a5"
ACCENT = "#4f9dff"
ACCENT_DARK = "#08111d"
GOOD = "#3ddc84"
WARN = "#ffc857"
BAD = "#ff6b6b"

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI Semibold", 9)
FONT_TITLE = ("Segoe UI Semibold", 16)
FONT_MONO = ("Consolas", 9)

WINDOW_WIDTH = 800
WINDOW_HEIGHT = 672
METER_LOW_DB = -70.0
METER_HIGH_DB = -10.0

DETECTOR_CHOICES = ("Neural (Silero VAD)", "Auto", "Spectral (classic)")
DETECTOR_VALUES = {
    "Neural (Silero VAD)": "neural",
    "Auto": "auto",
    "Spectral (classic)": "spectral",
}
DETECTOR_LABELS = {value: label for label, value in DETECTOR_VALUES.items()}


class VoiceMacroWindow:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.engine = None
        self.messages = queue.Queue()
        self.device_map = []
        self._save_job = None
        self.capture_active = False

        self._build_style()
        self._build_layout()
        self._refresh_devices()
        self._center_window()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._start_engine)
        self.root.after(60, self.tick)

    # ------------------------------------------------------------------ #
    # Look and feel
    # ------------------------------------------------------------------ #

    def _build_style(self):
        self.root.configure(bg=BG)
        self.root.title("League Voice Chat Macro")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, font=FONT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Dim.TLabel", background=CARD, foreground=DIM)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
        style.configure("Sub.TLabel", background=BG, foreground=DIM)
        style.configure("Mono.TLabel", background=CARD, foreground=TEXT, font=FONT_MONO)
        style.configure("MonoDim.TLabel", background=CARD, foreground=DIM, font=FONT_MONO)

        style.configure(
            "TButton",
            background=CARD2,
            foreground=TEXT,
            borderwidth=0,
            focusthickness=0,
            padding=(13, 7),
            font=FONT,
        )
        style.map(
            "TButton",
            background=[("pressed", "#39434f"), ("active", "#303845")],
            foreground=[("disabled", "#5c6473")],
        )
        style.configure(
            "Accent.TButton", background=ACCENT, foreground=ACCENT_DARK, font=FONT_BOLD
        )
        style.map(
            "Accent.TButton",
            background=[("pressed", "#2f7fd9"), ("active", "#6fb3ff")],
            foreground=[("disabled", "#4a5b70")],
        )

        style.configure(
            "Card.TCheckbutton",
            background=CARD,
            foreground=TEXT,
            indicatorbackground=CARD2,
            indicatorforeground=ACCENT,
        )
        style.map(
            "Card.TCheckbutton",
            background=[("active", CARD)],
            foreground=[("active", TEXT)],
        )

        style.configure(
            "Card.Horizontal.TScale",
            background=CARD,
            troughcolor=CARD2,
            bordercolor=CARD,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

        style.configure(
            "TCombobox",
            fieldbackground=CARD2,
            background=CARD2,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=LINE,
            lightcolor=CARD2,
            darkcolor=CARD2,
            selectbackground=CARD2,
            selectforeground=TEXT,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD2)],
            foreground=[("readonly", TEXT)],
        )

        self.root.option_add("*TCombobox*Listbox.background", CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_DARK)
        self.root.option_add("*TCombobox*Listbox.font", FONT)

    def _center_window(self):
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - WINDOW_WIDTH) // 2)
        y = max(0, (self.root.winfo_screenheight() - WINDOW_HEIGHT) // 3)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")
        self.root.minsize(760, 650)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_layout(self):
        outer = ttk.Frame(self.root, style="TFrame", padding=(22, 18, 22, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="League Voice Chat Macro", style="Title.TLabel").pack(side="left")
        self.status_pill = tk.Label(
            header,
            text="STARTING",
            bg=CARD2,
            fg=DIM,
            font=FONT_BOLD,
            padx=14,
            pady=5,
        )
        self.status_pill.pack(side="right")

        ttk.Label(
            outer,
            text="Hands-free push-to-talk - the key is held only while you actually speak",
            style="Sub.TLabel",
        ).pack(fill="x", pady=(3, 12))

        self._build_meter(outer)
        self._build_settings(outer)
        self._build_actions(outer)
        self._build_footer(outer)

    def _build_meter(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=14)
        card.pack(fill="x", pady=(0, 10))

        self.meter = tk.Canvas(card, height=48, bg=CARD, highlightthickness=0)
        self.meter.pack(fill="x")

        info = ttk.Frame(card, style="Card.TFrame")
        info.pack(fill="x", pady=(10, 0))
        self.level_label = ttk.Label(info, text="LEVEL      --", style="Mono.TLabel")
        self.level_label.pack(side="left")
        self.snr_label = ttk.Label(info, text="SNR    --", style="MonoDim.TLabel")
        self.snr_label.pack(side="left", padx=(14, 0))
        self.gate_label = ttk.Label(info, text="GATE   --", style="MonoDim.TLabel")
        self.gate_label.pack(side="left", padx=(14, 0))
        self.voice_label = ttk.Label(info, text="VOICE  --", style="MonoDim.TLabel")
        self.voice_label.pack(side="left", padx=(14, 0))
        self.floor_label = ttk.Label(info, text="FLOOR  --", style="MonoDim.TLabel")
        self.floor_label.pack(side="left", padx=(14, 0))

    def _build_settings(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(card, text="Microphone", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=6
        )
        self.device_box = ttk.Combobox(card, state="readonly", width=42)
        self.device_box.grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=6)
        self.device_box.bind("<<ComboboxSelected>>", self._on_device)
        ttk.Button(card, text="Refresh", command=self._refresh_devices).grid(
            row=row, column=2, pady=6
        )

        row += 1
        ttk.Label(card, text="Push-to-talk key", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=6
        )
        key_row = ttk.Frame(card, style="Card.TFrame")
        key_row.grid(row=row, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=6)
        self.key_button = ttk.Button(
            key_row,
            text=self.config["push_to_talk_key"],
            width=15,
            command=self._begin_capture,
        )
        self.key_button.pack(side="left")
        ttk.Label(
            key_row,
            text="Bind the same key in League -> Settings -> Voice",
            style="Dim.TLabel",
        ).pack(side="left", padx=(12, 0))

        row += 1
        ttk.Label(card, text="Detector", style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=6
        )
        self.detector_box = ttk.Combobox(
            card, state="readonly", width=40, values=list(DETECTOR_CHOICES)
        )
        self.detector_box.grid(
            row=row, column=1, columnspan=2, sticky="w", padx=(14, 10), pady=6
        )
        self.detector_box.set(
            DETECTOR_LABELS.get(
                str(self.config.get("detector", "auto") or "auto").strip().lower(),
                DETECTOR_CHOICES[1],
            )
        )
        self.detector_box.bind("<<ComboboxSelected>>", self._on_detector)

        row += 1
        self.focus_var = tk.BooleanVar(value=bool(self.config["only_when_league_focused"]))
        ttk.Checkbutton(
            card,
            text="Only send the key while League has focus",
            variable=self.focus_var,
            style="Card.TCheckbutton",
            command=self._on_focus_toggle,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 4))

        row += 1
        self.commands_var = tk.BooleanVar(
            value=bool(self.config.get("voice_commands", True))
        )
        ttk.Checkbutton(
            card,
            text='Voice commands: say "mute" or "unmute"',
            variable=self.commands_var,
            style="Card.TCheckbutton",
            command=self._on_commands,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 4))

        row += 1
        ttk.Separator(card, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 12)
        )

        row += 1
        self.sensitivity_scale = self._add_slider(
            card,
            row,
            "Sensitivity",
            3.0,
            20.0,
            float(self.config["open_offset_db"]),
            lambda value: f"{value:.0f} dB above noise",
            self._on_sensitivity,
        )

        row += 1
        self.confidence_scale = self._add_slider(
            card,
            row,
            "Voice confidence",
            0.2,
            0.9,
            float(self.config.get("vad_threshold", 0.5)),
            lambda value: f"{value:0.2f} speech score",
            self._on_confidence,
            0.05,
        )

        row += 1
        self.floor_scale = self._add_slider(
            card,
            row,
            "Minimum level",
            -70.0,
            -30.0,
            float(self.config["absolute_floor_db"]),
            lambda value: f"{value:.0f} dBFS",
            self._on_floor,
        )

        row += 1
        self.attack_scale = self._add_slider(
            card,
            row,
            "Reaction time",
            20.0,
            200.0,
            float(self.config["attack_ms"]),
            lambda value: f"{value:.0f} ms",
            self._on_attack,
            10.0,
        )

        row += 1
        self.release_scale = self._add_slider(
            card,
            row,
            "Hold after speech",
            100.0,
            800.0,
            float(self.config["release_ms"]),
            lambda value: f"{value:.0f} ms",
            self._on_release,
            20.0,
        )

        row += 1
        ttk.Label(
            card,
            text="Neural runs a speech model on your mic and is the accurate option; "
            "Spectral is the older threshold detector. Voice confidence is how sure the "
            "model must be before the key goes down (lower catches quiet speech but may "
            "react to background voices). Sensitivity and Minimum level only apply to the "
            "spectral detector. Reaction time is how much speech is needed before the key "
            "goes down.",
            style="Dim.TLabel",
            wraplength=610,
            justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _add_slider(self, parent, row, label, low, high, value, formatter, on_change, step=1.0):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=3
        )
        scale = ttk.Scale(
            parent,
            from_=low,
            to=high,
            value=value,
            style="Card.Horizontal.TScale",
            length=260,
        )
        scale.grid(row=row, column=1, sticky="ew", padx=(14, 12), pady=3)

        value_label = ttk.Label(parent, text=formatter(value), style="MonoDim.TLabel", width=20)
        value_label.grid(row=row, column=2, sticky="w")

        def handle(raw):
            number = round(float(raw) / step) * step
            value_label.config(text=formatter(number))
            on_change(number)

        scale.configure(command=handle)
        return scale

    def _build_actions(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=12)

        self.run_button = ttk.Button(
            bar,
            text="Start listening",
            style="Accent.TButton",
            width=18,
            command=self._toggle_running,
        )
        self.run_button.pack(side="left")
        ttk.Button(bar, text="Calibrate noise floor", command=self._recalibrate).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(bar, text="Test key", command=self._test_key).pack(
            side="left", padx=(10, 0)
        )

    def _build_footer(self, parent):
        footer = ttk.Frame(parent)
        footer.pack(fill="x", side="bottom")

        self.event_var = tk.StringVar(value="Ready.")
        ttk.Label(
            footer, textvariable=self.event_var, style="Sub.TLabel", wraplength=740
        ).pack(anchor="w")
        ttk.Label(
            footer,
            text="F9 pause / resume     F10 recalibrate     changes save automatically",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------------ #
    # Engine wiring
    # ------------------------------------------------------------------ #

    def _engine_event(self, message, kind="info"):
        # Called from the audio thread; hand it to the Tk thread.
        self.messages.put(("event", (message, kind)))

    def _start_engine(self):
        if self.engine is not None:
            return
        engine = core.VoiceEngine(self.config, on_event=self._engine_event, use_hotkeys=True)
        try:
            engine.start()
        except (RuntimeError, ValueError) as exc:
            self.event_var.set(str(exc).splitlines()[0])
            messagebox.showerror("Microphone", str(exc))
            return
        self.engine = engine
        self.run_button.config(text="Stop listening")
        self._refresh_devices()

    def _stop_engine(self):
        if self.engine is None:
            return
        self.engine.shutdown()
        self.engine = None
        self.run_button.config(text="Start listening")
        self.event_var.set("Stopped. The push-to-talk key has been released.")

    def _toggle_running(self):
        if self.engine is None:
            self._start_engine()
        else:
            self._stop_engine()

    def _recalibrate(self):
        if self.engine is None:
            self.event_var.set("Start listening first, then calibrate.")
            return
        self.engine.recalibrate()

    def _test_key(self):
        if self.engine is None:
            self.event_var.set("Start listening first, then test the key.")
            return
        key = self.engine.key
        try:
            key.press()
        except OSError as exc:
            messagebox.showerror("Key test", f"Could not send the key: {exc}")
            return
        self.event_var.set(f"Holding {key.name} for one second...")
        self.root.after(900, self._release_test_key)

    def _release_test_key(self):
        if self.engine is None:
            return
        try:
            self.engine.key.release()
        except Exception:
            pass
        self.event_var.set("Key released.")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def _refresh_devices(self):
        try:
            devices = core.sd.query_devices()
        except Exception as exc:
            self.event_var.set(f"Could not list microphones: {exc}")
            return

        labels = ["Windows default microphone"]
        mapping = [None]
        for index, info in enumerate(devices):
            if int(info.get("max_input_channels", 1)) < 1:
                continue
            name = str(info["name"])
            if len(name) > 44:
                name = name[:41] + "..."
            labels.append(f"{name}   [{index}]")
            mapping.append(index)

        self.device_map = mapping
        self.device_box.configure(values=labels)

        current = self.config.get("input_device")
        if current is None:
            self.device_box.current(0)
        else:
            try:
                self.device_box.current(mapping.index(int(current)))
            except ValueError:
                self.device_box.current(0)

    def _on_device(self, _event=None):
        index = self.device_map[self.device_box.current()]
        self.config["input_device"] = index
        self._schedule_save()
        if self.engine is None:
            return
        try:
            self.engine.set_device(index)
        except RuntimeError as exc:
            messagebox.showerror("Microphone", str(exc))
            self._refresh_devices()

    def _on_focus_toggle(self):
        self.config["only_when_league_focused"] = bool(self.focus_var.get())
        self._apply()

    def _on_commands(self):
        self.config["voice_commands"] = bool(self.commands_var.get())
        self._apply()

    def _on_sensitivity(self, value):
        self.config["open_offset_db"] = float(value)
        self._apply()

    def _on_confidence(self, value):
        self.config["vad_threshold"] = float(value)
        self.config["vad_release_threshold"] = round(max(0.1, float(value) - 0.15), 2)
        self._apply()

    def _on_detector(self, _event=None):
        self.config["detector"] = DETECTOR_VALUES.get(self.detector_box.get(), "auto")
        self._apply()
        if self.engine is not None and self.engine.gate is not None:
            problem = self.engine.gate.vad_error
            if problem:
                self.event_var.set(f"Neural detector unavailable: {problem}")

    def _on_floor(self, value):
        self.config["absolute_floor_db"] = float(value)
        self._apply()

    def _on_attack(self, value):
        self.config["attack_ms"] = float(value)
        self._apply()

    def _on_release(self, value):
        self.config["release_ms"] = float(value)
        self._apply()

    def _apply(self):
        if self.engine is not None:
            self.engine.apply_settings()
        self._schedule_save()

    def _schedule_save(self):
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self._save_now)

    def _save_now(self):
        self._save_job = None
        try:
            core.save_config(self.config)
        except OSError as exc:
            self.event_var.set(f"Could not save config.json: {exc}")

    # ------------------------------------------------------------------ #
    # Key capture
    # ------------------------------------------------------------------ #

    def _begin_capture(self):
        if self.capture_active:
            return
        self.capture_active = True
        self.key_button.config(text="Press any key...", state="disabled")

        def worker():
            name, error = core.capture_key(8.0)
            self.messages.put(("captured", (name, error)))

        threading.Thread(target=worker, daemon=True, name="capture").start()

    def _finish_capture(self, name, error):
        self.capture_active = False
        if name:
            try:
                if self.engine is not None:
                    self.engine.set_key(name)
                self.config["push_to_talk_key"] = name
                self._schedule_save()
                self.key_button.config(text=name, state="normal")
                self.event_var.set(
                    f"Push-to-talk key is now {name}. Bind the same key in League."
                )
                return
            except ValueError as exc:
                error = str(exc)
        self.key_button.config(text=self.config["push_to_talk_key"], state="normal")
        self.event_var.set(error or "No key detected.")

    # ------------------------------------------------------------------ #
    # Live updates
    # ------------------------------------------------------------------ #

    def tick(self):
        self._drain()
        snapshot = self.engine.snapshot() if self.engine is not None else None
        self._update_status(snapshot)
        self._draw_meter(snapshot)
        self.root.after(60, self.tick)

    def _drain(self):
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                return
            if kind == "event":
                self.event_var.set(payload[0])
            elif kind == "captured":
                self._finish_capture(*payload)

    def _set_pill(self, text, background, foreground):
        self.status_pill.config(text=text, bg=background, fg=foreground)

    def _update_status(self, snapshot):
        if snapshot is None:
            self._set_pill("STOPPED", "#39404e", DIM)
            self.level_label.config(text="LEVEL      --")
            self.snr_label.config(text="SNR    --")
            self.gate_label.config(text="GATE   --")
            self.voice_label.config(text="VOICE  --")
            self.floor_label.config(text="FLOOR  --")
            return

        if snapshot["calibrating"]:
            percent = int(snapshot["calibration_progress"] * 100)
            self._set_pill(f"CALIBRATING {percent}%", "#2b4460", ACCENT)
        elif not snapshot["enabled"]:
            self._set_pill("MUTED (F9)", "#4d3d15", WARN)
        elif snapshot["speaking"]:
            self._set_pill("TALKING", "#1b5238", GOOD)
        elif not snapshot["focus_ok"]:
            self._set_pill("LEAGUE NOT FOCUSED", "#2a2f3a", DIM)
        else:
            self._set_pill("LISTENING", "#1e3350", ACCENT)

        self.level_label.config(text=f"LEVEL {snapshot['level_db']:7.1f} dB")
        self.snr_label.config(text=f"SNR {snapshot['snr_db']:6.1f} dB")
        self.gate_label.config(text=f"GATE {snapshot['threshold_db']:6.1f} dB")
        if snapshot.get("detector") == "neural":
            self.voice_label.config(text=f"VAD {snapshot.get('vad', 0.0):0.2f}")
        else:
            self.voice_label.config(text=f"VOICE {snapshot['voicing']:0.2f}")
        self.floor_label.config(text=f"FLOOR {snapshot['floor_db']:6.1f} dB")

    def _x_for(self, db, width):
        span = METER_HIGH_DB - METER_LOW_DB
        fraction = (db - METER_LOW_DB) / span
        return max(0.0, min(1.0, fraction)) * width

    def _draw_meter(self, snapshot):
        canvas = self.meter
        canvas.delete("all")
        width = max(canvas.winfo_width(), 120)
        height = int(canvas.winfo_height()) or 48

        canvas.create_rectangle(0, 0, width, height, fill="#10131a", outline="")
        for db in range(-60, -9, 10):
            x = self._x_for(db, width)
            canvas.create_line(x, 0, x, height, fill="#1e2330")

        if snapshot is not None:
            level = snapshot["level_db"]
            speaking = snapshot["enabled"] and snapshot["speaking"]
            fill = GOOD if speaking else ACCENT
            x = self._x_for(level, width)
            if x > 1:
                canvas.create_rectangle(0, 0, x, height, fill=fill, outline="")

            threshold_x = self._x_for(snapshot["threshold_db"], width)
            canvas.create_line(
                threshold_x, 0, threshold_x, height, fill="#ffffff", dash=(3, 2)
            )
            canvas.create_text(
                10,
                height / 2,
                anchor="w",
                text=f"{level:.1f} dB",
                fill="#08111d" if x > 74 else TEXT,
                font=FONT_MONO,
            )
            canvas.create_text(
                width - 10,
                height / 2,
                anchor="e",
                text="above gate" if level >= snapshot["threshold_db"] else "below gate",
                fill=DIM,
                font=FONT_MONO,
            )
        else:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Not listening",
                fill=DIM,
                font=FONT_MONO,
            )

    # ------------------------------------------------------------------ #

    def on_close(self):
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
            try:
                core.save_config(self.config)
            except OSError:
                pass
        if self.engine is not None:
            self.engine.shutdown()
            self.engine = None
        self.root.destroy()


def main():
    try:
        config = core.load_config()
        root = tk.Tk()
        VoiceMacroWindow(root, config)
        root.mainloop()
    except Exception:
        import traceback

        detail = traceback.format_exc()
        try:
            (Path(__file__).resolve().parent / "gui-error.log").write_text(
                detail, encoding="utf-8"
            )
        except OSError:
            pass
        try:
            messagebox.showerror(
                "League Voice Chat Macro",
                "The app crashed:\n\n" + detail[-900:],
            )
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
