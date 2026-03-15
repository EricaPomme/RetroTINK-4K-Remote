# RetroTINK-4K Remote

![](screenshot.png)

A desktop recreation of the [RetroTINK-4K](https://www.retrotink.com/product-page/retrotink-4k) remote control that issues commands over USB serial, written in wxPython.

## Requirements

- [RetroTINK-4K firmware 1.6.6 or newer](https://retrotink-llc.github.io/firmware/), which adds [USB serial command support](https://consolemods.org/wiki/AV:RetroTINK-4K#USB_Serial_Configuration)
- Python 3.10+
- [wxPython](https://wxpython.org) and [PySerial](https://pyserial.readthedocs.io/en/latest/pyserial.html)

```shell
pip install wxPython pyserial
```

## Usage

Connect the RetroTINK-4K to your PC via USB, then enter its serial port in the **Port** field and click any button. The port value is persisted to `config.json` alongside the script.

Buttons cover power, input selection, profile recall, navigation, resolution presets, and AUX slots. A **Custom Command...** button lets you send arbitrary serial commands. **Always On Top** keeps the window above other applications.

### Hold-to-repeat

Buttons behave like keyboard autorepeat: a single click sends one command. Holding a button sends once immediately, pauses for the **Init** delay (default 0.4 s), then repeats at the **Rpt** interval (default 0.1 s) until released.

Both values are adjustable via the spinners in the top-right corner of the window and are saved to `config.json` on exit.

Serial I/O runs on a background thread so the UI stays responsive during holds.

## Configuration

Settings are stored in `config.json` next to the script and managed by `ConfigManager`. The file is created automatically on first exit. If it is absent or unreadable at startup, every setting falls back to the hardcoded default defined in `ConfigManager._DEFAULTS`.

### Runtime settings

These are written back to `config.json` automatically when the window is closed.

| Key | Default | Description |
|-----|---------|-------------|
| `port` | `""` | Serial port used to communicate with the device (e.g. `"COM3"` or `"/dev/ttyUSB0"`). |
| `always_on_top` | `false` | Whether the window floats above all other applications. |
| `hold_initial_delay` | `0.4` | Seconds to wait after the first send before repeat begins. Also editable via the **Init** spinner. |
| `hold_repeat_interval` | `0.1` | Seconds between successive sends during the repeat phase. Also editable via the **Rpt** spinner. |

### Layout overrides

These can be set manually in `config.json` to customise the UI geometry. They are **not** written back on exit, so edits persist across sessions. Safe minimum values are enforced in code regardless of what the file contains.

| Key | Default | Minimum | Description |
|-----|---------|---------|-------------|
| `button_height` | `23` | `16` | Height in pixels of every command button. |
| `min_window_width` | `200` | `200` | Minimum window width in pixels. The window will be at least as wide as its content requires after auto-fit. |
| `min_window_height` | `400` | `400` | Minimum window height in pixels. |
