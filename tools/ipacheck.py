#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Structural validation of a submitted .ipa, and the facts the catalog needs.

This is the desktop half of what FSIPAInspector does on the device, and it runs
before anything is published rather than after it is downloaded.

What it can answer: is this a real archive, is there an app inside it, does the
bundle id match what was claimed, will this binary run on an armv7 device, and
is MinimumOSVersion within reach of the target. What it cannot answer is what
the binary does once installed — that stays a human decision.

    python3 tools/ipacheck.py app.ipa [--bundle com.example.app]
"""

import argparse
import json
import os
import plistlib
import struct
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from iosimage import png_size          # noqa: E402

# Mach-O cpu subtypes we care about, all under CPU_TYPE_ARM (12).
ARM_SUBTYPES = {6: "armv6", 9: "armv7", 11: "armv7s"}
FAT_MAGIC = 0xCAFEBABE
MACHO_MAGIC_32 = 0xFEEDFACE
MACHO_CIGAM_32 = 0xCEFAEDFE


class IPAError(Exception):
    pass


def load_plist(raw):
    """XML, binary, or the old NeXT/OpenStep ASCII format.

    plistlib handles the first two. The third — `{ key = value; }` — is what
    Theos writes by default, so it is what a homebrew submission actually
    arrives in; rejecting it would turn away exactly the audience this store is
    for. Only the subset a bundle Info.plist uses is parsed: nested dicts,
    arrays, quoted and bare strings. <data> blocks are skipped rather than
    decoded, since nothing here reads one.
    """
    try:
        return plistlib.loads(raw)
    except Exception:
        pass
    try:
        return _parse_ascii_plist(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise IPAError("Info.plist не читается ни как XML/binary, ни как ASCII: %s" % e)


def _parse_ascii_plist(text):
    pos = [0]

    def skip():
        while pos[0] < len(text):
            c = text[pos[0]]
            if c in " \t\r\n":
                pos[0] += 1
            elif text.startswith("//", pos[0]):
                end = text.find("\n", pos[0])
                pos[0] = len(text) if end < 0 else end + 1
            elif text.startswith("/*", pos[0]):
                end = text.find("*/", pos[0] + 2)
                pos[0] = len(text) if end < 0 else end + 2
            else:
                return

    def value():
        skip()
        if pos[0] >= len(text):
            raise ValueError("неожиданный конец файла")
        c = text[pos[0]]
        if c == "{":
            pos[0] += 1
            out = {}
            while True:
                skip()
                if pos[0] < len(text) and text[pos[0]] == "}":
                    pos[0] += 1
                    return out
                k = value()
                skip()
                if pos[0] < len(text) and text[pos[0]] == "=":
                    pos[0] += 1
                out[k] = value()
                skip()
                if pos[0] < len(text) and text[pos[0]] == ";":
                    pos[0] += 1
        if c == "(":
            pos[0] += 1
            out = []
            while True:
                skip()
                if pos[0] < len(text) and text[pos[0]] == ")":
                    pos[0] += 1
                    return out
                out.append(value())
                skip()
                if pos[0] < len(text) and text[pos[0]] == ",":
                    pos[0] += 1
        if c == "<":
            end = text.find(">", pos[0])
            pos[0] = len(text) if end < 0 else end + 1
            return b""
        if c == '"':
            pos[0] += 1
            buf = []
            while pos[0] < len(text):
                ch = text[pos[0]]
                if ch == "\\":
                    pos[0] += 1
                    buf.append(text[pos[0]] if pos[0] < len(text) else "")
                elif ch == '"':
                    pos[0] += 1
                    return "".join(buf)
                else:
                    buf.append(ch)
                pos[0] += 1
            raise ValueError("незакрытая кавычка")
        start = pos[0]
        while pos[0] < len(text) and text[pos[0]] not in ' \t\r\n=;,(){}"':
            pos[0] += 1
        if start == pos[0]:
            raise ValueError("пустой токен на позиции %d" % start)
        return text[start:pos[0]]

    root = value()
    if not isinstance(root, dict):
        raise ValueError("корень не словарь")
    return root


def _app_root(zf):
    """Payload/<Something>.app/ — the prefix everything else hangs off."""
    for name in zf.namelist():
        parts = name.split("/")
        if len(parts) >= 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
            return "Payload/%s/" % parts[1]
    raise IPAError("внутри нет Payload/*.app — это не .ipa")


def _arches(data):
    """Architecture names in a Mach-O or fat binary."""
    if len(data) < 8:
        return []
    magic = struct.unpack(">I", data[:4])[0]

    if magic == FAT_MAGIC:
        count = struct.unpack(">I", data[4:8])[0]
        out = []
        for i in range(count):
            off = 8 + i * 20
            if off + 8 > len(data):
                break
            cputype, subtype = struct.unpack(">ii", data[off:off + 8])
            if cputype == 12:
                out.append(ARM_SUBTYPES.get(subtype & 0xFF, "arm?%d" % (subtype & 0xFF)))
        return out

    le = struct.unpack("<I", data[:4])[0]
    if le == MACHO_MAGIC_32:
        cputype, subtype = struct.unpack("<ii", data[4:12])
        if cputype == 12:
            return [ARM_SUBTYPES.get(subtype & 0xFF, "arm?%d" % (subtype & 0xFF))]
    return []


def _min_os_int(value):
    """'5.1.1' -> 50101, matching the catalog's minOS column."""
    if value is None:
        return 0
    parts = str(value).split(".")
    nums = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0] * 10000 + nums[1] * 100 + nums[2]


