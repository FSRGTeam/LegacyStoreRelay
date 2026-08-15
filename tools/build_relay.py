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

    gen = generation() + 1
    with open(VERSION, "w", encoding="utf-8") as f:
        f.write("generation=%d\n" % gen)
        f.write("updated=%s\n" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        f.write("catalog_sha256=%s\n" % hashlib.sha256(body.encode("utf-8")).hexdigest())
        f.write("catalog_bytes=%d\n" % len(body.encode("utf-8")))
        f.write("shards=%d\n" % len([r for r in rows if r[6] != "gone"]))

    if changed_relay:
        with open(RELAY, "w", encoding="utf-8") as f:
            f.write("# id\tname\tbase_url\tapps\tupdated\tsha256\tstatus\n")
            for r in rows:
                f.write("\t".join(r[:7]) + "\n")

    print("catalog-all.tsv: %d строк, %d байт%s"
          % (len(unique), len(body.encode("utf-8")),
             (", дубликатов отброшено %d" % dropped) if dropped else ""))
    print("поколение %d" % gen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
