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
clear history, restart as Administrator, Always running, Run at startup, quit.

To run without a console window: use `pythonw.exe clipboard_typer.py` instead
of `python.exe`.

Only one copy can run at a time — if you try to start a second one (double-clicking
the shortcut/EXE again, for example), it shows a message box saying Clipboard
Typer is already running and exits immediately, instead of creating a
conflicting second instance.

## Run automatically at startup

The easiest way is the tray menu's **"Run at startup"** checkbox — it adds
(or removes) a `HKEY_CURRENT_USER\...\CurrentVersion\Run` registry entry
pointing at the current install (the frozen `.exe` if you're running the
packaged build, or `pythonw.exe clipboard_typer.py` otherwise). No admin
rights needed, since it's a per-user registry key.

You can also do it manually:

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

## Always running (auto-restart after a crash)

The tray menu's **"Always running"** checkbox (on by default, persisted in
the registry) makes the app relaunch itself automatically if it hits an
unhandled error and crashes, instead of just staying dead until you notice
and restart it by hand. You still get the usual crash message box either
way — it just also says a fresh copy is being started.

To avoid a rapid crash-loop (the same bug crashing the app over and over,
every restart), it caps itself at 5 fast consecutive restarts; if the app
has been running fine for 30+ seconds it's considered "recovered" and that
counter resets, so a single flaky crash weeks apart from another one never
gets throttled. If it does hit the cap, the crash box says so and
auto-restarting is paused for that session — turning "Always running" off
and back on (or just starting it manually) resets it.

This only catches crashes Python itself can see (an unhandled exception).
It can't recover from things outside the process entirely, like the OS
killing it or a hard interpreter/DLL-level crash.

## Typing into elevated (Run as Administrator) apps

By default, Clipboard Typer runs at normal/standard privilege, and Windows'
User Interface Privilege Isolation (UIPI) blocks a standard-privilege
process from sending simulated keystrokes into a higher-privilege
("elevated" / "Run as administrator") window — the same protection that
stops a low-privilege app from tampering with an admin one. That shows up
as the shortcuts silently doing nothing when the window you're typing into
belongs to an elevated app (for example, a login/credential box inside an
elevated installer or admin tool).

The tray menu's **"Restart as Administrator"** item relaunches Clipboard
Typer elevated (you'll get the normal Windows UAC prompt) and exits the
non-elevated copy. Once it's running elevated, typing/pasting works into
both ordinary and elevated windows. The tray menu shows "Running as
Administrator" once that's active, and the item is disabled since there's
nothing further to do.

**What this does *not* reach:** the actual Secure Desktop UAC prompt itself
— the "Do you want to allow this app to make changes to your device?" box,
or its credential-entry variant — runs on a completely separate, isolated
desktop that no application, elevated or not, is permitted to inject input
into. That's a deliberate, unbypassable Windows security boundary, not a
limitation of this app; there's no legitimate way for any third-party tool
to type into that specific dialog.

## Notes / limitations

- History (last 50 items) is kept in memory only and resets when the app
  restarts, as requested.
- If the global shortcuts don't respond inside a specific app, that app may
  be running as Administrator — use the tray menu's **"Restart as
  Administrator"** item (see below) so both processes are at the same
  privilege level.
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

## Using it across a Remote Desktop / Windows App session

Typing now works when the target text box is inside a remote session
(Windows App, Remote Desktop / mstsc, Azure Virtual Desktop, Citrix-style
tools), not just on the local PC. The typing engine sends a real virtual-key
press for each character whenever the active keyboard layout supports it —
that's the same kind of event a physical keyboard produces, which is what
remote sessions forward over their keyboard channel. It only falls back to
raw Unicode injection for characters the current layout can't produce (most
emoji, non-Latin scripts) — that fallback is local-only and won't reach a
remote session.

A couple of things worth knowing when working across a remote session:
- **Ctrl+Alt+V** doesn't involve the Windows key, so it reliably triggers
  locally regardless of remote session settings.
- **Win+Alt+V** can be affected by the remote client's "Windows key
  combinations" setting — if that's set to send them to the remote computer
  instead of keeping them local, the manager may not open. If that happens,
  either switch that setting to "on this computer" (or "full screen only"),
  or just use Ctrl+Alt+V instead.
- If a paste still comes out slightly off on a high-latency connection, try
  raising `CHAR_DELAY` at the top of `clipboard_typer.py` — remote sessions
  have more delay between a keystroke being sent and the app on the other
  end actually processing it than a local app does.
- If the destination field there allows normal pasting (rather than
  blocking Ctrl+V), **Ctrl+Enter / Ctrl+click ("paste directly")** is
  simpler and relies on Remote Desktop's built-in clipboard redirection
  instead of simulated keystrokes at all.

