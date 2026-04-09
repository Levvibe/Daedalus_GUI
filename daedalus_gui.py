import math
import time
import tkinter as tk
from typing import Any, cast

try:
    import pygame
except Exception:
    pygame = None

# =========================
# CONFIG
# =========================
FPS_MS = 33
DEADZONE = 0.18
COMP_TIME_SECONDS = 15 * 60

AXIS_STRAFE = 0  # stick left/right
AXIS_SURGE = 1  # stick forward/back
AXIS_TURN = 2  # stick twist / yaw
AXIS_TURTLE = 3  # base throttle/slider on Logitech Extreme 3D Pro style joystick

# Flipped per pilot request (turtle lever polarity swap).
TURTLE_AXIS_INVERTED = False
TURTLE_MIN_SCALE = 0.15
MAX_PHYSICAL_BUTTONS = 12

# Physical button numbers on the joystick
BTN_GRIPPER = 1
BTN_VERTICAL_DOWN = 3
BTN_CAMERA_REAR = 2
BTN_CAMERA_BOTTOM = 4
BTN_VERTICAL_UP = 5
BTN_CAMERA_GRIPPER = 6
BTN_STOP = 7
BTN_MODE_TOGGLE = 8

# Safe toggle combo changed to 6 + 4
BTN_SAFE_TOGGLE_A = 6
BTN_SAFE_TOGGLE_B = 4

CAMERA_ORDER = ["FRONT CAM", "REAR CAM", "BOTTOM CAM", "GRIPPER CAM"]
CAMERA_BUTTONS = {
    1: "FRONT CAM",
    2: "REAR CAM",
    4: "BOTTOM CAM",
    6: "GRIPPER CAM",
}

# Hat absolute camera mapping (counter-clockwise):
# Up -> 1/front, Left -> 2/rear, Down -> 3/bottom, Right -> 4/gripper
HAT_CAMERA_MAP = {
    (0, 1): "FRONT CAM",
    (-1, 0): "REAR CAM",
    (0, -1): "BOTTOM CAM",
    (1, 0): "GRIPPER CAM",
}

# Logitech Extreme 3D Pro typically reports raw pygame button index = physical label - 1.
RAW_BTN_FROM_PHYSICAL = {i: i - 1 for i in range(1, MAX_PHYSICAL_BUTTONS + 1)}

# Colors
BG = "#090b10"
PANEL = "#111826"
PANEL_2 = "#0d1320"
GRID = "#1b2740"
TEXT = "#d8e1ee"
MUTED = "#8292aa"
GREEN = "#35ff9c"
GREEN_SOFT = "#174831"
RED = "#ff3b4d"
ORANGE = "#ff9a3c"
YELLOW = "#ffd54a"
BLUE = "#6ab8ff"
CYAN = "#45d4ff"
PURPLE = "#bc7bff"
GRAY_THRUSTER = "#253247"
BODY = "#17253a"
BODY_EDGE = "#3b5d91"
WHITE = "#f6f8fb"


class InputState:
    def __init__(self):
        self.strafe = 0.0
        self.turn = 0.0
        self.surge = 0.0
        self.vertical = 0.0
        self.gripper = False
        self.stop_pressed = False
        self.active_buttons = []
        self.raw_strafe = 0.0
        self.raw_turn = 0.0
        self.raw_surge = 0.0
        self.raw_vertical_up = 0
        self.raw_vertical_down = 0
        self.connected = False
        self.hat = (0, 0)


