#!/usr/bin/env python3
"""Pre-render static UI assets (labels, shapes, logos, icon) to PNGs at 1x and 2x.

Docs reference: docs/spec-ui-v2.md (milestone 3.1).
Requires Pillow in the dev venv only. Plugin runtime uses stdlib only.
"""

from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(REPO_ROOT, "dev", "fonts")
ASSETS_DIR = os.path.join(REPO_ROOT, "assets", "logo")
RES_DIR = os.path.join(REPO_ROOT, "plugin", "kitbridge", "res")
UI_OUT_DIR = os.path.join(RES_DIR, "ui")

# Colour tokens from docs/spec-ui-v2.md
TOKENS: Dict[str, str] = {
    "bg": "#262626",
    "panel": "#2E2E2E",
    "card": "#363636",
    "field": "#1D1D1D",
    "line": "#424242",
    "text": "#E2E2E2",
    "muted": "#8E8E8E",
    "accent": "#4D7FD6",
    "accent_light": "#7FA8F0",
    "dark": "#161616",
    "online": "#5BC46A",
    "paid_label": "#9DBDF5",
    "track": "#555555",
    "white": "#FFFFFF",
}


# Keep equal to layout.TAB_SPECS and layout.TAB_H: file stem, label, width.
TABS = (("models", "Models", 56), ("materials", "Materials", 68), ("hdris", "HDRIs", 50), ("downloaded", "Downloaded", 84))
TAB_H = 28
# File stem of each filter segment, and the label it shows.
SEGMENTS = {
    "all": "All", "free": "Free", "full_plan": "Full plan", "paid": "Paid",
    "models": "Models", "materials": "Materials", "hdris": "HDRIs",
    "512": "512", "1k": "1K", "2k": "2K", "4k": "4K", "8k": "8K", "original": "Original",
}


def hex_to_rgb(hex_code: str) -> Tuple[int, int, int]:
    clean = hex_code.lstrip("#")
    return int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)


def hex_to_rgba(hex_code: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    r, g, b = hex_to_rgb(hex_code)
    return r, g, b, alpha


def get_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(FONTS_DIR, filename)
    return ImageFont.truetype(path, size)


UPRIGHT = "BarlowCondensed-Medium.ttf"
# Heavier weights of the upright face, as a stroke relative to the type size.
SEMIBOLD = 0.018
BOLD = 0.032
SUPERSAMPLE = 4


def _draw_label(draw, text, font, stroke, x, y, fill) -> None:
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)


