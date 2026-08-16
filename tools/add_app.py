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
import devkeys                                  # noqa: E402
from ipacheck import inspect, IPAError          # noqa: E402
from debcheck import inspect as inspect_deb, DebError, _ar_members, _bundled_app_icon  # noqa: E402
from iosimage import load_cgbi_rgba             # noqa: E402


RELAY_TSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "relay.tsv")


def shard_base_url(shard_id):
    """Адрес, по которому раздаётся шард.

    Источник правды — колонка base_url в relay.tsv, а не шаблон в коде. Шарды
    живут на разных хостингах: у GitVerse квота артефактов считается на весь
    аккаунт и списывает вес всего сайта при каждой публикации, поэтому тяжёлые
    шарды с .ipa переехали на GitHub Pages, а релей остался там, где у iOS 5
    проверяемая цепочка сертификатов. Зашитый шаблон это бы просто сломал.

    Шарда ещё нет в relay.tsv — значит он новый; отдаём прежний адрес GitVerse,
    и build_relay.py впишет его в таблицу.
    """
    try:
        with open(RELAY_TSV, encoding="utf-8") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) > 2 and parts[0] == shard_id and parts[2].strip():
                    return parts[2].strip()
    except OSError:
        pass
    return "https://fsrgteam.gitverse.site/legacystore%s/" % shard_id.lower()


def write_icon(blob, dest):
    """Кладёт иконку так, чтобы её читали все.

    Артворк из бандла iOS — это CgBI: формально PNG, а на деле поток без
    zlib-заголовка и каналы BGRA. Устройство декодирует его нативно, но браузер
    и любая десктопная библиотека — нет, и на сайте такая иконка не появляется
    вовсе. Поэтому при публикации CgBI разбирается и пересохраняется обычным
    PNG: устройству всё равно, а витрине — нет.
    """
    with open(dest, "wb") as f:
        f.write(blob)
    got = load_cgbi_rgba(dest)
    if not got:
        return dest                      # обычный PNG — трогать нечего
    try:
        from PIL import Image
    except ImportError:
        print("CgBI не пересохранён: нет Pillow", file=sys.stderr)
        return dest
    w, h, rgba = got
    Image.frombytes("RGBA", (w, h), rgba).save(dest, "PNG", optimize=True)
    print("иконка пересохранена из CgBI: %dx%d" % (w, h))
    return dest

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


# Screenshots are the author's, not something that can be pulled out of an
# .ipa — a bundle carries no marketing shots. A local file is copied into the
# shard and served from Pages; a URL is trusted as given, which is how an app
# already listed on the App Store can point at Apple's CDN over plain http.
def collect_shots(shard_path, bundle, sources):
    out = []
    if not sources:
        return out
    dest_dir = os.path.join(shard_path, "shots", bundle)
    for i, src in enumerate(sources, 1):
        if src.startswith("http://") or src.startswith("https://"):
            out.append(src)
            continue
        if not os.path.isfile(src):
            print("нет файла скриншота: %s" % src, file=sys.stderr)
            continue
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower() or ".jpg"
        rel = "shots/%s/%d%s" % (bundle, i, ext)
        shutil.copyfile(src, os.path.join(shard_path, rel))
        out.append(rel)
    return out


