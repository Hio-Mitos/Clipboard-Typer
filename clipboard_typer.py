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
Each character is sent as a real virtual-key press whenever the active
keyboard layout supports it (falling back to Unicode injection only when it
doesn't), so typing also works into a Remote Desktop / Windows App session,
not just locally - see the README for details and caveats.

Compatible with Windows 7, 8, 8.1, 10, 11 and later (see README for the
Python version to use on each).

Requirements (Windows only):
    pip install pyperclip pywin32 pystray pillow

Run:
    pythonw.exe clipboard_typer.py      (no console window)
    python.exe clipboard_typer.py       (with console, useful for debugging)
"""

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import os
import subprocess
import sys
import threading
import time
import traceback
import winreg
from collections import deque

import pyperclip
import win32api
import win32con
import win32event
import win32gui
import win32process
import winerror
import pystray
from PIL import Image, ImageDraw
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_TITLE = "Clipboard Typer"
HISTORY_MAXLEN = 50              # keep last 50 copied text items, in memory only
POLL_INTERVAL = 0.4              # seconds between clipboard checks

# Global hotkeys, registered with the native Win32 RegisterHotKey API (see
# the "Global hotkeys" section below) rather than a third-party low-level
# keyboard hook. RegisterHotKey is the OS-sanctioned mechanism for exactly
# this purpose - it reliably delivers WM_HOTKEY regardless of the Windows
# key being involved, and isn't subject to being silently filtered by
# security/endpoint software the way a raw global hook can be (this is what
# caused Win+Alt+V to be reported as "unusable" during Store certification -
# it worked on our own dev machine but not on Microsoft's locked-down test
# device).
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_V_KEY = 0x56  # 'V'

HOTKEY_ID_MANAGER = 1
HOTKEY_ID_QUICK_TYPE = 2

# Factory-default shortcuts. Users can personalize both from the tray menu's
# "Customize shortcuts..." dialog - see manager_hotkey_mods/vk and
# quick_type_hotkey_mods/vk in Shared state below for the live values.
DEFAULT_MANAGER_HOTKEY_MODS = MOD_WIN | MOD_ALT
DEFAULT_MANAGER_HOTKEY_VK = VK_V_KEY
DEFAULT_QUICK_TYPE_HOTKEY_MODS = MOD_CONTROL | MOD_ALT
DEFAULT_QUICK_TYPE_HOTKEY_VK = VK_V_KEY

CHAR_DELAY = 0.014                # delay after each keystroke, before the next one
KEY_PRESS_GAP = 0.004             # delay between a key's down and its up event
NEWLINE_DELAY = 0.022
BREATHER_EVERY = 40               # extra pause every N characters, lets the
BREATHER_DELAY = 0.05             # target app's input queue catch its breath
MANAGER_INACTIVITY_MS = 20_000    # auto-close the history flyout after this much idle time

# Single-instance guard. "Global\" (not "Local\") so the check also holds
# across different user sessions / RDP sessions on the same machine, not
# just within the current login.
SINGLE_INSTANCE_MUTEX_NAME = r"Global\ClipboardTyperSingleInstanceMutex"

# Persisted settings (per-user, survive restarts) live under this registry
# key - just two small DWORD flags, nothing sensitive.
SETTINGS_REG_PATH = r"Software\ClipboardTyper"
STARTUP_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_RUN_VALUE_NAME = "ClipboardTyper"
STARTUP_TASK_ID = "ClipboardTyperStartupTask"  # must match AppxManifest.xml's <desktop:StartupTask TaskId=...>

# "Always Running" = auto-restart the app if it crashes. To avoid spinning
# forever on a crash that happens instantly every time (a real, unfixable
# bug), we cap consecutive *fast* restarts and pass the count to the child
# via an environment variable; a child that stays up longer than the reset
# window below is considered "recovered" and clears the counter for any
# future crash.
CRASH_RESTART_ENV_VAR = "CLIPBOARD_TYPER_RESTART_COUNT"
MAX_FAST_CRASH_RESTARTS = 5
CRASH_RESTART_RESET_AFTER_SECONDS = 30

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
history = deque(maxlen=HISTORY_MAXLEN)
history_lock = threading.Lock()
last_seen_value = None
monitoring_enabled = True
_manager_open = False
_hotkey_thread_id = None  # native thread ID of the hotkey listener, for clean shutdown
_hotkey_thread = None

# Live, user-personalizable shortcut bindings. Loaded from the registry in
# _load_persisted_settings(); changed via the tray menu's "Customize
# shortcuts..." dialog (open_hotkey_settings), which also persists them and
# restarts the hotkey listener with the new values.
manager_hotkey_mods = DEFAULT_MANAGER_HOTKEY_MODS
manager_hotkey_vk = DEFAULT_MANAGER_HOTKEY_VK
quick_type_hotkey_mods = DEFAULT_QUICK_TYPE_HOTKEY_MODS
quick_type_hotkey_vk = DEFAULT_QUICK_TYPE_HOTKEY_VK

_instance_mutex_handle = None
always_running_enabled = True
run_at_startup_enabled = False
esc_cancels_typing_enabled = True  # optional: Esc stops an in-progress typing burst
_crash_restart_recovered = False  # flips true after CRASH_RESTART_RESET_AFTER_SECONDS of uptime


# ---------------------------------------------------------------------------
# Crash reporting
#
# This app has no console window (run via pythonw.exe), so an unhandled
# exception would normally just kill a thread - or the whole process -
# completely silently: the tray icon would vanish and the user would have
# no idea why. These hooks make sure any crash that isn't the user choosing
# to Quit pops up a plain Windows message box explaining what happened.
# ---------------------------------------------------------------------------
def _show_error_box(title, message):
    try:
        # MB_OK | MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000010 | 0x00040000 | 0x00010000)
    except Exception:
        pass


def _show_confirm_box(title, message):
    """Yes/No prompt using the same plain ctypes MessageBoxW as
    _show_error_box (rather than tkinter's messagebox), so it's safe to call
    from any thread - including the pystray tray-icon callback thread, which
    has no Tk mainloop running on it."""
    try:
        MB_YESNO = 0x00000004
        MB_ICONQUESTION = 0x00000020
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        IDYES = 6
        result = ctypes.windll.user32.MessageBoxW(
            0, message, title, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND
        )
        return result == IDYES
    except Exception:
        return False


def _open_windows_startup_settings():
    try:
        os.startfile("ms-settings:startupapps")
    except Exception:
        pass


def _format_exc(exc_type, exc_value, exc_tb, limit_chars=1200):
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    if len(text) > limit_chars:
        text = "...\n" + text[-limit_chars:]
    return text


def _thread_crash_handler(args):
    """Installed as threading.excepthook: catches crashes in any background
    thread (clipboard monitor, typing bursts, the hotkey listener, ...)."""
    details = _format_exc(args.exc_type, args.exc_value, args.exc_traceback)
    _show_error_box(
        f"{APP_TITLE} - background task crashed",
        "A background task in Clipboard Typer stopped unexpectedly because of "
        "an internal error (not something you did).\n\n"
        f"Task: {args.thread.name}\n"
        "The app is still running, but this feature may not work until you "
        "restart Clipboard Typer.\n\n"
        f"Details:\n{details}",
    )


def _main_crash_handler(exc_type, exc_value, exc_tb):
    """Installed as sys.excepthook: catches any crash that escapes main()
    itself (main thread), e.g. a bug during startup or in the tray icon's
    own event loop.

    If "Always Running" is turned on (see the tray menu), this also tries to
    relaunch a fresh copy of the app before exiting, instead of just leaving
    it dead - see _attempt_crash_restart() below for the crash-loop guard."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    details = _format_exc(exc_type, exc_value, exc_tb)

    restarted = _attempt_crash_restart()

    if restarted:
        _show_error_box(
            f"{APP_TITLE} - restarting after a crash",
            "Clipboard Typer hit an internal error and stopped, but "
            "'Always Running' is turned on, so it is relaunching itself "
            "automatically now.\n\n"
            f"Details:\n{details}",
        )
    else:
        extra = (
            ""
            if not always_running_enabled
            else (
                "\n\n'Always Running' is on, but the app crashed too many times in a "
                "row, so automatic restarting has been paused for this session to "
                "avoid a crash loop - please check the details below."
            )
        )
        _show_error_box(
            f"{APP_TITLE} - stopped unexpectedly",
            "Clipboard Typer has crashed and is no longer running in the "
            "background, because of an internal error - not something you did.\n\n"
            "You'll need to start it again (from its shortcut, or by re-running "
            "clipboard_typer.py) to get the shortcuts working again."
            f"{extra}\n\n"
            f"Details:\n{details}",
        )
    os._exit(1)


threading.excepthook = _thread_crash_handler
sys.excepthook = _main_crash_handler


# ---------------------------------------------------------------------------
# Persisted settings (registry) - "Always Running", "Run at startup", and the
# two customizable shortcut bindings are all small DWORD values under HKCU;
# no need for a config file.
# ---------------------------------------------------------------------------
def _get_setting(name, default):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_PATH) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return bool(value)
    except OSError:
        return default


def _set_setting(name, value):
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_PATH)
        with key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1 if value else 0)
    except OSError:
        pass


