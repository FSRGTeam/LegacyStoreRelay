#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка .deb и факты о нём для каталога.

Пара к ipacheck.py, но пакет устроен иначе: это ar-архив, внутри которого
control.tar.gz с описанием и data.tar.gz с файлами. Ни Info.plist, ни срезов
архитектуры здесь может не быть вовсе — .deb бывает твиком, библиотекой или
набором ресурсов, и это нормально.

Что можно проверить честно: что это действительно ar-архив дебиановского вида,
что в нём есть control с именем пакета и версией, и что архитектура не спорит с
устройством. Что делает содержимое — не отвечает и этот скрипт.

    python3 tools/debcheck.py package.deb
"""

import argparse
import io
import json
import os
import re
import sys
import tarfile


class DebError(Exception):
    pass


def _ar_members(data):
    """Имя -> тело для членов ar-архива."""
    if not data.startswith(b"!<arch>\n"):
        raise DebError("это не .deb (нет сигнатуры ar)")
    pos = 8
    out = {}
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[0:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii", "replace").strip() or 0)
        except ValueError:
            break
        body = data[pos + 60:pos + 60 + size]
        out[name.rstrip("/")] = body
        pos += 60 + size + (size % 2)      # члены выравниваются по чётной границе
    return out


def _open_tar(name, body):
    mode = "r:gz"
    if name.endswith(".xz"):
        mode = "r:xz"
    elif name.endswith(".bz2"):
        mode = "r:bz2"
    elif name.endswith(".tar"):
        mode = "r:"
    return tarfile.open(fileobj=io.BytesIO(body), mode=mode)


def _control_fields(members):
    for name, body in members.items():
        if not name.startswith("control.tar"):
            continue
        with _open_tar(name, body) as tar:
            for member in tar.getmembers():
                if os.path.basename(member.name) != "control":
                    continue
                text = tar.extractfile(member).read().decode("utf-8", "replace")
                fields, key = {}, None
                for line in text.split("\n"):
                    if line.startswith((" ", "\t")) and key:
                        fields[key] += " " + line.strip()
                    elif ":" in line:
                        key, value = line.split(":", 1)
                        key = key.strip()
                        fields[key] = value.strip()
                return fields
    raise DebError("в пакете нет control")


def _min_os(depends):
    """'firmware (>= 5.0)' -> 50000."""
    if not depends:
        return 0
    m = re.search(r"firmware\s*\(\s*>=\s*([0-9.]+)\s*\)", depends)
    if not m:
        return 0
    parts = (m.group(1).split(".") + ["0", "0"])[:3]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    return nums[0] * 10000 + nums[1] * 100 + nums[2]


def _bundled_app_icon(members):
    """Самая крупная иконка из .app внутри пакета, если он ставит приложение."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from iosimage import png_size

    best, best_area, best_name = None, 0, None
    for name, body in members.items():
        if not name.startswith("data.tar"):
            continue
        with _open_tar(name, body) as tar:
            for member in tar.getmembers():
                low = member.name.lower()
                if ".app/" not in low or not low.endswith(".png"):
                    continue
                # Имя файла внутри .app, а не путь: "Icon@2x.png" лежит прямо
                # в корне бандла, и искать в нём "/icon" бессмысленно.
                if not low.rsplit(".app/", 1)[1].startswith("icon"):
                    continue
                blob = tar.extractfile(member).read()
                size = png_size(blob)
                if not size:
                    continue
                area = size[0] * size[1]
                if area > best_area:
                    best, best_area, best_name = blob, area, member.name
    return best, best_name


def inspect(path, expect_package=None, max_min_os=60103):
    with open(path, "rb") as f:
        data = f.read()
    members = _ar_members(data)
    fields = _control_fields(members)

    package = fields.get("Package", "")
    if not package:
        raise DebError("в control нет поля Package")
    if expect_package and package != expect_package:
        raise DebError("имя пакета не совпадает с заявкой: в файле %s, в заявке %s"
                       % (package, expect_package))

    arch = fields.get("Architecture", "")
    if arch and arch not in ("iphoneos-arm", "all", "darwin-arm"):
        raise DebError("архитектура %s — пакет не для этих устройств" % arch)

    min_os = _min_os(fields.get("Depends", ""))
    if min_os > max_min_os:
        raise DebError("Depends требует firmware выше целевой")

    return {
        "bundleId": package,
        "title": fields.get("Name") or package,
        "version": fields.get("Version", ""),
        "minOS": min_os,
        "arch": [arch] if arch else [],
        "size": os.path.getsize(path),
        "section": fields.get("Section", ""),
        "depends": fields.get("Depends", ""),
        "kind": "deb",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deb")
    ap.add_argument("--package", help="имя пакета, заявленное автором")
    ap.add_argument("--max-min-os", type=int, default=60103)
    args = ap.parse_args()
    try:
        facts = inspect(args.deb, args.package, args.max_min_os)
    except DebError as e:
        print("ОТКЛОНЕНО: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
