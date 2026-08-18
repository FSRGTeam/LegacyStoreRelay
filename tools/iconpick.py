"""Какой из PNG в бандле — иконка приложения.

Существует затем, что «самый крупный файл, чьё имя начинается с icon» — не
правило, а совпадение. У Senko рядом с настоящей Icon@2x.png лежит
icon-settings-src.png: шестерёнка в 512 точек, исходник для экрана настроек.
По площади она выигрывала у иконки вчетверо, и магазин показывал шестерёнку —
у двух приложений сразу, потому что исходник был один и тот же.

Правильный источник — Info.plist: там перечислено, что автор считает иконкой.
Догадки по имени остаются запасным вариантом для бандлов, где список пуст, но
теперь имя должно быть именно иконочным: Icon.png, Icon@2x.png, Icon-72.png,
AppIcon60x60@2x.png. Всё, что после «icon» продолжается словом, а не размером,
иконкой не считается.
"""

import re

# Icon.png, Icon@2x.png, Icon-72.png, Icon-60@3x.png, AppIcon76x76~ipad.png
_ICON_NAME = re.compile(
    r"^(icon|appicon)"
    r"([-_]?\d+(x\d+)?)?"        # -72, 60x60
    r"(@[23]x)?"
    r"(~(iphone|ipad))?"
    r"\.png$", re.IGNORECASE)


def names_from_info(info):
    """Имена иконок, объявленные в Info.plist, в порядке от новых ключей к старым."""
    names = []
    icons = info.get("CFBundleIcons") if isinstance(info, dict) else None
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            names += list(primary.get("CFBundleIconFiles") or [])
    if isinstance(info, dict):
        names += list(info.get("CFBundleIconFiles") or [])
        if info.get("CFBundleIconFile"):
            names.append(info["CFBundleIconFile"])
    return [n for n in names if isinstance(n, str) and n]


def candidates(info, bundle_files):
    """Кандидаты в иконки среди файлов корня бандла.

    bundle_files — имена файлов, лежащих прямо в .app, без пути.

    Объявленное в Info.plist имеет преимущество: если автор перечислил иконки,
    гадать по именам больше не нужно и вредно — именно там и подбирались чужие
    картинки.
    """
    present = {f.lower(): f for f in bundle_files}
    found = []
    for base in names_from_info(info):
        for suffix in ("", ".png", "@2x.png", "@3x.png", "~ipad.png", "@2x~ipad.png"):
            real = present.get((base + suffix).lower())
            if real and real not in found:
                found.append(real)
    if found:
        return found

    return [f for f in bundle_files
            if _ICON_NAME.match(f) and "-small" not in f.lower()]


def largest(entries, size_of):
    """Самая крупная из кандидаток: магазин рисует иконку на 128 точках при
    удвоенной плотности, и 57×57 там расплывается."""
    best, best_area = None, 0
    for entry in entries:
        size = size_of(entry)
        if not size:
            continue
        area = size[0] * size[1]
        if area > best_area:
            best, best_area = entry, area
    return best