def _get_reg_int(name, default):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_PATH) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return int(value)
    except OSError:
        return default


def _set_reg_int(name, value):
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_PATH)
        with key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
    except OSError:
        pass


def _load_persisted_settings():
    global always_running_enabled, run_at_startup_enabled, esc_cancels_typing_enabled
    global manager_hotkey_mods, manager_hotkey_vk, quick_type_hotkey_mods, quick_type_hotkey_vk
    always_running_enabled = _get_setting("AlwaysRunning", True)
    run_at_startup_enabled = _startup_shortcut_exists()
    esc_cancels_typing_enabled = _get_setting("EscCancelsTyping", True)
    manager_hotkey_mods = _get_reg_int("ManagerHotkeyMods", DEFAULT_MANAGER_HOTKEY_MODS)
    manager_hotkey_vk = _get_reg_int("ManagerHotkeyVk", DEFAULT_MANAGER_HOTKEY_VK)
    quick_type_hotkey_mods = _get_reg_int("QuickTypeHotkeyMods", DEFAULT_QUICK_TYPE_HOTKEY_MODS)
    quick_type_hotkey_vk = _get_reg_int("QuickTypeHotkeyVk", DEFAULT_QUICK_TYPE_HOTKEY_VK)


# ---------------------------------------------------------------------------
# Run at startup
#
# Packaged (MSIX/Desktop Bridge - i.e. the Microsoft Store install) and
# unpackaged (plain clipboard_typer.py / standalone EXE) builds need two
# completely different mechanisms here:
#   - Unpackaged: the classic HKCU ...\CurrentVersion\Run registry key,
#     which is what a normal Win32 app has always used.
#   - Packaged: Windows silently virtualizes/ignores writes to that same
#     Run key for MSIX apps, so an entry there is never actually launched
#     at logon even though the registry write itself succeeds with no
#     error - this was reported as "Run at startup doesn't actually start
#     the app after a reboot" and is exactly why. The OS-sanctioned
#     replacement is the windows.startupTask extension declared in
#     AppxManifest.xml, toggled at runtime via the WinRT
#     Windows.ApplicationModel.StartupTask API (through the `winsdk`
#     package) - this is also what makes the toggle show up under
#     Settings > Apps > Startup, same as any other Store app.
# ---------------------------------------------------------------------------
ctypes.windll.kernel32.GetCurrentPackageFullName.argtypes = [
    ctypes.POINTER(wintypes.UINT), ctypes.c_wchar_p
]
ctypes.windll.kernel32.GetCurrentPackageFullName.restype = ctypes.c_long
_APPMODEL_ERROR_NO_PACKAGE = 15700
_ERROR_INSUFFICIENT_BUFFER = 122


def _get_current_package_full_name():
    """This process's MSIX package full name, or None if running
    unpackaged (plain script / standalone EXE rather than the
    Store-installed package)."""
    try:
        length = wintypes.UINT(0)
        result = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        if result == _APPMODEL_ERROR_NO_PACKAGE:
            return None
        if result != _ERROR_INSUFFICIENT_BUFFER or length.value == 0:
            return None
        buf = ctypes.create_unicode_buffer(length.value)
        result = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), buf)
        return buf.value if result == 0 else None
    except Exception:
        return None


_IS_PACKAGED_APP = _get_current_package_full_name() is not None


