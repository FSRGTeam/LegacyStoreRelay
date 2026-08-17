#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Достаёт иконку из .ipa или .deb и кладёт обычным PNG.

    python3 tools/extract_icon.py <файл> <куда.png>

Нужен сайту: после одобрения заявки иконку надо положить в шард, а вся логика
её поиска уже написана — в ipacheck.py для .ipa и в debcheck.py для .deb. Здесь
только склейка, чтобы вызывать это одной командой.

Иконки может не быть вовсе: .deb бывает твиком или библиотекой. Это не ошибка —
код возврата 1 и молчание, карточка обойдётся без картинки.
"""

import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_app import write_icon                                    # noqa: E402
from debcheck import _ar_members, _bundled_app_icon               # noqa: E402
from ipacheck import inspect as inspect_ipa                       # noqa: E402


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dest = sys.argv[1], sys.argv[2]
    if not os.path.isfile(src):
        print("нет файла: %s" % src, file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

    try:
        if src.lower().endswith(".deb"):
            with open(src, "rb") as f:
                blob, name = _bundled_app_icon(_ar_members(f.read()))
            if not blob:
                print("иконки в пакете нет", file=sys.stderr)
                return 1
            write_icon(blob, dest)
            print(name)
            return 0

        facts = inspect_ipa(src)
        entry = facts.get("iconEntry")
        if not entry:
            print("иконки в бандле нет", file=sys.stderr)
            return 1
        with zipfile.ZipFile(src) as zf:
            write_icon(zf.read(entry), dest)
        print(entry)
        return 0
    except Exception as exc:                                       # noqa: BLE001
        # Сюда попадают битые архивы и бандлы без Info.plist. Для вызывающей
        # стороны это то же самое, что «иконки нет»: заявку из-за картинки не
        # отклоняют.
        print("иконку достать не вышло: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