def inspect(path, expect_bundle=None, max_min_os=60103):
    if not zipfile.is_zipfile(path):
        raise IPAError("файл не является zip-архивом")

    with zipfile.ZipFile(path) as zf:
        root = _app_root(zf)
        try:
            raw = zf.read(root + "Info.plist")
        except KeyError:
            raise IPAError("в бандле нет Info.plist")

        info = load_plist(raw)

        bundle = info.get("CFBundleIdentifier", "")
        if not bundle:
            raise IPAError("в Info.plist нет CFBundleIdentifier")
        if expect_bundle and bundle != expect_bundle:
            raise IPAError("bundleId не совпадает с заявкой: в файле %s, в заявке %s"
                           % (bundle, expect_bundle))

        exe = info.get("CFBundleExecutable")
        if not exe:
            raise IPAError("в Info.plist нет CFBundleExecutable")
        try:
            arches = _arches(zf.read(root + exe)[:4096])
        except KeyError:
            raise IPAError("исполняемый файл %s отсутствует в бандле" % exe)
        if not arches:
            raise IPAError("исполняемый файл не Mach-O под ARM")
        if not ({"armv6", "armv7"} & set(arches)):
            raise IPAError("нет среза armv6/armv7, только %s — на iPhone 4 не пойдёт"
                           % ", ".join(arches))

        min_os = _min_os_int(info.get("MinimumOSVersion"))
        if min_os > max_min_os:
            raise IPAError("MinimumOSVersion %s выше целевой"
                           % info.get("MinimumOSVersion"))

        icon = _icon_name(info, zf, root)

        return {
            "bundleId": bundle,
            "title": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle,
            "version": info.get("CFBundleShortVersionString")
                       or info.get("CFBundleVersion") or "",
            "minOS": min_os,
            "arch": arches,
            "size": os.path.getsize(path),
            "iconEntry": icon,
            "appRoot": root,
        }


def _icon_name(info, zf, root):
    """Самая крупная иконка приложения. Возвращается путь внутри архива.

    Выбор по имени не работает: в бандле лежат и Icon.png на 57 точек, и
    Icon-167.png, и Icon-60@3x.png — а порядок в CFBundleIconFiles ничего не
    говорит о размере. Магазин показывает иконку на 128 точек при удвоенной
    плотности, поэтому берётся самая большая, и это разница между чёткой
    картинкой и мылом.

    Файлы не перекодируются: iOS хранит их в варианте CgBI, который десктопные
    библиотеки не читают, а устройство декодирует нативно.
    """
    names = []
    icons = info.get("CFBundleIcons")
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            names += list(primary.get("CFBundleIconFiles") or [])
    names += list(info.get("CFBundleIconFiles") or [])
    if info.get("CFBundleIconFile"):
        names.append(info["CFBundleIconFile"])

    entries = set(zf.namelist())
    candidates = set()
    for base in names:
        for cand in (base, base + ".png", base + "@2x.png", base + "@3x.png"):
            if root + cand in entries:
                candidates.add(root + cand)

    # Плюс всё, что в корне бандла названо иконкой: у приложений вроде этого
    # половина размеров в Info.plist не перечислена вовсе.
    for entry in entries:
        rest = entry[len(root):] if entry.startswith(root) else ""
        if "/" in rest or not rest.lower().endswith(".png"):
            continue
        low = rest.lower()
        if low.startswith("icon") and "-small" not in low:
            candidates.add(entry)

    best, best_area = None, 0
    for entry in candidates:
        size = png_size(zf.read(entry))
        if not size:
            continue
        area = size[0] * size[1]
        if area > best_area:
            best, best_area = entry, area
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ipa")
    ap.add_argument("--bundle", help="bundleId, заявленный автором")
    ap.add_argument("--max-min-os", type=int, default=60103,
                    help="потолок minOS в формате каталога (по умолчанию 6.1.3)")
    args = ap.parse_args()

    try:
        facts = inspect(args.ipa, args.bundle, args.max_min_os)
    except IPAError as e:
        print("ОТКЛОНЕНО: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
