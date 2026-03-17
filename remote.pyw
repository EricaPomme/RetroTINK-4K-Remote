"""Desktop remote control UI for RetroTINK-4K over a serial connection."""

import json
import threading
from pathlib import Path

import serial
import wx

_CONFIG_PATH = Path(__file__).parent / "config.json"

class ConfigManager:
    """Loads, validates, and persists user settings."""

    # Hardcoded safe minimums — used as fallbacks when config.json is absent or corrupt.
    _DEFAULTS: dict = {
        "port": "",
        "always_on_top": False,
        "hold_initial_delay": 0.4,
        "hold_repeat_interval": 0.1,
        "button_height": 23,
        "min_window_width": 200,
        "min_window_height": 400,
        "custom_command": "",
    }

    def __init__(self, path: Path = _CONFIG_PATH):
        self._path = path
        self._data: dict = dict(self._DEFAULTS)
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with self._path.open() as f:
                    self._data.update(json.load(f))
                self._data["port"] = str(self._data.get("port", ""))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        with self._path.open("w") as f:
            json.dump(self._data, f, indent=4)

    @property
    def port(self) -> str:
        return self._data["port"]

    @port.setter
    def port(self, value: str):
        self._data["port"] = value

    @property
    def always_on_top(self) -> bool:
        return bool(self._data.get("always_on_top", self._DEFAULTS["always_on_top"]))

    @always_on_top.setter
    def always_on_top(self, value: bool):
        self._data["always_on_top"] = value

    @property
    def hold_initial_delay(self) -> float:
        return float(self._data.get("hold_initial_delay", self._DEFAULTS["hold_initial_delay"]))

    @hold_initial_delay.setter
    def hold_initial_delay(self, value: float):
        self._data["hold_initial_delay"] = value

    @property
    def hold_repeat_interval(self) -> float:
        return float(self._data.get("hold_repeat_interval", self._DEFAULTS["hold_repeat_interval"]))

    @hold_repeat_interval.setter
    def hold_repeat_interval(self, value: float):
        self._data["hold_repeat_interval"] = value

    @property
    def button_height(self) -> int:
        return max(int(self._data.get("button_height", self._DEFAULTS["button_height"])), 16)

    @property
    def min_window_width(self) -> int:
        return max(int(self._data.get("min_window_width", self._DEFAULTS["min_window_width"])), 200)

    @property
    def min_window_height(self) -> int:
        return max(int(self._data.get("min_window_height", self._DEFAULTS["min_window_height"])), 400)

    @property
    def custom_command(self) -> str:
        return str(self._data.get("custom_command", self._DEFAULTS["custom_command"]))

    @custom_command.setter
    def custom_command(self, value: str):
        self._data["custom_command"] = value