def _self_launch_command():
    """The exact command line that relaunches this app, whether it's running
    as a frozen PyInstaller .exe or as a plain .py script. Only used for the
    unpackaged (classic Run key) path - the packaged path doesn't need this,
    since Windows already knows how to relaunch the app from the StartupTask
    declaration in AppxManifest.xml."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script_path = os.path.abspath(__file__)
    # Prefer pythonw.exe (no console window) alongside the current
    # interpreter, falling back to whatever interpreter is currently running.
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    interpreter = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{interpreter}" "{script_path}"'


_startup_task_last_error = None  # human-readable text from the most recent
                                  # StartupTask call, if it failed - surfaced
                                  # in the error box instead of a generic
                                  # "couldn't change it" message with no detail.


def _run_winrt_async(awaitable):
    """winsdk's WinRT async calls (GetAsync, RequestEnableAsync, ...) return
    an IAsyncOperation - it's *awaitable* (has __await__), but it isn't a
    Python coroutine object, and asyncio.run() specifically requires the
    latter ("a coroutine was expected"). Wrapping the await in a real
    async def first gives asyncio.run() an actual coroutine to drive, which
    then awaits the WinRT object correctly underneath."""
    async def _runner():
        return await awaitable

    return asyncio.run(_runner())


def _startup_task_state():
    """Current WinRT StartupTaskState for the packaged app, or None if
    unpackaged or if the query fails for any reason."""
    global _startup_task_last_error
    if not _IS_PACKAGED_APP:
        return None
    try:
        from winsdk.windows.applicationmodel import StartupTask

        task = _run_winrt_async(StartupTask.get_async(STARTUP_TASK_ID))
        _startup_task_last_error = None
        return task.state
    except Exception as exc:
        _startup_task_last_error = f"{type(exc).__name__}: {exc}"
        return None


def _startup_task_request_enable():
    """Ask Windows to enable the startup task. Returns the resulting state,
    or None on failure (see _startup_task_last_error for why). The first
    time this is called, Windows may show its own brief system prompt;
    after a user disables it from Settings, this call can no longer
    silently re-enable it (see DISABLED_BY_USER handling in
    _set_run_at_startup)."""
    global _startup_task_last_error
    if not _IS_PACKAGED_APP:
        return None
    try:
        from winsdk.windows.applicationmodel import StartupTask

        task = _run_winrt_async(StartupTask.get_async(STARTUP_TASK_ID))
        result = _run_winrt_async(task.request_enable_async())
        _startup_task_last_error = None
        return result
    except Exception as exc:
        _startup_task_last_error = f"{type(exc).__name__}: {exc}"
        return None


def _startup_task_disable():
    global _startup_task_last_error
    if not _IS_PACKAGED_APP:
        return
    try:
        from winsdk.windows.applicationmodel import StartupTask

        task = _run_winrt_async(StartupTask.get_async(STARTUP_TASK_ID))
        task.disable()
        _startup_task_last_error = None
    except Exception as exc:
        _startup_task_last_error = f"{type(exc).__name__}: {exc}"


def _startup_menu_label():
    """Tray menu text for the "Run at startup" item. Usually just the plain
    label, but if a packaged install's startup task has been blocked from
    outside the app (Task Manager's Startup apps tab, Settings, or an org
    policy), say so right in the menu rather than leaving the checkbox
    quietly unchecked with no explanation."""
    if _IS_PACKAGED_APP:
        try:
            from winsdk.windows.applicationmodel import StartupTaskState

            state = _startup_task_state()
            if state == StartupTaskState.DISABLED_BY_USER:
                return "Run at startup (blocked in Windows Settings)"
            if state == StartupTaskState.DISABLED_BY_POLICY:
                return "Run at startup (blocked by policy)"
        except Exception:
            pass
    return "Run at startup"


def _startup_shortcut_exists():
    if _IS_PACKAGED_APP:
        try:
            from winsdk.windows.applicationmodel import StartupTaskState

            state = _startup_task_state()
            return state in (StartupTaskState.ENABLED, StartupTaskState.ENABLED_BY_POLICY)
        except Exception:
            return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, STARTUP_RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def _set_run_at_startup(enabled):
    global run_at_startup_enabled

    if _IS_PACKAGED_APP:
        try:
            from winsdk.windows.applicationmodel import StartupTaskState
        except Exception as exc:
            _show_error_box(
                f"{APP_TITLE} - startup toggle unavailable",
                f"Couldn't access Windows' startup task API in this build.\n\nDetails: {exc}",
            )
            return
        if enabled:
            new_state = _startup_task_request_enable()
            if new_state == StartupTaskState.DISABLED_BY_USER:
                # Someone turned this off from Task Manager's "Startup apps"
                # tab or from Settings directly - Windows treats that as an
                # explicit user decision, and RequestEnableAsync can't
                # silently override it from here. Offer to jump straight to
                # the Settings page instead of just describing where it is.
                open_it = _show_confirm_box(
                    f"{APP_TITLE} - can't enable automatically",
                    "Windows shows this app's startup entry as turned off at "
                    "the system level (from Task Manager or Settings), so it "
                    "can't be re-enabled from here.\n\n"
                    "Open Windows Settings' Startup Apps page now to turn it "
                    "back on?",
                )
                if open_it:
                    _open_windows_startup_settings()
                run_at_startup_enabled = False
                return
            if new_state == StartupTaskState.DISABLED_BY_POLICY:
                _show_error_box(
                    f"{APP_TITLE} - blocked by policy",
                    "Your organization's Windows policy prevents apps from "
                    "running at startup. Contact your administrator if you "
                    "need this enabled.",
                )
                run_at_startup_enabled = False
                return
            if new_state is None:
                detail = _startup_task_last_error or "No further details were reported."
                _show_error_box(
                    f"{APP_TITLE} - couldn't update startup setting",
                    "Could not change the 'Run at startup' setting.\n\n"
                    f"Details: {detail}",
                )
                run_at_startup_enabled = False
                return
            run_at_startup_enabled = new_state in (
                StartupTaskState.ENABLED,
                StartupTaskState.ENABLED_BY_POLICY,
            )
        else:
            _startup_task_disable()
            run_at_startup_enabled = False
        return

    # Unpackaged (plain script / standalone EXE) build: classic Run key.
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY_PATH)
        with key:
            if enabled:
                winreg.SetValueEx(key, STARTUP_RUN_VALUE_NAME, 0, winreg.REG_SZ, _self_launch_command())
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        run_at_startup_enabled = enabled
    except OSError as exc:
        _show_error_box(
            f"{APP_TITLE} - couldn't update startup setting",
            f"Could not change the 'Run at startup' setting.\n\nDetails: {exc}",
        )


def toggle_run_at_startup(icon, item):
    _set_run_at_startup(not run_at_startup_enabled)


# ---------------------------------------------------------------------------
# Elevation ("type into admin-elevated app credential boxes")
#
# A standard-privilege process cannot send synthetic input into a
# higher-integrity (elevated / "Run as administrator") window - Windows'
# User Interface Privilege Isolation (UIPI) blocks that by design, the same
# protection that stops a low-privilege app from tampering with an admin
# one. Running Clipboard Typer itself elevated removes that barrier for
# ordinary elevated app windows (e.g. a login/credential box inside an
# elevated app, an elevated installer, an admin console).
#
# Important limit: this does NOT reach the true Secure Desktop UAC consent
# prompt itself (the "Do you want to allow this app to make changes"
# dialog, or its credential-entry variant) - that runs on a separate,
# isolated desktop that no ordinary application, elevated or not, is
# allowed to inject input into. That boundary is intentional and can't be
# bypassed by this app; see the README for the full explanation.
# ---------------------------------------------------------------------------
def _is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _release_instance_mutex():
    global _instance_mutex_handle
    if _instance_mutex_handle is not None:
        try:
            win32event.ReleaseMutex(_instance_mutex_handle)
        except Exception:
            pass
        try:
            win32api.CloseHandle(_instance_mutex_handle)
        except Exception:
            pass
        _instance_mutex_handle = None


def relaunch_elevated(icon=None, item=None):
    """Relaunch this app with an elevation (UAC) prompt, so it can type into
    other elevated apps' windows. Exits the current, non-elevated instance
    once the elevated one is confirmed launching."""
    if _is_elevated():
        _show_error_box(APP_TITLE, "Clipboard Typer is already running as Administrator.")
        return
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            script_path = os.path.abspath(__file__)
            exe, params = sys.executable, f'"{script_path}"'
        # ShellExecuteW with the "runas" verb is what actually triggers the
        # UAC consent prompt - a plain CreateProcess/subprocess call cannot
        # elevate a process on its own.
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        if result <= 32:
            raise OSError(f"ShellExecuteW returned {result}")
    except Exception as exc:
        _show_error_box(
            f"{APP_TITLE} - couldn't restart as Administrator",
            "Restarting elevated was cancelled or failed (e.g. you clicked 'No' "
            f"on the UAC prompt).\n\nDetails: {exc}",
        )
        return
    # Release our slot in the single-instance mutex *before* exiting, so the
    # elevated copy that's about to start doesn't see itself as a duplicate.
    _release_instance_mutex()
    os._exit(0)


# ---------------------------------------------------------------------------
# Crash-loop guard for "Always Running"
# ---------------------------------------------------------------------------
def _mark_crash_restart_recovered():
    global _crash_restart_recovered
    _crash_restart_recovered = True


def _attempt_crash_restart():
    """Relaunch a fresh copy of the app after an unhandled crash, if 'Always
    Running' is enabled. Returns True if a restart was actually launched."""
    if not always_running_enabled:
        return False

    prev_count = 0 if _crash_restart_recovered else int(os.environ.get(CRASH_RESTART_ENV_VAR, "0"))
    if prev_count >= MAX_FAST_CRASH_RESTARTS:
        return False

    try:
        env = dict(os.environ)
        env[CRASH_RESTART_ENV_VAR] = str(prev_count + 1)
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable], env=env, close_fds=True)
        else:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env, close_fds=True)
        _release_instance_mutex()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Low level keystroke simulation (ctypes / SendInput)
#
# We build raw Windows SendInput events ourselves instead of relying on a
# library's text-typing helper, because:
#   - We can map characters to real virtual-key presses when possible (see
#     _char_to_vk below), which is what remote sessions actually forward.
#   - KEYEVENTF_UNICODE lets us send the *exact* character (accents, emoji,
#     non-Latin scripts, ...) regardless of keyboard layout, as a fallback
#     for characters that aren't reachable via the current layout.
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
VK_ESCAPE = 0x1B

# ctypes.windll.user32.GetAsyncKeyState is used both here (to let Esc cancel
# an in-progress typing burst - see type_text) and later by the shortcut
# customization dialog. Declared once, up front, so both call sites share
# the same signature.
ctypes.windll.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
ctypes.windll.user32.GetAsyncKeyState.restype = ctypes.c_short

