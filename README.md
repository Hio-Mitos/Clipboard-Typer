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

These are just the defaults — both can be personalized from the tray
icon's **"Customize shortcuts..."** menu item. Click "Change" next to
either one, then hold a modifier (Ctrl, Alt, Shift, and/or Win) and press
the key you want; "Reset to defaults" brings back Win+Alt+V / Ctrl+Alt+V.
Saving checks that your two shortcuts aren't the same combination and
that neither is already claimed by another running app, then applies the
new bindings immediately (no restart needed) and remembers them for next
time. The tray menu's item labels always show whatever is currently bound.

If one (or both) of your chosen shortcuts turns out to already be claimed
by another running app, saving doesn't just fail silently — you're offered
next steps on the spot:
- **If only one conflicts:** save the shortcut that's free, and keep the
  conflicting one exactly as it currently is (rather than leaving it
  unbound), or go straight back to picking a different combination for
  just that one.
- **If both conflict:** pick different combinations for both, or cancel
  and keep your current shortcuts unchanged.

Either way, nothing is ever left half-configured or unbound.

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

**Pressing and holding Esc while a typing burst is in progress stops it
immediately**, leaving whatever was already typed and abandoning the rest —
handy if you triggered the wrong item or typed into the wrong window. This
is optional and on by default; toggle it from the tray menu's "Cancel
typing by pressing Esc" checkbox. It only checks Esc while text is actively
being typed, so it never interferes with pressing Esc anywhere else.

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

The easiest way is the tray menu's **"Run at startup"** checkbox. What it
actually does depends on how you installed the app, and the app detects
this automatically:

- **Installed from the Microsoft Store (MSIX):** toggling it calls
  Windows' own `StartupTask` API, the same mechanism every other Store app
  uses to run at logon — it also shows up under **Settings > Apps >
  Startup**, where you can turn it off from Windows' side too. A classic
  registry `Run` key entry does *not* work for a Store-installed app —
  Windows silently virtualizes/ignores writes to it for packaged apps, so
  the entry would look like it saved successfully but the app would never
  actually launch at the next reboot. If you toggled this on in an earlier
  version and it didn't survive a reboot, that registry-key limitation was
  why; this is now fixed by using the proper API instead. If you ever turn
  it off from Task Manager's Startup apps tab or Windows Settings directly,
  the app can't silently turn it back on for you afterwards — the tray
  menu's label will say "(blocked in Windows Settings)" in that case, and
  clicking it offers to jump straight to Settings' Startup Apps page so you
  can re-enable it there.
- **Running the plain script or a standalone (non-Store) `.exe`:** toggling
  it adds/removes a normal `HKEY_CURRENT_USER\...\CurrentVersion\Run`
  registry entry pointing at the current install (the frozen `.exe`, or
  `pythonw.exe clipboard_typer.py`). No admin rights needed either way,
  since it's a per-user registry key.

You can also do it manually (unpackaged installs only):

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
- Shortcuts are best changed from the tray menu's "Customize shortcuts..."
  dialog (see above) rather than by editing the source. Other constants at
  the top of `clipboard_typer.py` (`HISTORY_MAXLEN`, typing speed delays)
  can still be changed directly if you want a different history size or
  faster/slower simulated typing.
- Both global shortcuts are registered with Windows' own `RegisterHotKey`
  API rather than a third-party keyboard hook, so they don't intercept or
  see the hundreds of individual keystrokes sent during a typing burst —
  only the exact Win+Alt+V / Ctrl+Alt+V combination itself triggers
  anything. If a paste ever comes out garbled on a very slow app, try
  raising `CHAR_DELAY` / `BREATHER_DELAY` a bit.
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
  the app registers global hotkeys and uses `SendInput`).
- `Assets/` — the icon set the manifest references (tile logos, store logo,
  splash screen), generated to match the app's blue clipboard glyph. These
  are functional placeholders; if you want a more polished/high-res set,
  regenerate them from `Assets/Icon-Master-512.png` with the MSIX Packaging
  Tool or Visual Studio's asset generator.
- `build_msix.ps1` — builds the EXE and packs the `.msix` (see below).
- `../clipboard_typer.spec` — the PyInstaller build config it uses.

### Local testing vs. the real Store submission

`AppxManifest.xml` on disk always holds your **real** Partner Center
identity, and `build_msix.ps1` never modifies that file — which of the two
builds you get is controlled entirely by a `-Target` flag, so switching
between "build something I can test right now" and "build the real Store
submission" never requires hand-editing the manifest back and forth:

