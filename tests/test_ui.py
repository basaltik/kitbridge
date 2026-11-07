import os

from kitbridge.ui import disclaimer, layout, prefs, theme


def test_prefs_roundtrip_and_sanitising(tmp_path):
    path = str(tmp_path / "prefs.json")
    assert prefs.load(path) == prefs.DEFAULTS
    prefs.save({"resolution": "4096", "free_only": True, "bogus": 1, "last_tab": "nope"}, path)
    loaded = prefs.load(path)
    assert loaded["resolution"] == "4096"
    assert loaded["free_only"] is True
    assert loaded["last_tab"] == "model"
    assert "bogus" not in loaded


def test_prefs_renderer_choice_is_validated(tmp_path):
    path = str(tmp_path / "prefs.json")
    prefs.save({"renderer": "arnold", "remember_renderer": True}, path)
    assert prefs.load(path)["renderer"] == "arnold"
    prefs.save({"renderer": "cycles"}, path)
    assert prefs.load(path)["renderer"] == "auto"


def test_pending_click_starts_after_login_search():
    from kitbridge.ui.app import KitbridgeApp

    app = KitbridgeApp.__new__(KitbridgeApp)  # no prefs file, no client
    started = []
    app._start_job = lambda item, source: started.append((item["name"], source))
    app._pending_after_login = {"assetBaseId": "b1", "name": "stale"}
    assert app._start_pending_from([{"assetBaseId": "x", "name": "other"}]) is False
    assert app._start_pending_from([{"assetBaseId": "b1", "name": "fresh"}]) is True
    assert started == [("fresh", "click")]           # the re-fetched item, not the logged-out copy
    assert app._pending_after_login is None