class ROVGui:
    def __init__(self, root):
        self.root = root
        self.root.title("ROV Pilot UI — Daedalus")
        self.root.configure(bg=BG)

        self.screen_w = root.winfo_screenwidth()
        self.screen_h = root.winfo_screenheight()

        self.mode_buttons = {}
        self.window_mode = "half"

        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.mini_info_win = None
        self.mini_info_canvas = None

        self.width = 0
        self.height = 0

        self.root.minsize(520, 560)
        self.set_window_mode("half")

        self.input = InputState()
        self.last_dt = time.time()

        self.timer_running = False
        self.time_left = float(COMP_TIME_SECONDS)

        self.safe_mode = False
        self.active_camera = "FRONT CAM"

        self.heading_deg = 0.0
        self.pitch_deg = 0.0
        self.roll_deg = 0.0

        self.turtle_mode = False
        self.turtle_scale = 1.0
        self.turtle_raw = -1.0

        self.last_camera_switch = time.time()
        self.last_reconnect_attempt = 0.0
        self.last_safe_toggle = 0.0
        self.last_mode_toggle = 0.0
        self.prev_hat = (0, 0)

        self.key_held = set()

        self.joystick = None
        self.pygame_ready = False
        self.try_init_controller()

        self.root.bind("<KeyPress>", self.on_key_press)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.root.bind("<Configure>", self.on_resize)
        self.canvas.bind("<Button-1>", self.on_click)

        self.tick()

    # -----------------------------
    # Window modes
    # -----------------------------
    def ensure_mini_window(self):
        if self.mini_info_win is not None and self.mini_info_win.winfo_exists():
            return

        self.mini_info_win = tk.Toplevel(self.root)
        self.mini_info_win.title("Daedalus — Mini Info")
        self.mini_info_win.configure(bg=BG)
        self.mini_info_win.minsize(360, 420)

        self.mini_info_canvas = tk.Canvas(self.mini_info_win, bg=BG, highlightthickness=0)
        self.mini_info_canvas.pack(fill="both", expand=True)

        self.mini_info_win.bind("<KeyPress>", self.on_key_press)
        self.mini_info_win.bind("<KeyRelease>", self.on_key_release)
        self.mini_info_win.protocol("WM_DELETE_WINDOW", lambda: self.set_window_mode("half"))

    def destroy_mini_window(self):
        if self.mini_info_win is not None:
            try:
                self.mini_info_win.destroy()
            except Exception:
                pass
        self.mini_info_win = None
        self.mini_info_canvas = None

    def cycle_window_mode(self):
        order = ["half", "quarter", "full"]
        idx = order.index(self.window_mode) if self.window_mode in order else 0
        self.set_window_mode(order[(idx + 1) % len(order)])

    def set_window_mode(self, mode):
        self.window_mode = mode

        # Ensure we never leave the window stuck in OS fullscreen.
        try:
            self.root.attributes("-fullscreen", False)
        except Exception:
            pass

        if mode == "full":
            self.destroy_mini_window()
            try:
                self.root.state("normal")
            except Exception:
                pass
            # Fill the screen without entering locked exclusive fullscreen.
            self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")

        elif mode == "quarter":
            try:
                self.root.state("normal")
            except Exception:
                pass
            # Small ROV window + separate info window.
            main_w = max(460, int(self.screen_w * 0.27))
            main_h = max(480, int(self.screen_h * 0.56))
            self.root.geometry(f"{main_w}x{main_h}+20+20")

            self.ensure_mini_window()
            info_w = max(300, int(self.screen_w * 0.18))
            info_h = max(280, int(self.screen_h * 0.34))
            info_x = 30 + main_w
            info_y = 20
            if self.mini_info_win is not None:
                self.mini_info_win.geometry(f"{info_w}x{info_h}+{info_x}+{info_y}")
                self.mini_info_win.deiconify()
                self.mini_info_win.lift()

        else:  # half
            self.destroy_mini_window()
            try:
                self.root.state("normal")
            except Exception:
                pass
            # Actual left-half style layout.
            w = max(860, int(self.screen_w * 0.50))
            h = max(760, int(self.screen_h * 0.90))
            x = 0
            y = 0
            self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -----------------------------
    # Controller / input
    # -----------------------------
    def try_init_controller(self):
        if pygame is None:
            self.pygame_ready = False
            self.input.connected = False
            return

        try:
            pg = cast(Any, pygame)
            if not pg.get_init():
                pg.init()
            if not pg.joystick.get_init():
                pg.joystick.init()

            count = pg.joystick.get_count()
            if count > 0:
                self.joystick = pg.joystick.Joystick(0)
                if not self.joystick.get_init():
                    self.joystick.init()
                self.pygame_ready = True
                self.input.connected = True
            else:
                self.joystick = None
                self.pygame_ready = True
                self.input.connected = False
        except Exception:
            self.joystick = None
            self.pygame_ready = False
            self.input.connected = False

    def maybe_reconnect_controller(self):
        now = time.time()
        if now - self.last_reconnect_attempt > 1.0:
            self.last_reconnect_attempt = now
            self.try_init_controller()

    def on_resize(self, event):
        if event.widget is self.root:
            self.width = max(event.width, 480)
            self.height = max(event.height, 540)

    def on_key_press(self, event):
        key = event.keysym.lower()
        self.key_held.add(key)

        if key == "space":
            self.safe_mode = not self.safe_mode
        elif key == "t":
            self.timer_running = not self.timer_running
        elif key == "r":
            self.time_left = float(COMP_TIME_SECONDS)
        elif key == "m":
            self.set_window_mode("quarter")
        elif key == "h":
            self.set_window_mode("half")
        elif key == "f":
            self.set_window_mode("full")
        elif key in {"1", "2", "3", "4"}:
            camera_lookup = {
                "1": "FRONT CAM",
                "2": "REAR CAM",
                "3": "BOTTOM CAM",
                "4": "GRIPPER CAM",
            }
            self.active_camera = camera_lookup[key]
            self.last_camera_switch = time.time()

    def on_key_release(self, event):
        self.key_held.discard(event.keysym.lower())

    def on_click(self, event):
        x = event.x
        y = event.y
        for name, rect in self.mode_buttons.items():
            x1, y1, x2, y2 = rect
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.set_window_mode(name)
                return

    def apply_deadzone(self, value):
        value = max(-1.0, min(1.0, value))
        mag = abs(value)
        if mag < DEADZONE:
            return 0.0
        scaled = (mag - DEADZONE) / (1.0 - DEADZONE)
        return math.copysign(min(1.0, scaled), value)

    def axis_value(self, axis_index, fallback=0.0):
        if self.joystick is None:
            return fallback
        try:
            if 0 <= axis_index < self.joystick.get_numaxes():
                return self.joystick.get_axis(axis_index)
        except Exception:
            pass
        return fallback

    def button_pressed(self, btn_index):
        if self.joystick is None:
            return False
        try:
            if 0 <= btn_index < self.joystick.get_numbuttons():
                return bool(self.joystick.get_button(btn_index))
        except Exception:
            pass
        return False

    def raw_button_index(self, physical_btn):
        return RAW_BTN_FROM_PHYSICAL.get(physical_btn, physical_btn)

    def physical_button_pressed(self, physical_btn):
        return self.button_pressed(self.raw_button_index(physical_btn))

    def get_hat(self):
        if self.joystick is None:
            return (0, 0)
        try:
            if self.joystick.get_numhats() > 0:
                return self.joystick.get_hat(0)
        except Exception:
            pass
        return (0, 0)

    def cycle_camera(self, delta):
        idx = CAMERA_ORDER.index(self.active_camera) if self.active_camera in CAMERA_ORDER else 0
        self.active_camera = CAMERA_ORDER[(idx + delta) % len(CAMERA_ORDER)]
        self.last_camera_switch = time.time()

    def update_input(self):
        inp = self.input

        inp.active_buttons = []
        inp.stop_pressed = False
        inp.gripper = False
        inp.raw_vertical_up = 0
        inp.raw_vertical_down = 0
        inp.raw_strafe = 0.0
        inp.raw_turn = 0.0
        inp.raw_surge = 0.0
        inp.hat = (0, 0)

        strafe = 0.0
        turn = 0.0
        surge = 0.0
        vertical = 0.0

        # Keyboard fallback
        if "left" in self.key_held:
            strafe -= 1.0
            inp.active_buttons.append("LEFT")
        if "right" in self.key_held:
            strafe += 1.0
            inp.active_buttons.append("RIGHT")
        if "up" in self.key_held:
            surge += 1.0
            inp.active_buttons.append("UP")
        if "down" in self.key_held:
            surge -= 1.0
            inp.active_buttons.append("DOWN")
        if "a" in self.key_held:
            turn -= 1.0
            inp.active_buttons.append("A")
        if "d" in self.key_held:
            turn += 1.0
            inp.active_buttons.append("D")
        if "z" in self.key_held:
            vertical -= 1.0
            inp.raw_vertical_down = 1
            inp.active_buttons.append("Z")
        if "x" in self.key_held:
            vertical += 1.0
            inp.raw_vertical_up = 1
            inp.active_buttons.append("X")
        if "g" in self.key_held:
            inp.gripper = True
            inp.active_buttons.append("G")
        if "s" in self.key_held:
            inp.stop_pressed = True
            inp.active_buttons.append("S")

        # Default turtle scale if no controller is connected.
        self.turtle_scale = 1.0
        self.turtle_mode = False
        self.turtle_raw = -1.0

        # Controller input
        inp.connected = bool(self.joystick is not None and self.joystick.get_init()) if self.joystick is not None else False
        if self.pygame_ready:
            if self.joystick is None:
                self.maybe_reconnect_controller()

            if self.joystick is not None:
                try:
                    pg = cast(Any, pygame)
                    pg.event.pump()
                    inp.connected = self.joystick.get_init()

                    # Read controller the same way as the known-working client code:
                    # enumerate all axes/buttons/hats first, then pull mapped indices.
                    num_axes = self.joystick.get_numaxes()
                    num_buttons = self.joystick.get_numbuttons()
                    num_hats = self.joystick.get_numhats()
                    raw_axes = [self.joystick.get_axis(i) for i in range(num_axes)]
                    _raw_buttons = [self.joystick.get_button(i) for i in range(num_buttons)]
                    _raw_hats = [self.joystick.get_hat(i) for i in range(num_hats)]

                    raw_strafe = raw_axes[AXIS_STRAFE] if len(raw_axes) > AXIS_STRAFE else 0.0
                    raw_surge = -(raw_axes[AXIS_SURGE] if len(raw_axes) > AXIS_SURGE else 0.0)
                    raw_turn = raw_axes[AXIS_TURN] if len(raw_axes) > AXIS_TURN else 0.0

                    inp.raw_strafe = raw_strafe
                    inp.raw_turn = raw_turn
                    inp.raw_surge = raw_surge

                    strafe = self.apply_deadzone(raw_strafe)
                    surge = self.apply_deadzone(raw_surge)
                    turn = self.apply_deadzone(raw_turn)

                    raw_turtle = raw_axes[AXIS_TURTLE] if len(raw_axes) > AXIS_TURTLE else -1.0
                    if TURTLE_AXIS_INVERTED:
                        raw_turtle = -raw_turtle
                    raw_turtle = max(-1.0, min(1.0, raw_turtle))
                    self.turtle_raw = raw_turtle

                    # Up = full speed, down = precision mode.
                    self.turtle_scale = 1.0 - (((raw_turtle + 1.0) / 2.0) * (1.0 - TURTLE_MIN_SCALE))
                    self.turtle_mode = self.turtle_scale < 0.995

                    pressed_physical = []
                    for physical in range(1, MAX_PHYSICAL_BUTTONS + 1):
                        if self.physical_button_pressed(physical):
                            pressed_physical.append(str(physical))
                    inp.active_buttons.extend(pressed_physical)

                    up_pressed = self.physical_button_pressed(BTN_VERTICAL_UP)
                    down_pressed = self.physical_button_pressed(BTN_VERTICAL_DOWN)
                    inp.raw_vertical_up = 1 if up_pressed else 0
                    inp.raw_vertical_down = 1 if down_pressed else 0

                    if up_pressed and not down_pressed:
                        vertical = 1.0
                    elif down_pressed and not up_pressed:
                        vertical = -1.0

                    now = time.time()

                    # Safe toggle combo changed from 5+3 to 6+4
                    safe_a = self.physical_button_pressed(BTN_SAFE_TOGGLE_A)
                    safe_b = self.physical_button_pressed(BTN_SAFE_TOGGLE_B)
                    if safe_a and safe_b and now - self.last_safe_toggle > 0.35:
                        self.safe_mode = not self.safe_mode
                        self.last_safe_toggle = now

                    if self.physical_button_pressed(BTN_GRIPPER):
                        inp.gripper = True

                    if self.physical_button_pressed(BTN_STOP):
                        inp.stop_pressed = True

                    if self.physical_button_pressed(BTN_MODE_TOGGLE) and now - self.last_mode_toggle > 0.35:
                        self.cycle_window_mode()
                        self.last_mode_toggle = now

                    for physical_btn, name in CAMERA_BUTTONS.items():
                        if self.physical_button_pressed(physical_btn):
                            self.active_camera = name
                            self.last_camera_switch = now

                    # Hat now maps each cardinal direction to a fixed camera.
                    hat = self.get_hat()
                    inp.hat = hat
                    if hat != self.prev_hat and hat in HAT_CAMERA_MAP:
                        self.active_camera = HAT_CAMERA_MAP[hat]
                        self.last_camera_switch = now
                    self.prev_hat = hat

                except Exception:
                    self.joystick = None
                    inp.connected = False

        inp.strafe = max(-1.0, min(1.0, strafe * self.turtle_scale))
        inp.turn = max(-1.0, min(1.0, turn * self.turtle_scale))
        inp.surge = max(-1.0, min(1.0, surge * self.turtle_scale))
        inp.vertical = max(-1.0, min(1.0, vertical * self.turtle_scale))

    # -----------------------------
    # Simulation
    # -----------------------------
    def get_mode(self):
        if self.safe_mode:
            return "SAFE"
        if (
            abs(self.input.strafe) < 1e-6
            and abs(self.input.turn) < 1e-6
            and abs(self.input.surge) < 1e-6
            and abs(self.input.vertical) < 1e-6
            and not self.input.gripper
        ):
            return "NEUTRAL"
        return "ACTIVE"

    def motor_outputs(self):
        out = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0}
        if self.safe_mode:
            return out

        surge = self.input.surge
        strafe = self.input.strafe
        turn = self.input.turn
        vertical = self.input.vertical

        out[1] = surge + strafe - turn
        out[2] = surge + strafe + turn
        out[3] = surge + strafe - turn
        out[4] = surge + strafe + turn
        out[5] = vertical
        out[6] = vertical

        for key in out:
            out[key] = max(-1.0, min(1.0, out[key]))
        return out

    def update_sim(self, dt):
        if self.timer_running and self.time_left > 0:
            self.time_left = max(0.0, self.time_left - dt)

        if not self.safe_mode:
            self.heading_deg = (self.heading_deg + self.input.turn * 85.0 * dt) % 360.0
            target_pitch = -self.input.surge * 10.0
            self.pitch_deg += (target_pitch - self.pitch_deg) * min(1.0, dt * 5.0)
            target_roll = self.input.strafe * 8.0
            self.roll_deg += (target_roll - self.roll_deg) * min(1.0, dt * 5.0)
        else:
            self.pitch_deg += (0.0 - self.pitch_deg) * min(1.0, dt * 5.0)
            self.roll_deg += (0.0 - self.roll_deg) * min(1.0, dt * 5.0)

    # -----------------------------
    # Drawing helpers
    # -----------------------------
    def draw_round_rect(self, canvas, x1, y1, x2, y2, r=16, fill="", outline="", width=1):
        points = [
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1,
        ]
        return canvas.create_polygon(points, smooth=True, fill=fill, outline=outline, width=width)

    def draw_panel(self, canvas, x1, y1, x2, y2, title=None, title_size=15):
        self.draw_round_rect(canvas, x1, y1, x2, y2, r=22, fill=PANEL, outline=GRID, width=2)
        if title:
            canvas.create_text(x1 + 16, y1 + 18, text=title, anchor="w", fill=TEXT, font=("Arial", title_size, "bold"))

    def motor_color(self, value):
        mag = abs(value)
        if mag < 0.02:
            return GRAY_THRUSTER, ""
        if mag < 0.35:
            return ORANGE, ORANGE
        if mag < 0.75:
            return "#ff6a3a", "#ff6a3a"
        return RED, RED

    def draw_badge(self, canvas, x, y, text, fill=PANEL_2, outline=GRID, fg=TEXT):
        self.draw_round_rect(canvas, x - 14, y - 12, x + 14, y + 12, r=10, fill=fill, outline=outline, width=1)
        canvas.create_text(x, y, text=text, fill=fg, font=("Arial", 10, "bold"))

    def draw_gauge(self, canvas, x, y, width, value, label):
        h = 10
        canvas.create_text(x, y - 12, text=label, anchor="w", fill=MUTED, font=("Arial", 10, "bold"))
        self.draw_round_rect(canvas, x, y, x + width, y + h, r=5, fill=PANEL_2, outline=GRID, width=1)

        value = max(-1.0, min(1.0, value))
        mid = x + width / 2
        canvas.create_line(mid, y - 1, mid, y + h + 1, fill=WHITE, width=1)

        if value >= 0:
            self.draw_round_rect(canvas, mid, y + 1, mid + (width / 2) * value, y + h - 1, r=4, fill=CYAN, outline="")
        else:
            self.draw_round_rect(
                canvas,
                mid - (width / 2) * abs(value),
                y + 1,
                mid,
                y + h - 1,
                r=4,
                fill=PURPLE,
                outline="",
            )

        canvas.create_text(x + width + 10, y + h / 2, text=f"{value:+.2f}", anchor="w", fill=TEXT, font=("Arial", 10, "bold"))

    def draw_axis_vector(self, canvas, cx, cy, axis_len, axis, value, label, label_angle=0, label_side="top"):
        canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=GREEN_SOFT, outline="")

        if axis == "horizontal":
            canvas.create_line(cx - axis_len, cy, cx + axis_len, cy, fill="#163122", width=2)
            dot_x = cx + value * axis_len
            if abs(value) > 1e-3:
                canvas.create_line(cx, cy, dot_x, cy, fill=GREEN, width=5, capstyle=tk.ROUND)
            canvas.create_oval(dot_x - 9, cy - 9, dot_x + 9, cy + 9, fill=GREEN, outline="")

            label_y = cy - 18 if label_side == "top" else cy + 18
            canvas.create_text(cx, label_y, text=label, fill=MUTED, font=("Arial", 10, "bold"), angle=label_angle)

        else:
            canvas.create_line(cx, cy - axis_len, cx, cy + axis_len, fill="#163122", width=2)
            dot_y = cy - value * axis_len
            if abs(value) > 1e-3:
                canvas.create_line(cx, cy, cx, dot_y, fill=GREEN, width=5, capstyle=tk.ROUND)
            canvas.create_oval(cx - 9, dot_y - 9, cx + 9, dot_y + 9, fill=GREEN, outline="")

            if label_side == "right":
                canvas.create_text(cx + 20, cy, text=label, fill=MUTED, font=("Arial", 10, "bold"), angle=label_angle)
            else:
                canvas.create_text(cx, cy - axis_len - 16, text=label, fill=MUTED, font=("Arial", 10, "bold"), angle=label_angle)

    def draw_thruster(self, canvas, x, y, radius, value, angle_deg=None, vertical=False):
        fill, glow = self.motor_color(value)

        if glow:
            canvas.create_oval(
                x - radius - 7,
                y - radius - 7,
                x + radius + 7,
                y + radius + 7,
                fill="",
                outline=glow,
                width=3,
            )

        canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=fill, outline="#0b0f15", width=3)
        canvas.create_oval(
            x - radius * 0.52,
            y - radius * 0.52,
            x + radius * 0.52,
            y + radius * 0.52,
            fill="#0b0f15",
            outline="",
        )

        if vertical:
            canvas.create_line(x, y - 12, x, y + 12, fill=WHITE, width=2)
            canvas.create_line(x - 5, y - 6, x, y - 12, x + 5, y - 6, fill=WHITE, width=2)
            canvas.create_line(x - 5, y + 6, x, y + 12, x + 5, y + 6, fill=WHITE, width=2)
        else:
            rad = math.radians(angle_deg or 0)
            x2 = x + math.cos(rad) * (radius - 4)
            y2 = y - math.sin(rad) * (radius - 4)
            canvas.create_line(x, y, x2, y2, fill=WHITE, width=2)

    def draw_top_strip(self, canvas, x1, y1, x2, y2):
        canvas.create_text(
            (x1 + x2) / 2,
            y1 + 14,
            text="ROV PILOT CONSOLE — Daedalus / Input Simulation",
            fill=TEXT,
            font=("Arial", 14, "bold"),
        )

        btn_y1 = y1 + 2
        btn_y2 = y2 - 4
        names = [("half", "HALF"), ("quarter", "MINI"), ("full", "FULL")]

        bx2 = x2 - 8
        self.mode_buttons = {}

        for key, label in reversed(names):
            bw = 64
            bx1 = bx2 - bw
            active = self.window_mode == key
            self.draw_round_rect(
                canvas,
                bx1,
                btn_y1,
                bx2,
                btn_y2,
                r=12,
                fill="#122238" if active else PANEL_2,
                outline=CYAN if active else GRID,
                width=2,
            )
            canvas.create_text(
                (bx1 + bx2) / 2,
                (btn_y1 + btn_y2) / 2,
                text=label,
                fill=CYAN if active else TEXT,
                font=("Arial", 9, "bold"),
            )
            self.mode_buttons[key] = (bx1, btn_y1, bx2, btn_y2)
            bx2 = bx1 - 8

    def draw_status_panel(self, canvas, x1, y1, x2, y2, tiny=False):
        self.draw_panel(canvas, x1, y1, x2, y2, "PILOT STATUS", 14 if not tiny else 12)

        mode = self.get_mode()
        banner_color = RED if mode == "SAFE" else (GREEN if mode == "ACTIVE" else YELLOW)
        self.draw_round_rect(canvas, x1 + 14, y1 + 42, x2 - 14, y1 + 96, r=18, fill="#0e1420", outline=banner_color, width=3)
        canvas.create_text(
            (x1 + x2) / 2,
            y1 + 69,
            text=mode,
            fill=banner_color,
            font=("Arial", 22 if not tiny else 18, "bold"),
        )

        if self.input.connected:
            conn = "CONTROLLER CONNECTED"
        elif pygame is None:
            conn = "KEYBOARD FALLBACK (PYGAME MISSING)"
        else:
            conn = "KEYBOARD FALLBACK"
        canvas.create_text(
            x1 + 18,
            y1 + 122,
            text=conn,
            anchor="w",
            fill=GREEN if self.input.connected else YELLOW,
            font=("Arial", 11 if not tiny else 10, "bold"),
        )

        gauge_w = max(100, int((x2 - x1) * (0.46 if tiny else 0.52)))
        canvas.create_text(x1 + 18, y1 + 150, text=f"DEADZONE {DEADZONE:.2f}", anchor="w", fill=MUTED, font=("Arial", 10, "bold"))

        self.draw_gauge(canvas, x1 + 18, y1 + 175, gauge_w, self.input.strafe, "LEFT / RIGHT")
        self.draw_gauge(canvas, x1 + 18, y1 + 218, gauge_w, self.input.surge, "FORWARD / BACK")
        self.draw_gauge(canvas, x1 + 18, y1 + 261, gauge_w, self.input.vertical, "VERTICAL")

        canvas.create_text(x1 + 18, y1 + 308, text="RAW INPUTS", anchor="w", fill=MUTED, font=("Arial", 10, "bold"))
        canvas.create_text(x1 + 18, y1 + 330, text=f"Strafe {self.input.raw_strafe:+.3f}", anchor="w", fill=TEXT, font=("Consolas", 10))
        canvas.create_text(x1 + 18, y1 + 350, text=f"Twist {self.input.raw_turn:+.3f}", anchor="w", fill=TEXT, font=("Consolas", 10))
        canvas.create_text(x1 + 18, y1 + 370, text=f"Surge {self.input.raw_surge:+.3f}", anchor="w", fill=TEXT, font=("Consolas", 10))
        canvas.create_text(
            x1 + 18,
            y1 + 390,
            text=f"Btn {BTN_VERTICAL_UP} Up {self.input.raw_vertical_up}",
            anchor="w",
            fill=TEXT,
            font=("Consolas", 10),
        )
        canvas.create_text(
            x1 + 18,
            y1 + 410,
            text=f"Btn {BTN_VERTICAL_DOWN} Down {self.input.raw_vertical_down}",
            anchor="w",
            fill=TEXT,
            font=("Consolas", 10),
        )

        pressed = ", ".join(self.input.active_buttons[:8]) if self.input.active_buttons else "NONE"
        canvas.create_text(x1 + 18, y1 + 438, text="BUTTONS DOWN", anchor="w", fill=MUTED, font=("Arial", 10, "bold"))
        canvas.create_text(x1 + 18, y1 + 460, text=pressed, anchor="w", fill=TEXT, font=("Arial", 11, "bold"))

        percent = int(round(self.turtle_scale * 100))
        turtle_text = f"TURTLE SCALE {percent}%"
        turtle_fill = CYAN if self.turtle_mode else MUTED
        self.draw_round_rect(canvas, x1 + 16, y2 - 54, x2 - 16, y2 - 18, r=14, fill=PANEL_2, outline=GRID, width=1)
        canvas.create_text((x1 + x2) / 2, y2 - 36, text=turtle_text, fill=turtle_fill, font=("Arial", 10, "bold"))

    def draw_motor_panel(self, canvas, x1, y1, x2, y2):
        self.draw_panel(canvas, x1, y1, x2, y2, "MOTOR OUTPUTS", 14)

        outputs = self.motor_outputs()
        row_y = y1 + 50
        row_gap = 38 if (y2 - y1) > 260 else 32

        for idx, key in enumerate([1, 2, 3, 4, 5, 6]):
            y = row_y + idx * row_gap
            canvas.create_text(x1 + 18, y, text=f"M{key}", anchor="w", fill=TEXT, font=("Arial", 10, "bold"))

            bx1 = x1 + 48
            bx2 = x2 - 44
            by1 = y - 8
            by2 = y + 8
            self.draw_round_rect(canvas, bx1, by1, bx2, by2, r=7, fill=PANEL_2, outline=GRID, width=1)

            mid = (bx1 + bx2) / 2
            canvas.create_line(mid, by1 - 1, mid, by2 + 1, fill=WHITE, width=1)

            val = outputs[key]
            if val >= 0:
                fill = CYAN if key in (5, 6) else GREEN
                self.draw_round_rect(canvas, mid, by1 + 1, mid + (bx2 - mid) * min(1.0, val), by2 - 1, r=6, fill=fill, outline="")
            else:
                fill = PURPLE if key in (5, 6) else ORANGE
                self.draw_round_rect(
                    canvas,
                    mid - (mid - bx1) * min(1.0, abs(val)),
                    by1 + 1,
                    mid,
                    by2 - 1,
                    r=6,
                    fill=fill,
                    outline="",
                )

            canvas.create_text(x2 - 10, y, text=f"{val:+.2f}", anchor="e", fill=TEXT, font=("Consolas", 10))

    def draw_telemetry_panel(self, canvas, x1, y1, x2, y2, tiny=False):
        self.draw_panel(canvas, x1, y1, x2, y2, "TELEMETRY / MATCH", 14 if not tiny else 12)

        mins = int(self.time_left // 60)
        secs = int(self.time_left % 60)
        timer_color = GREEN if self.time_left > 300 else (YELLOW if self.time_left > 60 else RED)

        canvas.create_text(x1 + 78, y1 + 68, text=f"{mins:02}:{secs:02}", fill=timer_color, font=("Arial", 30 if not tiny else 24, "bold"))
        canvas.create_text(x1 + 78, y1 + 98, text="MATCH COUNTDOWN", fill=MUTED, font=("Arial", 10, "bold"))

        now = time.strftime("%I:%M:%S %p")
        canvas.create_text(x2 - 16, y1 + 66, text=now, anchor="e", fill=TEXT, font=("Arial", 18 if not tiny else 14, "bold"))
        canvas.create_text(x2 - 16, y1 + 96, text="CURRENT TIME", anchor="e", fill=MUTED, font=("Arial", 10, "bold"))

        fields = [
            ("Depth", "--.-- ft"),
            ("Pressure", "--.-- psi"),
            ("Pitch", f"{self.pitch_deg:+05.1f}°"),
            ("Yaw", f"{self.heading_deg:06.1f}°"),
            ("Roll", f"{self.roll_deg:+05.1f}°"),
            ("Active Cam", self.active_camera),
        ]

        rows_y = y1 + 122
        col_gap = 10
        col_w = (x2 - x1 - 42 - col_gap) / 2
        row_h = 34
        for i, (label, value) in enumerate(fields):
            col = i % 2
            row = i // 2
            rx1 = x1 + 16 + col * (col_w + col_gap)
            ry1 = rows_y + row * (row_h + 6)
            rx2 = rx1 + col_w
            ry2 = ry1 + row_h
            self.draw_round_rect(canvas, rx1, ry1, rx2, ry2, r=14, fill=PANEL_2, outline=GRID, width=1)
            canvas.create_text(rx1 + 12, ry1 + 19, text=label.upper(), anchor="w", fill=MUTED, font=("Arial", 9, "bold"))
            canvas.create_text(rx2 - 12, ry1 + 19, text=value, anchor="e", fill=TEXT, font=("Arial", 11, "bold"))

        pills_y = y2 - 52
        canvas.create_text(x1 + 16, pills_y - 10, text="CAMERA BUTTONS", anchor="w", fill=MUTED, font=("Arial", 10, "bold"))
        pill_w = max(70, int((x2 - x1 - 48) / 4))
        gap = 6
        for i, name in enumerate(CAMERA_ORDER):
            px1 = x1 + 16 + i * (pill_w + gap)
            px2 = px1 + pill_w
            active = self.active_camera == name
            self.draw_round_rect(
                canvas,
                px1,
                pills_y,
                px2,
                pills_y + 32,
                r=12,
                fill="#102337" if active else PANEL_2,
                outline=CYAN if active else GRID,
                width=2 if active else 1,
            )
            canvas.create_text(
                (px1 + px2) / 2,
                pills_y + 16,
                text=str(self.camera_button_label_for(name)),
                fill=CYAN if active else TEXT,
                font=("Arial", 10, "bold"),
            )

        canvas.create_text(
            x1 + 16,
            y2 - 8,
            text="External camera UI uses the other half of the screen",
            anchor="w",
            fill=MUTED,
            font=("Arial", 9, "bold"),
        )

    def camera_button_label_for(self, name):
        for btn, mapped in CAMERA_BUTTONS.items():
            if mapped == name:
                return btn
        return "-"

    def draw_rov_panel(self, canvas, x1, y1, x2, y2, tiny=False):
        self.draw_panel(canvas, x1, y1, x2, y2, "ROV TOP VIEW", 14 if not tiny else 12)

        outputs = self.motor_outputs()

        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2 + (20 if not tiny else 14)

        panel_w = x2 - x1
        panel_h = y2 - y1

        hull_w = min(panel_w * 0.24, 120 if tiny else 150)
        hull_h = min(panel_h * 0.48, 190 if tiny else 270)

        hull_x1 = cx - hull_w / 2
        hull_x2 = cx + hull_w / 2
        hull_y1 = cy - hull_h / 2
        hull_y2 = cy + hull_h / 2

        self.draw_round_rect(canvas, hull_x1 - 26, hull_y1 - 12, hull_x2 + 26, hull_y2 + 12, r=46, fill="#0f1827", outline=BODY_EDGE, width=3)
        self.draw_round_rect(canvas, hull_x1, hull_y1, hull_x2, hull_y2, r=34, fill=BODY, outline="#7aa2d5", width=3)
        self.draw_round_rect(canvas, hull_x1 + 12, hull_y1 + 12, hull_x2 - 12, hull_y2 - 12, r=26, fill="#20304a", outline="#9ab9e6", width=2)
        canvas.create_line(hull_x1 + 14, hull_y1 + 18, hull_x2 - 14, hull_y1 + 18, fill=RED, width=3)
        canvas.create_line(hull_x1 + 14, hull_y2 - 18, hull_x2 - 14, hull_y2 - 18, fill=RED, width=3)

        offset_x = 76 if not tiny else 56
        offset_y = 54 if not tiny else 42
        arm_pts = {
            1: (hull_x1 - offset_x, hull_y1 - offset_y),
            2: (hull_x2 + offset_x, hull_y1 - offset_y),
            3: (hull_x1 - offset_x, hull_y2 + offset_y),
            4: (hull_x2 + offset_x, hull_y2 + offset_y),
            5: (hull_x1 - (92 if not tiny else 72), cy),
            6: (hull_x2 + (92 if not tiny else 72), cy),
        }

        corner_anchors = {
            1: (hull_x1 + 4, hull_y1 + 8),
            2: (hull_x2 - 4, hull_y1 + 8),
            3: (hull_x1 + 4, hull_y2 - 8),
            4: (hull_x2 - 4, hull_y2 - 8),
        }

        for key in [1, 2, 3, 4]:
            tx, ty = arm_pts[key]
            ax, ay = corner_anchors[key]
            canvas.create_line(ax, ay, tx, ty, fill=BODY_EDGE, width=10, capstyle=tk.ROUND)
            canvas.create_line(ax, ay, tx, ty, fill="#8db0de", width=4, capstyle=tk.ROUND)

        for key in [5, 6]:
            tx, ty = arm_pts[key]
            anchor_x = hull_x1 - 18 if key == 5 else hull_x2 + 18
            canvas.create_line(anchor_x, cy, tx, ty, fill=BODY_EDGE, width=10, capstyle=tk.ROUND)
            canvas.create_line(anchor_x, cy, tx, ty, fill="#8db0de", width=4, capstyle=tk.ROUND)

        grip_x = cx
        grip_y = hull_y1 - 38
        gripper_active = self.input.gripper and not self.safe_mode
        grip_outline = YELLOW if gripper_active else GRID
        if gripper_active:
            canvas.create_oval(grip_x - 28, grip_y - 22, grip_x + 28, grip_y + 22, outline=YELLOW, width=3)

        canvas.create_line(grip_x, hull_y1, grip_x, grip_y, fill="#9dbcf0", width=6, capstyle=tk.ROUND)
        canvas.create_line(grip_x, grip_y, grip_x - 22, grip_y + 18, fill=grip_outline, width=4, capstyle=tk.ROUND)
        canvas.create_line(grip_x, grip_y, grip_x + 22, grip_y + 18, fill=grip_outline, width=4, capstyle=tk.ROUND)
        canvas.create_line(grip_x - 22, grip_y + 18, grip_x - 30, grip_y + 6, fill=grip_outline, width=4)
        canvas.create_line(grip_x + 22, grip_y + 18, grip_x + 30, grip_y + 6, fill=grip_outline, width=4)
        canvas.create_text(grip_x, grip_y - 20, text="GRIPPER FRONT", fill=MUTED, font=("Arial", 10, "bold"))

        r_corner = 24 if not tiny else 20
        r_vert = 22 if not tiny else 18
        self.draw_thruster(canvas, *arm_pts[1], r_corner, outputs[1], angle_deg=135)
        self.draw_thruster(canvas, *arm_pts[2], r_corner, outputs[2], angle_deg=45)
        self.draw_thruster(canvas, *arm_pts[3], r_corner, outputs[3], angle_deg=225)
        self.draw_thruster(canvas, *arm_pts[4], r_corner, outputs[4], angle_deg=315)
        self.draw_thruster(canvas, *arm_pts[5], r_vert, outputs[5], vertical=True)
        self.draw_thruster(canvas, *arm_pts[6], r_vert, outputs[6], vertical=True)

        badge_offsets = {1: (-38, 26), 2: (38, 26), 3: (-38, 26), 4: (38, 26), 5: (-34, 0), 6: (34, 0)}
        for key in range(1, 7):
            tx, ty = arm_pts[key]
            ox, oy = badge_offsets[key]
            self.draw_badge(canvas, tx + ox, ty + oy, str(key))

        # Top bar should show strafe (left/right), not yaw.
        top_y = max(y1 + 44, hull_y1 - 90)
        self.draw_axis_vector(canvas, cx, top_y, 86 if not tiny else 56, "horizontal", self.input.strafe, "STRAFE")

        # Move side bar farther right and rotate its label to avoid overlap with bar/motor labels.
        surge_x = min(x2 - 20, hull_x2 + (150 if not tiny else 108))
        self.draw_axis_vector(
            canvas,
            surge_x,
            cy,
            112 if not tiny else 76,
            "vertical",
            self.input.surge,
            "FWD / BACK",
            label_angle=90,
            label_side="right",
        )

        # New center translation vector (strafe + surge): line from center to moving dot.
        vec_len = (30 if tiny else 46)
        dot_x = cx + self.input.strafe * vec_len
        dot_y = cy - self.input.surge * vec_len
        canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill="#244061", outline="")
        canvas.create_line(cx, cy, dot_x, dot_y, fill=CYAN, width=3, capstyle=tk.ROUND)
        canvas.create_oval(dot_x - 7, dot_y - 7, dot_x + 7, dot_y + 7, fill=CYAN, outline="")

    # -----------------------------
    # Layouts
    # -----------------------------
    def draw_half_or_full(self):
        c = self.canvas
        w = self.width
        h = self.height

        margin = 14
        gap = 12
        top_h = 30

        self.draw_top_strip(c, margin, 0, w - margin, top_h)

        left_w = max(260, int(w * 0.33))
        x_left1 = margin
        x_left2 = x_left1 + left_w
        x_right1 = x_left2 + gap
        x_right2 = w - margin

        y1 = top_h + 10
        y2 = h - margin

        status_h = max(450, int((y2 - y1) * 0.58))
        rov_h = max(420, int((y2 - y1) * 0.56))

        self.draw_status_panel(c, x_left1, y1, x_left2, y1 + status_h)
        self.draw_motor_panel(c, x_left1, y1 + status_h + gap, x_left2, y2)
        self.draw_rov_panel(c, x_right1, y1, x_right2, y1 + rov_h)
        self.draw_telemetry_panel(c, x_right1, y1 + rov_h + gap, x_right2, y2)

    def draw_quarter_main(self):
        c = self.canvas
        w = self.width
        h = self.height

        margin = 12
        top_h = 28
        self.draw_top_strip(c, margin, 0, w - margin, top_h)
        self.draw_rov_panel(c, margin, top_h + 8, w - margin, h - margin, tiny=True)

    def draw_mini_info_window(self):
        if self.mini_info_canvas is None or self.mini_info_win is None:
            return

        c = self.mini_info_canvas
        w = max(300, self.mini_info_win.winfo_width())
        h = max(260, self.mini_info_win.winfo_height())

        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=BG, outline="")

        margin = 10
        y = 10

        self.draw_panel(c, margin, y, w - margin, y + 74, "PILOT STATUS", 11)
        mode = self.get_mode()
        banner_color = RED if mode == "SAFE" else (GREEN if mode == "ACTIVE" else YELLOW)
        self.draw_round_rect(c, margin + 10, y + 26, w - margin - 10, y + 62, r=12, fill="#0e1420", outline=banner_color, width=2)
        c.create_text(w / 2, y + 44, text=mode, fill=banner_color, font=("Arial", 16, "bold"))

        y += 82

        self.draw_panel(c, margin, y, w - margin, y + 76, "MATCH", 11)
        mins = int(self.time_left // 60)
        secs = int(self.time_left % 60)
        timer_color = GREEN if self.time_left > 300 else (YELLOW if self.time_left > 60 else RED)
        c.create_text(margin + 18, y + 44, text=f"{mins:02}:{secs:02}", anchor="w", fill=timer_color, font=("Arial", 24, "bold"))
        c.create_text(w - margin - 12, y + 44, text=time.strftime("%I:%M:%S %p"), anchor="e", fill=TEXT, font=("Arial", 11, "bold"))

        y += 84

        self.draw_panel(c, margin, y, w - margin, h - margin, "ESSENTIALS", 11)

        left_x = margin + 16
        right_x = w / 2 + 8
        row1 = y + 32
        row_gap = 18

        compact = [
            ("DEPTH", "--.-- ft"),
            ("PRESS", "--.-- psi"),
            ("PITCH", f"{self.pitch_deg:+05.1f}°"),
            ("YAW", f"{self.heading_deg:06.1f}°"),
            ("ROLL", f"{self.roll_deg:+05.1f}°"),
            ("CAM", self.active_camera.replace(" CAM", "")),
            ("TURTLE", f"{int(round(self.turtle_scale * 100))}%"),
            ("CTRL", "YES" if self.input.connected else "NO"),
        ]

        for i, (label, value) in enumerate(compact):
            col_x = left_x if i % 2 == 0 else right_x
            row_y = row1 + (i // 2) * row_gap
            c.create_text(col_x, row_y, text=label, anchor="w", fill=MUTED, font=("Arial", 8, "bold"))
            c.create_text(col_x + 56, row_y, text=value, anchor="w", fill=TEXT, font=("Arial", 8, "bold"))

    # -----------------------------
    # Main draw loop
    # -----------------------------
    def draw(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, self.width, self.height, fill=BG, outline="")

        if self.window_mode == "quarter":
            self.draw_quarter_main()
            self.draw_mini_info_window()
        else:
            self.draw_half_or_full()
            self.destroy_mini_window()

    def tick(self):
        now = time.time()
        dt = max(0.001, now - self.last_dt)
        self.last_dt = now

        self.update_input()
        if self.input.stop_pressed:
            self.safe_mode = True

        self.update_sim(dt)
        self.draw()

        self.root.after(FPS_MS, self.tick)


def main():
    root = tk.Tk()
    app = ROVGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
