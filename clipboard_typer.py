"""
Clipboard Typer
================
Background Windows utility that:
  - Watches the system clipboard and keeps a history of the last N
    pieces of TEXT that were copied (non-text clipboard content, e.g.
    images or files, is ignored).
  - Lets you re-insert any item from that history either by:
      a) simulating real keystrokes (as if someone typed it), or
      b) pasting it directly (normal Ctrl+V paste).
  - Uses its own shortcuts, separate from the native Windows+V
    clipboard history:
      Win+Alt+V   -> opens the history manager (pick any past item)
      Ctrl+Alt+V  -> instantly types out the most recently copied item

When "typing", every line break in the copied text is sent as
Shift+Enter (instead of Enter), and the rest of the formatting
(spaces, tabs, indentation, unicode characters) is preserved exactly.

Compatible with Windows 7, 8, 8.1, 10, 11 and later (see README for the
Python version to use on each).

Requirements (Windows only):
    pip install keyboard pyperclip pywin32 pystray pillow

Run:
    pythonw.exe clipboard_typer.py      (no console window)
    python.exe clipboard_typer.py       (with console, useful for debugging)
"""

import ctypes
import threading
import time
from collections import deque

import keyboard
import pyperclip
import win32gui
import win32con
import win32process
import win32api
import pystray
from PIL import Image, ImageDraw
import tkinter as tk

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HISTORY_MAXLEN = 50              # keep last 50 copied text items, in memory only
POLL_INTERVAL = 0.4              # seconds between clipboard checks
MANAGER_HOTKEY = "windows+alt+v"
QUICK_TYPE_HOTKEY = "ctrl+alt+v"
CHAR_DELAY = 0.014                # delay after each keystroke, before the next one
KEY_PRESS_GAP = 0.004             # delay between a key's down and its up event
NEWLINE_DELAY = 0.022
BREATHER_EVERY = 40               # extra pause every N characters, lets the
BREATHER_DELAY = 0.05             # target app's input queue catch its breath

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
history = deque(maxlen=HISTORY_MAXLEN)
history_lock = threading.Lock()
last_seen_value = None
monitoring_enabled = True
_manager_open = False
_hotkey_refs = {}
_hotkey_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low level keystroke simulation (ctypes / SendInput)
#
# We build raw Windows SendInput events ourselves instead of relying on a
# library's text-typing helper, because:
#   - KEYEVENTF_UNICODE lets us send the *exact* character (spaces, tabs,
#     accents, emoji, etc.) regardless of keyboard layout.
#   - We need full control over when Shift+Enter is sent for line breaks.
# ---------------------------------------------------------------------------
PUL = ctypes.POINTER(ctypes.c_ulong)


class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", InputUnion)]


INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12       # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_RETURN = 0x0D

MODIFIER_SETTLE_DELAY = 0.06   # time to let real key-ups catch up before typing

_extra = ctypes.c_ulong(0)


def _enable_dpi_awareness():
    """Make the process DPI-aware so mouse-cursor coordinates and the
    flyout's position/size line up correctly on any Windows version and any
    monitor scaling setting (100% / 125% / 150% / mixed multi-monitor DPI).
    Without this, GetCursorPos()-based positioning can be off by the
    scaling factor on high-DPI screens. Each API below was introduced in a
    different Windows release, so we try the best one first and fall back
    for older systems - this is what lets the same build target Windows 7
    through 11+.
    """
    try:
        # Windows 10 version 1703+ : per-monitor v2 (most accurate).
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass
    try:
        # Windows 8.1 / early Windows 10 : per-monitor aware.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        # Windows Vista / 7 / 8 : system DPI aware (best available there).
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass  # very old/unusual setups: fall back to unaware, still works


def _send(*inputs):
    n = len(inputs)
    arr = (Input * n)(*inputs)
    ctypes.windll.user32.SendInput(n, ctypes.pointer(arr), ctypes.sizeof(Input))