# VkKeyScanW(ch) -> a real virtual-key + shift-state for the current
# keyboard layout, when that character can be typed at all with the active
# layout. Declaring argtypes/restype explicitly because ctypes would
# otherwise treat the WCHAR parameter as a pointer, not a value.
ctypes.windll.user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
ctypes.windll.user32.VkKeyScanW.restype = ctypes.c_short

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


def _char_to_vk(ch):
    """Map a character to (virtual_key, shift, ctrl, alt) on the active
    keyboard layout, if it can be typed that way at all.

    This matters for Remote Desktop / Windows App / VMs / Citrix-style
    sessions: those forward *real* virtual-key presses (the same thing a
    physical keyboard produces) over their keyboard channel, but they
    generally don't forward KEYEVENTF_UNICODE synthetic characters at all,
    since those have no underlying hardware scancode. A VK-based keystroke
    behaves like real typing and crosses that boundary correctly; a
    Unicode-injected one is a local-only trick that silently gets dropped
    once you're inside a remote session.

    Returns None if the character isn't reachable on the current layout
    (most emoji, many non-Latin scripts) - those fall back to Unicode
    injection, which still works for local typing, just not over RDP.

    Tab is deliberately excluded even though VK_TAB exists: a *real* Tab
    keypress moves focus to the next field in most apps, which would break
    the paste rather than insert a tab character - it's always sent as a
    plain Unicode character instead, same as before.
    """
    if ch == "\t":
        return None
    try:
        res = ctypes.windll.user32.VkKeyScanW(ch)
    except (TypeError, ValueError):
        return None
    if res == -1 or (res & 0xFF) == 0xFF:
        return None
    vk = res & 0xFF
    mod = (res >> 8) & 0xFF
    return vk, bool(mod & 1), bool(mod & 2), bool(mod & 4)  # vk, shift, ctrl, alt


def _type_char(ch):
    # Down and up are sent as two separate, ordered SendInput calls with a
    # small real gap between them: a zero-duration keypress is what a lot of
    # text widgets treat as unreliable/ignorable, which is how a character
    # goes missing while a neighbouring one gets duplicated in its place.
    # This function returns only after both events for this character have
    # been handed to the OS input queue in order, so the caller never starts
    # the next character until this one is fully done.
    mapped = _char_to_vk(ch)
    if mapped is not None:
        vk, shift, ctrl, alt = mapped
        mod_downs = []
        mod_ups = []
        for needed, vk_mod in ((shift, VK_SHIFT), (ctrl, VK_CONTROL), (alt, VK_MENU)):
            if needed:
                mod_downs.append(_vk_event(vk_mod))
                mod_ups.insert(0, _vk_event(vk_mod, keyup=True))
        _send(*mod_downs, _vk_event(vk))
        time.sleep(KEY_PRESS_GAP)
        _send(_vk_event(vk, keyup=True), *mod_ups)
    else:
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

    # Note: earlier versions of this app paused/resumed a third-party global
    # keyboard hook here, because that hook ran a Python callback for every
    # synthetic keystroke below and the resulting GIL contention could cause
    # a character to repeat while its neighbour dropped. Now that hotkeys are
    # registered with the native RegisterHotKey API instead (see "Global
    # hotkeys" below), WM_HOTKEY only ever fires for the exact registered key
    # combination - never for the individual characters typed here - so that
    # contention no longer exists and nothing needs to be paused.
    _release_modifiers()
    char_count = 0
    for i, line in enumerate(lines):
        cancelled = False
        for ch in line:
            # Optional: if the user is holding Esc, stop the burst right
            # here instead of continuing to type the rest of the text.
            # Checked before every character (not just periodically) so a
            # long paste can be interrupted almost the instant Esc is
            # pressed, not just at the next breather pause.
            if esc_cancels_typing_enabled and (ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000):
                cancelled = True
                break
            _type_char(ch)
            char_count += 1
            if char_count % BREATHER_EVERY == 0:
                time.sleep(BREATHER_DELAY)
        if cancelled:
            break
        if i < len(lines) - 1:
            _send_shift_enter()


# ---------------------------------------------------------------------------
# Global hotkeys
#
# Registered with the native Win32 RegisterHotKey() API instead of a
# third-party low-level keyboard hook. This matters for two reasons:
#   - RegisterHotKey is the OS's own supported mechanism for exactly this
#     use case, and reliably fires WM_HOTKEY even for Windows-key
#     combinations - a raw global hook can be delayed, filtered, or blocked
#     by security/endpoint software, which is what made Win+Alt+V work on
#     our own dev machine but get reported as "unusable" by the Microsoft
#     Store certification test device.
#   - It only ever delivers a message for the *exact* registered combo, so
#     it doesn't see (and can't be confused by) the hundreds of individual
#     synthetic keystrokes type_text() sends during a typing burst.
#
# RegisterHotKey with hWnd=None posts WM_HOTKEY to the calling *thread's*
# message queue rather than to a window, so a small dedicated thread just
# registers both hotkeys and runs a standard GetMessage/DispatchMessage loop
# for the lifetime of the app.
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
# GetAsyncKeyState's signature is already declared near the top of the file
# (ctypes.windll.user32 is the same cached DLL object as this module-level
# `user32`), reused here for the shortcut-recording dialog below.
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetKeyNameTextW.argtypes = [ctypes.c_long, wintypes.LPWSTR, ctypes.c_int]
user32.GetKeyNameTextW.restype = ctypes.c_int
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def _vk_display_name(vk):
    """Human-readable name for a virtual-key code, e.g. 0x56 -> 'V',
    0x70 -> 'F1' - used to show the current shortcuts in the tray menu and
    the customization dialog."""
    scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
    if scan:
        buf = ctypes.create_unicode_buffer(32)
        if user32.GetKeyNameTextW(scan << 16, buf, 32) > 0 and buf.value:
            return buf.value
    return f"Key 0x{vk:02X}"


def _hotkey_display(mods, vk):
    parts = []
    if mods & MOD_WIN:
        parts.append("Win")
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    parts.append(_vk_display_name(vk))
    return "+".join(parts)


def _hotkey_listener_loop():
    global _hotkey_thread_id
    _hotkey_thread_id = win32api.GetCurrentThreadId()

    ok_manager = user32.RegisterHotKey(
        None, HOTKEY_ID_MANAGER, manager_hotkey_mods | MOD_NOREPEAT, manager_hotkey_vk
    )
    ok_quick = user32.RegisterHotKey(
        None, HOTKEY_ID_QUICK_TYPE, quick_type_hotkey_mods | MOD_NOREPEAT, quick_type_hotkey_vk
    )
    if not ok_manager or not ok_quick:
        failed = []
        if not ok_manager:
            failed.append(_hotkey_display(manager_hotkey_mods, manager_hotkey_vk))
        if not ok_quick:
            failed.append(_hotkey_display(quick_type_hotkey_mods, quick_type_hotkey_vk))
        _show_error_box(
            f"{APP_TITLE} - shortcut already in use",
            "Couldn't register the following shortcut(s), because another "
            "running app has already claimed them:\n\n"
            + "\n".join(failed)
            + "\n\nClose the other app (or change its shortcut), or pick a "
            "different combination from the tray menu's 'Customize "
            "shortcuts...' dialog.",
        )

    msg = wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret <= 0:
            break
        if msg.message == WM_HOTKEY:
            if msg.wParam == HOTKEY_ID_MANAGER:
                open_manager()
            elif msg.wParam == HOTKEY_ID_QUICK_TYPE:
                quick_type_latest()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    if ok_manager:
        user32.UnregisterHotKey(None, HOTKEY_ID_MANAGER)
    if ok_quick:
        user32.UnregisterHotKey(None, HOTKEY_ID_QUICK_TYPE)


