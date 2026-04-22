#!/usr/bin/env python3
"""
DS5 -> League of Legends mapper, built on SDL2 directly (pysdl2).

Why this one works: SDL 2.30+ handles the DualSense vendor handshake internally
when you enable its gyro sensor, giving clean calibrated angular velocity
(~0.03 deg/sec noise floor vs. the ~8 dps we got from raw simplified-mode HID).

Inputs (buttons, sticks, triggers, gyro) all come from the same SDL game
controller instance, so there's no HID contention.

Mapping:
  L stick              -> WASD movement
  R stick              -> mouse cursor (additive with gyro)
  Gyro                 -> mouse cursor (clean, 0.03 dps noise)
  L1                   -> MB2 (Q ability)
  L2 (analog)          -> LShift (W ability)
  R1                   -> E (E ability)
  R2 (analog)          -> R (R ability)
  Cross ✕              -> left click (attack-move via [WASD] override)
  Circle ○             -> V (Role Quest)
  Square □             -> Q (D summoner)
  Triangle △           -> F (F summoner)
  D-pad                -> 1/2/3/4 (items + trinket)
  L3 / R3 / Touchpad   -> 5/6/7 (items 4/5/6)
  Share                -> Tab
  Options              -> Esc
  Touchpad double-tap  -> toggle gyro on/off
  PS button            -> pause/resume mapper
"""
import atexit
import ctypes
import json
import math
import os
import signal
import sys
import time

import sdl2
from pynput.keyboard import Controller as KeyCtl, Key, KeyCode
from pynput.mouse import Controller as MouseCtl, Button as MBtn


# Cross-platform config dir: macOS uses ~/.config/ds5-league (XDG-style),
# Windows uses %APPDATA%\ds5-league.
def _user_config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ds5-league")
    return os.path.expanduser("~/.config/ds5-league")