def _unicode_event(code, keyup=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    ki = KeyBdInput(0, code, flags, 0, ctypes.pointer(_extra))
    return Input(INPUT_KEYBOARD, InputUnion(ki=ki))


def _vk_event(vk, keyup=False):
    flags = KEYEVENTF_KEYUP if keyup else 0
    ki = KeyBdInput(vk, 0, flags, 0, ctypes.pointer(_extra))
    return Input(INPUT_KEYBOARD, InputUnion(ki=ki))


def _utf16_units(ch):
    """Return the one (BMP) or two (surrogate pair) UTF-16 code units for a
    single Python character, since SendInput's Unicode field is 16-bit."""
    code = ord(ch)
    if code <= 0xFFFF:
        return (code,)
    code -= 0x10000
    high = 0xD800 + (code >> 10)
    low = 0xDC00 + (code & 0x3FF)
    return (high, low)


def _type_char(ch):
    # Down and up are sent as two separate, ordered SendInput calls with a
    # small real gap between them: a zero-duration keypress is what a lot of
    # text widgets treat as unreliable/ignorable, which is how a character
    # goes missing while a neighbouring one gets duplicated in its place.
    # This function returns only after both events for this character have
    # been handed to the OS input queue in order, so the caller never starts
    # the next character until this one is fully done.
    for unit in _utf16_units(ch):
        _send(_unicode_event(unit))
        time.sleep(KEY_PRESS_GAP)
        _send(_unicode_event(unit, keyup=True))
    time.sleep(CHAR_DELAY)


def _send_shift_enter():
    _send(
        _vk_event(VK_SHIFT),
        _vk_event(VK_RETURN),
        _vk_event(VK_RETURN, keyup=True),
        _vk_event(VK_SHIFT, keyup=True),
    )
    time.sleep(NEWLINE_DELAY)


def _release_modifiers():
    """Force Ctrl/Alt/Shift/Win to a released state before we start typing.

    Ctrl+Alt+V fires on the key-down of 'V' - at that instant the user is
    usually still physically holding Ctrl and Alt down for a moment. If we
    start sending Unicode characters while Windows still sees Ctrl/Alt as
    held, some apps read the first few keystrokes as Ctrl+<char> / Alt+<char>
    shortcuts instead of plain text, which is what causes the occasional
    dropped/altered character right at the start of a Ctrl+Alt+V paste (this
    doesn't happen from the history manager, since by the time you click an
    item there, the hotkey's keys were already released).
    """
    _send(
        _vk_event(VK_SHIFT, keyup=True),
        _vk_event(VK_CONTROL, keyup=True),
        _vk_event(VK_MENU, keyup=True),
        _vk_event(VK_LWIN, keyup=True),
        _vk_event(VK_RWIN, keyup=True),
    )
    time.sleep(MODIFIER_SETTLE_DELAY)


def type_text(text):
    """Simulate typing `text` keystroke by keystroke.
    Every line break becomes Shift+Enter instead of Enter."""
    if not text:
        return
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # Our own global hotkey hook (Win+Alt+V / Ctrl+Alt+V) sees every
    # synthetic keystroke system-wide too. Running its Python callback for
    # each of the hundreds of events below, on the same GIL as this typing
    # loop, is what causes events to back up and a key to look "stuck" /
    # repeated in the output. Pausing the hotkeys for the duration of the
    # typing burst removes that contention entirely.
    _unregister_hotkeys()
    try:
        _release_modifiers()
        char_count = 0
        for i, line in enumerate(lines):
            for ch in line:
                _type_char(ch)
                char_count += 1
                if char_count % BREATHER_EVERY == 0:
                    time.sleep(BREATHER_DELAY)
            if i < len(lines) - 1:
                _send_shift_enter()
    finally:
        _register_hotkeys()


# ---------------------------------------------------------------------------
# Global hotkey (de)registration
#
# Kept separate so the typing routine can unhook completely for the brief
# duration of a keystroke burst (see type_text) and reliably restore
# afterwards, even if something goes wrong mid-burst.
#
# Important: we call keyboard.unhook_all() here rather than
# keyboard.remove_hotkey() for each hotkey. remove_hotkey() only removes our
# handlers - the module's low-level, system-wide keyboard hook keeps running
# underneath and still runs a Python callback for every one of our own
# synthetic keystrokes. That callback competes with the typing loop for the
# GIL, and that contention is what causes a character to be typed twice
# while its neighbour gets dropped. unhook_all() removes the OS-level hook
# itself, so nothing intercepts our own typing traffic while it's in flight.
# ---------------------------------------------------------------------------
def _register_hotkeys():
    with _hotkey_lock:
        if _hotkey_refs:
            return  # already registered
        _hotkey_refs["manager"] = keyboard.add_hotkey(MANAGER_HOTKEY, open_manager)
        _hotkey_refs["quick"] = keyboard.add_hotkey(QUICK_TYPE_HOTKEY, quick_type_latest)


def _unregister_hotkeys():
    with _hotkey_lock:
        if not _hotkey_refs:
            return
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        _hotkey_refs.clear()


# ---------------------------------------------------------------------------
# Clipboard monitoring
# ---------------------------------------------------------------------------
def monitor_clipboard():
    global last_seen_value
    while True:
        if monitoring_enabled:
            try:
                current = pyperclip.paste()
            except Exception:
                current = None
            if current and current != last_seen_value:
                last_seen_value = current
                with history_lock:
                    # move-to-front if it already exists, else insert new
                    if current in history:
                        history.remove(current)
                    history.appendleft(current)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Foreground window helpers (so the manager popup doesn't steal focus
# permanently from the app you were typing/pasting into). We use the
# AttachThreadInput trick, the same one Windows flyouts like the native
# clipboard history rely on internally, so switching back is instant and
# doesn't need an artificial delay.
# ---------------------------------------------------------------------------
def _restore_foreground(hwnd):
    if not hwnd:
        return
    try:
        cur_thread = win32api.GetCurrentThreadId()
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

        attached_fg = fg_thread != cur_thread
        attached_target = target_thread != cur_thread and target_thread != fg_thread

        if attached_fg:
            win32process.AttachThreadInput(cur_thread, fg_thread, True)
        if attached_target:
            win32process.AttachThreadInput(cur_thread, target_thread, True)
        try:
            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)
        finally:
            if attached_fg:
                win32process.AttachThreadInput(cur_thread, fg_thread, False)
            if attached_target:
                win32process.AttachThreadInput(cur_thread, target_thread, False)
    except Exception:
        pass


def _paste_directly(hwnd, text):
    _restore_foreground(hwnd)
    time.sleep(0.03)
    try:
        pyperclip.copy(text)
        keyboard.send("ctrl+v")
    except Exception:
        pass


def _type_into(hwnd, text):
    _restore_foreground(hwnd)
    time.sleep(0.03)
    type_text(text)


# ---------------------------------------------------------------------------
# Quick-type: Ctrl+Alt+V -> type the most recent clipboard entry immediately
# ---------------------------------------------------------------------------
def quick_type_latest():
    with history_lock:
        if not history:
            return
        text = history[0]
    threading.Thread(target=type_text, args=(text,), daemon=True).start()


# ---------------------------------------------------------------------------
# History manager popup (Win+Alt+V)
#
# Styled and behaved like a lightweight flyout (similar spirit to the native
# Win+V panel): no title bar, appears next to the mouse/caret, single click
# (or Enter) commits the item immediately, and it closes itself the instant
# it loses focus so it never lingers on screen.
# ---------------------------------------------------------------------------
POPUP_WIDTH = 360
POPUP_MAX_HEIGHT = 420
ROW_HEIGHT = 26
BORDER_COLOR = "#0078D4"   # Windows accent blue, for a native-ish flyout border
BG_COLOR = "#FFFFFF"
SELECT_COLOR = "#CCE4F7"
HINT_COLOR = "#6B6B6B"


def open_manager():
    global _manager_open
    if _manager_open:
        return
    _manager_open = True

    origin_hwnd = win32gui.GetForegroundWindow()

    def build_ui():
        global _manager_open
        with history_lock:
            items = list(history)

        root = tk.Tk()
        root.withdraw()  # position it before showing, to avoid a visible jump
        root.overrideredirect(True)   # no title bar / borders -> flyout look
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        root.configure(bg=BORDER_COLOR)

        outer = tk.Frame(root, bg=BORDER_COLOR)
        outer.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        inner = tk.Frame(outer, bg=BG_COLOR)
        inner.pack(fill=tk.BOTH, expand=True)

        hint = tk.Label(
            inner,
            text="Click / Enter = type it   ·   Ctrl+click / Ctrl+Enter = paste directly   ·   Esc = close",
            fg=HINT_COLOR,
            bg=BG_COLOR,
            anchor="w",
            justify="left",
            font=("Segoe UI", 8),
            wraplength=POPUP_WIDTH - 20,
        )
        hint.pack(fill=tk.X, padx=10, pady=(8, 4))

        list_frame = tk.Frame(inner, bg=BG_COLOR)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(
            list_frame,
            font=("Segoe UI", 10),
            yscrollcommand=scrollbar.set,
            activestyle="none",
            highlightthickness=0,
            borderwidth=0,
            bg=BG_COLOR,
            selectbackground=SELECT_COLOR,
            selectforeground="#000000",
        )
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        if not items:
            listbox.insert(tk.END, "(clipboard history is empty)")
        else:
            for entry in items:
                preview = entry.replace("\n", " ⏎ ").replace("\t", "→")
                if len(preview) > 120:
                    preview = preview[:120] + "..."
                listbox.insert(tk.END, preview)

        listbox.selection_set(0)
        listbox.activate(0)

        # --- size + position: appears right next to the mouse cursor,
        # clamped so it never runs off the edge of the screen ---
        visible_rows = min(max(len(items), 1), 10)
        height = 40 + visible_rows * ROW_HEIGHT
        height = min(height, POPUP_MAX_HEIGHT)

        cursor_x, cursor_y = win32api.GetCursorPos()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = min(cursor_x + 12, screen_w - POPUP_WIDTH - 8)
        y = min(cursor_y + 12, screen_h - height - 8)
        x = max(x, 0)
        y = max(y, 0)
        root.geometry(f"{POPUP_WIDTH}x{height}+{x}+{y}")

        closed = {"done": False}

        def close(event=None):
            if closed["done"]:
                return
            closed["done"] = True
            global _manager_open
            _manager_open = False
            root.destroy()

        def act(type_as_keystrokes):
            sel = listbox.curselection()
            if not sel or not items:
                close()
                return
            text = items[sel[0]]
            close()
            if type_as_keystrokes:
                threading.Thread(target=_type_into, args=(origin_hwnd, text), daemon=True).start()
            else:
                threading.Thread(target=_paste_directly, args=(origin_hwnd, text), daemon=True).start()

        def on_click(event):
            # let the click set the selection first, then commit on release
            index = listbox.nearest(event.y)
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(index)
            ctrl_held = bool(event.state & 0x0004)
            root.after(1, lambda: act(not ctrl_held))

        listbox.bind("<ButtonRelease-1>", on_click)
        listbox.bind("<Return>", lambda e: act(True))
        listbox.bind("<Control-Return>", lambda e: act(False))
        root.bind("<Escape>", close)
        # Auto-close as soon as the flyout loses focus, just like a native
        # popup (e.g. user clicks elsewhere or alt-tabs away).
        root.bind("<FocusOut>", lambda e: root.after(120, _close_if_unfocused))

        def _close_if_unfocused():
            try:
                if root.focus_get() is None:
                    close()
            except Exception:
                close()

        root.protocol("WM_DELETE_WINDOW", close)

        root.deiconify()
        root.lift()
        root.after(10, lambda: (root.focus_force(), listbox.focus_set()))

        root.mainloop()

    threading.Thread(target=build_ui, daemon=True).start()


# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------
def _make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([14, 6, 50, 58], radius=6, outline=(60, 60, 60), width=3, fill=(245, 245, 245, 255))
    d.rectangle([24, 2, 40, 12], fill=(90, 90, 90, 255))
    d.line([22, 22, 42, 22], fill=(60, 60, 60), width=3)
    d.line([22, 32, 42, 32], fill=(60, 60, 60), width=3)
    d.line([22, 42, 36, 42], fill=(60, 60, 60), width=3)
    return img


def toggle_monitoring(icon, item):
    global monitoring_enabled
    monitoring_enabled = not monitoring_enabled


def clear_history(icon, item):
    with history_lock:
        history.clear()


def quit_app(icon, item):
    icon.stop()
    # daemon threads will exit with the process
    import os
    os._exit(0)


def run_tray():
    icon = pystray.Icon(
        "clipboard_typer",
        _make_icon_image(),
        "Clipboard Typer",
        menu=pystray.Menu(
            pystray.MenuItem("Open history manager (Win+Alt+V)", lambda icon, item: open_manager()),
            pystray.MenuItem("Type most recent (Ctrl+Alt+V)", lambda icon, item: quick_type_latest()),
            pystray.MenuItem(
                "Monitoring enabled",
                toggle_monitoring,
                checked=lambda item: monitoring_enabled,
            ),
            pystray.MenuItem("Clear history", clear_history),
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    _enable_dpi_awareness()

    threading.Thread(target=monitor_clipboard, daemon=True).start()

    _register_hotkeys()

    run_tray()  # blocks until Quit is chosen


if __name__ == "__main__":
    main()