- **`.\build_msix.ps1`** (or `-Target Store`, the default) — packages
  `AppxManifest.xml` exactly as checked in, unsigned, ready to upload to
  Partner Center, as `packaging/out/ClipboardTyper-<version>.msix`. If it
  still has the placeholder local-test identity, the script warns you at
  build time.
- **`.\build_msix.ps1 -Target Local`** — builds
  `packaging/out/ClipboardTyper-LocalTest-<version>.msix`: a *staged copy*
  of the manifest with its Identity swapped to a local-test Name/Publisher,
  automatically signed with a matching self-signed test certificate
  (created on first use). The swap only happens in memory and in the
  gitignored `packaging/staging` folder — the real `AppxManifest.xml` is
  untouched either way.

Either way, `<version>` in the filename is always read back out of the
manifest that was actually packaged (whatever `AppxManifest.xml` has, or
whatever `-Version` overrode it to) — the file always tells you exactly
which version is inside it, which matters once `packaging/out/` has more
than one build in it.

So the day-to-day loop is: `.\build_msix.ps1 -Target Local` as often as you
like to test changes on your own PC, and `.\build_msix.ps1` (no flags) when
you're ready to upload to Partner Center — same manifest, no editing in
between.

Before your *first* real submission, put your actual Partner Center
identity into `AppxManifest.xml` once:

1. Create/sign in to your Partner Center developer account and reserve the
   app name under **App management > App identity**.
2. That page shows the exact **Package/Identity/Name** and **Publisher**
   (a `CN=...` string) values for *your* reservation — put those into
   `AppxManifest.xml`'s `Name`/`Publisher`, and set `PublisherDisplayName`
   to your Partner Center publisher display name. This is the only time you
   touch the manifest by hand — from then on, both build targets work off
   this same file without further edits.

### Building the package

On a Windows machine with Python, this project's `requirements.txt`, and
`pyinstaller` installed:

```powershell
cd packaging
.\build_msix.ps1                    # Store submission build (unsigned, real identity)
.\build_msix.ps1 -Target Local       # local sideload test build (signed, test identity)
.\build_msix.ps1 -Version 1.1.0.0    # either target, bump the package version
```

Both targets run PyInstaller and stage the EXE + manifest + assets first.
`-Target Local` additionally creates (once) and reuses a local self-signed
test certificate so the resulting `.msix` can be installed on your own PC
right away (double-click it, or `Add-AppxPackage`) — see the script's own
comments for what that certificate is and isn't used for.

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

Unsigned `.msix` files (the `-Target Store` output) are fine to upload —
the Store re-signs the package itself during certification, so you don't
need to sign it yourself. In Partner Center: create a submission, choose
**MSIX or PWA app → MSIX**, upload `packaging/out/ClipboardTyper-<version>.msix`,
fill in the Store listing (description, screenshots, age rating, etc.), and
submit for certification.

Because the app registers global hotkeys and simulates keystrokes into
other apps, it's worth writing a plain-language explanation of *why* in the
Store listing description (clipboard history + "type instead of paste"
accessibility-style tool) — reviewers look more closely at
anything that touches global input.

Remember to bump `<Identity Version="...">` for every new submission —
Partner Center rejects a resubmission that reuses a version it has already
seen. `build_msix.ps1` now enforces this itself for `-Target Store` builds
(see below), so a forgotten bump fails the build loudly instead of failing
certification later.

### Versioning policy

Version format is `Major.Minor.Build.0` (the last segment, Revision, must
always stay `0`). How far to bump depends on how significant the change is:

- **Build** (3rd number) — small fixes, polish, docs-only changes. e.g.
  `1.3.0.0` → `1.3.1.0`.
- **Minor** (2nd number, reset Build to 0) — real enhancements: a new
  feature, a meaningful behavior change. e.g. `1.3.1.0` → `1.4.0.0`.
- **Major** (1st number, reset Minor and Build to 0) — drastic changes: a
  significant rework, a large set of features shipped together, or
  anything that changes how the app fundamentally behaves. e.g.
  `1.4.0.0` → `2.0.0.0`.

Every version bump also gets a `CHANGELOG.md` entry and matching "What's
new in this version" text for the Store listing (see the note at the
bottom of `CHANGELOG.md`).

`build_msix.ps1` enforces the "every Store build gets a new version" half
of this automatically: it records the version of the last successful
`-Target Store` build in `packaging\.last_store_version` (tracked in git),
and refuses to build again for the Store with the same
`<Identity Version>` still in `AppxManifest.xml`. `-Target Local` builds
are unaffected, since those aren't submissions.