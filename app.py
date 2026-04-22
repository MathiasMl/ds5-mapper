#!/usr/bin/env python3
"""
DS5 Mapper — single-process desktop app (macOS + Windows).

Runs the controller mapper in a background thread inside this GUI process.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox


IS_MAC     = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def _check_accessibility():
    """macOS only. Returns True if this process has Accessibility permission,
    or None on Windows (no such concept — synthesis always allowed)."""
    if not IS_MAC:
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def _check_input_monitoring():
    """macOS only. Returns True if this process has Input Monitoring permission.
    On Windows returns None."""
    if not IS_MAC:
        return None
    try:
        from Quartz import CGPreflightListenEventAccess
        return bool(CGPreflightListenEventAccess())
    except Exception:
        return None


# ---- Cross-platform user-config path ----
def _user_config_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ds5-league")
    return os.path.expanduser("~/.config/ds5-league")


USER_CFG_DIR = _user_config_dir()
os.makedirs(USER_CFG_DIR, exist_ok=True)


def _seed_default_config():
    """If the user's config.json doesn't exist, copy the bundled default."""
    target = os.path.join(USER_CFG_DIR, "config.json")
    if os.path.exists(target):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    # py2app: __file__ in Contents/Resources; PyInstaller: _MEIPASS/
    candidates = [
        os.path.join(here, "config.json"),
        os.path.join(here, "..", "Resources", "config.json"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "config.json") if getattr(sys, "_MEIPASS", None) else None,
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            shutil.copyfile(c, target)
            return


_seed_default_config()

# Local modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapper as mapper_mod
from mapper import (
    MapperController, CFG, CONFIG_PATH, CALIBRATION_PATH,
    load_calibration, GYRO_CALIB_SECS, RAD_TO_DEG,
)

# ---- Button-mapping dropdown helpers ----
DS5_BUTTONS_ORDER = [
    ("cross",       "Cross ✕"),
    ("circle",      "Circle ○"),
    ("square",      "Square □"),
    ("triangle",    "Triangle △"),
    ("l1",          "L1"),
    ("r1",          "R1"),
    ("l2_trigger",  "L2 (analog past threshold)"),
    ("r2_trigger",  "R2 (analog past threshold)"),
    ("dpad_up",     "D-pad ↑"),
    ("dpad_right",  "D-pad →"),
    ("dpad_down",   "D-pad ↓"),
    ("dpad_left",   "D-pad ←"),
    ("l3",          "L3 (stick click)"),
    ("r3",          "R3 (stick click)"),
    ("touchpad",    "Touchpad click"),
    ("share",       "Share"),
    ("options",     "Options"),
]

_LETTER_KEYS   = list("abcdefghijklmnopqrstuvwxyz")
_NUMBER_VK_MAP = {"1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
                  "6": 22, "7": 26, "8": 28, "9": 25, "0": 29}
_MOUSE_OPTS    = ["left", "right"]
_SPECIAL_OPTS  = ["tab", "esc", "space", "shift_l", "shift_r",
                  "ctrl_l", "ctrl_r", "alt_l", "alt_r", "enter",
                  "backspace", "up", "down", "left", "right"]


def spec_to_display(spec):
    kind, val = spec["kind"], spec["value"]
    if kind == "mouse":   return f"mouse: {val}"
    if kind == "key":     return f"key: {val}"
    if kind == "vk":
        for k, v in _NUMBER_VK_MAP.items():
            if v == val: return f"number: {k}"
        return f"vk: {val}"
    if kind == "special": return f"special: {val}"
    return str(spec)


def display_to_spec(display):
    try:
        prefix, val = display.split(": ", 1)
    except ValueError:
        return None
    if prefix == "mouse":   return {"kind": "mouse",   "value": val}
    if prefix == "key":     return {"kind": "key",     "value": val}
    if prefix == "number":  return {"kind": "vk",      "value": _NUMBER_VK_MAP[val]}
    if prefix == "vk":      return {"kind": "vk",      "value": int(val)}
    if prefix == "special": return {"kind": "special", "value": val}
    return None


def all_binding_options():
    out = []
    for m in _MOUSE_OPTS:     out.append(f"mouse: {m}")
    for k in _LETTER_KEYS:    out.append(f"key: {k}")
    for n in _NUMBER_VK_MAP:  out.append(f"number: {n}")
    for s in _SPECIAL_OPTS:   out.append(f"special: {s}")
    return out


APP_TITLE = "DS5 Mapper"


class Tooltip:
    """Lightweight hover tooltip — no deps, works in tkinter."""
    def __init__(self, widget, text, delay_ms=400, wraplength=380):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id = None
        self._win = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _e=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _e=None):
        self._cancel()
        if self._win:
            self._win.destroy()
            self._win = None

    def _cancel(self):
        if self._after_id is not None:
            try: self.widget.after_cancel(self._after_id)
            except Exception: pass
            self._after_id = None

    def _show(self):
        if self._win:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._win = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(tw, background="#222", borderwidth=1, relief="solid")
        frame.pack()
        tk.Label(
            frame, text=self.text, justify="left",
            background="#222", foreground="#eee",
            wraplength=self.wraplength, padx=8, pady=6,
            font=("TkDefaultFont", 11),
        ).pack()


# Explanations shown on hover. Keep them technical but readable.
TIPS = {
    "yaw_px": (
        "Yaw sensitivity (left/right).\n\n"
        "How many screen pixels the cursor moves when you rotate the controller "
        "a full 360° around the vertical axis. Higher = less rotation needed to "
        "traverse the screen.\n\n"
        "Typical: 5,000–40,000. Match this to yaw if you want a 1:1 feel, "
        "or raise it for faster turning."
    ),
    "pitch_px": (
        "Pitch sensitivity (up/down).\n\n"
        "Same idea as yaw, but for rotating the controller around the horizontal "
        "axis. Lowering it relative to yaw gives finer vertical precision — "
        "useful because most cursor motion is horizontal."
    ),
    "roll_contrib": (
        "Roll contribution (Local Space).\n\n"
        "Steam Input's 'Local Space' gyro convention. When the controller is "
        "held at a tilt, what you feel as 'yaw' is partly physically measured "
        "on the roll axis. This slider mixes the roll signal into horizontal "
        "output so cursor direction stays consistent regardless of pad tilt.\n\n"
        "0 = ignore roll (works only if pad is held flat).\n"
        "1.0 = Steam default, fully compensated.\n"
        "0.5 = partial."
    ),
    "cutoff": (
        "Gyro dead zone (deg/sec).\n\n"
        "Rotation speeds below this threshold are treated as zero — kills "
        "hand tremor, idle sensor noise, and residual bias drift.\n\n"
        "Too low → cursor shakes at rest.\n"
        "Too high → slow intentional aim is ignored.\n\n"
        "Typical: 0.5–3 dps. Gyro noise floor on a clean calibration is ~0.03 dps."
    ),
    "recovery": (
        "Recovery speed (deg/sec).\n\n"
        "Motion above the cutoff fades in smoothly until this speed, where it "
        "reaches full strength. Prevents the hard snap you'd get jumping "
        "straight from zero to full output when crossing the cutoff.\n\n"
        "Set it at 2–5× your cutoff. Too close to cutoff = feels on/off."
    ),
    "smooth_high": (
        "Smoothing upper threshold (deg/sec).\n\n"
        "Fast motion above this speed receives NO smoothing — zero lag on "
        "quick flicks.\n\n"
        "Typical: 6–15 dps. Lower = even faster response but grainier at medium speeds."
    ),
    "smooth_low": (
        "Smoothing lower threshold (deg/sec).\n\n"
        "Slow motion below this speed gets FULL smoothing (rolling-average over "
        "~10 samples) — kills jitter during fine tracking.\n\n"
        "Between low and high, smoothing blends linearly. Must be < smooth-high."
    ),
    "invert_yaw":   "Flip horizontal gyro direction if aim feels reversed left/right.",
    "invert_pitch": "Flip vertical gyro direction if tilting up makes the cursor go down.",
    "gyro_default": (
        "If checked, gyro aim is enabled the moment the app starts.\n\n"
        "If unchecked, you can toggle it at runtime with a touchpad double-tap."
    ),
    "stick_deadzone": (
        "Left stick deadzone (0..1).\n\n"
        "Fraction of deflection ignored around center. Prevents unintended "
        "movement from a stick that doesn't re-center perfectly. "
        "Default 0.22 (22%)."
    ),
    "right_mouse_speed": (
        "Right-stick cursor speed (px/tick).\n\n"
        "Pixels added per mapper tick (200 Hz) at full deflection. Additive "
        "with gyro. Set to 0 to rely entirely on gyro."
    ),
    "stick_exp": (
        "Stick response curve (exponent).\n\n"
        "1.0 = linear. Values above 1 give precision near center (small "
        "deflection → tiny movement) while preserving full speed at the edges. "
        "Default 2.0."
    ),
    "trigger_thresh": (
        "Analog trigger press threshold (0..1).\n\n"
        "How far L2/R2 must be pulled before the bound action fires. "
        "Default 0.5 (half-pull)."
    ),
}


class App:
    def __init__(self):
        self.cfg = self._load_config()
        self.ctrl = None
        self.ctrl_thread = None
        self.status_queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("820x780")
        try: ttk.Style().theme_use("aqua")
        except Exception: pass

        self._build()
        self._refresh_status()

        # Pump status messages from the mapper thread into the UI
        self.root.after(200, self._drain_status)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # First-run wizard if flagged
        if not self.cfg.get("first_run_completed"):
            self.root.after(400, self._first_run_wizard)
            return  # skip auto-start until wizard done

        # Run a permission self-test so we know if pynput can actually post events
        self.root.after(300, self._permission_selftest)

        # Auto-start the mapper if we have saved calibration.
        if load_calibration() is not None:
            self.root.after(800, self.start_mapper)

    # ---------- First-run wizard ----------
    def _first_run_wizard(self):
        win = tk.Toplevel(self.root)
        win.title("Welcome to DS5 Mapper")
        win.geometry("560x540")
        win.transient(self.root)
        win.grab_set()

        self._wizard_win = win
        step = tk.IntVar(value=0)
        body = ttk.Frame(win, padding=20)
        body.pack(fill=tk.BOTH, expand=True)

        title_lbl = ttk.Label(body, text="", font=("TkDefaultFont", 18, "bold"))
        title_lbl.pack(anchor="w", pady=(0, 10))

        msg_lbl = ttk.Label(body, text="", wraplength=500, justify="left")
        msg_lbl.pack(anchor="w", pady=(0, 12))

        # Live permission status row (shown on permission steps)
        self._wizard_perm_var = tk.StringVar(value="")
        perm_lbl = ttk.Label(body, textvariable=self._wizard_perm_var,
                             font=("TkDefaultFont", 13, "bold"))
        perm_lbl.pack(anchor="w", pady=(0, 8))

        action_btn = ttk.Button(body, text="")

        hint_lbl = ttk.Label(body, text="", foreground="#777",
                             wraplength=500, justify="left")
        hint_lbl.pack(anchor="w", pady=(0, 10))

        btns = ttk.Frame(body); btns.pack(side=tk.BOTTOM, fill=tk.X)
        prev_btn = ttk.Button(btns, text="Back")
        prev_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="Skip",
                   command=lambda: self._finish_wizard(win)).pack(side=tk.LEFT, padx=8)
        next_btn = ttk.Button(btns, text="Next")
        next_btn.pack(side=tk.RIGHT)

        # On Windows, skip the Accessibility + Input Monitoring steps entirely.
        welcome_msg = (
            "DS5 Mapper turns your DualSense into a mouse & keyboard, "
            "with real gyro aim.\n\n"
            + ("We'll set up two macOS permissions and calibrate the gyro. "
               "Takes about 30 seconds — no restart needed."
               if IS_MAC else
               "Just calibrate the gyro and you're ready to go.")
        )

        STEPS = [
            {   # 0
                "title": "Welcome 👋",
                "msg": welcome_msg,
                "hint": "",
                "action": None,
                "perm_check": None,
            },
        ]
        if IS_MAC:
            STEPS.extend([
                {
                    "title": "Step 1: Input Monitoring",
                    "msg": (
                        "macOS needs permission for this app to read your controller.\n\n"
                        "Click 'Open Settings'. Then:\n"
                        "  1. Click the + button\n"
                        "  2. Choose 'DS5 Mapper' from Applications\n"
                        "  3. Toggle the switch ON\n\n"
                        "Come back here — the status below updates automatically."
                    ),
                    "hint": "",
                    "action": ("Open Settings",
                               lambda: subprocess.run(["open",
                                   "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"])),
                    "perm_check": "input_monitoring",
                },
                {
                    "title": "Step 2: Accessibility",
                    "msg": (
                        "Now the permission to control the mouse & keyboard.\n\n"
                        "Click 'Open Settings'. Then:\n"
                        "  1. Click the + button\n"
                        "  2. Choose 'DS5 Mapper' from Applications\n"
                        "  3. Toggle the switch ON\n\n"
                        "This one takes effect immediately — no app restart."
                    ),
                    "hint": "",
                    "action": ("Open Settings",
                               lambda: subprocess.run(["open",
                                   "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])),
                    "perm_check": "accessibility",
                },
            ])
        STEPS.extend([
            {
                "title": ("Step 3: Gyro calibration" if IS_MAC else "Step 1: Gyro calibration"),
                "msg": (
                    "Place the DualSense flat and still on a surface.\n\n"
                    "Don't touch it during calibration — takes ~1.5 seconds.\n\n"
                    "You can recalibrate anytime from the main window."
                ),
                "hint": "Connect your DualSense via USB or Bluetooth first.",
                "action": ("Calibrate now", lambda: self._wizard_calibrate(win)),
                "perm_check": None,
            },
            {
                "title": "All set ✓",
                "msg": (
                    "You're ready to go.\n\n"
                    "Default layout:\n"
                    "  • L1 / L2 / R1 / R2 → Q / W / E / R abilities\n"
                    "  • Left stick → WASD movement\n"
                    "  • Gyro → mouse cursor (tilt to aim)\n"
                    "  • Cross ✕ → left click\n\n"
                    "Tweak anything in the main window's Buttons / Gyro / Stick tabs."
                ),
                "hint": "",
                "action": None,
                "perm_check": None,
            },
        ])

        self._wizard_poller = None

        def update_perm_status():
            s = STEPS[step.get()]
            kind = s.get("perm_check")
            if not kind:
                self._wizard_perm_var.set("")
                next_btn.state(["!disabled"])
                return
            if kind == "input_monitoring":
                granted = _check_input_monitoring()
                label = "Input Monitoring"
            else:
                granted = _check_accessibility()
                label = "Accessibility"
            if granted is None:
                self._wizard_perm_var.set("")
                next_btn.state(["!disabled"])
            elif granted:
                self._wizard_perm_var.set(f"✓ {label} granted — you can continue.")
                perm_lbl.configure(foreground="#2a7f2a")
                next_btn.state(["!disabled"])
            else:
                self._wizard_perm_var.set(f"✗ {label} NOT granted yet — waiting…")
                perm_lbl.configure(foreground="#a33")
                next_btn.state(["disabled"])
            # keep polling while on this step
            self._wizard_poller = win.after(800, update_perm_status)

        def render():
            # Cancel any pending poll from previous step
            if self._wizard_poller is not None:
                try: win.after_cancel(self._wizard_poller)
                except Exception: pass
                self._wizard_poller = None

            s = STEPS[step.get()]
            title_lbl.config(text=s["title"])
            msg_lbl.config(text=s["msg"])
            hint_lbl.config(text=s["hint"])
            if s["action"]:
                label, fn = s["action"]
                action_btn.config(text=label, command=fn)
                action_btn.pack(anchor="w", pady=(0, 8), before=hint_lbl)
            else:
                action_btn.pack_forget()
            prev_btn.state(["!disabled"] if step.get() > 0 else ["disabled"])
            next_btn.config(text="Finish" if step.get() == len(STEPS) - 1 else "Next")
            update_perm_status()

        def go_next():
            if step.get() >= len(STEPS) - 1:
                self._finish_wizard(win)
                return
            step.set(step.get() + 1)
            render()

        def go_prev():
            step.set(max(0, step.get() - 1))
            render()

        prev_btn.config(command=go_prev)
        next_btn.config(command=go_next)
        render()

    def _wizard_calibrate(self, parent):
        if not (self.ctrl_thread and self.ctrl_thread.is_alive()):
            self.start_mapper()
            # wait briefly for the mapper to connect
            parent.after(1500, lambda: self._wizard_calibrate(parent))
            return
        if not self.ctrl or not self.ctrl.mapper:
            messagebox.showwarning(APP_TITLE,
                "Controller not connected yet — plug in or pair your DS5, then click Calibrate again.")
            return
        def worker():
            self.ctrl.recalibrate_blocking(self.cfg["gyro"]["calib_secs"])
            self.root.after(0, self._refresh_cal_info)
            self.root.after(0, lambda: messagebox.showinfo(APP_TITLE, "Calibration complete."))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_wizard(self, win):
        self.cfg["first_run_completed"] = True
        self._save_config()
        win.destroy()
        # Resume normal startup
        self.root.after(300, self._permission_selftest)
        if load_calibration() is not None and not (self.ctrl_thread and self.ctrl_thread.is_alive()):
            self.root.after(800, self.start_mapper)

    def _permission_selftest(self):
        """Move the mouse 1px and back. If pynput is blocked by TCC, nothing will move."""
        try:
            from pynput.mouse import Controller as MouseCtl
            m = MouseCtl()
            x0, y0 = m.position
            m.move(5, 0)
            time.sleep(0.05)
            x1, y1 = m.position
            m.move(-(x1 - x0), -(y1 - y0))
            moved = (x1 - x0) != 0 or (y1 - y0) != 0
            if moved:
                self._log(f"[selftest] pynput mouse OK (moved from {x0},{y0} to {x1},{y1})")
            else:
                self._log(
                    "[selftest] pynput mouse FAILED — cursor did not move.\n"
                    "           Grant Accessibility + Input Monitoring to DS5 Mapper,\n"
                    "           then Cmd+Q and relaunch.")
                messagebox.showwarning(
                    APP_TITLE,
                    "DS5 Mapper doesn't have permission to control the cursor.\n\n"
                    "Open System Settings → Privacy & Security and grant:\n"
                    "  • Accessibility → DS5 Mapper\n"
                    "  • Input Monitoring → DS5 Mapper\n\n"
                    "Then Cmd+Q this app and launch it again.")
        except Exception as e:
            self._log(f"[selftest] error: {e}")

    # ---------- Config I/O ----------
    def _load_config(self):
        with open(CONFIG_PATH) as f:
            return json.load(f)

    def _save_config(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.cfg, f, indent=2)

    # ---------- UI ----------
    def _build(self):
        # Header: status + start/stop + calibrate
        hdr = ttk.Frame(self.root, padding=10)
        hdr.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(hdr, text="Mapper status: ").pack(side=tk.LEFT)
        self.status_lbl = ttk.Label(hdr, textvariable=self.status_var, font=("TkDefaultFont", 12, "bold"))
        self.status_lbl.pack(side=tk.LEFT)

        self.start_btn = ttk.Button(hdr, text="Start", command=self.start_mapper)
        self.start_btn.pack(side=tk.RIGHT, padx=4)
        self.stop_btn  = ttk.Button(hdr, text="Stop",  command=self.stop_mapper, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT, padx=4)
        self.calib_btn = ttk.Button(hdr, text="Calibrate Gyro…", command=self.open_calibration_wizard)
        self.calib_btn.pack(side=tk.RIGHT, padx=12)

        # Calibration summary line
        self.cal_info_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.cal_info_var, foreground="#666").pack(fill=tk.X, padx=12)
        self._refresh_cal_info()

        # Permissions strip — only on macOS (Windows has no TCC equivalent for input synthesis)
        if IS_MAC:
            perm_row = ttk.Frame(self.root)
            perm_row.pack(fill=tk.X, padx=12, pady=(2, 4))

            ttk.Label(perm_row, text="Permissions:").pack(side=tk.LEFT)

            self._acc_status_var  = tk.StringVar(value="Accessibility: ?")
            self._inp_status_var  = tk.StringVar(value="Input Monitoring: ?")

            self._acc_lbl = ttk.Label(perm_row, textvariable=self._acc_status_var)
            self._acc_lbl.pack(side=tk.LEFT, padx=(8, 4))
            self._acc_btn = ttk.Button(perm_row, text="Grant",
                                       command=self._open_accessibility)
            self._acc_btn.pack(side=tk.LEFT)

            ttk.Label(perm_row, text="·", foreground="#888").pack(side=tk.LEFT, padx=8)

            self._inp_lbl = ttk.Label(perm_row, textvariable=self._inp_status_var)
            self._inp_lbl.pack(side=tk.LEFT, padx=(0, 4))
            self._inp_btn = ttk.Button(perm_row, text="Grant",
                                       command=self._open_input_monitoring)
            self._inp_btn.pack(side=tk.LEFT)

        # Tabs
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tab_gyro    = ttk.Frame(nb); nb.add(self.tab_gyro, text="Gyro")
        self.tab_buttons = ttk.Frame(nb); nb.add(self.tab_buttons, text="Buttons")
        self.tab_stick   = ttk.Frame(nb); nb.add(self.tab_stick, text="Stick")
        self.tab_log     = ttk.Frame(nb); nb.add(self.tab_log, text="Log")

        self._build_gyro_tab()
        self._build_buttons_tab()
        self._build_stick_tab()
        self._build_log_tab()

        # Footer
        foot = ttk.Frame(self.root, padding=8)
        foot.pack(fill=tk.X)
        ttk.Button(foot, text="Save settings",         command=self.save_only).pack(side=tk.LEFT)
        ttk.Button(foot, text="Save & Restart mapper", command=self.save_and_restart).pack(side=tk.LEFT, padx=8)

    def _slider(self, parent, label, lo, hi, getter, setter, tip=None):
        row = ttk.Frame(parent); row.pack(fill=tk.X, padx=12, pady=4)
        lbl = ttk.Label(row, text=label, width=32, anchor="w")
        lbl.pack(side=tk.LEFT)
        if tip:
            Tooltip(lbl, tip)

        entry_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=entry_var, width=10, justify="right")
        entry.pack(side=tk.RIGHT)

        cur = getter()
        entry_var.set(f"{cur:g}")
        sc = ttk.Scale(row, from_=lo, to=hi, orient="horizontal", length=400)
        sc.set(cur)
        sc.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        syncing = {"flag": False}   # suppress reentrancy between slider and entry

        def from_slider(v):
            if syncing["flag"]:
                return
            syncing["flag"] = True
            try:
                v = float(v)
                setter(v)
                entry_var.set(f"{v:g}")
            finally:
                syncing["flag"] = False

        def from_entry(*_):
            if syncing["flag"]:
                return
            try:
                v = float(entry_var.get())
            except ValueError:
                # restore last good value from the underlying config
                entry_var.set(f"{getter():g}")
                return
            syncing["flag"] = True
            try:
                setter(v)
                # Slider is clamped to lo..hi; the entry can go beyond.
                sc.set(max(lo, min(hi, v)))
                entry_var.set(f"{v:g}")
            finally:
                syncing["flag"] = False

        sc.configure(command=from_slider)
        entry.bind("<Return>",   from_entry)
        entry.bind("<FocusOut>", from_entry)

    def _build_gyro_tab(self):
        g = self.cfg["gyro"]
        self._slider(self.tab_gyro, "Yaw px/360° (horizontal)", 1000, 60000,
                     lambda: g["yaw_px_per_360"], lambda v: g.__setitem__("yaw_px_per_360", int(v)),
                     tip=TIPS["yaw_px"])
        self._slider(self.tab_gyro, "Pitch px/360° (vertical)", 1000, 60000,
                     lambda: g["pitch_px_per_360"], lambda v: g.__setitem__("pitch_px_per_360", int(v)),
                     tip=TIPS["pitch_px"])
        self._slider(self.tab_gyro, "Roll contribution (Local Space)", 0, 1.5,
                     lambda: g["roll_contribution"], lambda v: g.__setitem__("roll_contribution", round(v, 2)),
                     tip=TIPS["roll_contrib"])
        self._slider(self.tab_gyro, "Cutoff (dps)", 0.1, 10.0,
                     lambda: g["cutoff_dps"], lambda v: g.__setitem__("cutoff_dps", round(v, 2)),
                     tip=TIPS["cutoff"])
        self._slider(self.tab_gyro, "Recovery (dps)", 0.2, 20.0,
                     lambda: g["recovery_dps"], lambda v: g.__setitem__("recovery_dps", round(v, 2)),
                     tip=TIPS["recovery"])
        self._slider(self.tab_gyro, "Smooth-high (dps)", 1.0, 30.0,
                     lambda: g["smooth_high_dps"], lambda v: g.__setitem__("smooth_high_dps", round(v, 1)),
                     tip=TIPS["smooth_high"])
        self._slider(self.tab_gyro, "Smooth-low (dps)", 0.5, 15.0,
                     lambda: g["smooth_low_dps"], lambda v: g.__setitem__("smooth_low_dps", round(v, 1)),
                     tip=TIPS["smooth_low"])

        frm = ttk.Frame(self.tab_gyro); frm.pack(fill=tk.X, padx=12, pady=10)
        self.inv_yaw   = tk.BooleanVar(value=g["yaw_sign"]   < 0)
        self.inv_pitch = tk.BooleanVar(value=g["pitch_sign"] < 0)
        self.g_default = tk.BooleanVar(value=g["enabled_default"])
        cb_yaw = ttk.Checkbutton(frm, text="Invert Yaw", variable=self.inv_yaw,
                        command=lambda: g.__setitem__("yaw_sign", -1 if self.inv_yaw.get() else 1))
        cb_yaw.pack(side=tk.LEFT, padx=8); Tooltip(cb_yaw, TIPS["invert_yaw"])
        cb_pitch = ttk.Checkbutton(frm, text="Invert Pitch", variable=self.inv_pitch,
                        command=lambda: g.__setitem__("pitch_sign", -1 if self.inv_pitch.get() else 1))
        cb_pitch.pack(side=tk.LEFT, padx=8); Tooltip(cb_pitch, TIPS["invert_pitch"])
        cb_default = ttk.Checkbutton(frm, text="Gyro enabled on startup", variable=self.g_default,
                        command=lambda: g.__setitem__("enabled_default", self.g_default.get()))
        cb_default.pack(side=tk.LEFT, padx=8); Tooltip(cb_default, TIPS["gyro_default"])

    def _build_buttons_tab(self):
        frm = ttk.Frame(self.tab_buttons, padding=12); frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="Assign an action to each DS5 input.", foreground="#555").pack(anchor="w", pady=(0, 8))
        options = all_binding_options()
        for name, label in DS5_BUTTONS_ORDER:
            row = ttk.Frame(frm); row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=28, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar(value=spec_to_display(self.cfg["buttons"][name]))
            cb = ttk.Combobox(row, textvariable=var, values=options, state="readonly", width=22)
            cb.pack(side=tk.LEFT)
            cb.bind("<<ComboboxSelected>>",
                    lambda _e, n=name, v=var: self.cfg["buttons"].__setitem__(n, display_to_spec(v.get())))

    def _build_stick_tab(self):
        s = self.cfg["stick"]
        self._slider(self.tab_stick, "Left stick deadzone",        0.0, 0.5,
                     lambda: s["deadzone"], lambda v: s.__setitem__("deadzone", round(v, 2)),
                     tip=TIPS["stick_deadzone"])
        self._slider(self.tab_stick, "Right stick mouse speed",    1, 80,
                     lambda: s["mouse_speed"], lambda v: s.__setitem__("mouse_speed", int(v)),
                     tip=TIPS["right_mouse_speed"])
        self._slider(self.tab_stick, "Stick exponential",          1.0, 4.0,
                     lambda: s["exp"], lambda v: s.__setitem__("exp", round(v, 2)),
                     tip=TIPS["stick_exp"])
        self._slider(self.tab_stick, "Trigger threshold",          0.05, 0.95,
                     lambda: self.cfg["trigger_threshold"],
                     lambda v: self.cfg.__setitem__("trigger_threshold", round(v, 2)),
                     tip=TIPS["trigger_thresh"])

    def _build_log_tab(self):
        self.log_text = tk.Text(self.tab_log, wrap="none", height=30, state="disabled",
                                font=("Menlo", 11), background="#111", foreground="#ddd")
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ---------- Status + log pumping ----------
    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        # keep last ~400 lines
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > 400:
            self.log_text.delete("1.0", f"{lines - 400}.0")
        self.log_text.config(state="disabled")

    def _drain_status(self):
        try:
            while True:
                msg = self.status_queue.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        # Update state
        running = bool(self.ctrl_thread and self.ctrl_thread.is_alive())
        self.status_var.set("Running" if running else "Stopped")
        self.status_lbl.configure(foreground="#2a7f2a" if running else "#a00")
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        # Refresh permission status every tick too (cheap)
        self._refresh_permission_strip()
        self.root.after(400, self._drain_status)

    def _open_settings_pane(self, which):
        # macOS 13+ changed the URL scheme; try both. Use absolute /usr/bin/open
        # because py2app-bundled apps sometimes have a stripped PATH.
        urls = {
            "accessibility": [
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
            ],
            "input_monitoring": [
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
                "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent",
            ],
        }[which]
        for url in urls:
            try:
                rc = subprocess.run(
                    ["/usr/bin/open", url],
                    capture_output=True, text=True, timeout=5,
                ).returncode
                self._log(f"[perm] open {url!r} rc={rc}")
                if rc == 0:
                    return
            except Exception as e:
                self._log(f"[perm] error opening {url!r}: {e}")
        # Last resort: open the top-level Privacy pane
        subprocess.run(["/usr/bin/open", "-b", "com.apple.systempreferences"])

    def _open_accessibility(self):
        self._open_settings_pane("accessibility")

    def _open_input_monitoring(self):
        self._open_settings_pane("input_monitoring")

    def _refresh_permission_strip(self):
        if not hasattr(self, "_acc_status_var"):
            return
        a = _check_accessibility()
        i = _check_input_monitoring()

        def style(lbl, btn, granted, name):
            if granted is None:
                lbl.configure(foreground="#888")
                btn.state(["!disabled"])
                return f"{name}: ?"
            if granted:
                lbl.configure(foreground="#2a7f2a")
                btn.state(["disabled"])
                return f"{name}: ✓"
            lbl.configure(foreground="#a33")
            btn.state(["!disabled"])
            return f"{name}: ✗"

        self._acc_status_var.set(style(self._acc_lbl, self._acc_btn, a, "Accessibility"))
        self._inp_status_var.set(style(self._inp_lbl, self._inp_btn, i, "Input Monitoring"))

    def _push_status(self, msg):
        self.status_queue.put(msg)

    def _refresh_cal_info(self):
        cal = load_calibration()
        if cal:
            self.cal_info_var.set(
                f"Gyro calibrated {cal.get('saved_at','?')}  ·  "
                f"bias pitch={cal['bias_pitch']*RAD_TO_DEG:+.3f}dps  "
                f"yaw={cal['bias_yaw']*RAD_TO_DEG:+.3f}dps  "
                f"roll={cal['bias_roll']*RAD_TO_DEG:+.3f}dps"
            )
        else:
            self.cal_info_var.set("Gyro NOT calibrated — click 'Calibrate Gyro…' before enabling gyro.")

    def _refresh_status(self):
        self._refresh_cal_info()

    # ---------- Mapper control ----------
    def start_mapper(self):
        if self.ctrl_thread and self.ctrl_thread.is_alive():
            return
        self.ctrl = MapperController(on_status=self._push_status)
        self.ctrl_thread = threading.Thread(target=self.ctrl.run, daemon=True)
        self.ctrl_thread.start()

    def stop_mapper(self):
        if self.ctrl:
            self.ctrl.stop()

    def save_only(self):
        self._save_config()
        self._log(f"[app] saved {CONFIG_PATH}")

    def save_and_restart(self):
        self.save_only()
        if self.ctrl_thread and self.ctrl_thread.is_alive():
            self.stop_mapper()
            # wait for thread to finish, then restart
            self.root.after(1500, self._restart_after_stop)
        else:
            self.start_mapper()

    def _restart_after_stop(self):
        if self.ctrl_thread and self.ctrl_thread.is_alive():
            self.root.after(300, self._restart_after_stop)
            return
        # Re-import mapper to pick up new config — it caches CFG at module load.
        import importlib
        importlib.reload(mapper_mod)
        global MapperController
        from mapper import MapperController as _NewCtl
        MapperController = _NewCtl
        self.start_mapper()

    # ---------- Calibration wizard ----------
    def open_calibration_wizard(self):
        # Mapper must be running to access the sensor
        if not (self.ctrl_thread and self.ctrl_thread.is_alive()):
            messagebox.showwarning(APP_TITLE,
                "Start the mapper first (click 'Start'), then calibrate.")
            return
        if self.ctrl is None or self.ctrl.mapper is None:
            messagebox.showwarning(APP_TITLE, "Controller not connected yet — try again in a moment.")
            return

        win = tk.Toplevel(self.root)
        win.title("Gyro calibration")
        win.geometry("420x240")
        win.transient(self.root)
        win.grab_set()

        msg_var = tk.StringVar(value=(
            "Place the DS5 flat and still on a surface.\n\n"
            "Click 'Calibrate' to measure its resting bias.\n"
            "Don't touch the controller during calibration (~1.5 s).\n\n"
            "You can recalibrate anytime from the main window."
        ))
        ttk.Label(win, textvariable=msg_var, padding=16, justify="left").pack(fill=tk.X)

        btn_frame = ttk.Frame(win); btn_frame.pack(fill=tk.X, padx=16, pady=8)
        ttk.Button(btn_frame, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=4)
        calib_btn = ttk.Button(btn_frame, text="Calibrate")
        calib_btn.pack(side=tk.RIGHT, padx=4)

        def go():
            calib_btn.configure(state="disabled")
            msg_var.set("Calibrating… hold still for 1.5 seconds…")
            win.update_idletasks()

            def worker():
                result = self.ctrl.recalibrate_blocking(self.cfg["gyro"]["calib_secs"])
                self.root.after(0, done, result)

            def done(_):
                self._refresh_cal_info()
                cal = load_calibration()
                if cal:
                    msg_var.set(
                        "Calibration saved ✓\n\n"
                        f"bias pitch={cal['bias_pitch']*RAD_TO_DEG:+.3f} dps\n"
                        f"bias yaw  ={cal['bias_yaw']*RAD_TO_DEG:+.3f} dps\n"
                        f"noise pitch={cal.get('noise_pitch_dps', 0):.3f} dps  "
                        f"yaw={cal.get('noise_yaw_dps', 0):.3f} dps"
                    )
                else:
                    msg_var.set("Calibration failed — controller may have moved. Try again.")
                calib_btn.configure(state="normal")

            threading.Thread(target=worker, daemon=True).start()

        calib_btn.configure(command=go)

    # ---------- Close ----------
    def _on_close(self):
        if self.ctrl_thread and self.ctrl_thread.is_alive():
            self.stop_mapper()
            # give thread a moment to clean up held keys
            for _ in range(15):
                if not self.ctrl_thread.is_alive():
                    break
                time.sleep(0.1)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # Kill any lingering legacy mapper.py subprocesses (macOS-only; pkill isn't on Windows)
    if IS_MAC:
        try:
            subprocess.run(["pkill", "-TERM", "-f", "mapper.py"], check=False)
            time.sleep(0.4)
        except FileNotFoundError:
            pass
    App().run()