class SerialController:
    """Sends commands to a RetroTINK-4K over serial with keyboard-autorepeat behaviour.

    On press(): sends once immediately, waits _HOLD_INITIAL_DELAY, then repeats at
    _HOLD_REPEAT_INTERVAL until release() is called. Releasing during the initial
    delay cancels repeat entirely, giving a clean single-send for quick taps.
    Pressing a *different* key during the initial delay resets the window
    immediately so the new key is handled without waiting out the old delay.
    """

    _BAUD_RATE = 115200

    def __init__(self, port_getter, on_status, initial_delay: float, repeat_interval: float):
        self._port_getter      = port_getter      # callable → current port string
        self._on_status        = on_status        # callable(str), invoked via wx.CallAfter
        self._initial_delay    = initial_delay
        self._repeat_interval  = repeat_interval
        self._command: str | None = None
        self._press_seq   = 0                   # increments on every press(), same key or not
        self._held        = threading.Event()   # set while button is physically down
        self._released    = threading.Event()   # set while button is up (inverse of _held)
        self._released.set()
        self._lock        = threading.Lock()
        self._stop        = threading.Event()
        self._thread      = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def press(self, command: str):
        """Send once immediately; begin repeat after _HOLD_INITIAL_DELAY if still held."""
        with self._lock:
            self._command = command
            self._press_seq += 1
        self._released.clear()
        self._held.set()

    def release(self):
        """Stop sending; called on EVT_LEFT_UP."""
        self._held.clear()
        self._released.set()
        with self._lock:
            self._command = None

    def stop(self):
        """Shut down the worker thread; call on app exit."""
        self._stop.set()
        self._held.set()     # unblock _held.wait()
        self._released.set() # unblock _released.wait()

    def set_initial_delay(self, value: float):
        """Update hold initial delay; takes effect on the next press."""
        self._initial_delay = value

    def set_repeat_interval(self, value: float):
        """Update repeat interval; takes effect on the next repeat cycle."""
        self._repeat_interval = value

    def _send(self, ser, active_port):
        """Send the current command; returns updated (ser, active_port)."""
        with self._lock:
            cmd = self._command
        if cmd is None:
            return ser, active_port
        port = self._port_getter()
        if not port:
            wx.CallAfter(self._on_status, "No port configured")
            return ser, active_port
        try:
            if ser is None or not ser.is_open or port != active_port:
                if ser is not None and ser.is_open:
                    ser.close()
                ser = serial.Serial(port, self._BAUD_RATE, timeout=1, write_timeout=1)
                active_port = port
            ser.write(f"{cmd}\n".encode("utf-8"))
            wx.CallAfter(self._on_status, "")
        except serial.SerialException:
            ser = None
            wx.CallAfter(self._on_status, "Send failed — check port")
        return ser, active_port

    def _run(self):
        ser: serial.Serial | None = None
        active_port = ""

        while not self._stop.is_set():
            # Block here while idle; wakes immediately on press() or stop().
            self._held.wait(timeout=0.1)
            if not self._held.is_set():
                if ser is not None and ser.is_open:
                    ser.close()
                    ser = None
                continue

            # ── Initial send ─────────────────────────────────────────────────
            with self._lock:
                sent_seq = self._press_seq
            ser, active_port = self._send(ser, active_port)

            # Wait _HOLD_INITIAL_DELAY; exit early if:
            #   - the button is released (single tap, no repeat)
            #   - any press() is called again (different key or same key spam)
            _POLL = 0.02
            delay_left = self._initial_delay
            early_exit = False
            while delay_left > 0:
                if self._released.wait(timeout=min(_POLL, delay_left)):
                    early_exit = True
                    break
                with self._lock:
                    if self._press_seq != sent_seq:
                        early_exit = True
                        break
                delay_left -= _POLL

            if early_exit:
                if ser is not None and ser.is_open:
                    ser.close()
                    ser = None
                continue

            # ── Repeat phase ─────────────────────────────────────────────────
            # _released.wait() doubles as an interruptible sleep: it returns
            # False on timeout (keep repeating) and True when released (stop).
            while not self._released.wait(timeout=self._repeat_interval):
                if self._stop.is_set():
                    break
                ser, active_port = self._send(ser, active_port)

            if ser is not None and ser.is_open:
                ser.close()
                ser = None

        if ser is not None and ser.is_open:
            ser.close()