def test_prefs_broken_file_gives_defaults(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text("{not json", encoding="utf-8")
    assert prefs.load(str(path)) == prefs.DEFAULTS


def test_grid_columns_and_hit_test():
    m = layout.GridMetrics(cell=100, label=20, gap=10, margin=5)
    assert layout.columns(0, m) == 1
    assert layout.columns(5 + 100 + 10 + 100 + 5, m) == 2
    cols = 2
    assert layout.cell_rect(3, cols, m) == layout.Rect(5 + 110, 5 + 130, 100, 120)
    assert layout.hit_test(10, 10, 4, cols, m) == 0
    assert layout.hit_test(5 + 105, 10, 4, cols, m) is None      # in the gap
    assert layout.hit_test(5 + 115, 5 + 135, 4, cols, m) == 3
    assert layout.hit_test(5 + 115, 5 + 135, 3, cols, m) is None  # past the last item
    assert layout.content_height(3, cols, m) == 2 * 5 + 2 * 130 - 10
    hit = layout.hit_test(10.0, 10.0, 4, cols, m)   # C4D 2027 sends float coordinates
    assert hit == 0 and isinstance(hit, int)


def test_fit_fills_width_evenly():
    base = layout.GridMetrics(cell=128, label=18, gap=8, margin=8)
    for width in (300, 440, 461, 640, 1000):
        m = layout.fit(width, base)
        cols = layout.columns(width, m)
        used = 2 * m.margin + cols * m.cell + (cols - 1) * m.gap
        assert width - cols < used <= width          # leftover is less than one px per column
        assert cols == layout.columns(width, base)   # stretching never changes the column count


def test_v2_card_dimensions_at_480_width():
    base = layout.GridMetrics(card_w=140, card_h=176, thumb_h=112, gap=12, margin=18)
    assert layout.columns(480, base) == 3
    m = layout.fit(480, base)
    assert m.card_w == 140
    assert m.card_h == 176
    assert m.thumb_h == 112


def test_ellipsize():
    assert layout.ellipsize("chair", 10) == "chair"
    assert layout.ellipsize("dining chair", 6) == "dinin…"


def test_disclaimer_language_pick():
    assert disclaimer.pick_language("ru-RU") == "ru"
    assert disclaimer.pick_language("uk-UA") == "uk"
    assert disclaimer.pick_language("de-DE") == "en"
    assert set(disclaimer.TEXT) == set(disclaimer.BUTTON)


def test_tab_rects_and_hit_test():
    rects = layout.tab_rects(margin=18, y=200)
    assert len(rects) == 4
    keys = [k for k, _ in rects]
    assert keys == ["model", "material", "hdr", "downloaded"]
    # Check hit test on first tab (models)
    assert layout.tab_hit_test(25, 210, margin=18, y=200) == "model"
    # Gap between tabs
    first_w = rects[0][1].w
    gap_x = 18 + first_w + 1  # 4px gap
    assert layout.tab_hit_test(gap_x, 210, margin=18, y=200) is None
    # Outside vertical bounds
    assert layout.tab_hit_test(25, 190, margin=18, y=200) is None
    assert layout.tab_hit_test(25, 235, margin=18, y=200) is None


def test_filter_row_rects_and_hit_test():
    # Browse tab mode
    hit = layout.filter_row_hit_test(25, 10, width=480, tab="model", margin=18, y=0, h=22)
    assert hit == ("tier", "ALL")

    # Resolution segment on right side (1K, 2K, 4K, 8K)
    # Right edge is 480 - 18 = 462. 8K is rightmost (462 - 30 = 432)
    hit_res = layout.filter_row_hit_test(440, 10, width=480, tab="model", margin=18, y=0, h=22)
    assert hit_res == ("resolution", "8192")

    # Downloaded tab mode (type sub-filter)
    hit_type = layout.filter_row_hit_test(25, 10, width=480, tab="downloaded", margin=18, y=0, h=22)
    assert hit_type == ("type", "ALL")


def test_tier_classification():
    free_asset = {"name": "Chair", "isFree": True}
    assert layout.classify_tier(free_asset) == "FREE"

    access_free = {"name": "Table", "access": "free"}
    assert layout.classify_tier(access_free) == "FREE"

    paid_asset = {"name": "Car", "isForSale": True}
    assert layout.classify_tier(paid_asset) == "PAID"

    full_plan_asset = {"name": "Studio", "isFree": False, "access": "full"}
    assert layout.classify_tier(full_plan_asset) == "FULL PLAN"


def test_filter_items():
    items = [
        {"name": "Free Chair", "isFree": True, "assetType": "model"},
        {"name": "Pro Car", "isFree": False, "access": "full", "assetType": "model"},
        {"name": "Free Wood", "isFree": True, "assetType": "material"},
    ]
    # ALL
    assert len(layout.filter_items(items, tab="model", tier_filter="ALL")) == 3
    # FREE only
    free_only = layout.filter_items(items, tab="model", tier_filter="FREE")
    assert len(free_only) == 2
    assert [i["name"] for i in free_only] == ["Free Chair", "Free Wood"]
    # FULL PLAN only
    full_only = layout.filter_items(items, tab="model", tier_filter="FULL PLAN")
    assert len(full_only) == 1
    assert full_only[0]["name"] == "Pro Car"

    # Downloaded type filter
    mat_only = layout.filter_items(items, tab="downloaded", type_filter="MATERIALS")
    assert len(mat_only) == 1
    assert mat_only[0]["name"] == "Free Wood"


def test_parse_clipboard_code():
    valid = "asset_base_id:12345678-1234-1234-1234-123456789abc"
    assert layout.parse_clipboard_code(valid) == "12345678-1234-1234-1234-123456789abc"
    assert layout.parse_clipboard_code(f"  {valid}  \n") == "12345678-1234-1234-1234-123456789abc"
    assert layout.parse_clipboard_code("something else") is None
    assert layout.parse_clipboard_code("asset_base_id:short") is None
    assert layout.parse_clipboard_code("") is None


def test_theme_tokens_and_colors():
    assert theme.rgb255("bg") == (38, 38, 38)
    assert theme.rgb255("accent") == (77, 127, 214)
    r, g, b = theme.rgb("accent")
    assert abs(r - 77 / 255.0) < 1e-4
    cache = theme.AssetCache()
    assert cache.get_path("header_title", scale=1).endswith("header_title.png")
    assert cache.get_path("header_title", scale=2).endswith("header_title@2x.png")

def test_layout_attributes_used_by_ui_modules_exist():
    """c4d cannot be imported here, so a missing layout.X only shows up inside Cinema 4D."""
    import ast
    import os

    ui_dir = os.path.dirname(layout.__file__)
    for name in ("dialog.py", "views.py", "grid.py", "app.py"):
        with open(os.path.join(ui_dir, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "layout"
            ):
                assert hasattr(layout, node.attr), f"{name}: layout.{node.attr}"


def test_every_tab_has_its_rendered_assets():
    import os

    ui_png = os.path.normpath(os.path.join(os.path.dirname(layout.__file__), "..", "..", "res", "ui"))
    for _key, label, _width in layout.TAB_SPECS:
        name = label.lower()
        for asset in (f"tab_btn_{name}_active", f"tab_btn_{name}_inactive"):
            assert os.path.isfile(os.path.join(ui_png, asset + ".png")), asset

def test_settings_folders_match_download_subfolders():
    from kitbridge.core import paths

    assert set(layout.SETTINGS_FOLDERS) == set(paths.ASSET_FOLDERS.values())


def test_settings_hit_test():
    rects = layout.settings_rects(480)
    for name in ("browse_folder", "open:hdris", "res:ORIGINAL", "clear_cache", "toggle_menu"):
        rect = rects[name]
        assert layout.settings_hit_test(rect.x + 2, rect.y + 2, 480) == name
    field = rects["folder_field"]
    assert layout.settings_hit_test(field.x + 2, field.y + 2, 480) is None
    assert layout.settings_hit_test(1, 1, 480) is None
    assert max(r.y + r.h for r in rects.values()) <= layout.SETTINGS_HEIGHT


def test_settings_buttons_fit_their_rendered_assets():
    import os

    from PIL import Image

    ui = os.path.normpath(os.path.join(os.path.dirname(layout.__file__), "..", "..", "res", "ui"))
    rects = layout.settings_rects(480)
    for png, name in (
        ("btn_browse", "browse_folder"),
        ("btn_use_default", "default_folder"),
        ("btn_auto", "auto_blender"),
        ("btn_clear_cache", "clear_cache"),
    ):
        with Image.open(os.path.join(ui, png + ".png")) as im:
            assert im.size == (rects[name].w, rects[name].h), png



def test_buttons_are_plain_rectangles_at_their_layout_size():
    from PIL import Image

    ui_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "kitbridge", "res", "ui")
    sized = {"btn_search": layout.SEARCH_BUTTON_SIZE}
    for _key, label, width in layout.TAB_SPECS:
        sized[f"tab_btn_{label.lower()}_active"] = (width, layout.TAB_H)
    for name, (w, h) in sized.items():
        im = Image.open(os.path.join(ui_dir, name + ".png")).convert("RGBA")
        assert im.size == (w, h), name
        for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            assert im.getpixel(corner)[3] == 255, (name, corner)


def test_every_filter_segment_has_its_labels():
    ui_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "kitbridge", "res", "ui")
    labels = list(layout.TIER_SEGMENTS) + list(layout.DOWNLOADED_SEGMENTS) + list(layout.RES_SEGMENTS)
    labels += [label for label, _value, _width in layout.SETTINGS_RESOLUTIONS]
    for label in labels:
        stem = label.lower().replace(" ", "_")
        for state in ("active", "inactive"):
            assert os.path.isfile(os.path.join(ui_dir, f"seg_{stem}_{state}.png")), (label, state)
