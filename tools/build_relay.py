#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble catalog-all.tsv and version.txt from the shards listed in relay.tsv.

    python3 tools/build_relay.py

Shards are sibling checkouts of this repository: ../LegacyStoreDC1 and so on.
A shard that is not checked out locally keeps whatever the previous build put in
the merged catalog only if --strict is off; with --strict a missing shard is an
error, because silently publishing a catalog with a third of the apps gone is
worse than not publishing at all.
"""

import argparse
import datetime
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(HERE, "relay.tsv")
CATALOG = os.path.join(HERE, "catalog-all.tsv")
VERSION = os.path.join(HERE, "version.txt")
FEATURED_CONF = os.path.join(HERE, "featured.conf")
FEATURED = os.path.join(HERE, "featured.tsv")
# Адрес счётчика установок. Файла нет — строки в version.txt нет, и устройства
# ничего никуда не отправляют: это и есть выключатель сбора статистики, общий
# для всех устройств сразу и не требующий новой сборки магазина.
STATS_URL = os.path.join(HERE, "stats.url")

# Адрес оценок. Их держит сайт, а не этот репозиторий: рейтинг меняется от
# каждой поставленной звезды, и коммит на каждую был бы издевательством и над
# историей, и над GitHub Pages. Файла нет — строки в version.txt нет, и
# магазин рисует звёзды только по данным Apple, как и раньше.
RATINGS_URL = os.path.join(HERE, "ratings.url")


def read_relay():
    rows = []
    with open(RELAY, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            while len(parts) < 7:
                parts.append("")
            rows.append(parts)
    return rows


def read_url_file(path, what):
    """Публичный адрес из однострочного файла, если он вообще есть.

    Локальный адрес сюда класть нельзя: version.txt читают все устройства, и
    192.168.x на чужом телефоне — это восемь секунд ожидания после каждой
    установки впустую. Только публичный адрес или ничего.
    """
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            if not url.startswith(("http://", "https://")):
                print("%s: не адрес, пропускаю: %s" % (what, url), file=sys.stderr)
                return ""
            return url
    return ""


def generation():
    """Reads the current counter so a rebuild always moves it forward."""
    if not os.path.exists(VERSION):
        return 0
    with open(VERSION, encoding="utf-8") as f:
        for line in f:
            if line.startswith("generation="):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    return 0
    return 0


# The banner rotation: curated order in, verified order out.
#
# A bundleId that is not in the catalog is dropped rather than published, so a
# typo here cannot become an empty banner on someone's phone. The words on the
# banner are not here at all — they travel with the app, from its author.
def build_featured(catalog_lines):
    if not os.path.exists(FEATURED_CONF):
        return []
    known = set()
    for line in catalog_lines:
        parts = line.split("\t")
        if len(parts) > 3 and parts[3]:
            known.add(parts[3].lower())

    out, missing = [], []
    with open(FEATURED_CONF, encoding="utf-8") as f:
        for line in f:
            bid = line.strip()
            if not bid or bid.startswith("#"):
                continue
            (out if bid.lower() in known else missing).append(bid)
    if missing:
        print("не в каталоге, пропущены: %s" % ", ".join(missing))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="ошибка, если шард не найден локально")
    args = ap.parse_args()

    rows = read_relay()
    merged = []
    parent = os.path.dirname(HERE)
    changed_relay = False

    for r in rows:
        shard_id, name, base_url, apps, updated, digest, status = r[:7]
        if status == "gone":
            continue
        path = os.path.join(parent, "LegacyStore" + shard_id, "catalog.tsv")
        if not os.path.exists(path):
            msg = "шард %s не найден локально (%s)" % (shard_id, path)
            if args.strict:
                print("ОШИБКА: " + msg, file=sys.stderr)
                return 1
            print("пропускаю: " + msg)
            continue

        with open(path, encoding="utf-8") as f:
            body = f.read()
        lines = [l for l in body.split("\n") if l.strip()]
        merged.extend(lines)

        r[3] = str(len(lines))
        r[4] = datetime.date.today().isoformat()
        r[5] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        changed_relay = True
        print("%-5s %-16s %4d приложений" % (shard_id, name, len(lines)))

    # One bundleId may exist in several shards (an author moving a build, a
    # frozen shard still holding an old copy). The first shard listed wins, so
    # relay.tsv order is also the precedence order.
    seen = set()
    unique = []
    for line in merged:
        parts = line.split("\t")
        key = (parts[3] if len(parts) > 3 else line, parts[4] if len(parts) > 4 else "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    dropped = len(merged) - len(unique)

    body = "\n".join(unique) + ("\n" if unique else "")
    with open(CATALOG, "w", encoding="utf-8") as f:
        f.write(body)

    featured = build_featured(unique)
    featured_body = "\n".join(featured) + ("\n" if featured else "")
    with open(FEATURED, "w", encoding="utf-8") as f:
        f.write(featured_body)

    gen = generation() + 1
    with open(VERSION, "w", encoding="utf-8") as f:
        f.write("generation=%d\n" % gen)
        f.write("updated=%s\n" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        f.write("catalog_sha256=%s\n" % hashlib.sha256(body.encode("utf-8")).hexdigest())
        f.write("catalog_bytes=%d\n" % len(body.encode("utf-8")))
        f.write("shards=%d\n" % len([r for r in rows if r[6] != "gone"]))
        f.write("featured_sha256=%s\n"
                % hashlib.sha256(featured_body.encode("utf-8")).hexdigest())
        f.write("featured_count=%d\n" % len(featured))
        stats_url = read_url_file(STATS_URL, "stats.url")
        if stats_url:
            f.write("stats_url=%s\n" % stats_url)
        ratings_url = read_url_file(RATINGS_URL, "ratings.url")
        if ratings_url:
            f.write("ratings_url=%s\n" % ratings_url)

    if changed_relay:
        with open(RELAY, "w", encoding="utf-8") as f:
            f.write("# id\tname\tbase_url\tapps\tupdated\tsha256\tstatus\n")
            for r in rows:
                f.write("\t".join(r[:7]) + "\n")

    print("catalog-all.tsv: %d строк, %d байт%s"
          % (len(unique), len(body.encode("utf-8")),
             (", дубликатов отброшено %d" % dropped) if dropped else ""))
    print("на баннерах: %d" % len(featured))
    print("поколение %d" % gen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
