<p align="center">
  <img src="icon_1024.png" width="128" alt="DS5 Mapper icon">
</p>

<h1 align="center">DS5 Mapper</h1>

<p align="center">
  <b>macOS + Windows app that maps a DualSense (PS5) controller to keyboard + mouse, with real gyro aim.</b>
</p>

Built originally to play League of Legends in WASD mode with controller aim. Works for any game that reads keyboard/mouse.

## Features

- **Full DualSense support via SDL2** — vendor init handshake is automatic, so gyro data is clean (~0.03 dps noise floor).
- **Gyro → mouse cursor** — Steam-Input-style Local Space conversion (yaw + roll combined). Cutoff ramp + tiered smoothing (JoyShockMapper algorithm).
- **Persistent calibration** — calibrate once, reused on every launch. Recalibrate from the app when you need to.
- **Per-axis sensitivity** — yaw and pitch independently tunable via sliders or direct numeric input.
- **Full button remapping** — every DS5 input can be bound to a key, mouse button, macOS virtual keycode, or special key (shift/ctrl/tab/esc/…).
- **First-run wizard** — walks through permissions (macOS) and gyro calibration.
- **Stale-data watchdog** — cursor never runs away if the controller disconnects; signal-stability bias correction prevents long-session drift.

## Install

### macOS

1. Download **DS5-Mapper-macOS-vX.Y.zip** from the [Releases](https://github.com/MathiasMl/ds5-mapper/releases) page.
2. Unzip and drag **DS5 Mapper.app** to `/Applications`.
3. First launch: **right-click → Open** (the app is unsigned; Gatekeeper requires this once).
4. Grant **Accessibility** and **Input Monitoring** when prompted by the built-in wizard.
5. Calibrate the gyro (wizard does this) — one-time.

### Windows

1. Download **DS5-Mapper-Windows-vX.Y.zip** from the [Releases](https://github.com/MathiasMl/ds5-mapper/releases) page.
2. Unzip to any folder (e.g. `C:\Program Files\DS5 Mapper`).
3. Run **DS5 Mapper.exe**. If SmartScreen blocks it: **More info → Run anyway**.
4. Calibrate the gyro (wizard does this) — one-time.

Plug in the DualSense via USB or pair via Bluetooth before launching.

## Build from source

Prereqs: Python 3.10+, [Homebrew](https://brew.sh) (macOS), Git.

```bash
git clone https://github.com/MathiasMl/ds5-mapper.git
cd ds5-mapper
```

### macOS

```bash
./setup.sh                      # venv, hidapi, Python deps
./venv/bin/python3 app.py       # run from source
./build.sh                      # produces dist/DS5 Mapper.app
```

### Windows

```powershell
python -m venv venv
.\venv\Scripts\pip install pysdl2 pysdl2-dll pynput pyinstaller
.\venv\Scripts\python app.py                       # run from source
.\venv\Scripts\pyinstaller windows.spec --noconfirm # build dist\DS5 Mapper\DS5 Mapper.exe
```

## Configuration

Settings live in the user config file:

| OS | Path |
|---|---|
| macOS   | `~/.config/ds5-league/config.json` |
| Windows | `%APPDATA%\ds5-league\config.json` |

Edit directly or use the GUI (Gyro / Buttons / Stick tabs). Click **Save & Restart mapper** to apply.

Calibration is in `calibration.json` alongside. Delete it (or click *Calibrate Gyro…*) to redo.

## Runtime controls

- **PS button** — pause/resume the mapper
- **Touchpad (double-tap)** — toggle gyro on/off
- **Recalibrate gyro** — click *Calibrate Gyro…* in the app window
- Everything else — whatever you've bound in the Buttons tab

## How it works

- **Reading input**: SDL2's `GameController` API with sensor support (`SDL_SENSOR_GYRO`). SDL does the DualSense vendor init that raw HID skips, giving clean calibrated angular velocity.
- **Writing output**: `pynput` synthesizes OS-level keyboard + mouse events. Same category as Steam Input / AutoHotkey — not injected into any game process, Vanguard-compatible.
- **Gyro math**: JoyShockMapper-style pipeline — bias calibration → hard cutoff + linear recovery ramp → tiered smoothing (immediate above threshold, full moving-average below) → rad/s-to-pixels integration.
- **Stability**: signal-variance bias correction runs continuously; if readings are stable but non-zero for ~2 s, that's drift and gets subtracted out automatically.

## License

MIT. See [LICENSE](LICENSE).