def render_text_png(
    text: str,
    size_pt: int,
    color_hex: str,
    scale: int = 1,
    weight: float = SEMIBOLD,
    font_file: str = UPRIGHT,
) -> Image.Image:
    """Render a text snippet with an exact bounding box on a transparent background."""
    size = size_pt * scale * SUPERSAMPLE
    font = get_font(font_file, size)
    stroke = int(round(size * weight))
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    im = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)), (0, 0, 0, 0))
    _draw_label(ImageDraw.Draw(im), text, font, stroke, -left, -top, hex_to_rgba(TOKENS.get(color_hex, color_hex)))
    return im.resize((max(1, im.width // SUPERSAMPLE), max(1, im.height // SUPERSAMPLE)), Image.Resampling.LANCZOS)


def render_button(
    text: str,
    size_pt: int,
    text_color_hex: str,
    bg_color_hex: str,
    width: int,
    height: int,
    outline_color_hex: Optional[str] = None,
    scale: int = 1,
) -> Image.Image:
    """Render a plain rectangular button with its label centred."""
    ss = scale * SUPERSAMPLE
    w, h = width * ss, height * ss
    im = Image.new("RGBA", (w, h), hex_to_rgba(TOKENS.get(bg_color_hex, bg_color_hex)))
    draw = ImageDraw.Draw(im)
    if outline_color_hex:
        draw.rectangle([0, 0, w - 1, h - 1], outline=hex_to_rgba(TOKENS.get(outline_color_hex, outline_color_hex)), width=ss)
    font = get_font(UPRIGHT, size_pt * ss)
    stroke = int(round(size_pt * ss * SEMIBOLD))
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    _draw_label(
        draw, text, font, stroke,
        (w - (right - left)) // 2 - left, (h - (bottom - top)) // 2 - top,
        hex_to_rgba(TOKENS.get(text_color_hex, text_color_hex)),
    )
    return im.resize((width * scale, height * scale), Image.Resampling.LANCZOS)


def render_tab(text: str, width: int, active: bool, scale: int = 1) -> Image.Image:
    """Render a tab: a label on the window background, underlined in accent while active."""
    ss = scale * SUPERSAMPLE
    w, h = width * ss, TAB_H * ss
    im = Image.new("RGBA", (w, h), hex_to_rgba(TOKENS["bg"]))
    draw = ImageDraw.Draw(im)
    font = get_font(UPRIGHT, 16 * ss)
    stroke = int(round(16 * ss * SEMIBOLD))
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    bar = 2 * ss
    _draw_label(
        draw, text, font, stroke,
        (w - (right - left)) // 2 - left, (h - bar - (bottom - top)) // 2 - top,
        hex_to_rgba(TOKENS["text" if active else "muted"]),
    )
    if active:
        draw.rectangle([0, h - bar, w - 1, h - 1], fill=hex_to_rgba(TOKENS["accent"]))
    return im.resize((width * scale, TAB_H * scale), Image.Resampling.LANCZOS)


def render_search_icon(size: int, scale: int = 1) -> Image.Image:
    """Render the search button: a white magnifier on accent."""
    ss = scale * SUPERSAMPLE
    w = size * ss
    im = Image.new("RGBA", (w, w), hex_to_rgba(TOKENS["accent"]))
    draw = ImageDraw.Draw(im)
    white = hex_to_rgba(TOKENS["white"])
    line = 2 * ss
    x0, y0, r = 10 * ss, 10 * ss, 5 * ss + line // 2
    draw.ellipse([x0, y0, x0 + 2 * r, y0 + 2 * r], outline=white, width=line)
    cx = x0 + r
    tip = int(r * 0.72)
    draw.line([cx + tip, y0 + r + tip, 24 * ss, 24 * ss], fill=white, width=line)
    return im.resize((size * scale, size * scale), Image.Resampling.LANCZOS)


def render_footer_mark(scale: int = 1) -> Image.Image:
    """Render the kitbridge-mark-footer vector geometry at scale 1x or 2x."""
    w, h = 61 * scale, 29 * scale
    ss = 8
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    sx = (w * ss) / 122.0
    sy = (h * ss) / 58.0

    def pt(x: float, y: float) -> Tuple[float, float]:
        return ((x - 19.0) * sx, (y - 51.5) * sy)

    p_left = [pt(44, 54), pt(66.5, 67), pt(66.5, 93), pt(44, 106), pt(21.5, 93), pt(21.5, 67)]
    d.polygon(p_left, fill="#C9C9C9")

    sw_left = int(round(3.5 * sx))
    d.line([pt(44, 80), pt(44, 106)], fill="#262626", width=sw_left)
    d.line([pt(44, 80), pt(21.5, 67)], fill="#262626", width=sw_left)
    d.line([pt(44, 80), pt(66.5, 67)], fill="#262626", width=sw_left)

    p_right = [pt(116, 54), pt(138.5, 67), pt(138.5, 93), pt(116, 106), pt(93.5, 93), pt(93.5, 67)]
    sw_right = int(round(5.0 * sx))
    for i in range(len(p_right)):
        d.line([p_right[i], p_right[(i + 1) % len(p_right)]], fill="#C9C9C9", width=sw_right)
    d.line([pt(116, 80), pt(116, 106)], fill="#C9C9C9", width=sw_right)
    d.line([pt(116, 80), pt(93.5, 67)], fill="#C9C9C9", width=sw_right)
    d.line([pt(116, 80), pt(138.5, 67)], fill="#C9C9C9", width=sw_right)

    d.line([pt(72, 80), pt(84, 80)], fill="#C9C9C9", width=sw_right)

    return im.resize((w, h), Image.Resampling.LANCZOS)


def render_plugin_icon(out_size: int = 64) -> Image.Image:
    """Render kitbridge-icon.svg geometry into a 64px rounded rect PNG."""
    ss = 8
    target_canvas = out_size * ss
    im = Image.new("RGBA", (target_canvas, target_canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 160x160 canvas, rounded rect radius 32
    scale = target_canvas / 160.0
    r = int(round(32 * scale))
    d.rounded_rectangle([(0, 0), (target_canvas - 1, target_canvas - 1)], radius=r, fill="#000000")

    def pt(x: float, y: float) -> Tuple[float, float]:
        return (x * scale, y * scale)

    # Left hex
    p_left = [pt(44, 54), pt(66.5, 67), pt(66.5, 93), pt(44, 106), pt(21.5, 93), pt(21.5, 67)]
    d.polygon(p_left, fill="#FFFFFF")

    sw_left = int(round(3.5 * scale))
    d.line([pt(44, 80), pt(44, 106)], fill="#000000", width=sw_left)
    d.line([pt(44, 80), pt(21.5, 67)], fill="#000000", width=sw_left)
    d.line([pt(44, 80), pt(66.5, 67)], fill="#000000", width=sw_left)

    # Right hex outline
    p_right = [pt(116, 54), pt(138.5, 67), pt(138.5, 93), pt(116, 106), pt(93.5, 93), pt(93.5, 67)]
    sw_right = int(round(5.0 * scale))
    for i in range(len(p_right)):
        d.line([p_right[i], p_right[(i + 1) % len(p_right)]], fill="#FFFFFF", width=sw_right)
    d.line([pt(116, 80), pt(116, 106)], fill="#FFFFFF", width=sw_right)
    d.line([pt(116, 80), pt(93.5, 67)], fill="#FFFFFF", width=sw_right)
    d.line([pt(116, 80), pt(138.5, 67)], fill="#FFFFFF", width=sw_right)

    d.line([pt(72, 80), pt(84, 80)], fill="#FFFFFF", width=sw_right)

    return im.resize((out_size, out_size), Image.Resampling.LANCZOS)


def main() -> None:
    os.makedirs(UI_OUT_DIR, exist_ok=True)
    os.makedirs(RES_DIR, exist_ok=True)

    # Copy font license OFL.txt to res/
    src_ofl = os.path.join(FONTS_DIR, "OFL.txt")
    if os.path.isfile(src_ofl):
        shutil.copyfile(src_ofl, os.path.join(RES_DIR, "OFL.txt"))
        print("Copied OFL.txt to plugin/kitbridge/res/OFL.txt")

    # Render plugin icon (64px)
    icon = render_plugin_icon(64)
    icon_path = os.path.join(RES_DIR, "icon.png")
    icon.save(icon_path)
    print(f"Generated {icon_path}")

    # Generate assets at 1x and 2x
    for scale, suffix in ((1, ""), (2, "@2x")):
        print(f"\n--- Generating {scale}x assets ---")

        # 1. Name in the header band
        render_text_png("Kitbridge", 26, "white", scale=scale).save(os.path.join(UI_OUT_DIR, f"header_title{suffix}.png"))

        # 2. Tabs, and their labels for the settings tiles
        for stem, label, width in TABS:
            render_tab(label, width, True, scale=scale).save(os.path.join(UI_OUT_DIR, f"tab_btn_{stem}_active{suffix}.png"))
            render_tab(label, width, False, scale=scale).save(os.path.join(UI_OUT_DIR, f"tab_btn_{stem}_inactive{suffix}.png"))
            render_text_png(label, 15, "text", scale=scale).save(os.path.join(UI_OUT_DIR, f"tab_label_{stem}_active{suffix}.png"))

        # 3. Meta chip
        render_text_png("Blendkit", 13, "muted", scale=scale).save(os.path.join(UI_OUT_DIR, f"chip_blendkit{suffix}.png"))

        # 4. Search button
        render_search_icon(34, scale=scale).save(os.path.join(UI_OUT_DIR, f"btn_search{suffix}.png"))

        # 5. Filter row segments
        for stem, label in SEGMENTS.items():
            render_text_png(label, 13, "white", scale=scale).save(os.path.join(UI_OUT_DIR, f"seg_{stem}_active{suffix}.png"))
            render_text_png(label, 13, "muted", scale=scale).save(os.path.join(UI_OUT_DIR, f"seg_{stem}_inactive{suffix}.png"))

        # 6. Status box label
        render_text_png("Kitbridge", 13, "accent_light", scale=scale).save(
            os.path.join(UI_OUT_DIR, f"status_kitbridge{suffix}.png")
        )

        # 7. Footer:
        # Logo 61x29
        im_footer_logo = render_footer_mark(scale=scale)
        im_footer_logo.save(os.path.join(UI_OUT_DIR, f"footer_logo{suffix}.png"))

        # Lines 1 & 2 (12 px muted BarlowCondensed-Medium)
        line1_text = "Unofficial community plugin · not affiliated with Blendkit, Maxon or the Blender Foundation"
        line2_text = "Assets keep their Blendkit license · no resale or redistribution"
        im_line1 = render_text_png(line1_text, 12, "muted", scale=scale, weight=0)
        im_line1.save(os.path.join(UI_OUT_DIR, f"footer_line1{suffix}.png"))
        im_line2 = render_text_png(line2_text, 12, "muted", scale=scale, weight=0)
        im_line2.save(os.path.join(UI_OUT_DIR, f"footer_line2{suffix}.png"))

    # 8. Settings screen: title, section headings, buttons (same style as inactive tabs)
    for scale, suffix in ((1, ""), (2, "@2x")):
        for key, label in (
            ("download_folder", "Download folder"),
            ("quality", "Download quality"),
            ("blender", "Blender"),
            ("cache", "Cache"),
            ("interface", "Interface"),
        ):
            render_text_png(label, 16, "text", scale=scale).save(os.path.join(UI_OUT_DIR, f"sec_{key}{suffix}.png"))
        for key, label, width, height in (
            ("browse", "Browse", 92, 32),
            ("use_default", "Use default", 110, 26),
            ("auto", "Auto", 64, 26),
            ("clear_cache", "Clear cache", 110, 26),
        ):
            render_button(label, 14, "text", "panel", width, height, outline_color_hex="line", scale=scale).save(
                os.path.join(UI_OUT_DIR, f"btn_{key}{suffix}.png")
            )

    print("\nAll UI assets rendered successfully.")


if __name__ == "__main__":
    main()
