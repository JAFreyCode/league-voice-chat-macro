# League Voice Chat Macro

Hands-free push-to-talk for League of Legends voice chat.

League only transmits while you hold its push-to-talk key. This program listens
to your microphone and holds that key for you while you are actually speaking,
then releases it when you stop.

Detection is done by a neural speech model (Silero VAD) by default. It tells
speech apart from fans, hiss and keyboard clatter far more reliably than a
volume threshold, and it works the same whether you whisper or shout.

## Quick start

1. **In League:** Settings -> Voice -> set the mode to **Push to Talk** and bind
   the key to **V** (or whatever you set in the app).
2. Double-click **`run.bat`**. The first run creates `.venv` and installs
   `numpy`, `sounddevice` and `onnxruntime`.
3. In the window, pick your microphone from the **Microphone** dropdown.
4. Stay quiet for about 4 seconds while it learns your room, then talk normally.
   The pill turns green while your voice is being transmitted.

## Installing from a fresh clone

The third-party model files are not committed (about 72 MB in total). Both are
permissively licensed: Silero VAD is MIT, the Vosk English model is Apache 2.0.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

mkdir models
curl -fsSL -o models/silero_vad.onnx https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
curl -fsSL -o vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
tar -xf vosk.zip -C models
del vosk.zip
```

Without them the app still runs, but falls back to the older spectral detector
and voice commands stay off. `config.json` is created on first run and is not
tracked by git, so your microphone and keybind stay local.

## The window

| Control | What it does |
| ------- | ------------ |
| **Microphone** | Which input device to listen to. `Windows default microphone` follows the system setting. **Refresh** re-scans after you plug something in. |
| **Push-to-talk key** | Click the key button, then press the key you bound in League. Mouse 4/5 work too; mouse 1-3 are refused because simulating them would click in game. |
| **Detector** | `Neural (Silero VAD)` is the accurate option and the default. `Spectral (classic)` is the older threshold detector. `Auto` uses the neural model when it can load and falls back otherwise. |
| **Voice confidence** | How sure the speech model must be before the key goes down. Lower catches quiet speech but may react to a TV or a teammate in the room. |
| **Only send the key while League has focus** | Safety: the key is never sent into Discord, a browser or the desktop. |
| **Voice commands** | Say "mute" to stop the macro holding the key, "unmute" to resume. |
| **Sensitivity** | *Spectral only.* How far, in dB, a sound must stand out above the learned noise profile (per frequency) to count as voice. |
| **Minimum level** | *Spectral only.* Absolute floor. Anything quieter than this never opens the gate. |
| **Reaction time** | How much speech is needed before the key goes down. Lower = snappier, higher = fewer false starts. |
| **Hold after speech** | How long the key stays held after you stop, so word endings are not clipped. |
| **Calibrate noise floor** | Re-learn the room. Do this after moving, changing mic gain, or when a new fan/AC starts. |
| **Test key** | Holds the key for one second so you can confirm it is the right one. |

Under the meter: `LEVEL` is raw loudness, `SNR` is how much you stick out above
the learned noise, `GATE` is where the spectral gate would open, `FLOOR` is the
learned noise level, and the last figure is `VAD` (the speech model's score,
0.7-1.0 for speech, under 0.05 for noise) or `VOICE` (pitch periodicity) when
the spectral detector is selected. The dashed line on the bar is the gate.

Every change is written to `config.json` immediately. Global hotkeys still work:
**F9** pause/resume, **F10** recalibrate.

## Voice commands

Say **"mute"** and the macro stops holding the key (releasing it if it was
held), so nothing more is transmitted. Say **"unmute"** to turn it back on.
Listening never stops - that is how it hears you ask to unmute.

Recognition is offline, using Vosk with a 68 MB English model in
`models/vosk-model-small-en-us-0.15`. The decoder is restricted to a grammar of
just those words, which makes it about 60x faster than real time and stops it
inventing sentences. A "mute" is normally acted on roughly 0.4 s after you say
it.

The rules are deliberately conservative, because a false "unmute" would talk
over your team:

* a partial transcript only fires when it is *exactly* the command, so
  "I muted my phone" does not mute the macro once the sentence finishes;
* a completed sentence may contain at most one unrecognised word;
* "muted", "muting", and "mute" buried in a longer sentence are ignored;
* commands are rate-limited to one every 1.2 s.

The pill shows **MUTED** while it is off, F9 still toggles it, and unticking the
checkbox disables voice commands entirely.

## How detection works

There are two detectors; the **Detector** dropdown picks one.

### Neural (default)

A small pretrained speech model (Silero VAD: 2 MB, MIT licence, runs on ONNX
Runtime on the CPU, about a millisecond per 32 ms of audio) scores each chunk
with the probability that a human is speaking. It was trained on real speech in
real noise, so it does not have to guess from loudness or pitch:

* it is **level independent** - quiet speech scores the same as loud speech, so
  low mic gain no longer causes missed words;
* fans, air conditioning, hiss, mains hum and keyboard clatter score under 0.05
  where speech scores 0.7-1.0;
* it needs no learned profile, so calibration matters far less.

The key opens when `VAD` reaches **Voice confidence** (0.6 by default, for three
consecutive blocks) and stays held until it drops below 0.35. A hard silence guard (-70 dBFS) keeps a muted or
unplugged microphone from ever holding the key down.

This is *voice detection*, not noise suppression: nothing here cleans the audio
League transmits. It only decides when to hold the key. To clean what teammates
hear, use League's own voice threshold or route the mic through a suppressor
such as a virtual audio cable.

### Spectral (classic)

Used when the model cannot load, or if you pick it. A plain volume meter goes
off for fans, keyboards and desk bumps; this asks a better question: *how much
of this sound sticks out above the noise this microphone actually produces, and
does it look like a voice?*

1. **Per-frequency noise profile.** Calibration records the average spectrum of
   your room, bin by bin (about 150 frames). A fan, hum or air conditioner is
   then *subtracted*, not compared against - so noise at any volume is invisible
   as long as it is steady.
2. **Spectral-subtraction SNR.** Each 20 ms frame is scored by how much energy
   it has above that profile. The gate needs `Sensitivity` dB above the floor,
   so a loud room and a quiet room behave the same way.
3. **Voice-band shape.** At least 55% of the energy must be between 150 Hz and
   4 kHz, which rejects rumble, thumps and bass-heavy noise.
4. **Spectral flatness.** Hiss and keyboard clatter are spectrally flat (energy
   spread evenly); voice is peaky. Flat frames are rejected even when loud.
5. **Voicing (pitch).** A normalized autocorrelation finds periodicity between
   70 and 350 Hz. Speech is periodic; fans, hiss and most machinery are not.
6. **Spectral density.** A pure tone or a mains hum occupies 2-4 frequency bins;
   a voice spreads harmonics across a dozen or more. Both of the last two checks
   have to pass, which is what kills beeps, hum and fan whine.
7. **Stationarity guard.** Nothing human holds a perfectly frozen spectrum for
   a second. If a sound does, the gate closes and the profile absorbs it.
8. **Attack and hold.** Several consistent frames are required before the key
   goes down (one noisy frame can never trigger it) and it stays held briefly
   after you stop, so pauses inside a sentence do not chop the audio.

### Calibration accuracy

Calibration runs in two stages, about 4.2 seconds total:

* **Learning** (3 s): each frame's spectrum is recorded; frames polluted by a
  cough, a door or somebody talking are thrown away (anything well above the
  median), then the profile is the mean of what is left. The spread of those
  frames is reported as **room stability**.
* **Verification** (1.2 s): with the profile in place, it measures the residual
  noise SNR and sets the opening threshold *above* the 95th percentile of that
  residual, automatically. If your room is noisier than the default sensitivity
  assumes, the threshold is raised instead of firing constantly. It never
  raises more than 8 dB above your setting.

The status line reports the result: floor, room stability, residual noise and
the final opening threshold. A "room was noisy" warning means the measurement
was unreliable and is worth repeating in a quieter moment.

## Command line

The console version is still there:

```
run-console.bat                        # text UI with a live meter
python voice_ptt.py --list-devices     # print mic indexes
python voice_ptt.py --device 3         # force a device
python voice_ptt.py --test-key         # press and release the PTT key once
python voice_ptt.py --selftest         # run the detector test suite, no mic needed
```

`--selftest` feeds synthetic silence, room noise, keyboard clicks, loud hiss,
mains hum, a pure tone, speech, and speech mixed with fan noise through the same
code path and checks that only speech opens the gate. It then repeats the same
checks through the neural detector using a real recorded voice
(`tests/speech_16k.wav`), including a version 15x quieter. Finally it feeds
recorded "mute" / "unmute" phrases (`tests/commands/`) through the command
listener and checks that commands are caught and look-alikes ("I muted my
phone") are not.

## Configuration (`config.json`)

The app writes this file for you; it is also fine to edit by hand while the app
is closed.

| Option | Default | Meaning |
| ------ | ------- | ------- |
| `push_to_talk_key` | `"V"` | Key to hold. Letters, digits, `SPACE`, `CAPSLOCK`, `F1`-`F24`, `MOUSE4`, `MOUSE5`. |
| `input_device` | `null` | Mic index. `null` = Windows default. |
| `sample_rate` | `0` | `0` = use the microphone's native rate. |
| `only_when_league_focused` | `true` | Only send the key while a League window has focus. |
| `focus_processes` | League executables | Which window names count as "League". |
| `calibration_seconds` | `3.0` | Length of the noise-learning stage. |
| `verify_seconds` | `1.2` | Length of the residual-noise verification stage. |
| `open_offset_db` | `8.0` | The **Sensitivity** slider: required SNR above the noise profile. |
| `absolute_floor_db` | `-55.0` | The **Minimum level** slider. |
| `attack_ms` | `60` | The **Reaction time** slider. |
| `release_ms` | `320` | The **Hold after speech** slider. |
| `close_hysteresis_db` | `5.0` | How far below the opening threshold you must fall before the gate closes. |
| `highpass_hz` | `120` | Rumble below this is ignored. |
| `voice_band_low_hz` / `voice_band_high_hz` | `150` / `4000` | The band that counts as voice. |
| `min_voice_ratio` | `0.55` | Share of energy that must be in the voice band. |
| `max_flatness` | `0.45` | Reject frames flatter than this (hiss, clatter). |
| `min_voicing` | `0.30` | Required pitch periodicity. Lower it for a breathy or quiet voice. |
| `min_spectral_bins` | `6` | How many frequency bins must carry energy (rejects tones and hum). |
| `over_subtraction` | `1.0` | How aggressively the learned profile is subtracted. |
| `noise_floor_adapt_rate` | `0.02` | How fast the profile follows a changing room while idle. |
| `detector` | `"auto"` | `"auto"`, `"neural"` or `"spectral"`. `"neural"` refuses to start if the model cannot load. |
| `vad_threshold` | `0.6` | The **Voice confidence** slider: speech score needed to open. |
| `vad_release_threshold` | `0.35` | Score below which the key is released. |
| `vad_model` | `""` | Optional path to a different `silero_vad.onnx`. |
| `voice_commands` | `true` | Listen for "mute" / "unmute". |
| `command_model` | `""` | Optional path to a different Vosk model folder. |
| `toggle_hotkey` / `recalibrate_hotkey` | `F9` / `F10` | Global hotkeys. |
| `show_meter` | `true` | Live meter in the console version. |
| `log_file` | `""` | Optional path for a timestamped log of key presses. |

## Troubleshooting

- **Background noise still triggers it.** With the neural detector, raise
  **Voice confidence** to 0.65-0.75. With the spectral one, raise
  **Sensitivity** to 11-14 and recalibrate (F10).
- **It misses quiet speech or the start of words.** Lower **Voice confidence**
  to 0.35-0.4 and **Reaction time** to 40 ms. On the spectral detector, also
  lower **Sensitivity** to 6 and `min_voicing` to `0.20`.
- **The detector says it is unavailable.** `pip install onnxruntime` in
  `.venv` and make sure `models/silero_vad.onnx` exists; or set `detector` to
  `"spectral"` in `config.json`.
- **"Voice commands off: ..." in the event line.** `pip install vosk` in
  `.venv` and make sure the `models/vosk-model-small-en-us-0.15` folder exists.
- **It reacts to the word "mute" in normal conversation.** That is the
  trade-off of a keyword command; untick **Voice commands**, or say "unmute"
  to recover.
- **It does not react to your voice at all.** Watch `VOICE` while you talk: it
  should be 0.4+. If it stays low, your mic may be picking up mostly room noise
  - move closer, raise the gain, and set `min_voicing` to `0.15`.
- **It reacts to keyboard clatter but not voice.** Raise **Minimum level**
  slightly and check that `VOICE` is high when you speak.
- **`LEAGUE NOT FOCUSED` in the pill.** The game window is not in front; the
  macro deliberately stays silent. Alt-tab back, or untick the focus option.
- **"This microphone is delivering almost no signal".** Wrong device, a muted
  mic, or a virtual device with no input. Pick your real microphone.
- **The key types into other windows.** Keep the focus option ticked. If League
  runs elevated, run the app as administrator too.
- **Nothing is transmitted in game.** Check the key matches League's PTT
  binding and use **Test key**.

`gui-error.log` is written next to the app if the window ever fails to start.

## Notes

- Only synthetic keystrokes are sent, via `SendInput` with real hardware scan
  codes (the same mechanism as a keyboard). No memory reading, no DLL
  injection, no network traffic.
- `MOUSE1`/`MOUSE2` are deliberately refused as PTT keys because simulating
  them would click in game.
- The first syllable can be clipped by **Reaction time**; that is inherent to
  voice-activated PTT. Keep it at 40-60 ms to minimise it.
- The push-to-talk key is released when the app exits, the window is closed or
  the PC logs off, so voice chat cannot get stuck transmitting.