USER_CFG_DIR     = _user_config_dir()
CONFIG_PATH      = os.path.join(USER_CFG_DIR, "config.json")
CALIBRATION_PATH = os.path.join(USER_CFG_DIR, "calibration.json")
os.makedirs(USER_CFG_DIR, exist_ok=True)


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_calibration():
    """Return a dict {'bias_pitch','bias_yaw','bias_roll','saved_at'} or None."""
    try:
        with open(CALIBRATION_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_calibration(bias_pitch, bias_yaw, bias_roll, noise_pitch_dps, noise_yaw_dps):
    data = {
        "bias_pitch": bias_pitch,
        "bias_yaw":   bias_yaw,
        "bias_roll":  bias_roll,
        "noise_pitch_dps": noise_pitch_dps,
        "noise_yaw_dps":   noise_yaw_dps,
        "saved_at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[gyro] failed to save calibration: {e}", file=sys.stderr)


CFG = _load_config()


def vk(code):
    return KeyCode.from_vk(code)


# -------- config-backed tuning --------
STICK_DEADZONE       = float(CFG["stick"]["deadzone"])
STICK_MOUSE_SPEED    = float(CFG["stick"]["mouse_speed"])
STICK_EXP            = float(CFG["stick"]["exp"])
TICK_HZ              = float(CFG.get("tick_hz", 200))
TRIGGER_THRESHOLD    = float(CFG["trigger_threshold"])

GYRO_YAW_PX_PER_360   = float(CFG["gyro"]["yaw_px_per_360"])
GYRO_PITCH_PX_PER_360 = float(CFG["gyro"]["pitch_px_per_360"])
GYRO_YAW_SIGN         = float(CFG["gyro"]["yaw_sign"])
GYRO_PITCH_SIGN       = float(CFG["gyro"]["pitch_sign"])
GYRO_ROLL_CONTRIB     = float(CFG["gyro"]["roll_contribution"])
GYRO_CALIB_SECS       = float(CFG["gyro"]["calib_secs"])
GYRO_ENABLED_DEFAULT  = bool(CFG["gyro"]["enabled_default"])
GYRO_CUTOFF_DPS       = float(CFG["gyro"]["cutoff_dps"])
GYRO_RECOVERY_DPS     = float(CFG["gyro"]["recovery_dps"])
GYRO_SMOOTH_HIGH_DPS  = float(CFG["gyro"]["smooth_high_dps"])
GYRO_SMOOTH_LOW_DPS   = float(CFG["gyro"]["smooth_low_dps"])
GYRO_SMOOTH_WINDOW    = 10
GYRO_AUTO_RECAL_RATE  = 0.005
# Stale-data watchdog. Real DS5 always has micro-noise; identical floats means frozen.
STALE_TICK_LIMIT     = 100     # identical-sensor ticks in a row = stale (~0.5s)
# Disconnect detection (conservative): only exit after this many consecutive ticks
# where SDL says controller isn't attached AND gyro data is stale.
DISCONNECT_EXIT_TICKS = 400    # ~2s at 200 Hz
# Signal-stability bias correction (fixes long-session drift).
# If the gyro reading has low variance for a while, whatever the MEAN is must be
# bias drift (if it were real motion, variance would be high). So: shift bias
# toward the mean regardless of magnitude. Cures the chicken-and-egg bug where
# drifted bias keeps the reading above cutoff so the normal recal never fires.
STABLE_WINDOW         = 400    # samples (~2s at 200 Hz)
STABLE_STD_MAX_DPS    = 0.25   # std below this = signal is stable
STABLE_MEAN_MAX_DPS   = 5.0    # only correct if mean is within reasonable drift range
STABLE_RECAL_RATE     = 0.01   # per-tick drain toward the observed mean
# Safety: cap the per-tick mouse delta so a runaway signal can't fly the cursor offscreen.
MAX_MOUSE_DELTA_PX   = 60
# Emergency stop: creating this file kills the mapper within a tick.
STOP_FILE            = os.path.expanduser("~/.ds5-stop")
# ------------------------

RAD_TO_DEG = 180.0 / math.pi

kb    = KeyCtl()
mouse = MouseCtl()


# SDL_GameControllerButton names (Xbox-style; maps to DS5 face buttons)
B_A            = sdl2.SDL_CONTROLLER_BUTTON_A             # Cross
B_B            = sdl2.SDL_CONTROLLER_BUTTON_B             # Circle
B_X            = sdl2.SDL_CONTROLLER_BUTTON_X             # Square
B_Y            = sdl2.SDL_CONTROLLER_BUTTON_Y             # Triangle
B_BACK         = sdl2.SDL_CONTROLLER_BUTTON_BACK          # Share
B_GUIDE        = sdl2.SDL_CONTROLLER_BUTTON_GUIDE         # PS
B_START        = sdl2.SDL_CONTROLLER_BUTTON_START         # Options
B_LSTICK       = sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK     # L3
B_RSTICK       = sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK    # R3
B_LSHOULDER    = sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER  # L1
B_RSHOULDER    = sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER # R1
B_DPAD_UP      = sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP
B_DPAD_DOWN    = sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN
B_DPAD_LEFT    = sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT
B_DPAD_RIGHT   = sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT
B_MISC1        = sdl2.SDL_CONTROLLER_BUTTON_MISC1         # Touchpad on DS5

AX_LX = sdl2.SDL_CONTROLLER_AXIS_LEFTX
AX_LY = sdl2.SDL_CONTROLLER_AXIS_LEFTY
AX_RX = sdl2.SDL_CONTROLLER_AXIS_RIGHTX
AX_RY = sdl2.SDL_CONTROLLER_AXIS_RIGHTY
AX_L2 = sdl2.SDL_CONTROLLER_AXIS_TRIGGERLEFT
AX_R2 = sdl2.SDL_CONTROLLER_AXIS_TRIGGERRIGHT

# Target-spec -> press_target target.
# Spec is a dict: {"kind": "mouse"|"key"|"vk"|"special", "value": ...}
_SPECIAL_KEYS = {
    "tab": Key.tab, "esc": Key.esc, "space": Key.space,
    "shift_l": Key.shift_l, "shift_r": Key.shift_r,
    "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r,
    "alt_l": Key.alt_l, "alt_r": Key.alt_r,
    "enter": Key.enter, "backspace": Key.backspace,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
}


class _LMB:
    pass


class _RMB:
    pass


# macOS kVK_ANSI_* values for the top-row digits used in default configs.
# Reverse map so Windows builds can translate them back to characters.
_MAC_VK_DIGIT_TO_CHAR = {
    18: "1", 19: "2", 20: "3", 21: "4", 23: "5",
    22: "6", 26: "7", 28: "8", 25: "9", 29: "0",
}


def _target_from_spec(spec):
    kind = spec["kind"]
    val = spec["value"]
    if kind == "mouse":
        return _LMB if val == "left" else _RMB if val == "right" else None
    if kind == "key":
        return str(val)
    if kind == "vk":
        # Config uses macOS kVK_ANSI_* codes. On macOS feed them to from_vk so
        # pynput bypasses layout-dependent char-to-vk translation (fixed a
        # "4 key doesn't work" bug on some non-US layouts).
        # On Windows those kVK codes are meaningless — translate digit codes
        # back to characters.
        if sys.platform == "darwin":
            return KeyCode.from_vk(int(val))
        ch = _MAC_VK_DIGIT_TO_CHAR.get(int(val))
        return ch if ch is not None else KeyCode.from_vk(int(val))
    if kind == "special":
        return _SPECIAL_KEYS.get(val, val)
    return None


# Maps config button names to SDL button constants
_DS5_NAME_TO_SDL = {
    "cross":       sdl2.SDL_CONTROLLER_BUTTON_A,
    "circle":      sdl2.SDL_CONTROLLER_BUTTON_B,
    "square":      sdl2.SDL_CONTROLLER_BUTTON_X,
    "triangle":    sdl2.SDL_CONTROLLER_BUTTON_Y,
    "l1":          sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER,
    "r1":          sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER,
    "l3":          sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK,
    "r3":          sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK,
    "dpad_up":     sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP,
    "dpad_down":   sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN,
    "dpad_left":   sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT,
    "dpad_right":  sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT,
    "touchpad":    sdl2.SDL_CONTROLLER_BUTTON_MISC1,
    "share":       sdl2.SDL_CONTROLLER_BUTTON_BACK,
    "options":     sdl2.SDL_CONTROLLER_BUTTON_START,
}


def _build_button_map(cfg):
    m = {}
    for name, spec in cfg["buttons"].items():
        if name in _DS5_NAME_TO_SDL:
            m[_DS5_NAME_TO_SDL[name]] = _target_from_spec(spec)
    return m


BUTTON_MAP = _build_button_map(CFG)
TRIGGER_L2_KEY = _target_from_spec(CFG["buttons"]["l2_trigger"])
TRIGGER_R2_KEY = _target_from_spec(CFG["buttons"]["r2_trigger"])

# (Turbo / auto-fire removed in v1.0)


def apply_stick_dz(v):
    if abs(v) < STICK_DEADZONE:
        return 0.0
    s = 1 if v > 0 else -1
    n = (abs(v) - STICK_DEADZONE) / (1 - STICK_DEADZONE)
    return s * (n ** STICK_EXP)


def press_target(target, down):
    try:
        if target is _LMB:
            (mouse.press if down else mouse.release)(MBtn.left)
        elif target is _RMB:
            (mouse.press if down else mouse.release)(MBtn.right)
        else:
            (kb.press if down else kb.release)(target)
    except Exception as e:
        print(f"[press] {target}: {e}", file=sys.stderr)


def cutoff_ramp(v, cutoff, recovery):
    a = abs(v)
    if a < cutoff:
        return 0.0
    if a < recovery:
        factor = (a - cutoff) / (recovery - cutoff)
        return v * factor
    return v


class Mapper:
    def __init__(self, pad):
        self.pad = pad
        self.enabled = True
        self.gyro_on = GYRO_ENABLED_DEFAULT
        self.btn_state = {b: False for b in BUTTON_MAP}
        self.wasd_state = {k: False for k in "wasd"}
        self.l2_down = False
        self.r2_down = False
        self.frac_dx = 0.0
        self.frac_dy = 0.0
        self.touchpad_prev = False
        self.touchpad_last_press = 0.0
        self.ps_prev = False
        self.detach_counter = 0


        self.sensor_buf = (ctypes.c_float * 3)()
        self.gyro_available = bool(sdl2.SDL_GameControllerHasSensor(pad, sdl2.SDL_SENSOR_GYRO))
        if self.gyro_available:
            sdl2.SDL_GameControllerSetSensorEnabled(pad, sdl2.SDL_SENSOR_GYRO, 1)
        # Signal-stability rolling buffers (pitch_dps, horizontal_dps after bias)
        self.stability_pitch = []
        self.stability_yaw   = []
        self.next_debug = time.time() + 1.0

        # Stale-data watchdog: if the gyro emits the EXACT same 3 floats for too
        # many consecutive ticks, the controller is frozen/disconnected and we
        # must not forward those values to mouse.move (otherwise the cursor
        # keeps drifting on cached data).
        self.last_sensor_raw = (None, None, None)
        self.stale_ticks     = 0
        self.detach_streak   = 0

        # bias (in rad/s)
        self.bias_pitch = 0.0
        self.bias_yaw   = 0.0
        self.bias_roll  = 0.0
        self.yaw_buf    = []
        self.pitch_buf  = []

    def read_gyro(self):
        """Return (pitch_dps, horizontal_dps, is_stale). Horizontal combines yaw + roll."""
        if not self.gyro_available:
            return 0.0, 0.0, False
        sdl2.SDL_GameControllerGetSensorData(
            self.pad, sdl2.SDL_SENSOR_GYRO, self.sensor_buf, 3
        )
        raw = (self.sensor_buf[0], self.sensor_buf[1], self.sensor_buf[2])
        # Stale data detection: real gyro always has micro-noise, so identical
        # values across many ticks means SDL is returning cached data (disconnect).
        if raw == self.last_sensor_raw:
            self.stale_ticks += 1
        else:
            self.stale_ticks = 0
        self.last_sensor_raw = raw
        is_stale = self.stale_ticks > STALE_TICK_LIMIT

        pitch = (raw[0] - self.bias_pitch) * RAD_TO_DEG
        yaw   = (raw[1] - self.bias_yaw)   * RAD_TO_DEG
        roll  = (raw[2] - self.bias_roll)  * RAD_TO_DEG
        horizontal = yaw + GYRO_ROLL_CONTRIB * roll
        return pitch, horizontal, is_stale

    def calibrate_gyro(self, secs, countdown=3):
        """Calibrate bias. Warns with a countdown first so user can place controller still."""
        if not self.gyro_available:
            print("[gyro] no sensor available", flush=True)
            return
        print("", flush=True)
        print("=" * 60, flush=True)
        print("  GYRO CALIBRATION — PUT THE DS5 FLAT AND STILL", flush=True)
        print("=" * 60, flush=True)
        for i in range(countdown, 0, -1):
            print(f"  calibrating in {i}...", flush=True)
            time.sleep(1.0)
        print(f"  calibrating NOW ({secs}s) — DO NOT MOVE", flush=True)
        samples = []
        t_end = time.time() + secs
        while time.time() < t_end:
            sdl2.SDL_GameControllerUpdate()
            sdl2.SDL_GameControllerGetSensorData(
                self.pad, sdl2.SDL_SENSOR_GYRO, self.sensor_buf, 3
            )
            samples.append((self.sensor_buf[0], self.sensor_buf[1], self.sensor_buf[2]))
            time.sleep(0.005)
        self.bias_pitch = sum(s[0] for s in samples) / len(samples)
        self.bias_yaw   = sum(s[1] for s in samples) / len(samples)
        self.bias_roll  = sum(s[2] for s in samples) / len(samples)
        p_std = (sum((s[0] - self.bias_pitch) ** 2 for s in samples) / len(samples)) ** 0.5
        y_std = (sum((s[1] - self.bias_yaw)   ** 2 for s in samples) / len(samples)) ** 0.5
        p_std_dps = p_std * RAD_TO_DEG
        y_std_dps = y_std * RAD_TO_DEG
        is_good = p_std_dps < 1 and y_std_dps < 1
        is_ok   = p_std_dps < 3 and y_std_dps < 3
        quality = "GOOD" if is_good else ("OK" if is_ok else
                  "POOR (you may see drift — recalibrate via the app)")
        print(
            f"[gyro] bias: pitch={self.bias_pitch*RAD_TO_DEG:.3f}dps  yaw={self.bias_yaw*RAD_TO_DEG:.3f}dps   "
            f"noise: pitch={p_std_dps:.3f}dps  yaw={y_std_dps:.3f}dps   -> {quality}",
            flush=True,
        )
        # Only save calibration if quality is OK or GOOD — don't persist junk.
        if is_ok:
            save_calibration(self.bias_pitch, self.bias_yaw, self.bias_roll, p_std_dps, y_std_dps)
            print(f"[gyro] saved to {CALIBRATION_PATH}", flush=True)
        print("=" * 60, flush=True)

    def release_all(self):
        for b, down in list(self.btn_state.items()):
            if down:
                press_target(BUTTON_MAP[b], False)
                self.btn_state[b] = False
        for k in "wasd":
            if self.wasd_state[k]:
                kb.release(k)
                self.wasd_state[k] = False
        if self.l2_down:
            press_target(TRIGGER_L2_KEY, False); self.l2_down = False
        if self.r2_down:
            press_target(TRIGGER_R2_KEY, False); self.r2_down = False

    def handle_gyro_to_mouse(self, dt):
        pitch_dps, yaw_dps, is_stale = self.read_gyro()
        # If SDL is handing us frozen data (controller disconnected/sleep), stop forwarding.
        if is_stale:
            # drain buffers so we don't emit stale smoothed motion either
            self.yaw_buf.clear()
            self.pitch_buf.clear()
            return 0.0, 0.0

        # --- Standard auto-recal (below cutoff) ---
        if (GYRO_AUTO_RECAL_RATE > 0
                and abs(pitch_dps) < GYRO_CUTOFF_DPS
                and abs(yaw_dps)   < GYRO_CUTOFF_DPS):
            r = GYRO_AUTO_RECAL_RATE
            self.bias_pitch += r * (pitch_dps / RAD_TO_DEG)
            half = 0.5 * r * (yaw_dps / RAD_TO_DEG)
            self.bias_yaw   += half
            self.bias_roll  += half

        # --- Signal-stability recal (fixes drift-above-cutoff) ---
        self.stability_pitch.append(pitch_dps)
        self.stability_yaw.append(yaw_dps)
        if len(self.stability_pitch) > STABLE_WINDOW:
            self.stability_pitch.pop(0)
            self.stability_yaw.pop(0)

        stable_correction_applied = False
        if len(self.stability_pitch) == STABLE_WINDOW:
            mp = sum(self.stability_pitch) / STABLE_WINDOW
            my = sum(self.stability_yaw)   / STABLE_WINDOW
            sp = (sum((v - mp) ** 2 for v in self.stability_pitch) / STABLE_WINDOW) ** 0.5
            sy = (sum((v - my) ** 2 for v in self.stability_yaw)   / STABLE_WINDOW) ** 0.5
            if (sp < STABLE_STD_MAX_DPS and sy < STABLE_STD_MAX_DPS
                    and abs(mp) < STABLE_MEAN_MAX_DPS and abs(my) < STABLE_MEAN_MAX_DPS):
                # Stable signal above cutoff -> this is bias drift, not motion.
                # Drain bias toward zero at a gentle rate.
                r = STABLE_RECAL_RATE
                self.bias_pitch += r * (mp / RAD_TO_DEG)
                half = 0.5 * r * (my / RAD_TO_DEG)
                self.bias_yaw   += half
                self.bias_roll  += half
                stable_correction_applied = True

        # --- Debug: once per second, print state ---
        now = time.time()
        if now >= self.next_debug:
            self.next_debug = now + 1.0
            if len(self.stability_pitch) >= 50:
                n = min(200, len(self.stability_pitch))
                recent_p = self.stability_pitch[-n:]
                recent_y = self.stability_yaw[-n:]
                mp = sum(recent_p) / n
                my = sum(recent_y) / n
                sp = (sum((v - mp) ** 2 for v in recent_p) / n) ** 0.5
                sy = (sum((v - my) ** 2 for v in recent_y) / n) ** 0.5
                tag = " [STABLE-RECAL]" if stable_correction_applied else ""
                print(
                    f"[gyro] last1s  pitch: mean={mp:+.3f} std={sp:.3f}  "
                    f"yaw(H): mean={my:+.3f} std={sy:.3f}  "
                    f"bias: p={self.bias_pitch*RAD_TO_DEG:.3f} y={self.bias_yaw*RAD_TO_DEG:.3f} r={self.bias_roll*RAD_TO_DEG:.3f}{tag}",
                    flush=True,
                )

        yaw   = cutoff_ramp(yaw_dps,   GYRO_CUTOFF_DPS, GYRO_RECOVERY_DPS)
        pitch = cutoff_ramp(pitch_dps, GYRO_CUTOFF_DPS, GYRO_RECOVERY_DPS)

        self.yaw_buf.append(yaw)
        self.pitch_buf.append(pitch)
        if len(self.yaw_buf)   > GYRO_SMOOTH_WINDOW: self.yaw_buf.pop(0)
        if len(self.pitch_buf) > GYRO_SMOOTH_WINDOW: self.pitch_buf.pop(0)
        yaw_avg   = sum(self.yaw_buf)   / len(self.yaw_buf)
        pitch_avg = sum(self.pitch_buf) / len(self.pitch_buf)

        speed = max(abs(yaw), abs(pitch))
        if speed >= GYRO_SMOOTH_HIGH_DPS:
            imm = 1.0
        elif speed <= GYRO_SMOOTH_LOW_DPS:
            imm = 0.0
        else:
            imm = (speed - GYRO_SMOOTH_LOW_DPS) / (GYRO_SMOOTH_HIGH_DPS - GYRO_SMOOTH_LOW_DPS)
        yaw_out   = imm * yaw   + (1 - imm) * yaw_avg
        pitch_out = imm * pitch + (1 - imm) * pitch_avg

        dx = GYRO_YAW_SIGN   * yaw_out   * dt * (GYRO_YAW_PX_PER_360   / 360.0)
        dy = GYRO_PITCH_SIGN * pitch_out * dt * (GYRO_PITCH_PX_PER_360 / 360.0)
        return dx, dy

    def tick(self, dt):
        sdl2.SDL_GameControllerUpdate()

        # Disconnect detection is LOGGED only — we never auto-exit. The stale-data
        # watchdog in read_gyro freezes mouse output when sensor data stops updating,
        # so there's no drift even if the controller vanishes. Keeping the mapper
        # alive lets it resume when the controller reconnects.
        attached = bool(sdl2.SDL_GameControllerGetAttached(self.pad))
        stale    = self.stale_ticks > STALE_TICK_LIMIT
        if not attached and stale:
            if self.detach_streak == 0:
                print("[ds5-mapper] controller appears disconnected; inputs frozen (will resume when it returns)", flush=True)
            self.detach_streak = min(self.detach_streak + 1, 1_000_000)
        else:
            if self.detach_streak > 0:
                print("[ds5-mapper] controller back online", flush=True)
            self.detach_streak = 0

        # Emergency stop file
        if os.path.exists(STOP_FILE):
            print("[ds5-mapper] stop file detected — exiting", flush=True)
            self.release_all()
            try: os.remove(STOP_FILE)
            except Exception: pass
            raise SystemExit(0)

        # PS button toggles the whole mapper
        ps_now = bool(sdl2.SDL_GameControllerGetButton(self.pad, B_GUIDE))
        if ps_now and not self.ps_prev:
            self.enabled = not self.enabled
            print(f"[mapper] {'ENABLED' if self.enabled else 'PAUSED'}", flush=True)
            if not self.enabled:
                self.release_all()
        self.ps_prev = ps_now

        # Share is just Tab (scoreboard). Recalibration is done via the app's
        # "Calibrate Gyro" button — not by holding a controller button — because
        # users naturally hold Tab for several seconds to read enemy team comp.

        # touchpad double-tap toggles gyro
        touch_now = bool(sdl2.SDL_GameControllerGetButton(self.pad, B_MISC1))
        if touch_now and not self.touchpad_prev:
            now = time.time()
            if now - self.touchpad_last_press < 0.4:
                self.gyro_on = not self.gyro_on
                print(f"[gyro] {'ON' if self.gyro_on else 'OFF'}", flush=True)
            self.touchpad_last_press = now
        self.touchpad_prev = touch_now

        if not self.enabled:
            return

        # sticks (int16 range -32768..32767)
        lx = sdl2.SDL_GameControllerGetAxis(self.pad, AX_LX) / 32768.0
        ly = sdl2.SDL_GameControllerGetAxis(self.pad, AX_LY) / 32768.0
        rx = sdl2.SDL_GameControllerGetAxis(self.pad, AX_RX) / 32768.0
        ry = sdl2.SDL_GameControllerGetAxis(self.pad, AX_RY) / 32768.0

        # triggers (0..32767)
        l2 = sdl2.SDL_GameControllerGetAxis(self.pad, AX_L2) / 32767.0
        r2 = sdl2.SDL_GameControllerGetAxis(self.pad, AX_R2) / 32767.0

        # movement
        want = {
            "w": ly < -STICK_DEADZONE,
            "s": ly >  STICK_DEADZONE,
            "a": lx < -STICK_DEADZONE,
            "d": lx >  STICK_DEADZONE,
        }
        for k, w in want.items():
            if w != self.wasd_state[k]:
                (kb.press if w else kb.release)(k)
                self.wasd_state[k] = w

        # right stick -> mouse
        stick_dx = apply_stick_dz(rx) * STICK_MOUSE_SPEED
        stick_dy = apply_stick_dz(ry) * STICK_MOUSE_SPEED

        # gyro -> mouse
        gyro_dx, gyro_dy = (0.0, 0.0)
        if self.gyro_on:
            gyro_dx, gyro_dy = self.handle_gyro_to_mouse(dt)

        total_dx = stick_dx + gyro_dx + self.frac_dx
        total_dy = stick_dy + gyro_dy + self.frac_dy
        # Clamp per-tick delta as a safety net against any runaway
        if total_dx >  MAX_MOUSE_DELTA_PX: total_dx =  MAX_MOUSE_DELTA_PX; self.frac_dx = 0.0
        if total_dx < -MAX_MOUSE_DELTA_PX: total_dx = -MAX_MOUSE_DELTA_PX; self.frac_dx = 0.0
        if total_dy >  MAX_MOUSE_DELTA_PX: total_dy =  MAX_MOUSE_DELTA_PX; self.frac_dy = 0.0
        if total_dy < -MAX_MOUSE_DELTA_PX: total_dy = -MAX_MOUSE_DELTA_PX; self.frac_dy = 0.0
        idx = int(total_dx)
        idy = int(total_dy)
        self.frac_dx = total_dx - idx
        self.frac_dy = total_dy - idy
        if idx or idy:
            mouse.move(idx, idy)

        # triggers -> ability keys
        want_l2 = l2 > TRIGGER_THRESHOLD
        want_r2 = r2 > TRIGGER_THRESHOLD
        if want_l2 != self.l2_down:
            press_target(TRIGGER_L2_KEY, want_l2); self.l2_down = want_l2
        if want_r2 != self.r2_down:
            press_target(TRIGGER_R2_KEY, want_r2); self.r2_down = want_r2

        # buttons
        for b, key in BUTTON_MAP.items():
            pressed = bool(sdl2.SDL_GameControllerGetButton(self.pad, b))
            if pressed != self.btn_state[b]:
                press_target(key, pressed)
                self.btn_state[b] = pressed


def open_pad():
    sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_SENSOR)
    while True:
        sdl2.SDL_GameControllerUpdate()
        n = sdl2.SDL_NumJoysticks()
        for i in range(n):
            if sdl2.SDL_IsGameController(i):
                pad = sdl2.SDL_GameControllerOpen(i)
                if pad:
                    name_bytes = sdl2.SDL_GameControllerName(pad)
                    name = name_bytes.decode() if name_bytes else "controller"
                    print(f"[ds5-mapper] connected: {name}", flush=True)
                    return pad
        print("[ds5-mapper] waiting for controller...", flush=True)
        time.sleep(1)