def _start_hotkey_listener():
    global _hotkey_thread
    _hotkey_thread = threading.Thread(target=_hotkey_listener_loop, daemon=True, name="HotkeyListener")
    _hotkey_thread.start()


def _stop_hotkey_listener():
    """Ask the listener thread's message loop to exit and wait for it - used
    when the user changes their shortcuts, so the old bindings are released
    before the new ones are registered."""
    global _hotkey_thread, _hotkey_thread_id
    if _hotkey_thread_id is not None:
        user32.PostThreadMessageW(_hotkey_thread_id, WM_QUIT, 0, 0)
    if _hotkey_thread is not None:
        _hotkey_thread.join(timeout=2)
    _hotkey_thread = None
    _hotkey_thread_id = None


def restart_hotkey_listener():
    _stop_hotkey_listener()
    _start_hotkey_listener()


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


def _send_ctrl_v():
    _send(_vk_event(VK_CONTROL), _vk_event(VK_V_KEY))
    time.sleep(KEY_PRESS_GAP)
    _send(_vk_event(VK_V_KEY, keyup=True), _vk_event(VK_CONTROL, keyup=True))


def _paste_directly(hwnd, text):
    _restore_foreground(hwnd)
    time.sleep(0.03)
    try:
        pyperclip.copy(text)
        _send_ctrl_v()
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
# Win+V panel): no title bar, rounded corners, appears next to the
# mouse/caret, single click (or Enter) commits the item immediately, and it
# closes itself the instant it loses focus so it never lingers on screen.
# ---------------------------------------------------------------------------
POPUP_WIDTH = 380
POPUP_MAX_HEIGHT = 460
ROW_HEIGHT = 40
HEADER_HEIGHT = 40
FOOTER_HEIGHT = 34
CORNER_RADIUS = 12

ACCENT = "#2F6FED"          # modern blue accent (header icon, selection, top strip)
BORDER_COLOR = "#E3E6EC"    # soft neutral frame around the whole flyout
BG_COLOR = "#FFFFFF"
HEADER_BG = "#F7F9FC"
TEXT_PRIMARY = "#1F2430"
TEXT_SECONDARY = "#8890A0"
ROW_HOVER = "#F3F6FD"
ROW_SELECTED = "#E8EFFE"
CHIP_BG = "#EEF1F6"
CHIP_FG = "#5B6472"
SCROLLBAR_THUMB = "#D2D6DE"