## Packaging for the Microsoft Store (MSIX)

The `packaging/` folder has everything needed to build an MSIX package and
submit it under Partner Center's **"MSIX or PWA app"** product type (choose
the MSIX path — this is a Win32 desktop app, not a website or a game, so
that's the one that fits). MSIX is Microsoft's recommended path for desktop
apps: Store-managed updates, free Store code signing, and it registers the
Start menu entry / search / Add-or-remove-programs listing for you — that's
what makes the app show up when someone searches the Start menu after
installing it, without any custom installer scripting.

**Important scope note:** MSIX/Store distribution only works on Windows 10
(1809+) and Windows 11. It doesn't reach Windows 7/8 users — for those, keep
using the plain `clipboard_typer.py` / standalone EXE distribution described
earlier in this README. The Store listing is an *additional* channel for
Windows 10/11 users, not a replacement.

### What's in `packaging/`

- `AppxManifest.xml` — the package manifest (declares it as a full-trust
  Win32 app via Desktop Bridge, not a sandboxed UWP app — required, since
  the app uses system-wide keyboard hooks and `SendInput`).
- `Assets/` — the icon set the manifest references (tile logos, store logo,
  splash screen), generated to match the app's blue clipboard glyph. These
  are functional placeholders; if you want a more polished/high-res set,
  regenerate them from `Assets/Icon-Master-512.png` with the MSIX Packaging
  Tool or Visual Studio's asset generator.
- `build_msix.ps1` — builds the EXE and packs the `.msix` (see below).
- `../clipboard_typer.spec` — the PyInstaller build config it uses.

### Local testing vs. the real Store submission

`AppxManifest.xml` ships with a working local-test identity
(`Name="ClipboardTyper.LocalTest"`, `Publisher="CN=ClipboardTyperLocalTest"`)
instead of a placeholder that fails to build — that Publisher value
deliberately matches the self-signed test certificate `build_msix.ps1
-SignForTesting` creates, so you can build and sideload-install a real
`.msix` on your own PC right away, before touching Partner Center at all.

Before actually submitting to the Store, swap that identity for your real
one:

1. Create/sign in to your Partner Center developer account and reserve the
   app name under **App management > App identity**.
2. That page shows the exact **Package/Identity/Name** and **Publisher**
   (a `CN=...` string) values for *your* reservation — replace the
   `Name`/`Publisher` in `AppxManifest.xml` with those, and set
   `PublisherDisplayName` to your Partner Center publisher display name.
   `build_msix.ps1` will warn you at build time if the manifest still has
   the local-test identity in it.

### Building the package

On a Windows machine with Python, this project's `requirements.txt`, and
`pyinstaller` installed:

```powershell
cd packaging
.\build_msix.ps1
```

This runs PyInstaller, stages the EXE + manifest + assets, and produces
`packaging/out/ClipboardTyper.msix`. Pass `-Version 1.1.0.0` to bump the
package version, and add `-SignForTesting` if you want to sideload-install
it on your own PC first to confirm it works before submitting (see the
script's own comments — the test certificate it creates is only for that
local check, not for the Store submission itself).

**About makeappx/signtool:** the script needs these two Windows SDK tools to
actually build the `.msix`. It looks for them on your `PATH`, then in a
normal Windows SDK install location, and if neither is found it
automatically downloads the small (~15MB) `Microsoft.Windows.SDK.BuildTools`
NuGet package into `packaging\.tools\` and uses that — no SDK installer, no
admin rights, no reboot needed. If that download also fails (common on a
locked-down corporate network that blocks `nuget.org`), install the Windows
SDK manually instead: run the standalone installer from
https://developer.microsoft.com/windows/downloads/windows-sdk/ and choose
**Custom install → check only "MSIX Packaging Tools"** (you don't need the
rest of the SDK).

### Submitting

Unsigned `.msix` files are fine to upload — the Store re-signs the package
itself during certification, so you don't need to sign it yourself unless
you're testing a local sideload install. In Partner Center: create a
submission, choose **MSIX or PWA app → MSIX**, upload
`packaging/out/ClipboardTyper.msix`, fill in the Store listing (description,
screenshots, age rating, etc.), and submit for certification.

Because the app installs a system-wide keyboard hook and simulates
keystrokes into other apps, it's worth writing a plain-language explanation
of *why* in the Store listing description (clipboard history + "type
instead of paste" accessibility-style tool) — reviewers look more closely at
anything that touches global input.

Remember to bump `<Identity Version="...">` for every new submission —
Partner Center rejects a resubmission that reuses a version it has already
seen.