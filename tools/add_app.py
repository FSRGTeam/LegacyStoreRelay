#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add one approved submission to a shard.

    python3 tools/add_app.py --url https://.../app.ipa \\
        --bundle com.example.app --genre Games --author @nick --issue 12

Downloads the file, runs it through ipacheck, pulls the icon out of the bundle,
writes apps/<bundleId>.json and rebuilds the shard's catalog.tsv. Nothing is
published by this script — run tools/build_relay.py afterwards and push.

The .ipa itself is not copied anywhere: the catalog keeps the author's link and
the sha256 of what was checked, so a link that later changes is caught by the
client before it installs anything. Use --keep to store the file in the shard
(files/) for small apps and have the link point at Pages instead.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipacheck import inspect, IPAError          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "LegacyStoreRelay/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def shard_dir(shard):
    """Shards are sibling checkouts: ../LegacyStoreDC1 next to this repo."""
    return os.path.join(os.path.dirname(HERE), "LegacyStore" + shard)


def write_catalog(shard_path, base_url):
    """Rebuild the shard's catalog.tsv from its app cards.

    Regenerated wholesale rather than appended to, so a card edited by hand is
    always what ends up in the catalog.
    """
    apps_dir = os.path.join(shard_path, "apps")
    rows = []
    for name in sorted(os.listdir(apps_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(apps_dir, name), encoding="utf-8") as f:
            a = json.load(f)
        icon = a.get("icon") or ""
        if icon and not icon.startswith("http"):
            icon = base_url.rstrip("/") + "/" + icon.lstrip("/")
        url = a.get("url", "")
        if url and not url.startswith("http"):
            url = base_url.rstrip("/") + "/" + url.lstrip("/")
        rows.append("\t".join([
            "0",                                  # pk: no relikd id for these
            str(a.get("minOS", 0)),
            a.get("title", ""),
            a.get("bundleId", ""),
            a.get("version", ""),
            "url:",                               # column 6: link is ready-made
            url,
            str(a.get("size", 0)),
            a.get("genre", "Uncategorized"),
            "",                                   # no Apple rating, ever
            icon,
            a.get("sha256", ""),
        ]))
    out = os.path.join(shard_path, "catalog.tsv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + ("\n" if rows else ""))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="ссылка на .ipa из заявки")
    ap.add_argument("--bundle", help="bundleId из заявки (сверяется с файлом)")
    ap.add_argument("--shard", default="DC1")
    ap.add_argument("--base-url", default="", help="адрес Pages этого шарда")
    ap.add_argument("--genre", default="Uncategorized")
    ap.add_argument("--author", default="")
    ap.add_argument("--issue", type=int, default=0)
    ap.add_argument("--note", default="")
    ap.add_argument("--keep", action="store_true",
                    help="положить .ipa в шард (files/) вместо ссылки автора")
    args = ap.parse_args()

    path = shard_dir(args.shard)
    if not os.path.isdir(path):
        print("нет шарда %s — создай его через tools/new_shard.py" % path, file=sys.stderr)
        return 2

    tmp = tempfile.mkdtemp(prefix="lsrelay_")
    try:
        local = os.path.join(tmp, "submission.ipa")
        print("качаю %s" % args.url)
        fetch(args.url, local)

        try:
            facts = inspect(local, args.bundle)
        except IPAError as e:
            print("ОТКЛОНЕНО: %s" % e, file=sys.stderr)
            return 1

        digest = sha256_of(local)
        bundle = facts["bundleId"]
        print("принято: %s %s, %s, minOS %d, %.1f МБ"
              % (facts["title"], facts["version"], "/".join(facts["arch"]),
                 facts["minOS"], facts["size"] / 1048576.0))

        icon_rel = ""
        if facts["iconEntry"]:
            os.makedirs(os.path.join(path, "icons"), exist_ok=True)
            ext = os.path.splitext(facts["iconEntry"])[1] or ".png"
            icon_rel = "icons/%s%s" % (bundle, ext)
            with zipfile.ZipFile(local) as zf, \
                 open(os.path.join(path, icon_rel), "wb") as out:
                out.write(zf.read(facts["iconEntry"]))
            print("иконка: %s" % icon_rel)

        url = args.url
        if args.keep:
            os.makedirs(os.path.join(path, "files"), exist_ok=True)
            rel = "files/%s-%s.ipa" % (bundle, facts["version"] or "0")
            shutil.copyfile(local, os.path.join(path, rel))
            url = rel
            print("файл положен в шард: %s" % rel)

        card = {
            "bundleId": bundle,
            "title": facts["title"],
            "version": facts["version"],
            "minOS": facts["minOS"],
            "arch": facts["arch"],
            "size": facts["size"],
            "sha256": digest,
            "url": url,
            "icon": icon_rel,
            "genre": args.genre,
            "author": args.author,
            "issue": args.issue,
            "note": args.note,
        }
        os.makedirs(os.path.join(path, "apps"), exist_ok=True)
        with open(os.path.join(path, "apps", bundle + ".json"), "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2, sort_keys=True)

        base = args.base_url or "https://porns.gitverse.page/LegacyStore%s/" % args.shard
        n = write_catalog(path, base)
        print("каталог шарда пересобран: %d приложений" % n)
        print("дальше: python3 tools/build_relay.py, затем git push в обоих репозиториях")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
