"""Desktop remote control UI for RetroTINK-4K over a serial connection."""

import json
import threading
from pathlib import Path

import serial
import wx

_CONFIG_PATH = Path(__file__).parent / "config.json"

class ConfigManager:
    """Loads, validates, and persists user settings."""

    _DEFAULTS: dict = {
        "port": "",
        "always_on_top": False,
        "hold_initial_delay": 0.4,
        "hold_repeat_interval": 0.1,
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
        return bool(self._data.get("always_on_top", False))

    @always_on_top.setter
    def always_on_top(self, value: bool):
        self._data["always_on_top"] = value

    @property
    def hold_initial_delay(self) -> float:
        return float(self._data.get("hold_initial_delay", 0.4))

    @hold_initial_delay.setter
    def hold_initial_delay(self, value: float):
        self._data["hold_initial_delay"] = value

    @property
    def hold_repeat_interval(self) -> float:
        return float(self._data.get("hold_repeat_interval", 0.1))

    @hold_repeat_interval.setter
    def hold_repeat_interval(self, value: float):
        self._data["hold_repeat_interval"] = value


class SerialController:
    """Sends commands to a RetroTINK-4K over serial with keyboard-autorepeat behaviour.

    On press(): sends once immediately, waits _HOLD_INITIAL_DELAY, then repeats at
    _HOLD_REPEAT_INTERVAL until release() is called. Releasing during the initial
    delay cancels repeat entirely, giving a clean single-send for quick taps.
    """

    _BAUD_RATE = 115200

    def __init__(self, port_getter, on_status, initial_delay: float, repeat_interval: float):
        self._port_getter      = port_getter      # callable → current port string
        self._on_status        = on_status        # callable(str), invoked via wx.CallAfter
        self._initial_delay    = initial_delay
        self._repeat_interval  = repeat_interval
        self._command: str | None = None
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
            ser, active_port = self._send(ser, active_port)

            # Wait _HOLD_INITIAL_DELAY; _released fires early if the button
            # is lifted before repeat would begin — no repeat in that case.
            if self._released.wait(timeout=self._initial_delay):
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
            size=wx.Size(268, 680),
        )
        self._config = ConfigManager()

        self.CreateStatusBar()
        self._build_ui()

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

        # Top row: port selector on the left; hold-repeat tuning on the right.
        self._com_port_label = wx.StaticText(main_panel, label="Port")
        self._com_port = wx.TextCtrl(main_panel, value=self._config.port)
        com_port_sizer = wx.BoxSizer(wx.HORIZONTAL)
        com_port_sizer.Add(self._com_port_label, 0, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT, 8)
        com_port_sizer.Add(self._com_port, 1, wx.EXPAND)

        self._hold_initial_ctrl = wx.SpinCtrlDouble(
            main_panel, min=0.0, max=5.0, inc=0.05,
            initial=self._config.hold_initial_delay, size=wx.Size(65, -1),
        )
        self._hold_initial_ctrl.SetDigits(2)
        self._hold_repeat_ctrl = wx.SpinCtrlDouble(
            main_panel, min=0.01, max=2.0, inc=0.01,
            initial=self._config.hold_repeat_interval, size=wx.Size(65, -1),
        )
        self._hold_repeat_ctrl.SetDigits(2)

        spin_sizer = wx.FlexGridSizer(rows=2, cols=2, vgap=2, hgap=4)
        spin_sizer.Add(wx.StaticText(main_panel, label="Init:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        spin_sizer.Add(self._hold_initial_ctrl)
        spin_sizer.Add(wx.StaticText(main_panel, label="Rpt:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        spin_sizer.Add(self._hold_repeat_ctrl)

        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        top_sizer.Add(com_port_sizer, 1, wx.EXPAND)
        top_sizer.Add(spin_sizer, 0, wx.ALIGN_CENTRE_VERTICAL | wx.LEFT, 10)

        # Button spec format: (name, label, command, size, row, col).
        # Sizer assignment is handled by the (specs, sizer) loop below,
        # keeping layout concerns out of the data tables.
        power_specs = [
            ("pwr_on",  "PWR ON",  "pwr on",     wx.Size(120, 23), 0, 0),
            ("pwr_off", "PWR OFF", "remote pwr",  wx.Size(120, 23), 0, 1),
        ]
        input_profile_specs = [
            ("input",  "INPUT",  "remote input",   wx.Size(79, 23), 0, 0),
            ("out",    "OUT",    "remote out",      wx.Size(80, 23), 0, 1),
            ("scl",    "SCL",    "remote scl",      wx.Size(79, 23), 0, 2),
            ("sfx",    "SFX",    "remote sfx",      wx.Size(79, 23), 1, 0),
            ("adc",    "ADC",    "remote adc",      wx.Size(80, 23), 1, 1),
            ("prof",   "PROF",   "remote prof",     wx.Size(79, 23), 1, 2),
            ("num1",   "1",      "remote prof1",    wx.Size(79, 23), 3, 0),
            ("num2",   "2",      "remote prof2",    wx.Size(80, 23), 3, 1),
            ("num3",   "3",      "remote prof3",    wx.Size(79, 23), 3, 2),
            ("num4",   "4",      "remote prof4",    wx.Size(79, 23), 4, 0),
            ("num5",   "5",      "remote prof5",    wx.Size(80, 23), 4, 1),
            ("num6",   "6",      "remote prof6",    wx.Size(79, 23), 4, 2),
            ("num7",   "7",      "remote prof7",    wx.Size(79, 23), 5, 0),
            ("num8",   "8",      "remote prof8",    wx.Size(80, 23), 5, 1),
            ("num9",   "9",      "remote prof9",    wx.Size(79, 23), 5, 2),
            ("num10",  "10",     "remote prof10",   wx.Size(79, 23), 6, 0),
            ("num11",  "11",     "remote prof11",   wx.Size(80, 23), 6, 1),
            ("num12",  "12",     "remote prof12",   wx.Size(79, 23), 6, 2),
        ]
        navigation_specs = [
            ("menu",   "MENU",      "remote menu",    wx.Size(79, 23), 0, 0),
            ("up",     "↑",         "remote up",      wx.Size(80, 23), 0, 1),
            ("back",   "BACK",      "remote back",    wx.Size(79, 23), 0, 2),
            ("left",   "←",         "remote left",    wx.Size(79, 23), 1, 0),
            ("ok",     "ENTER",     "remote ok",      wx.Size(80, 23), 1, 1),
            ("right",  "→",         "remote right",   wx.Size(79, 23), 1, 2),
            ("diag",   "DIAG",      "remote diag",    wx.Size(79, 23), 2, 0),
            ("down",   "↓",         "remote down",    wx.Size(79, 23), 2, 1),
            ("stat",   "STAT",      "remote stat",    wx.Size(79, 23), 2, 2),
            ("gain",   "A. GAIN",   "remote gain",    wx.Size(79, 23), 4, 0),
            ("pause",  "PAUSE",     "remote pause",   wx.Size(80, 23), 4, 1),
            ("gen",    "GENLOCK",   "remote genlock", wx.Size(79, 23), 4, 2),
            ("phase",  "A. PHASE",  "remote phase",   wx.Size(79, 23), 5, 0),
            ("safe",   "SAFE",      "remote safe",    wx.Size(80, 23), 5, 1),
            ("buffer", "T. BUFFER", "remote buffer",  wx.Size(79, 23), 5, 2),
        ]
        resolution_aux_specs = [
            ("res4k",    "4K",    "remote res4k",    wx.Size(59, 23), 0, 0),
            ("res1080p", "1080p", "remote res1080p", wx.Size(59, 23), 0, 1),
            ("res1440p", "1440p", "remote res1440p", wx.Size(59, 23), 0, 2),
            ("res480p",  "480p",  "remote res480p",  wx.Size(59, 23), 0, 3),
            ("res1",     "RES1",  "remote res1",     wx.Size(59, 23), 1, 0),
            ("res2",     "RES2",  "remote res2",     wx.Size(59, 23), 1, 1),
            ("res3",     "RES3",  "remote res3",     wx.Size(59, 23), 1, 2),
            ("res4",     "RES4",  "remote res4",     wx.Size(59, 23), 1, 3),
            ("aux1",     "AUX1",  "remote aux1",     wx.Size(59, 23), 3, 0),
            ("aux2",     "AUX2",  "remote aux2",     wx.Size(59, 23), 3, 1),
            ("aux3",     "AUX3",  "remote aux3",     wx.Size(59, 23), 3, 2),
            ("aux4",     "AUX4",  "remote aux4",     wx.Size(59, 23), 3, 3),
            ("aux5",     "AUX5",  "remote aux5",     wx.Size(59, 23), 4, 0),
            ("aux6",     "AUX6",  "remote aux6",     wx.Size(59, 23), 4, 1),
            ("aux7",     "AUX7",  "remote aux7",     wx.Size(59, 23), 4, 2),
            ("aux8",     "AUX8",  "remote aux8",     wx.Size(59, 23), 4, 3),
        ]

        self._buttons: dict[str, wx.Button] = {}
        for specs, sizer in (
            (power_specs,           buttons_sizer_1),
            (input_profile_specs,   buttons_sizer_2),
            (navigation_specs,      buttons_sizer_3),
            (resolution_aux_specs,  buttons_sizer_4),
        ):
            for name, label, command, size, row, col in specs:
                btn = self._make_command_button(main_panel, label, command, size)
                self._buttons[name] = btn
                sizer.Add(btn, wx.GBPosition(row, col))

        buttons_sizer_2.SetEmptyCellSize(wx.Size(1, 5))
        buttons_sizer_3.SetEmptyCellSize(wx.Size(1, 8))
        buttons_sizer_4.SetEmptyCellSize(wx.Size(1, 18))

        self._custom_btn = wx.Button(
            main_panel, label="Custom Command...", size=wx.Size(242, 23)
        )
        self._custom_btn.Bind(wx.EVT_BUTTON, self._on_custom)

        self._always_on_top = wx.CheckBox(main_panel, label="Always On Top")
        self._always_on_top.Bind(wx.EVT_CHECKBOX, self._on_always_on_top_changed)

        buttons_sizer_5.Add(self._custom_btn, 0, flag=wx.ALL | wx.EXPAND)
        buttons_sizer_5.Add(self._always_on_top, 0, wx.TOP, border=10)

        panel_sizer.Add(top_sizer, 0, wx.ALL | wx.EXPAND, border=5)
        panel_sizer.Add(buttons_sizer_1, 0, wx.ALL, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_2, 0, wx.ALL, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_3, 0, wx.ALL, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_4, 0, wx.ALL, border=5)
        panel_sizer.AddSpacer(10)
        panel_sizer.Add(buttons_sizer_5, 0, wx.ALL, border=5)

        main_panel.SetSizer(panel_sizer)

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(main_panel, 1, wx.EXPAND)
        self.SetSizer(main_sizer)

    def _restore_state(self):
        """Apply persisted config values to UI widgets after construction."""
        if self._config.always_on_top:
            self._always_on_top.SetValue(True)
            self._apply_always_on_top(True)

    def _make_command_button(self, parent, label: str, command: str, size: wx.Size) -> wx.Button:
        """Create a button that sends continuously while held and stops on release."""
        btn = wx.Button(parent, label=label, size=size)
        btn.Bind(wx.EVT_LEFT_DOWN, lambda _event, cmd=command: self._serial.press(cmd))
        btn.Bind(wx.EVT_LEFT_UP,   lambda _event: self._serial.release())
        return btn

    def _apply_always_on_top(self, enabled: bool):
        style = (self._BASE_STYLE | wx.STAY_ON_TOP) if enabled else self._BASE_STYLE
        self.SetWindowStyle(style)

    def _on_custom(self, event):
        with wx.TextEntryDialog(self, "Custom Command:") as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                cmd = dialog.GetValue()
                if cmd:
                    self._serial.press(cmd)

    def _on_always_on_top_changed(self, event):
        self._apply_always_on_top(self._always_on_top.GetValue())
        event.Skip()

    def _on_exit(self, event):
        self._serial.stop()
        self._config.port = self._com_port.GetValue()
        self._config.always_on_top = self._always_on_top.GetValue()
        self._config.hold_initial_delay = self._hold_initial_ctrl.GetValue()
        self._config.hold_repeat_interval = self._hold_repeat_ctrl.GetValue()
        self._config.save()
        event.Skip()


if __name__ == "__main__":
    app = wx.App()
    frame = Frame(None, title="RT4K Remote")
    frame.Show()
    app.MainLoop()