def _safe_release_everything():
    """Belt-and-braces: release any plausibly-held key/button even if state is corrupt.
    Called by SIGTERM/SIGINT handlers and atexit — prevents stuck keys on any exit
    path except SIGKILL."""
    try:
        for k in "wasd":
            kb.release(k)
        for k in ("q", "w", "e", "r", "b", "f", "x", "v",
                  "1", "2", "3", "4", "5", "6", "7"):
            kb.release(k)
        kb.release(Key.shift_l)
        kb.release(Key.shift_r)
        kb.release(Key.tab)
        kb.release(Key.esc)
        mouse.release(MBtn.left)
        mouse.release(MBtn.right)
    except Exception:
        pass


class MapperController:
    """Runnable wrapper — owns the mapper lifecycle so a GUI can start/stop it in-process."""

    def __init__(self, on_status=None):
        self.stop_event = __import__("threading").Event()
        self.pad = None
        self.mapper = None
        self.error = None
        self.on_status = on_status  # optional callback (str) -> None

    def _say(self, msg):
        print(msg, flush=True)
        if self.on_status:
            try: self.on_status(msg)
            except Exception: pass

    def run(self):
        try:
            self._open_and_loop()
        except Exception as e:
            self.error = e
            self._say(f"[ds5-mapper] ERROR: {e}")
            raise

    def _open_and_loop(self):
        sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_SENSOR)
        self.pad = None
        while not self.stop_event.is_set():
            sdl2.SDL_GameControllerUpdate()
            for i in range(sdl2.SDL_NumJoysticks()):
                if sdl2.SDL_IsGameController(i):
                    self.pad = sdl2.SDL_GameControllerOpen(i)
                    break
            if self.pad:
                break
            self._say("[ds5-mapper] waiting for controller...")
            self.stop_event.wait(1.0)
        if self.stop_event.is_set() or not self.pad:
            return

        name_bytes = sdl2.SDL_GameControllerName(self.pad)
        self._say(f"[ds5-mapper] connected: {name_bytes.decode() if name_bytes else 'controller'}")
        self.mapper = Mapper(self.pad)

        # Load saved calibration if available (no blocking calibration by default)
        cal = load_calibration()
        if cal:
            self.mapper.bias_pitch = cal["bias_pitch"]
            self.mapper.bias_yaw   = cal["bias_yaw"]
            self.mapper.bias_roll  = cal["bias_roll"]
            self._say(f"[gyro] loaded calibration from {cal.get('saved_at','?')}")
        else:
            self._say("[gyro] no saved calibration — click 'Calibrate gyro' in the app")

        dt_target = 1.0 / TICK_HZ
        last_t = time.time()
        try:
            while not self.stop_event.is_set():
                now = time.time()
                dt = now - last_t
                last_t = now
                if dt > 0.1:
                    dt = 0.1
                self.mapper.tick(dt)
                sleep_left = dt_target - (time.time() - now)
                if sleep_left > 0:
                    self.stop_event.wait(sleep_left)
        finally:
            try: self.mapper.release_all()
            except Exception: pass
            _safe_release_everything()
            try: sdl2.SDL_GameControllerClose(self.pad)
            except Exception: pass
            self.pad = None
            self.mapper = None
            self._say("[ds5-mapper] stopped")

    def stop(self):
        self.stop_event.set()

    def recalibrate_blocking(self, secs=None):
        if not self.mapper:
            return "controller not connected"
        self.mapper.calibrate_gyro(secs or GYRO_CALIB_SECS, countdown=0)
        return "done"


