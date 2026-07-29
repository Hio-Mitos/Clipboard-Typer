# Changelog

All notable changes to Clipboard Typer are recorded here, one entry per
`AppxManifest.xml` version bump. This is the developer-facing history —
see the note at the bottom for where the *user*-facing "what's new" text
for each release lives.

## 1.2.0.0

### Fixed
- **Win+Alt+V shortcut unusable on some PCs** (the issue Microsoft Store
  certification reported). The two global shortcuts were registered using a
  third-party keyboard hook that could be silently blocked by
  security/endpoint software; both are now registered with Windows' own
  native shortcut API instead, which doesn't have that problem.

### Added
- **Customizable shortcuts** — personalize both global shortcuts from the
  tray menu's "Customize shortcuts..." dialog, with live conflict detection
  and case-by-case resolution options if a chosen combination is already in
  use by another app.
- **Esc cancels typing** (optional, on by default) — press and hold Esc to
  stop an in-progress typing burst partway through.
- **Single-instance enforcement** — trying to start a second copy shows a
  message instead of running two conflicting instances.
- **Restart as Administrator** — lets shortcuts type into other elevated
  apps' windows (e.g. an admin tool's own login box), which a
  standard-privilege process can't otherwise reach.
- **Always running** — optional auto-restart if the app crashes, with a
  crash-loop guard.
- **Run at startup** — toggle from the tray menu instead of manually
  creating a shortcut.

### Changed
- Removed the third-party `keyboard` library dependency entirely (both the
  hotkeys and the "paste directly" shortcut now use Windows' own input
  APIs directly).
- MSIX builds now support a `-Target Local` / `-Target Store` build script
  flag so a local test build and a real Store submission build never
  require hand-editing `AppxManifest.xml` back and forth.
- Packaged `.msix` filenames now include the version number.

## 1.1.1.1 and earlier

Initial release and early iterations: clipboard history (last 50 items,
in-memory only), type-as-keystrokes or paste-directly via Win+Alt+V and
Ctrl+Alt+V, Remote Desktop / Windows App compatible typing, draggable
auto-closing history flyout, crash reporting, and the initial MSIX
packaging pipeline.

---

**Where the user-facing "what's new" text lives:** this file is the
detailed developer history. The shorter, plain-language version that
Store users actually see when updating goes in Partner Center's Store
listing → **"What's new in this version"** field for each submission —
see the project notes/chat history for the exact text used per version,
or ask for it to be regenerated from this changelog.
