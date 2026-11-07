# Kitbridge — window restyle v2 + filters (milestone 3.1)

> Since 0.3.8 the look below is replaced: plain rectangular buttons, upright type in sentence case, a low grey header,
> underlined tabs, a magnifier for search, no section title and no index diamonds. Current geometry is in `ui/layout.py`, the look in
> `dev/render_ui_assets.py`, and the options that were compared in the Figma file `ubhcDheCodeAWMQ0ZrzG5k`.

Source of truth for the look: Figma file "Kitbridge — logo", page "UI concept", frame
"Kitbridge window · Browse v2" (480 px wide). Numbers below are copied from it. Current code to
change: `plugin/kitbridge/kitbridge/ui/{dialog,grid,app,layout,prefs}.py`, `kitbridge.pyp`.
Constraints from `spec-core.md` still apply (Python 3.9 floor, stdlib only, main thread for scene
edits, never block UI). Do not touch `core/` or `c4d_import/` except where listed.

## Tokens
| token  | hex     | use |
|--------|---------|-----|
| bg     | #262626 | window background |
| panel  | #2E2E2E | chips, inactive tabs, status box |
| card   | #363636 | asset cards |
| field  | #1D1D1D | search field |
| line   | #424242 | 1 px outlines |
| text   | #E2E2E2 | primary text |
| muted  | #8E8E8E | secondary text |
| accent | #4D7FD6 | header band, active tab/segment, FULL PLAN tag, progress |
| accent-light | #7FA8F0 | "IN SCENE", status label |
| dark   | #161616 | index diamond, busy overlay (78 % opacity), PAID tag bg |
| online | #5BC46A | user dot |
Margins: 18 px left/right. Corner radius 3–4 px. Skewed shapes: parallelogram with 9–10 px skew.

## Layout (top → bottom, y in px at 480 wide; widths stretch, x anchored to 18 px margins)
1. Header band 0–104, accent. "KITBRIDGE" Barlow Condensed Black Italic 78 px, white, left edge
   at 18, optically centred on y 52. No logo here.
2. Meta chips y 118, h 24: "BLENDKIT", "<RENDERER>", "<RES>" — panel bg, Bold Italic 13 muted,
   10 px side padding, 6 px gap. Right-aligned user chip: green dot + name, text colour.
3. Section title y 160: "BROWSE" (text) + " / <TAB>" (muted), Black Italic 26.
4. Tabs y 200, h 30: MODELS, MATERIALS, HDRIS, DOWNLOADED as parallelograms (skew 9), 15 px
   ExtraBold Italic, 15 px side padding, 4 px gap. Active = accent fill + white text; inactive =
   panel fill + line outline + muted text.
5. Search y 244, h 38: native edit text styled as far as C4D allows inside a drawn field
   (field fill, line outline); button "SEARCH" parallelogram (skew 10) text-coloured fill, dark
   Black Italic 18 label, 102 px wide, right edge at margin, ≥ 8 px gap from the field.
6. Filter row y 296, h 22: segmented ALL · FREE · FULL PLAN · PAID (exclusive; accent = active)
   on the left; resolution segmented 1K 2K 4K 8K on the right (30 px cells, 2 px gap).
   ORIGINAL/512 stay available in Settings, not in this row.
7. Grid from y 336: cards 140×176 at 3 columns for 480 px (columns adapt), 12 px gap. Card: card
   fill, radius 4; thumbnail 112 px tall; index diamond 24 px at (7,7) dark fill + text outline +
   number; tier tag top-right (FREE = text fill/dark label, FULL PLAN = accent/white, PAID = dark
   fill + accent outline + #9DBDF5 label); name Black Italic-ish bold 17, text colour, 1 line,
   ellipsis, at (10,122); state line at (10,148) 12 px: "IN SCENE" accent-light, "IN CACHE ✓"
   muted, "BUY ON BLENDKIT" muted for PAID not owned, "CLICK TO CANCEL" while busy. Busy overlay
   on the thumbnail: dark 78 %, big "64%" 34 px + "CONVERTING" 14 px, 4 px progress bar at the
   thumbnail bottom (accent on #555555).
8. Status box: panel fill, radius 4, 4 px accent bar on the left, label "KITBRIDGE" accent-light
   13 px over one line of status text (Inter-like UI font 13, text colour).
9. Footer: logo `assets/logo/kitbridge-mark-footer.svg` (61×29, frame = drawing) centred, 20 px
   below the status box; two centred lines 12 px muted 20 px below the logo:
   "Unofficial community plugin · not affiliated with Blendkit, Maxon or the Blender Foundation"
   "Assets keep their Blendkit license · no resale or redistribution"; 20 px to the bottom.

## Rendering approach
- One custom `GeUserArea` for header + chips + section title + tabs + filter row (click hit
  areas), the existing grid user area for cards, one for status + footer. Native controls only for
  the search text field (and the search button may stay native if a drawn one is impractical).
- Fonts: C4D cannot load a bundled font. Pre-render STATIC labels (title, tab labels in both
  states, "BROWSE", section names, "SEARCH", segment labels, footer lines) to PNGs at 1× and 2×
  with Barlow Condensed (OFL, Google Fonts; ship the font's OFL.txt) via a dev script
  `dev/render_ui_assets.py` (Pillow in the dev venv only), output to `plugin/kitbridge/res/ui/`,
  commit the PNGs. Dynamic text (asset names, user, status, percentages) uses C4D's UI font in
  bold via `DrawSetFont`; uppercase for names.
- Logo: rasterise the footer SVG to PNG (1×, 2×) in the same script.
- Pick 2× assets when C4D reports a HiDPI scale (`c4d.gui.GetGuiWorldColor`? — find the correct
  DPI query for 2023–2027; fall back to 1×).

## Behaviour changes
- Tier filter: ALL / FREE / FULL PLAN / PAID. Determine from search result fields which assets
  are free, in the full plan, or sold individually (inspect a real search response first:
  `isFree`, `canDownload`, any price / `isForSale`-like field; document what you found). If the
  API has no individually-sold assets, hide the PAID segment and say so in the report.
  Server-side filter if the search API supports it, else client-side.
- Downloaded tab: show thumbnails (copy the search thumbnail into the package dir as
  `thumbnail.jpg` when a download starts; `list_cached` items use it) and a type sub-filter
  (ALL / MODELS / MATERIALS / HDRIS) in place of the tier filter on that tab.
- Top-level "Kitbridge" menu in C4D's main menu bar (C4DPL_BUILDMENU), on by default, with a
  Settings toggle "Show Kitbridge menu" (prefs `show_main_menu`); Extensions entry always exists.
- Plugin icon `res/icon.png` from the mark (64 px).
- Website "Get this" clipboard code: the site copies text like `asset_base_id:<uuid>` (the
  Blender add-on watches the clipboard for it). In the dialog timer, read
  `c4d.GetStringFromClipboard()` at most once per second while the window is open; when it changes
  and matches `asset_base_id:[0-9a-f-]{36}`, switch to the right tab, run that search (the search
  API accepts the code as the query — verified), highlight the result and show "From website:
  <name> — click to import" in the status. Never auto-import from the clipboard; never clear or
  overwrite the user's clipboard. Pasting the code into the search field must keep working.