def _apply_rounded_corners(root, width, height, radius=CORNER_RADIUS):
    """Clip the (overrideredirect) window to a rounded rectangle so it reads
    as a modern flyout instead of a hard-edged box. Works the same way on
    Windows 7 through 11, since it doesn't depend on DWM auto-rounding
    (which only some Windows 11 top-level windows get for free)."""
    try:
        hwnd = int(root.winfo_id())
        region = win32gui.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
        win32gui.SetWindowRgn(hwnd, region, True)
    except Exception:
        pass


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

        def _tk_callback_exception(exc_type, exc_value, exc_tb):
            # Errors inside Tk event callbacks (a click handler, a key
            # binding, ...) don't normally propagate anywhere - Tkinter just
            # prints them to stderr, which is invisible with no console.
            # Route them through the same crash notification instead.
            _thread_crash_handler(
                threading.ExceptHookArgs(exc_type, exc_value, exc_tb, threading.current_thread())
            )

        root.report_callback_exception = _tk_callback_exception

        root.withdraw()  # position/size it before showing, to avoid a visible jump
        root.overrideredirect(True)   # no title bar / native borders -> flyout look
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.99)
        except tk.TclError:
            pass
        root.configure(bg=BORDER_COLOR)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Flyout.Vertical.TScrollbar",
            background=SCROLLBAR_THUMB,
            troughcolor=BG_COLOR,
            bordercolor=BG_COLOR,
            lightcolor=SCROLLBAR_THUMB,
            darkcolor=SCROLLBAR_THUMB,
            arrowsize=0,
            gripcount=0,
            width=7,
        )

        # 1px soft border, then the actual card content on top
        card = tk.Frame(root, bg=BG_COLOR)
        card.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # thin accent strip along the top edge
        top_strip = tk.Frame(card, bg=ACCENT, height=3)
        top_strip.pack(fill=tk.X, side=tk.TOP)

        # --- header: icon + title + item count ---------------------------
        # Also doubles as the drag handle, since overrideredirect windows
        # have no title bar to grab - see the drag bindings just below.
        header = tk.Frame(card, bg=HEADER_BG, cursor="fleur")
        header.pack(fill=tk.X, side=tk.TOP)

        header_inner = tk.Frame(header, bg=HEADER_BG, cursor="fleur")
        header_inner.pack(fill=tk.X, padx=12, pady=8)

        icon_canvas = tk.Canvas(
            header_inner, width=16, height=16, bg=HEADER_BG, highlightthickness=0
        )
        icon_canvas.pack(side=tk.LEFT, padx=(0, 8))
        icon_canvas.create_rectangle(2, 1, 14, 15, outline=ACCENT, width=2, fill=BG_COLOR)
        icon_canvas.create_rectangle(5, 0, 11, 3, outline=ACCENT, width=1, fill=ACCENT)
        icon_canvas.create_line(4, 6, 12, 6, fill=ACCENT, width=1)
        icon_canvas.create_line(4, 9, 12, 9, fill=ACCENT, width=1)
        icon_canvas.create_line(4, 12, 9, 12, fill=ACCENT, width=1)

        title_label = tk.Label(
            header_inner,
            text="Clipboard History",
            bg=HEADER_BG,
            fg=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 10),
            cursor="fleur",
        )
        title_label.pack(side=tk.LEFT)

        count_text = f"{len(items)} item" + ("" if len(items) == 1 else "s")
        count_label = tk.Label(
            header_inner,
            text=count_text,
            bg=HEADER_BG,
            fg=TEXT_SECONDARY,
            font=("Segoe UI", 8),
            cursor="fleur",
        )
        count_label.pack(side=tk.RIGHT)

        # --- drag-to-move: the header area (and the accent strip above it)
        # act as the title bar this overrideredirect window doesn't have.
        _drag = {"x": 0, "y": 0}

        def _drag_start(event):
            _drag["x"] = event.x_root - root.winfo_x()
            _drag["y"] = event.y_root - root.winfo_y()

        def _drag_move(event):
            root.geometry(f"+{event.x_root - _drag['x']}+{event.y_root - _drag['y']}")
            _reset_inactivity_timer()

        for widget in (top_strip, header, header_inner, title_label, count_label):
            widget.bind("<ButtonPress-1>", _drag_start)
            widget.bind("<B1-Motion>", _drag_move)

        tk.Frame(card, bg=BORDER_COLOR, height=1).pack(fill=tk.X, side=tk.TOP)

        # --- scrollable row list -------------------------------------------
        list_area = tk.Frame(card, bg=BG_COLOR)
        list_area.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        canvas = tk.Canvas(list_area, bg=BG_COLOR, highlightthickness=0)
        vscroll = ttk.Scrollbar(
            list_area, orient="vertical", command=canvas.yview, style="Flyout.Vertical.TScrollbar"
        )
        scroll_frame = tk.Frame(canvas, bg=BG_COLOR)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width)
        )

        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_wheel)

        state = {"index": 0, "rows": []}

        def _highlight():
            for i, row in enumerate(state["rows"]):
                selected = i == state["index"]
                bg = ROW_SELECTED if selected else BG_COLOR
                row.configure(bg=bg)
                row.accent_bar.configure(bg=ACCENT if selected else BG_COLOR)
                row.text_label.configure(bg=bg)
                row.meta_label.configure(bg=bg)

        def _ensure_visible(row):
            canvas.update_idletasks()
            bbox = canvas.bbox("all")
            if not bbox:
                return
            total_h = max(bbox[3], 1)
            view_h = canvas.winfo_height()
            top = row.winfo_y()
            bottom = top + row.winfo_height()
            first, last = canvas.yview()
            visible_top = first * total_h
            visible_bottom = last * total_h
            if top < visible_top:
                canvas.yview_moveto(top / total_h)
            elif bottom > visible_bottom:
                canvas.yview_moveto((bottom - view_h) / total_h)

        def set_selection(index):
            if not state["rows"]:
                return
            index = max(0, min(index, len(state["rows"]) - 1))
            state["index"] = index
            _highlight()
            _ensure_visible(state["rows"][index])

        def act(type_as_keystrokes):
            if not items or not state["rows"]:
                close()
                return
            text = items[state["index"]]
            close()
            if type_as_keystrokes:
                threading.Thread(target=_type_into, args=(origin_hwnd, text), daemon=True).start()
            else:
                threading.Thread(target=_paste_directly, args=(origin_hwnd, text), daemon=True).start()

        def build_row(entry, index):
            row = tk.Frame(scroll_frame, bg=BG_COLOR, height=ROW_HEIGHT)
            row.pack(fill=tk.X, side=tk.TOP)
            row.pack_propagate(False)

            accent_bar = tk.Frame(row, bg=BG_COLOR, width=3)
            accent_bar.pack(side=tk.LEFT, fill=tk.Y)

            text_body = tk.Frame(row, bg=BG_COLOR)
            text_body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 6), pady=4)

            single_line = entry.replace("\n", "  ").replace("\t", "  ")
            preview = single_line if len(single_line) <= 58 else single_line[:58] + "..."
            text_label = tk.Label(
                text_body,
                text=preview,
                bg=BG_COLOR,
                fg=TEXT_PRIMARY,
                font=("Segoe UI", 10),
                anchor="w",
                justify="left",
            )
            text_label.pack(fill=tk.X, anchor="w")

            line_count = entry.count("\n") + 1
            meta_bits = [f"{len(entry)} chars"]
            if line_count > 1:
                meta_bits.append(f"{line_count} lines")
            meta_label = tk.Label(
                text_body,
                text="  ·  ".join(meta_bits),
                bg=BG_COLOR,
                fg=TEXT_SECONDARY,
                font=("Segoe UI", 8),
                anchor="w",
            )
            meta_label.pack(fill=tk.X, anchor="w")

            row.accent_bar = accent_bar
            row.text_label = text_label
            row.meta_label = meta_label

            def on_enter(_e, i=index):
                if i != state["index"]:
                    row.configure(bg=ROW_HOVER)
                    text_label.configure(bg=ROW_HOVER)
                    meta_label.configure(bg=ROW_HOVER)

            def on_leave(_e, i=index):
                if i != state["index"]:
                    row.configure(bg=BG_COLOR)
                    text_label.configure(bg=BG_COLOR)
                    meta_label.configure(bg=BG_COLOR)

            def on_press(_e, i=index):
                set_selection(i)

            def on_release(_e, i=index):
                ctrl_held = bool(_e.state & 0x0004)
                set_selection(i)
                root.after(1, lambda: act(not ctrl_held))

            for widget in (row, text_body, text_label, meta_label):
                widget.bind("<Enter>", on_enter)
                widget.bind("<Leave>", on_leave)
                widget.bind("<ButtonPress-1>", on_press)
                widget.bind("<ButtonRelease-1>", on_release)

            return row

        if not items:
            empty = tk.Frame(scroll_frame, bg=BG_COLOR, height=ROW_HEIGHT * 2)
            empty.pack(fill=tk.X)
            empty.pack_propagate(False)
            tk.Label(
                empty,
                text="Clipboard history is empty",
                bg=BG_COLOR,
                fg=TEXT_SECONDARY,
                font=("Segoe UI", 9),
            ).pack(expand=True)
        else:
            for idx, entry in enumerate(items):
                state["rows"].append(build_row(entry, idx))
            set_selection(0)

        # --- footer: key hints as small chips -------------------------------
        tk.Frame(card, bg=BORDER_COLOR, height=1).pack(fill=tk.X, side=tk.TOP)
        footer = tk.Frame(card, bg=HEADER_BG)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        footer_inner = tk.Frame(footer, bg=HEADER_BG)
        footer_inner.pack(padx=10, pady=6)

        def chip(parent, key, label):
            group = tk.Frame(parent, bg=HEADER_BG)
            group.pack(side=tk.LEFT, padx=(0, 12))
            tk.Label(
                group, text=key, bg=CHIP_BG, fg=CHIP_FG, font=("Segoe UI", 7, "bold"), padx=5, pady=1
            ).pack(side=tk.LEFT)
            tk.Label(
                group, text=" " + label, bg=HEADER_BG, fg=TEXT_SECONDARY, font=("Segoe UI", 8)
            ).pack(side=tk.LEFT)

        chip(footer_inner, "Enter", "Type")
        chip(footer_inner, "Ctrl+Enter", "Paste")
        chip(footer_inner, "Esc", "Close")

        # --- size + position: appears right next to the mouse cursor,
        # clamped so it never runs off the edge of the screen ---
        visible_rows = min(max(len(items), 1), 8)
        content_height = max(visible_rows * ROW_HEIGHT, ROW_HEIGHT * 2)
        height = HEADER_HEIGHT + content_height + FOOTER_HEIGHT + 4
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
        inactivity = {"job": None}

        def close(event=None):
            if closed["done"]:
                return
            closed["done"] = True
            global _manager_open
            _manager_open = False
            if inactivity["job"] is not None:
                try:
                    root.after_cancel(inactivity["job"])
                except Exception:
                    pass
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            root.destroy()

        def _reset_inactivity_timer(event=None):
            # Closing here only ever destroys this popup window - the
            # clipboard monitor, hotkeys, and tray icon all keep running in
            # the background regardless of whether this window is open.
            if inactivity["job"] is not None:
                try:
                    root.after_cancel(inactivity["job"])
                except Exception:
                    pass
            inactivity["job"] = root.after(MANAGER_INACTIVITY_MS, close)

        root.bind("<Up>", lambda e: set_selection(state["index"] - 1))
        root.bind("<Down>", lambda e: set_selection(state["index"] + 1))
        root.bind("<Return>", lambda e: act(True))
        root.bind("<Control-Return>", lambda e: act(False))
        root.bind("<Escape>", close)
        # Auto-close as soon as the flyout loses focus, just like a native
        # popup (e.g. user clicks elsewhere or alt-tabs away).
        root.bind("<FocusOut>", lambda e: root.after(120, _close_if_unfocused))

        # Any mouse movement, click, key press, or scroll inside the window
        # counts as activity and pushes the 20s auto-close timer back out.
        root.bind_all("<Motion>", _reset_inactivity_timer)
        root.bind_all("<Button>", _reset_inactivity_timer)
        root.bind_all("<Key>", _reset_inactivity_timer)
        root.bind_all("<MouseWheel>", _reset_inactivity_timer, add="+")

        def _close_if_unfocused():
            try:
                if root.focus_get() is None:
                    close()
            except Exception:
                close()

        root.protocol("WM_DELETE_WINDOW", close)

        root.deiconify()
        _apply_rounded_corners(root, POPUP_WIDTH, height)
        root.lift()
        root.after(10, lambda: root.focus_force())
        _reset_inactivity_timer()

        root.mainloop()

    threading.Thread(target=build_ui, daemon=True).start()


