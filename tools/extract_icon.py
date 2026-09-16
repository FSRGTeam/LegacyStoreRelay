#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Достаёт иконку из .ipa или .deb и кладёт обычным PNG.

    python3 tools/extract_icon.py <файл> <куда.png>

Нужен сайту: после одобрения заявки иконку надо положить в шард, а вся логика
её поиска уже написана — в ipacheck.py для .ipa и в debcheck.py для .deb. Здесь
только склейка, чтобы вызывать это одной командой.

Иконки может не быть вовсе: .deb бывает твиком или библиотекой. Это не ошибка —
код возврата 1 и молчание, карточка обойдётся без картинки.

Отдельный случай — пакет-установщик: внутри нет ни одного .app, зато лежит
готовая .ipa, которую он и ставит. Так собран LegacyGram 0.0.5 и так будет
собрано всё, что уходит от Theos. Для витрины это обычное приложение, и иконка
у него есть — просто на слой глубже.
"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_app import write_icon                                    # noqa: E402
from debcheck import _ar_members, _bundled_app_icon, _open_tar    # noqa: E402
from ipacheck import inspect as inspect_ipa                       # noqa: E402


def _nested_ipa_icon(members):
    """Иконка из .ipa, которую пакет несёт с собой.

    Пакет-установщик кладёт .ipa куда-нибудь в /usr/share и ставит её сам. В
    таком .deb нет ни одного .app, поэтому обычный поиск не находит ничего и
    честно говорит «иконки нет» — а она есть, просто внутри вложенного архива.

    Берём самую большую .ipa из всех, что нашлись: пакет может нести и
    какую-нибудь мелочь вроде заглушки, а приложение — это самый крупный
    архив в нём.
    """
    best = None
    for name, body in members.items():
        if not name.startswith("data.tar"):
            continue
        with _open_tar(name, body) as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.lower().endswith(".ipa"):
                    continue
                if best is None or member.size > best[0]:
                    best = (member.size, tar.extractfile(member).read())
    if not best:
        return None, None

    # Разбор .ipa живёт в ipacheck и работает с файлом на диске. Кладём
    # вложенный архив во временный файл и спрашиваем его же: свой поиск иконки
    # здесь означал бы второй набор правил, который разойдётся с первым.
    tmp = tempfile.NamedTemporaryFile(suffix=".ipa", delete=False)
    try:
        tmp.write(best[1])
        tmp.close()
        entry = (inspect_ipa(tmp.name) or {}).get("iconEntry")
        if not entry:
            return None, None
        with zipfile.ZipFile(tmp.name) as zf:
            return zf.read(entry), entry
    finally:
        os.unlink(tmp.name)


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
                members = _ar_members(f.read())
            blob, name = _bundled_app_icon(members)
            if not blob:
                # Ни одного .app — возможно, это установщик со вложенной .ipa.
                blob, name = _nested_ipa_icon(members)
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
