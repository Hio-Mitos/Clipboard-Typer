# Clipboard Typer

Background Windows tool that remembers your last 50 copied **text** items
and lets you replay any of them as simulated keystrokes (looks like someone
typing) or as a normal paste — using shortcuts that don't clash with the
native Windows+V clipboard history.

## Shortcuts

| Shortcut | Action |
|---|---|
| `Win + Alt + V` | Opens the history flyout, right next to your mouse cursor — pick an item and it's inserted immediately. |
| `Ctrl + Alt + V` | Instantly types out the most recently copied text, with no window shown. |

The history flyout is built to feel like the native Windows+V panel:
- It pops up beside your cursor (not in a fixed corner), with a rounded border and a header/footer bar.
- **Click an item, or press Enter** on the highlighted one, and it's typed out immediately wherever your cursor/focus was (a text box, chat window, etc.) — the flyout closes itself the instant you act.
- **Ctrl+click, or Ctrl+Enter**, pastes it directly instead (normal clipboard paste).
- **Drag the header bar** (or the thin accent strip above it) to move the window anywhere on screen.
- Clicking away, alt-tabbing, or pressing **Esc** closes it instantly.
- If you leave it open and don't touch it, it **auto-closes after 20 seconds of inactivity** (any click, keypress, drag, or mouse movement over the window resets that timer). Closing the flyout — whether you close it yourself or it times out — never stops the background app: clipboard monitoring, both hotkeys, and the tray icon keep running exactly as before.

While typing, every line break in the original text is sent as
`Shift+Enter` instead of `Enter`, and spacing/tabs/indentation are kept
exactly as copied.

Only plain text is captured — images, files, and other clipboard formats
are ignored.

## Windows version compatibility

Works on Windows 7, 8, 8.1, 10, and 11+. All the Windows APIs the tool uses
(`SendInput`, `AttachThreadInput`, `SetForegroundWindow`, layered/DPI-aware
windows) have existed since Windows Vista/7, and the tool automatically
detects the best DPI-awareness mode available on each OS so the flyout lines
up correctly next to your cursor regardless of monitor scaling.

The one thing that differs per OS is which **Python** version to install,
since recent Python releases dropped support for older Windows:

| Windows version | Python version to use |
|---|---|
| Windows 7 | Python 3.8 (last version supporting Windows 7) |
| Windows 8 | Python 3.8 |
| Windows 8.1 | Python 3.9 – 3.12 |
| Windows 10 / 11+ | Python 3.9 – latest (3.12/3.13 recommended) |

Everything else (the script itself, `requirements.txt`) is identical across
all of these — only the Python installer you download differs.

## Install

```
pip install -r requirements.txt
```

(Windows only — uses `pywin32` and Windows keystroke APIs.)

## Run

```
python clipboard_typer.py
```

Runs in the background with a system tray icon (clipboard glyph). Right-click
the tray icon for: open manager, type most recent, pause/resume monitoring,
clear history, quit.

To run without a console window: use `pythonw.exe clipboard_typer.py` instead
of `python.exe`.

## Run automatically at startup

1. Press `Win+R`, type `shell:startup`, hit Enter.
2. Create a shortcut in that folder pointing to:
   `pythonw.exe "C:\path\to\clipboard_typer.py"`

Or package it as a standalone .exe with PyInstaller:

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name ClipboardTyper clipboard_typer.py
```

The .exe will be in `dist\ClipboardTyper.exe` — you can point a startup
shortcut at that instead, so Python doesn't need to be installed.

## Notes / limitations

- History (last 50 items) is kept in memory only and resets when the app
  restarts, as requested.
- If the global shortcuts don't respond inside a specific app, that app may
  be running as Administrator — try running `clipboard_typer.py` as
  Administrator too (both processes need to be at the same privilege level
  for Windows to deliver the keyboard hook).
- Constants at the top of `clipboard_typer.py` (`HISTORY_MAXLEN`,
  `MANAGER_HOTKEY`, `QUICK_TYPE_HOTKEY`, typing speed delays) can be changed
  directly if you want a different history size, different shortcuts, or
  faster/slower simulated typing.
- While a keystroke burst is being typed, the two global hotkeys are briefly
  paused and automatically restored right after — this avoids a bug where a
  long paste could come out with one character repeated dozens of times
  (caused by the hotkey listener and the typing loop fighting over the same
  stream of synthetic keystrokes). If you ever see garbled output again on a
  very slow app, try raising `CHAR_DELAY` / `BREATHER_DELAY` a bit.
- The flyout's auto-close time (default 20s) is `MANAGER_INACTIVITY_MS` at
  the top of `clipboard_typer.py`.

## If something crashes

The app has no console window, so by default a bug would just silently kill
a thread — or the whole app — with no explanation. To avoid that, any crash
that isn't you choosing **Quit** from the tray icon pops up a Windows message
box telling you what happened:
- A crash in a background task (clipboard monitoring, a typing burst, the
  hotkey listener, the history window) shows a box saying the app is *still
  running* but that one feature may be degraded until you restart it.
- A crash that takes down the whole app (e.g. during startup, or in the tray
  icon's own loop) shows a box saying the app *has stopped* and needs to be
  started again.

Either way, the message box includes the underlying error so it can be
reported/debugged.