# ---------------------------------------------------------------------------
# Shortcut customization dialog (tray menu -> "Customize shortcuts...")
#
# Lets the user personalize both global shortcuts. Recording a new
# combination is done by polling GetAsyncKeyState while this dialog has
# focus (not a global hook) - the user must hold at least one modifier
# (Ctrl/Alt/Shift/Win) and press a non-modifier key, or press Esc to cancel.
# Saving validates that the two shortcuts differ and that each one isn't
# already claimed by another running app (via a throwaway test
# RegisterHotKey/UnregisterHotKey call), persists the choice to the
# registry, and restarts the hotkey listener with the new bindings.
# ---------------------------------------------------------------------------
_RECORD_IGNORE_VKS = {
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # left/right-specific modifier VKs
    0x01, 0x02, 0x04, 0x05, 0x06,        # mouse buttons
    VK_ESCAPE,                            # reserved to cancel recording
}
_HOTKEY_TEST_ID = 0xF000


def open_hotkey_settings(icon=None, item=None):
    def build_ui():
        root = tk.Tk()
        root.title(f"{APP_TITLE} - Customize Shortcuts")
        root.resizable(False, False)
        root.configure(bg=BG_COLOR)
        root.attributes("-topmost", True)

        def _tk_callback_exception(exc_type, exc_value, exc_tb):
            _thread_crash_handler(
                threading.ExceptHookArgs(exc_type, exc_value, exc_tb, threading.current_thread())
            )

        root.report_callback_exception = _tk_callback_exception

        pending = {
            "manager": (manager_hotkey_mods, manager_hotkey_vk),
            "quick": (quick_type_hotkey_mods, quick_type_hotkey_vk),
        }
        recording = {"key": None, "poll_job": None}
        value_labels = {}
        change_buttons = {}
        status_var = tk.StringVar(value="")

        container = tk.Frame(root, bg=BG_COLOR, padx=18, pady=16)
        container.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            container,
            text="Customize Shortcuts",
            bg=BG_COLOR,
            fg=TEXT_PRIMARY,
            font=("Segoe UI Semibold", 12),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        def refresh_row(key):
            mods, vk = pending[key]
            value_labels[key].configure(text=_hotkey_display(mods, vk))

        def stop_recording(result):
            key = recording["key"]
            if key is None:
                return
            if recording["poll_job"] is not None:
                root.after_cancel(recording["poll_job"])
                recording["poll_job"] = None
            if result is not None:
                pending[key] = result
                refresh_row(key)
            change_buttons[key].configure(text="Change")
            for btn in change_buttons.values():
                btn.configure(state="normal")
            recording["key"] = None
            status_var.set("")

        def poll():
            key = recording["key"]
            if key is None:
                return
            if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:  # Esc cancels
                stop_recording(None)
                return
            mods = 0
            if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
                mods |= MOD_CONTROL
            if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
                mods |= MOD_ALT
            if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
                mods |= MOD_SHIFT
            if (user32.GetAsyncKeyState(VK_LWIN) & 0x8000) or (user32.GetAsyncKeyState(VK_RWIN) & 0x8000):
                mods |= MOD_WIN
            if mods:
                for vk in range(0x08, 0xFF):
                    if vk in _RECORD_IGNORE_VKS:
                        continue
                    if user32.GetAsyncKeyState(vk) & 0x8000:
                        stop_recording((mods, vk))
                        return
            recording["poll_job"] = root.after(40, poll)

        def start_recording(key):
            if recording["key"] is not None:
                return
            recording["key"] = key
            status_var.set("Hold Ctrl, Alt, Shift, and/or Win, then press a key. Esc to cancel.")
            change_buttons[key].configure(text="Press keys...")
            for k, btn in change_buttons.items():
                if k != key:
                    btn.configure(state="disabled")
            poll()

        rows_info = (("manager", "Open history manager"), ("quick", "Type most recent"))
        for r, (key, label_text) in enumerate(rows_info, start=1):
            tk.Label(
                container,
                text=label_text,
                bg=BG_COLOR,
                fg=TEXT_PRIMARY,
                font=("Segoe UI", 10),
                anchor="w",
            ).grid(row=r, column=0, sticky="w", padx=(0, 14), pady=6)
            lbl = tk.Label(
                container,
                text=_hotkey_display(*pending[key]),
                bg=CHIP_BG,
                fg=TEXT_PRIMARY,
                font=("Segoe UI Semibold", 10),
                padx=8,
                pady=3,
                width=16,
            )
            lbl.grid(row=r, column=1, sticky="w", padx=(0, 10))
            value_labels[key] = lbl
            btn = tk.Button(container, text="Change", width=12, command=lambda k=key: start_recording(k))
            btn.grid(row=r, column=2, sticky="w")
            change_buttons[key] = btn

        tk.Label(
            container, textvariable=status_var, bg=BG_COLOR, fg=TEXT_SECONDARY, font=("Segoe UI", 8),
            wraplength=360, justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 10))

        def close():
            if recording["key"] is not None:
                stop_recording(None)
            root.destroy()

        def do_reset():
            pending["manager"] = (DEFAULT_MANAGER_HOTKEY_MODS, DEFAULT_MANAGER_HOTKEY_VK)
            pending["quick"] = (DEFAULT_QUICK_TYPE_HOTKEY_MODS, DEFAULT_QUICK_TYPE_HOTKEY_VK)
            refresh_row("manager")
            refresh_row("quick")

        row_labels = dict(rows_info)  # {"manager": "Open history manager", "quick": "Type most recent"}

        def commit(final_values):
            global manager_hotkey_mods, manager_hotkey_vk
            global quick_type_hotkey_mods, quick_type_hotkey_vk
            manager_hotkey_mods, manager_hotkey_vk = final_values["manager"]
            quick_type_hotkey_mods, quick_type_hotkey_vk = final_values["quick"]
            _set_reg_int("ManagerHotkeyMods", manager_hotkey_mods)
            _set_reg_int("ManagerHotkeyVk", manager_hotkey_vk)
            _set_reg_int("QuickTypeHotkeyMods", quick_type_hotkey_mods)
            _set_reg_int("QuickTypeHotkeyVk", quick_type_hotkey_vk)
            restart_hotkey_listener()
            messagebox.showinfo(APP_TITLE, "Shortcuts saved.", parent=root)
            close()

        def show_conflict_dialog(conflicts, candidates, current):
            # `conflicts` holds only the shortcut(s) that are actually taken
            # by another app; the other one (if any) is already known-good
            # and doesn't need to be thrown away just because its sibling
            # collided with something.
            dlg = tk.Toplevel(root)
            dlg.title("Shortcut already in use")
            dlg.configure(bg=BG_COLOR)
            dlg.resizable(False, False)
            dlg.transient(root)
            dlg.attributes("-topmost", True)
            dlg.grab_set()

            body = tk.Frame(dlg, bg=BG_COLOR, padx=18, pady=16)
            body.pack(fill=tk.BOTH, expand=True)

            lines = [
                f"• {row_labels[key]}: ‘{_hotkey_display(mods, vk)}’ is already "
                f"used by another running app."
                for key, (mods, vk) in conflicts.items()
            ]
            tk.Label(
                body,
                text="\n".join(lines),
                bg=BG_COLOR,
                fg=TEXT_PRIMARY,
                font=("Segoe UI", 10),
                justify="left",
                wraplength=380,
            ).pack(anchor="w", pady=(0, 14))

            button_col = tk.Frame(body, bg=BG_COLOR)
            button_col.pack(fill=tk.X)

            def do_retry():
                dlg.destroy()
                # Send just the conflicting row(s) back into recording mode
                # so the user can immediately pick something else, without
                # having to redo whichever shortcut (if any) was already fine.
                for key in conflicts:
                    start_recording(key)

            def do_cancel_all():
                dlg.destroy()

            if len(conflicts) == 1:
                (conflict_key,) = conflicts.keys()
                ok_key = next(k for k in candidates if k != conflict_key)

                def do_save_partial():
                    dlg.destroy()
                    # Keep the shortcut that's free, revert the conflicting
                    # one back to whatever it's currently, successfully
                    # bound to - never left half-configured or unbound.
                    final_values = dict(candidates)
                    final_values[conflict_key] = current[conflict_key]
                    pending[conflict_key] = current[conflict_key]
                    refresh_row(conflict_key)
                    commit(final_values)

                tk.Button(
                    button_col,
                    text=(
                        f"Save “{row_labels[ok_key]}”, keep “{row_labels[conflict_key]}” "
                        f"as {_hotkey_display(*current[conflict_key])}"
                    ),
                    wraplength=360,
                    justify="left",
                    command=do_save_partial,
                ).pack(fill=tk.X, pady=(0, 6))
                tk.Button(
                    button_col,
                    text=f"Pick a different shortcut for “{row_labels[conflict_key]}”",
                    command=do_retry,
                ).pack(fill=tk.X, pady=(0, 6))
            else:
                tk.Button(
                    button_col, text="Pick different shortcuts for both", command=do_retry
                ).pack(fill=tk.X, pady=(0, 6))

            tk.Button(button_col, text="Cancel (keep current shortcuts)", command=do_cancel_all).pack(
                fill=tk.X
            )

            dlg.update_idletasks()
            w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            rx, ry = root.winfo_x(), root.winfo_y()
            rw, rh = root.winfo_width(), root.winfo_height()
            dlg.geometry(f"{w}x{h}+{rx + (rw - w) // 2}+{ry + (rh - h) // 2}")
            dlg.after(10, dlg.focus_force)

        def do_save():
            m_mods, m_vk = pending["manager"]
            q_mods, q_vk = pending["quick"]
            if (m_mods, m_vk) == (q_mods, q_vk):
                messagebox.showerror(
                    APP_TITLE,
                    "The two shortcuts can't be identical - pick a different "
                    "combination for each.",
                    parent=root,
                )
                return

            candidates = {"manager": (m_mods, m_vk), "quick": (q_mods, q_vk)}
            current = {
                "manager": (manager_hotkey_mods, manager_hotkey_vk),
                "quick": (quick_type_hotkey_mods, quick_type_hotkey_vk),
            }
            conflicts = {}
            for key, combo in candidates.items():
                if combo == current[key]:
                    continue  # unchanged - already registered by this app, nothing to test
                mods, vk = combo
                ok = user32.RegisterHotKey(None, _HOTKEY_TEST_ID, mods | MOD_NOREPEAT, vk)
                if ok:
                    user32.UnregisterHotKey(None, _HOTKEY_TEST_ID)
                else:
                    conflicts[key] = combo

            if conflicts:
                show_conflict_dialog(conflicts, candidates, current)
            else:
                commit(candidates)

        button_row = tk.Frame(container, bg=BG_COLOR)
        button_row.grid(row=4, column=0, columnspan=3, sticky="e", pady=(4, 0))
        tk.Button(button_row, text="Reset to defaults", command=do_reset).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(button_row, text="Cancel", command=close).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(button_row, text="Save", command=do_save).pack(side=tk.LEFT)

        root.protocol("WM_DELETE_WINDOW", close)
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        root.lift()
        root.after(10, root.focus_force)
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