def main():
    pad = open_pad()
    m = Mapper(pad)

    def on_exit_signal(signum, _frame):
        print(f"[ds5-mapper] got signal {signum}, cleaning up", flush=True)
        try:
            m.release_all()
        except Exception:
            pass
        _safe_release_everything()
        sys.exit(0)

    # SIGTERM is what pkill (no -9) sends; SIGINT is Ctrl-C.
    signal.signal(signal.SIGTERM, on_exit_signal)
    signal.signal(signal.SIGINT,  on_exit_signal)
    # atexit runs on normal interpreter shutdown. Can't catch SIGKILL.
    atexit.register(_safe_release_everything)

    # Load persisted calibration if enabled; otherwise run it now.
    auto_load = bool(CFG["gyro"].get("auto_load_calibration", True))
    cal = load_calibration() if auto_load else None
    if cal:
        m.bias_pitch = cal["bias_pitch"]
        m.bias_yaw   = cal["bias_yaw"]
        m.bias_roll  = cal["bias_roll"]
        print(
            f"[gyro] loaded saved calibration from {cal.get('saved_at','?')}  "
            f"bias: pitch={m.bias_pitch*RAD_TO_DEG:.3f}dps yaw={m.bias_yaw*RAD_TO_DEG:.3f}dps roll={m.bias_roll*RAD_TO_DEG:.3f}dps  "
            f"(recalibrate via the app's Calibrate Gyro button)",
            flush=True,
        )
    else:
        m.calibrate_gyro(GYRO_CALIB_SECS)

    dt_target = 1.0 / TICK_HZ
    last_t = time.time()
    try:
        while True:
            now = time.time()
            dt = now - last_t
            last_t = now
            if dt > 0.1:
                dt = 0.1
            m.tick(dt)
            sleep_left = dt_target - (time.time() - now)
            if sleep_left > 0:
                time.sleep(sleep_left)
    finally:
        m.release_all()
        _safe_release_everything()
        sdl2.SDL_GameControllerClose(pad)
        sdl2.SDL_Quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as e:
        # On any unhandled exception, still release keys before crashing
        _safe_release_everything()
        raise
