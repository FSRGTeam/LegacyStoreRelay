#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пересобирает catalog.tsv одного шарда из его карточек.

    python3 tools/shard_catalog.py <путь к шарду> [базовый адрес]

Тонкая обёртка над write_catalog из add_app.py — той же самой, которой
пользуется студия. Нужна потому, что сайт на хостинге вызывает сборку каталога
после одобрения заявки, а тащить туда весь add_app.py с его аргументами и
загрузкой файла незачем: ему нужен ровно один шаг.

Базовый адрес можно не передавать — тогда он берётся из relay.tsv, как и везде.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_app import write_catalog, shard_base_url          # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = os.path.abspath(sys.argv[1])
    if not os.path.isdir(os.path.join(path, "apps")):
        print("не похоже на шард: нет %s/apps" % path, file=sys.stderr)
        return 1

    base = sys.argv[2] if len(sys.argv) > 2 else ""
    if not base:
        # Идентификатор шарда — хвост имени каталога: LegacyStoreDC1 -> DC1.
        name = os.path.basename(path)
        base = shard_base_url(name.replace("LegacyStore", "", 1) or name)

    count = write_catalog(path, base)
    print("каталог шарда пересобран: %d приложений, база %s" % (count, base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
