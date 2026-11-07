"""First-run asset usage notice (short form of DISCLAIMER.md)."""

from __future__ import annotations

TEXT = {
    "en": (
        "Kitbridge is an unofficial community tool, not affiliated with BlenderKit, Maxon or the "
        "Blender Foundation.\n\n"
        "It only lets you bring assets you already have access to on BlenderKit into Cinema 4D for "
        "your own work. It gives you no rights to them: every asset keeps its BlenderKit license "
        "(Royalty Free or CC0).\n\n"
        "You may use assets in your own renders, animations, games and commercial projects.\n"
        "You may NOT sell, share or redistribute the assets themselves, including the files "
        "Kitbridge converts (FBX, textures, manifests in the cache).\n"
        "Paid assets need your own BlenderKit subscription. Do not share your account or cache."
    ),
    "ru": (
        "Kitbridge — неофициальный инструмент сообщества, не связанный с BlenderKit, Maxon или "
        "Blender Foundation.\n\n"
        "Он лишь позволяет переносить в Cinema 4D ассеты, к которым у вас уже есть доступ на "
        "BlenderKit, для вашей собственной работы. Прав на ассеты он не даёт: каждый остаётся под "
        "своей лицензией BlenderKit (Royalty Free или CC0).\n\n"
        "Ассеты можно использовать в своих рендерах, анимации, играх и коммерческих проектах.\n"
        "НЕЛЬЗЯ продавать, раздавать или выкладывать сами ассеты, в том числе файлы, которые "
        "сконвертировал Kitbridge (FBX, текстуры, манифесты в кэше).\n"
        "Платные ассеты требуют вашей подписки BlenderKit. Не передавайте другим аккаунт или кэш."
    ),
    "uk": (
        "Kitbridge — неофіційний інструмент спільноти, не пов'язаний з BlenderKit, Maxon чи "
        "Blender Foundation.\n\n"
        "Він лише дає змогу переносити в Cinema 4D ассети, до яких у вас уже є доступ на "
        "BlenderKit, для вашої власної роботи. Прав на ассети він не надає: кожен лишається під "
        "своєю ліцензією BlenderKit (Royalty Free або CC0).\n\n"
        "Ассети можна використовувати у власних рендерах, анімації, іграх і комерційних проєктах.\n"
        "НЕ МОЖНА продавати, поширювати чи викладати самі ассети, зокрема файли, які сконвертував "
        "Kitbridge (FBX, текстури, маніфести в кеші).\n"
        "Платні ассети потребують вашої підписки BlenderKit. Не передавайте іншим акаунт чи кеш."
    ),
}

BUTTON = {"en": "I understand", "ru": "Понятно", "uk": "Зрозуміло"}


def pick_language(code: str) -> str:
    """Map a C4D language code like 'ru-RU' or 'uk-UA' to a TEXT key (fallback 'en')."""
    prefix = (code or "").lower()[:2]
    return prefix if prefix in TEXT else "en"


def current_language() -> str:
    try:
        import c4d

        index = 0
        while True:
            lang = c4d.GeGetLanguage(index)
            if lang is None:
                break
            if lang.get("default_language"):
                return pick_language(str(lang.get("extensions") or ""))
            index += 1
    except Exception:
        pass
    return "en"