def write_catalog(shard_path, base_url):
    """Rebuild the shard's catalog.tsv from its app cards.

    Regenerated wholesale rather than appended to, so a card edited by hand is
    always what ends up in the catalog.
    """
    def absolute(u, base):
        return u if u.startswith("http") else base.rstrip("/") + "/" + u.lstrip("/")

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
            a.get("author", ""),
            ",".join(absolute(s, base_url) for s in (a.get("shots") or [])),
            (a.get("quote") or "").replace("\t", " "),
            (a.get("by") or "").replace("\t", " "),
            # Переносы строк в TSV невозможны, поэтому абзацы едут символом
            # \u2028 и разворачиваются обратно на устройстве.
            (a.get("desc") or "").replace("\t", " ").replace("\n", "\u2028"),
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
    ap.add_argument("--shot", action="append", default=[], metavar="ПУТЬ|URL",
                    help="скриншот: локальный файл кладётся в шард, ссылка "
                         "берётся как есть. Можно повторять.")
    ap.add_argument("--quote", default="", metavar="ТЕКСТ",
                    help="слова автора для баннера (одна-две строки)")
    ap.add_argument("--by", default="", metavar="ПОДПИСЬ",
                    help="кто это сказал: имя или команда")
    ap.add_argument("--desc", default="", metavar="ТЕКСТ",
                    help="описание приложения словами автора")
    ap.add_argument("--icon", default="", metavar="ПУТЬ",
                    help="своя иконка вместо вынутой из бандла")
    ap.add_argument("--token", default="", metavar="LSD1...",
                    help="ключ разработчика: подтверждает, что заявка от него")
    ap.add_argument("--no-token", action="store_true",
                    help="опубликовать без ключа (только для своих приложений)")
    args = ap.parse_args()

    if not args.token and not args.no_token:
        print("нужен --token разработчика (или --no-token, если приложение своё)",
              file=sys.stderr)
        return 2

    path = shard_dir(args.shard)
    if not os.path.isdir(path):
        print("нет шарда %s — создай его через tools/new_shard.py" % path, file=sys.stderr)
        return 2

    tmp = tempfile.mkdtemp(prefix="lsrelay_")
    try:
        # Расширение решает, чем это проверять и чем потом ставить: .ipa идёт
        # через installd, .deb — через dpkg, и общего у них только то, что оба
        # приезжают по ссылке.
        is_deb = args.url.lower().split("?")[0].endswith(".deb")
        local = os.path.join(tmp, "submission." + ("deb" if is_deb else "ipa"))
        print("качаю %s" % args.url)
        fetch(args.url, local)

        try:
            facts = inspect_deb(local, args.bundle) if is_deb \
                    else inspect(local, args.bundle)
        except (IPAError, DebError) as e:
            print("ОТКЛОНЕНО: %s" % e, file=sys.stderr)
            return 1

        digest = sha256_of(local)
        bundle = facts["bundleId"]
        print("принято: %s %s, %s, minOS %d, %.1f МБ"
              % (facts["title"], facts["version"], "/".join(facts["arch"]),
                 facts["minOS"], facts["size"] / 1048576.0))

        # Ключ проверяется после разбора файла, а не до: права выданы на
        # bundleId, а настоящий bundleId известен только из самого бандла.
        # Верить тому, что написано в заявке, — значит проверять не то.
        developer = ""
        if args.token:
            try:
                who = devkeys.parse_token(args.token, bundle=bundle)
            except devkeys.DevKeyError as e:
                print("ОТКЛОНЕНО: ключ разработчика — %s" % e, file=sys.stderr)
                return 1
            developer = who["handle"]
            print("подписано: %s (права «%s», до %s)"
                  % (developer, who["scope"], who["expires"].isoformat()))
        else:
            print("без ключа разработчика (--no-token)")

        icon_rel = ""
        # Своя иконка бьёт вынутую из бандла: в старых сборках она бывает
        # только 57×57, а магазин рисует её на 128 точках при удвоенной
        # плотности — то есть растягивает вчетверо.
        if args.icon:
            if not os.path.isfile(args.icon):
                print("нет файла иконки: %s" % args.icon, file=sys.stderr)
                return 1
            os.makedirs(os.path.join(path, "icons"), exist_ok=True)
            ext = os.path.splitext(args.icon)[1].lower() or ".png"
            icon_rel = "icons/%s%s" % (bundle, ext)
            shutil.copyfile(args.icon, os.path.join(path, icon_rel))
            print("иконка своя: %s" % icon_rel)
        elif is_deb:
            # У пакета иконки может не быть вовсе — он бывает твиком или
            # библиотекой. Если внутри есть .app, берём оттуда самую крупную.
            with open(local, "rb") as f:
                blob, name = _bundled_app_icon(_ar_members(f.read()))
            if blob:
                os.makedirs(os.path.join(path, "icons"), exist_ok=True)
                icon_rel = "icons/%s.png" % bundle
                write_icon(blob, os.path.join(path, icon_rel))
                print("иконка из пакета: %s (%s)" % (icon_rel, name))
            else:
                print("иконки в пакете нет — задайте своей через --icon")
        elif facts["iconEntry"]:
            os.makedirs(os.path.join(path, "icons"), exist_ok=True)
            ext = os.path.splitext(facts["iconEntry"])[1] or ".png"
            icon_rel = "icons/%s%s" % (bundle, ext)
            with zipfile.ZipFile(local) as zf:
                write_icon(zf.read(facts["iconEntry"]), os.path.join(path, icon_rel))
            print("иконка: %s" % icon_rel)

        url = args.url
        if args.keep:
            os.makedirs(os.path.join(path, "files"), exist_ok=True)
            rel = "files/%s-%s.%s" % (bundle, facts["version"] or "0",
                                      "deb" if is_deb else "ipa")
            shutil.copyfile(local, os.path.join(path, rel))
            url = rel
            print("файл положен в шард: %s" % rel)

            # Прошлые сборки того же приложения удаляются: каталог ссылается
            # ровно на один файл, а каждая публикация шарда целиком уходит в
            # артефакт Pages, где на репозиторий отведено 500 МБ на 30 дней.
            # Забытая версия — это минус одна публикация из девяти.
            keep_name = os.path.basename(rel)
            files_dir = os.path.join(path, "files")
            for old in sorted(os.listdir(files_dir)):
                if old == keep_name:
                    continue
                stem, ext = os.path.splitext(old)
                if ext.lower() in (".ipa", ".deb") and stem.startswith(bundle + "-"):
                    os.remove(os.path.join(files_dir, old))
                    print("убрана прошлая сборка: files/%s" % old)

        shots = collect_shots(path, bundle, args.shot)
        if shots:
            print("скриншотов: %d" % len(shots))

        # Trimmed here rather than in the client: a banner has room for about
        # this much, and cutting words on the device would cut them differently
        # on a phone and on an iPad.
        quote = " ".join(args.quote.split())
        if len(quote) > 130:
            quote = quote[:129].rsplit(" ", 1)[0] + "…"
            print("цитата обрезана до 130 символов")

        card = {
            "bundleId": bundle,
            "kind": "deb" if is_deb else "ipa",
            "shots": shots,
            "quote": quote,
            "by": args.by,
            "desc": " ".join(args.desc.split()),
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
            # Ник из ключа, а не из --author: тот пишется руками и означает
            # «кто себя так назвал», а этот — «чей ключ это подтвердил».
            "developer": developer,
        }
        os.makedirs(os.path.join(path, "apps"), exist_ok=True)
        with open(os.path.join(path, "apps", bundle + ".json"), "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2, sort_keys=True)

        base = args.base_url or shard_base_url(args.shard)
        n = write_catalog(path, base)
        print("каталог шарда пересобран: %d приложений" % n)
        print("дальше: python3 tools/build_relay.py, затем git push в обоих репозиториях")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