def toggle_always_running(icon, item):
    global always_running_enabled
    always_running_enabled = not always_running_enabled
    _set_setting("AlwaysRunning", always_running_enabled)


def toggle_esc_cancels_typing(icon, item):
    global esc_cancels_typing_enabled
    esc_cancels_typing_enabled = not esc_cancels_typing_enabled
    _set_setting("EscCancelsTyping", esc_cancels_typing_enabled)


def quit_app(icon, item):
    icon.stop()
    _release_instance_mutex()
    # daemon threads will exit with the process
    os._exit(0)


def _build_tray_menu_items():
    # A callable (rather than a static tuple) so pystray re-evaluates it
    # every time the menu is about to be shown - that's what lets the two
    # shortcut labels and the elevation status stay current after the user
    # changes them, without having to rebuild/restart the whole tray icon.
    global run_at_startup_enabled
    if _IS_PACKAGED_APP:
        # Someone could have flipped this from Task Manager or Settings
        # since the menu was last opened - re-check rather than trust
        # whatever this process last set it to.
        run_at_startup_enabled = _startup_shortcut_exists()
    startup_label = _startup_menu_label()
    elevation_label = (
        "\U0001F6E1 Running as Administrator"
        if _is_elevated()
        else "\U0001F6E1 Restart as Administrator (for admin app credential boxes)"
    )
    manager_label = f"\U0001F4C1 Open history manager ({_hotkey_display(manager_hotkey_mods, manager_hotkey_vk)})"
    quick_label = f"⌨ Type most recent ({_hotkey_display(quick_type_hotkey_mods, quick_type_hotkey_vk)})"
    # pystray doesn't support real per-item icon bitmaps (only text, plus a
    # native OS checkmark on checkable items) - these are plain Unicode
    # glyphs prepended to the label text, a lightweight way to give each
    # action a visual cue without replacing the whole tray/menu subsystem
    # with a hand-built native Win32 popup menu. Checkable items (the ones
    # below with `checked=`) intentionally have no glyph prefix, since the
    # OS already draws a checkmark for those.
    return (
        pystray.MenuItem(manager_label, lambda icon, item: open_manager()),
        pystray.MenuItem(quick_label, lambda icon, item: quick_type_latest()),
        pystray.MenuItem(
            "Monitoring enabled",
            toggle_monitoring,
            checked=lambda item: monitoring_enabled,
        ),
        pystray.MenuItem("\U0001F5D1 Clear history", clear_history),
        pystray.MenuItem("⚙ Customize shortcuts...", lambda icon, item: open_hotkey_settings()),
        pystray.MenuItem(
            "Cancel typing by pressing Esc",
            toggle_esc_cancels_typing,
            checked=lambda item: esc_cancels_typing_enabled,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            elevation_label,
            relaunch_elevated,
            enabled=not _is_elevated(),
        ),
        pystray.MenuItem(
            "Always running (auto-restart if it crashes)",
            toggle_always_running,
            checked=lambda item: always_running_enabled,
        ),
        pystray.MenuItem(
            startup_label,
            toggle_run_at_startup,
            checked=lambda item: run_at_startup_enabled,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("✖ Quit", quit_app),
    )


def run_tray():
    icon = pystray.Icon(
        "clipboard_typer",
        _make_icon_image(),
        "Clipboard Typer",
        menu=pystray.Menu(_build_tray_menu_items),
    )
    icon.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global _instance_mutex_handle

    # --- Single-instance guard --------------------------------------------
    # A named mutex is visible across the whole session (and, with the
    # "Global\" prefix, across other sessions too) the instant it's
    # created - checking GetLastError() right after CreateMutex tells us
    # whether we're the first copy or a duplicate, with no race window.
    _instance_mutex_handle = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        _show_error_box(
            APP_TITLE,
            "Clipboard Typer is already running (check your system tray).\n\n"
            "Only one copy can run at a time, so the shortcuts and clipboard "
            "history stay consistent.",
        )
        try:
            win32api.CloseHandle(_instance_mutex_handle)
        except Exception:
            pass
        os._exit(0)

    _load_persisted_settings()
    # If this process is still alive after a while, treat it as recovered:
    # a future crash starts the fast-restart counter back at zero instead
    # of inheriting whatever count this process itself was launched with.
    threading.Timer(CRASH_RESTART_RESET_AFTER_SECONDS, _mark_crash_restart_recovered).start()

    _enable_dpi_awareness()

    threading.Thread(target=monitor_clipboard, daemon=True, name="ClipboardMonitor").start()

    _start_hotkey_listener()

    run_tray()  # blocks until Quit is chosen


if __name__ == "__main__":
    main()