class Frame(wx.Frame):
    """Main window that maps button presses to serial CLI commands."""

    _BASE_STYLE = wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)

    def __init__(self, parent, title):
        super().__init__(
            parent=None,
            title=title,
            style=self._BASE_STYLE,
        )
        self._config = ConfigManager()

        self.CreateStatusBar()
        self._build_ui()
        self.Fit()
        self.SetMinSize(wx.Size(self._config.min_window_width, self._config.min_window_height))

        # Instantiated after _build_ui so self._com_port exists for port_getter.
        self._serial = SerialController(
            port_getter=lambda: self._com_port.GetValue(),
            on_status=self.SetStatusText,
            initial_delay=self._config.hold_initial_delay,
            repeat_interval=self._config.hold_repeat_interval,
        )

        self._hold_initial_ctrl.Bind(
            wx.EVT_SPINCTRLDOUBLE,
            lambda e: self._serial.set_initial_delay(e.GetValue()),
        )
        self._hold_repeat_ctrl.Bind(
            wx.EVT_SPINCTRLDOUBLE,
            lambda e: self._serial.set_repeat_interval(e.GetValue()),
        )

        self._restore_state()
        self.Bind(wx.EVT_CLOSE, self._on_exit)

    def _build_ui(self):
        main_panel = wx.Panel(self)
        panel_sizer = wx.BoxSizer(wx.VERTICAL)
        buttons_sizer_1 = wx.GridBagSizer(hgap=2, vgap=2)
        buttons_sizer_2 = wx.GridBagSizer(hgap=2, vgap=2)
        buttons_sizer_3 = wx.GridBagSizer(hgap=2, vgap=2)
        buttons_sizer_4 = wx.GridBagSizer(hgap=2, vgap=2)
        buttons_sizer_5 = wx.BoxSizer(wx.VERTICAL)

        # Top section: row 0 — port selector spanning both columns;
        # row 1 — Init and Rpt controls, each left-aligned in their own growable column.
        self._com_port_label = wx.StaticText(main_panel, label="Port")
        self._com_port = wx.TextCtrl(main_panel, value=self._config.port)
        com_port_sizer = wx.BoxSizer(wx.HORIZONTAL)
        com_port_sizer.Add(self._com_port_label, 0, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT, 8)
        com_port_sizer.Add(self._com_port, 1, wx.EXPAND)

        self._hold_initial_ctrl = wx.SpinCtrlDouble(
            main_panel, min=0.0, max=5.0, inc=0.05,
            initial=self._config.hold_initial_delay,
        )
        self._hold_initial_ctrl.SetDigits(2)
        self._hold_repeat_ctrl = wx.SpinCtrlDouble(
            main_panel, min=0.01, max=2.0, inc=0.01,
            initial=self._config.hold_repeat_interval,
        )
        self._hold_repeat_ctrl.SetDigits(2)

        init_sizer = wx.BoxSizer(wx.HORIZONTAL)
        init_sizer.Add(wx.StaticText(main_panel, label="Init:"), 0, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT, 4)
        init_sizer.Add(self._hold_initial_ctrl, 1, wx.EXPAND)

        rpt_sizer = wx.BoxSizer(wx.HORIZONTAL)
        rpt_sizer.Add(wx.StaticText(main_panel, label="Repeat:"), 0, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT, 4)
        rpt_sizer.Add(self._hold_repeat_ctrl, 1, wx.EXPAND)

        top_sizer = wx.GridBagSizer(hgap=4, vgap=2)
        top_sizer.Add(com_port_sizer, pos=(0, 0), span=(1, 2), flag=wx.EXPAND)
        top_sizer.Add(init_sizer, pos=(1, 0), flag=wx.EXPAND)
        top_sizer.Add(rpt_sizer, pos=(1, 1), flag=wx.EXPAND)
        top_sizer.AddGrowableCol(0)
        top_sizer.AddGrowableCol(1)

        # Button spec format: (name, label, command, row, col).
        # Widths are dynamic — see AddGrowableCol calls below.
        # Sizer assignment is handled by the (specs, sizer, ncols) loop,
        # keeping layout concerns out of the data tables.
        power_specs = [
            ("pwr_on",  "PWR ON",  "pwr on",     0, 0),
            ("pwr_off", "PWR OFF", "remote pwr",  0, 1),
        ]
        input_profile_specs = [
            ("input",  "INPUT",  "remote input",   0, 0),
            ("out",    "OUT",    "remote out",      0, 1),
            ("scl",    "SCL",    "remote scl",      0, 2),
            ("sfx",    "SFX",    "remote sfx",      1, 0),
            ("adc",    "ADC",    "remote adc",      1, 1),
            ("prof",   "PROF",   "remote prof",     1, 2),
            ("num1",   "1",      "remote prof1",    3, 0),
            ("num2",   "2",      "remote prof2",    3, 1),
            ("num3",   "3",      "remote prof3",    3, 2),
            ("num4",   "4",      "remote prof4",    4, 0),
            ("num5",   "5",      "remote prof5",    4, 1),
            ("num6",   "6",      "remote prof6",    4, 2),
            ("num7",   "7",      "remote prof7",    5, 0),
            ("num8",   "8",      "remote prof8",    5, 1),
            ("num9",   "9",      "remote prof9",    5, 2),
            ("num10",  "10",     "remote prof10",   6, 0),
            ("num11",  "11",     "remote prof11",   6, 1),
            ("num12",  "12",     "remote prof12",   6, 2),
        ]
        navigation_specs = [
            ("menu",   "MENU",      "remote menu",    0, 0),
            ("up",     "↑",         "remote up",      0, 1),
            ("back",   "BACK",      "remote back",    0, 2),
            ("left",   "←",         "remote left",    1, 0),
            ("ok",     "ENTER",     "remote ok",      1, 1),
            ("right",  "→",         "remote right",   1, 2),
            ("diag",   "DIAG",      "remote diag",    2, 0),
            ("down",   "↓",         "remote down",    2, 1),
            ("stat",   "STAT",      "remote stat",    2, 2),
            ("gain",   "A. GAIN",   "remote gain",    4, 0),
            ("pause",  "PAUSE",     "remote pause",   4, 1),
            ("gen",    "GENLOCK",   "remote genlock", 4, 2),
            ("phase",  "A. PHASE",  "remote phase",   5, 0),
            ("safe",   "SAFE",      "remote safe",    5, 1),
            ("buffer", "T. BUFFER", "remote buffer",  5, 2),
        ]
        resolution_aux_specs = [
            ("res4k",    "4K",    "remote res4k",    0, 0),
            ("res1080p", "1080p", "remote res1080p", 0, 1),
            ("res1440p", "1440p", "remote res1440p", 0, 2),
            ("res480p",  "480p",  "remote res480p",  0, 3),
            ("res1",     "RES1",  "remote res1",     1, 0),
            ("res2",     "RES2",  "remote res2",     1, 1),
            ("res3",     "RES3",  "remote res3",     1, 2),
            ("res4",     "RES4",  "remote res4",     1, 3),
            ("aux1",     "AUX1",  "remote aux1",     3, 0),
            ("aux2",     "AUX2",  "remote aux2",     3, 1),
            ("aux3",     "AUX3",  "remote aux3",     3, 2),
            ("aux4",     "AUX4",  "remote aux4",     3, 3),
            ("aux5",     "AUX5",  "remote aux5",     4, 0),
            ("aux6",     "AUX6",  "remote aux6",     4, 1),
            ("aux7",     "AUX7",  "remote aux7",     4, 2),
            ("aux8",     "AUX8",  "remote aux8",     4, 3),
        ]

        self._buttons: dict[str, wx.Button] = {}
        for specs, sizer, ncols in (
            (power_specs,           buttons_sizer_1, 2),
            (input_profile_specs,   buttons_sizer_2, 3),
            (navigation_specs,      buttons_sizer_3, 3),
            (resolution_aux_specs,  buttons_sizer_4, 4),
        ):
            for name, label, command, row, col in specs:
                btn = self._make_command_button(main_panel, label, command)
                self._buttons[name] = btn
                sizer.Add(btn, wx.GBPosition(row, col), flag=wx.EXPAND)
            for col in range(ncols):
                sizer.AddGrowableCol(col)

        buttons_sizer_2.SetEmptyCellSize(wx.Size(1, 5))
        buttons_sizer_3.SetEmptyCellSize(wx.Size(1, 8))
        buttons_sizer_4.SetEmptyCellSize(wx.Size(1, 18))

        self._custom_btn = wx.Button(main_panel, label="Custom Command")
        self._custom_btn.Bind(wx.EVT_LEFT_DOWN, lambda e: self._serial.press(self._custom_cmd_ctrl.GetValue()) if self._custom_cmd_ctrl.GetValue() else None)
        self._custom_btn.Bind(wx.EVT_LEFT_UP, lambda e: self._serial.release())
        self._custom_cmd_ctrl = wx.TextCtrl(main_panel, value=self._config.custom_command)
        custom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        custom_sizer.Add(self._custom_btn, 0, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT, 4)
        custom_sizer.Add(self._custom_cmd_ctrl, 1, wx.EXPAND)

        self._always_on_top = wx.CheckBox(main_panel, label="Always On Top")
        self._always_on_top.Bind(wx.EVT_CHECKBOX, self._on_always_on_top_changed)

        buttons_sizer_5.Add(custom_sizer, 0, flag=wx.EXPAND)
        buttons_sizer_5.Add(self._always_on_top, 0, wx.TOP, border=10)

        panel_sizer.Add(top_sizer, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.Add(buttons_sizer_1, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_2, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_3, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_4, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_5, 0, wx.ALL | wx.EXPAND, border=5)

        main_panel.SetSizer(panel_sizer)

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(main_panel, 1, wx.EXPAND)
        self.SetSizer(main_sizer)

    def _restore_state(self):
        """Apply persisted config values to UI widgets after construction."""
        if self._config.always_on_top:
            self._always_on_top.SetValue(True)
            self._apply_always_on_top(True)

    def _make_command_button(self, parent, label: str, command: str) -> wx.Button:
        """Create a button that sends continuously while held and stops on release."""
        btn = wx.Button(parent, label=label, size=wx.Size(-1, self._config.button_height))
        btn.Bind(wx.EVT_LEFT_DOWN, lambda _event, cmd=command: self._serial.press(cmd))
        btn.Bind(wx.EVT_LEFT_UP,   lambda _event: self._serial.release())
        return btn

    def _apply_always_on_top(self, enabled: bool):
        style = (self._BASE_STYLE | wx.STAY_ON_TOP) if enabled else self._BASE_STYLE
        self.SetWindowStyle(style)

    def _on_always_on_top_changed(self, event):
        self._apply_always_on_top(self._always_on_top.GetValue())
        event.Skip()

    def _on_exit(self, event):
        self._serial.stop()
        self._config.port = self._com_port.GetValue()
        self._config.always_on_top = self._always_on_top.GetValue()
        self._config.hold_initial_delay = self._hold_initial_ctrl.GetValue()
        self._config.hold_repeat_interval = self._hold_repeat_ctrl.GetValue()
        self._config.custom_command = self._custom_cmd_ctrl.GetValue()
        self._config.save()
        event.Skip()


if __name__ == "__main__":
    app = wx.App()
    frame = Frame(None, title="RT4K Remote")
    frame.Show()
    app.MainLoop()