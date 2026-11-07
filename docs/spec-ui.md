# Kitbridge — plugin window spec (milestone 3)

The C4D-facing part: an entry point and a dockable window to search, download and import. Uses
`core/` (client, tasks, auth, search, pipeline) and `c4d_import/`. Read `spec-core.md` constraints
(Python 3.9 floor, main thread for scene edits, never block the UI on network or Blender).

## Files
```
plugin/kitbridge/kitbridge.pyp       registers the command (+ menu entry under Extensions), PluginMessage
                                      handling (C4DPL_ENDACTIVITY → stop poller, unsubscribe client)
plugin/kitbridge/res/                 icon (convert assets/logo/kitbridge-icon.svg to a 64px PNG: res/icon.png),
                                      strings if needed
plugin/kitbridge/kitbridge/ui/
  app.py           singleton controller: owns BKClient, Poller, Dispatcher, auth state, job queue;
                   lazily started when the window first opens; safe to call from timers
  dialog.py        KitbridgeDialog(GeDialog), async + dockable
  grid.py          AssetGrid(GeUserArea): thumbnail grid, hover, click, scroll, badges, progress
  disclaimer.py    first-run notice (text from DISCLAIMER.md, English + Russian + Ukrainian picked by
                   c4d.GeGetLanguage / resource language, fallback English), "I understand" stores
                   a flag in the Kitbridge prefs json; shown inside the dialog, not modal
  prefs.py         small json prefs file in ~/blenderkit_data/kitbridge/prefs.json: resolution,
                   blender path override, free_only, accepted_disclaimer, last tab
dev/c4d_open_window.py   for development: exec'd inside C4D, reloads modules and opens the dialog
                         without restarting C4D (keeps a module-level reference to the dialog)
dev/install_plugin.py    creates a directory junction (Windows) / symlink (macOS) from C4D's user
                         plugins folder to plugin/kitbridge; prints what it will do and needs --yes
```
Plugin ids (registered at developers.maxon.net/forum/pid): command 1073125, background message
plugin 1073126. Never reuse another plugin's id.

## Window layout (one GeDialog, dockable, min ~360×420, resizes well)
1. Header row: logo + "Kitbridge", login status on the right ("Log in" button → browser OAuth;
   when logged in show username and a small "Log out").
2. Tabs (radio/quicktab): Models · Materials · HDRIs · Downloaded (offline: cached packages only).
3. Search row: text field (Enter searches), Free only checkbox, resolution combo
   (512/1K/2K/4K/8K/Original, default 2K), search button.
4. Grid: AssetGrid fills the rest. Thumbnail cells (~128 px, adapts to width), name under it
   (ellipsized), small badges: FREE / paid lock if not downloadable for this user, ✓ if cached.
   Hover highlight. Click = download + import into the active document. Per-cell progress bar
   while working (download % → converting → importing). Right-click menu: Import, Import to
   selection (materials only), Open on blenderkit.com, Remove from cache.
   Infinite scroll or a "Load more" cell at the end (search `next` page).
5. Status bar: last message (e.g. "Imported Ferrari SF90 (35 materials) · Redshift"), errors in
   plain words, link-like button "Show log".
6. Settings button (gear) → small inline panel: Blender path (auto-detected value shown, Browse…),
   cache size + Clear cache, renderer used ("Auto: Redshift").

## Behaviour
- Opening the window starts the client + poller (if not running). Closing it does not stop them;
  C4D exit does (unsubscribe, not shutdown).
- Dialog `Timer` every 100 ms: drain poller queue → dispatcher; drain pipeline results → call
  `c4d_import.import_package` on the main thread → status + `EventAdd`.
- Thumbnails: bk_client downloads them (thumbnail_download tasks) to its temp dir; load with
  `c4d.bitmaps.BaseBitmap().InitWith(path)` on the main thread, cache BaseBitmaps by path, scale
  when drawing. Placeholder cell while missing.
- Search is debounced (only on Enter / button). Keep the last results per tab.
- Pipeline jobs run in worker threads (max 2 concurrent); a second click on a busy cell cancels
  after confirmation-free toggle ("Cancel" shown in the cell).
- Website "Get this" (`bkclientjs/get_asset` task) → same job path as a click, status says
  "From website: <name>".
- Not logged in + paid asset → status "Log in to download paid assets" and start login on click.
  Logged in but no subscription → the server error message as-is.
- Blender missing → status with a Settings shortcut; HDRIs still work.
- First run: the disclaimer panel replaces the grid until accepted.
- Every exception in UI callbacks is caught, logged (logger "kitbridge"), and shown as status —
  never let it propagate into C4D's message loop.

## Also in this milestone
- `c4d_import/compat.py`: wrap port value setters — use `SetPortValue`/`GetPortValue` when
  present (2024.4+), else `SetDefaultValue`/`GetDefaultValue`. Replace all direct calls.
- Keep everything Python 3.9 compatible; vermin must pass on plugin/ (exclude blender_scripts).
