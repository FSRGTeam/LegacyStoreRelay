#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Legacy Store Studio — вся публикация приложения в одном окне.

Обёртка над tools/: проверка .ipa, карточка в шарде, очередь баннеров, сборка
релея, отправка в GitVerse и ожидание раскатки Pages. Скрипты остаются главными
— студия их запускает и показывает их же вывод, а не повторяет их логику. Всё,
что она делает своими руками, — читает JSON-карточки и правит featured.conf.

Требует GTK 4 и libadwaita (Fedora: python3-gobject, gtk4, libadwaita).

    python3 studio/legacystore_studio.py
"""

import hashlib
import json
import os
import subprocess
import threading
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(HERE)
TOOLS = os.path.join(HERE, "tools")
RELAY_TSV = os.path.join(HERE, "relay.tsv")
FEATURED_CONF = os.path.join(HERE, "featured.conf")
VERSION_TXT = os.path.join(HERE, "version.txt")
SITE = "https://fsrgteam.gitverse.site/legacystorerelay/"

# Pages ограничивает артефакт сборки; показываем, сколько занято, пока шард не
# упёрся, а не после.
PAGES_LIMIT = 500 * 1024 * 1024

GENRES = [
    "Social Networking", "Games", "Utilities", "Entertainment", "Music",
    "Photo & Video", "Productivity", "Education", "Sports", "Lifestyle",
    "Travel", "Reference", "Navigation", "Business", "Finance",
    "Food & Drink", "News", "Books", "Shopping", "Health & Fitness",
    "Weather", "Medical", "Uncategorized",
]


# --------------------------------------------------------------------------
# Данные
# --------------------------------------------------------------------------

def shards():
    """id -> путь к локальной копии шарда, по relay.tsv."""
    out = {}
    if not os.path.exists(RELAY_TSV):
        return out
    with open(RELAY_TSV, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            path = os.path.join(PARENT, "LegacyStore" + parts[0])
            if os.path.isdir(path):
                out[parts[0]] = path
    return out


def load_apps():
    """Все карточки из всех шардов: список (shard_id, путь, dict)."""
    apps = []
    for sid, path in shards().items():
        apps_dir = os.path.join(path, "apps")
        if not os.path.isdir(apps_dir):
            continue
        for name in sorted(os.listdir(apps_dir)):
            if not name.endswith(".json"):
                continue
            full = os.path.join(apps_dir, name)
            try:
                with open(full, encoding="utf-8") as f:
                    apps.append((sid, full, json.load(f)))
            except Exception:
                continue
    return apps


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        if ".git" in root.split(os.sep):
            continue
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def read_featured():
    if not os.path.exists(FEATURED_CONF):
        return [], []
    head, ids = [], []
    with open(FEATURED_CONF, encoding="utf-8") as f:
        for line in f:
            s = line.rstrip("\n")
            if s.strip().startswith("#") or not s.strip():
                if not ids:
                    head.append(s)
                continue
            ids.append(s.strip())
    return head, ids


def write_featured(head, ids):
    with open(FEATURED_CONF, "w", encoding="utf-8") as f:
        f.write("\n".join(head).rstrip("\n") + "\n\n")
        f.write("\n".join(ids) + ("\n" if ids else ""))


def local_generation():
    if not os.path.exists(VERSION_TXT):
        return None
    with open(VERSION_TXT, encoding="utf-8") as f:
        for line in f:
            if line.startswith("generation="):
                return line.split("=", 1)[1].strip()
    return None


def human(n):
    if n >= 1048576:
        return "%.1f МБ" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f КБ" % (n / 1024.0)
    return "%d Б" % n


# --------------------------------------------------------------------------
# Окно
# --------------------------------------------------------------------------

class Studio(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="Legacy Store Studio",
                         default_width=980, default_height=760)
        self.busy = False
        self.checked_facts = None      # результат ipacheck для формы добавления

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        outer = Adw.ToolbarView()
        self.toasts.set_child(outer)

        header = Adw.HeaderBar()
        self.publish_btn = Gtk.Button(label="Собрать и отправить")
        self.publish_btn.add_css_class("suggested-action")
        self.publish_btn.connect("clicked", lambda *_: self.publish_all())
        header.pack_end(self.publish_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Перечитать с диска")
        refresh.connect("clicked", lambda *_: self.reload_all())
        header.pack_start(refresh)
        outer.add_top_bar(header)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.stack.add_titled_with_icon(self.build_apps_page(), "apps",
                                        "Приложения", "view-list-symbolic")
        self.stack.add_titled_with_icon(self.build_add_page(), "add",
                                        "Добавить", "list-add-symbolic")
        self.stack.add_titled_with_icon(self.build_banner_page(), "banner",
                                        "Баннеры", "view-paged-symbolic")
        self.stack.add_titled_with_icon(self.build_log_page(), "log",
                                        "Публикация", "network-transmit-symbolic")
        outer.set_content(self.stack)

        self.reload_all()

    # -- страница «Приложения» ---------------------------------------------

    def build_apps_page(self):
        page = Adw.PreferencesPage()
        self.apps_group = Adw.PreferencesGroup(
            title="В каталоге",
            description="Карточки из шардов. Отсюда же — на баннер и в удаление.")
        page.add(self.apps_group)

        self.storage_group = Adw.PreferencesGroup(title="Место")
        page.add(self.storage_group)
        return page

    def fill_apps(self):
        for row in getattr(self, "_app_rows", []):
            self.apps_group.remove(row)
        self._app_rows = []

        apps = load_apps()
        _head, featured = read_featured()
        if not apps:
            row = Adw.ActionRow(title="Пусто",
                                subtitle="Ни одной карточки в шардах")
            self.apps_group.add(row)
            self._app_rows.append(row)
        for sid, path, card in apps:
            bid = card.get("bundleId", "")
            marks = []
            if card.get("shots"):
                marks.append("%d скр." % len(card["shots"]))
            if card.get("quote"):
                marks.append("цитата")
            if bid in featured:
                marks.append("на баннере")
            row = Adw.ActionRow(
                title="%s %s" % (card.get("title", bid), card.get("version", "")),
                subtitle="%s · %s · %s · %s"
                         % (bid, sid, human(card.get("size", 0)),
                            ", ".join(marks) if marks else "без цитаты"))

            promote = Gtk.Button(icon_name="starred-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Поставить на баннер")
            promote.connect("clicked", lambda _b, b=bid: self.promote(b))
            row.add_suffix(promote)

            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Убрать из каталога")
            delete.add_css_class("destructive-action")
            delete.connect("clicked", lambda _b, p=path, c=card, s=sid:
                           self.confirm_delete(p, c, s))
            row.add_suffix(delete)

            self.apps_group.add(row)
            self._app_rows.append(row)

    def fill_storage(self):
        for row in getattr(self, "_storage_rows", []):
            self.storage_group.remove(row)
        self._storage_rows = []
        for sid, path in shards().items():
            size = dir_size(path)
            pct = size * 100.0 / PAGES_LIMIT
            row = Adw.ActionRow(
                title="LegacyStore" + sid,
                subtitle="%s из 500 МБ (%.1f%%)" % (human(size), pct))
            bar = Gtk.ProgressBar(fraction=min(1.0, size / float(PAGES_LIMIT)),
                                  valign=Gtk.Align.CENTER, hexpand=False)
            bar.set_size_request(160, -1)
            row.add_suffix(bar)
            self.storage_group.add(row)
            self._storage_rows.append(row)

    # -- страница «Добавить» -----------------------------------------------

    def build_add_page(self):
        page = Adw.PreferencesPage()

        src = Adw.PreferencesGroup(
            title="Файл",
            description="Локальный .ipa или прямая ссылка из заявки автора.")
        self.ipa_row = Adw.ActionRow(title="Файл не выбран", subtitle="—")
        pick = Gtk.Button(label="Выбрать .ipa", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda *_: self.pick_ipa())
        self.ipa_row.add_suffix(pick)
        src.add(self.ipa_row)

        self.url_entry = Adw.EntryRow(title="…или ссылка (http/https)")
        src.add(self.url_entry)

        check = Gtk.Button(label="Проверить")
        check.connect("clicked", lambda *_: self.run_check())
        check_row = Adw.ActionRow(title="Проверка структуры",
                                  subtitle="zip, Info.plist, bundleId, срез armv7, minOS")
        check_row.add_suffix(check)
        src.add(check_row)

        self.facts_row = Adw.ActionRow(title="Ещё не проверено", subtitle="")
        src.add(self.facts_row)
        page.add(src)

        meta = Adw.PreferencesGroup(title="Карточка")
        self.bundle_entry = Adw.EntryRow(title="bundleId (сверяется с файлом)")
        meta.add(self.bundle_entry)

        self.genre_row = Adw.ComboRow(title="Категория",
                                      model=Gtk.StringList.new(GENRES))
        meta.add(self.genre_row)

        self.author_entry = Adw.EntryRow(title="Автор (@ник)")
        meta.add(self.author_entry)
        self.issue_entry = Adw.EntryRow(title="Номер заявки")
        meta.add(self.issue_entry)
        page.add(meta)

        words = Adw.PreferencesGroup(
            title="Слова автора",
            description="Показываются на баннере. Это его текст, не наш — "
                        "длиннее 130 символов обрезается при сборке.")
        self.quote_entry = Adw.EntryRow(title="Цитата")
        words.add(self.quote_entry)
        self.by_entry = Adw.EntryRow(title="Подпись")
        words.add(self.by_entry)
        page.add(words)

        extra = Adw.PreferencesGroup(title="Дополнительно")
        self.shots_row = Adw.ActionRow(title="Скриншоты", subtitle="не выбраны")
        shots_btn = Gtk.Button(label="Выбрать", valign=Gtk.Align.CENTER)
        shots_btn.connect("clicked", lambda *_: self.pick_shots())
        self.shots_row.add_suffix(shots_btn)
        extra.add(self.shots_row)

        self.keep_row = Adw.SwitchRow(
            title="Хранить .ipa у нас",
            subtitle="Иначе в каталоге останется ссылка автора")
        self.keep_row.set_active(True)
        extra.add(self.keep_row)

        add_btn = Gtk.Button(label="Добавить в шард")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: self.run_add())
        add_row = Adw.ActionRow(title="")
        add_row.add_suffix(add_btn)
        extra.add(add_row)
        page.add(extra)

        self.ipa_path = None
        self.shot_paths = []
        return page

    # -- страница «Баннеры» -------------------------------------------------

    def build_banner_page(self):
        page = Adw.PreferencesPage()
        self.banner_group = Adw.PreferencesGroup(
            title="Очередь баннеров",
            description="Порядок соблюдается. Что написано на баннере — решает "
                        "автор приложения; здесь только очередь.")
        page.add(self.banner_group)
        return page

    def fill_banners(self):
        for row in getattr(self, "_banner_rows", []):
            self.banner_group.remove(row)
        self._banner_rows = []

        head, ids = read_featured()
        titles = {c.get("bundleId"): c for _s, _p, c in load_apps()}
        if not ids:
            row = Adw.ActionRow(title="Пусто", subtitle="Ни одного баннера")
            self.banner_group.add(row)
            self._banner_rows.append(row)

        for i, bid in enumerate(ids):
            card = titles.get(bid)
            quote = (card or {}).get("quote") or ""
            row = Adw.ActionRow(
                title="%d. %s" % (i + 1, (card or {}).get("title") or bid),
                subtitle=("«%s»" % quote) if quote
                         else ("нет цитаты — покажется тэглайн"
                               if card else "нет в каталоге, будет пропущен"))
            up = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER)
            up.set_sensitive(i > 0)
            up.connect("clicked", lambda _b, k=i: self.move_banner(k, -1))
            row.add_suffix(up)
            down = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER)
            down.set_sensitive(i < len(ids) - 1)
            down.connect("clicked", lambda _b, k=i: self.move_banner(k, 1))
            row.add_suffix(down)
            rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
            rm.connect("clicked", lambda _b, k=i: self.remove_banner(k))
            row.add_suffix(rm)
            self.banner_group.add(row)
            self._banner_rows.append(row)

    def move_banner(self, index, delta):
        head, ids = read_featured()
        j = index + delta
        if 0 <= j < len(ids):
            ids[index], ids[j] = ids[j], ids[index]
            write_featured(head, ids)
            self.fill_banners()

    def remove_banner(self, index):
        head, ids = read_featured()
        if 0 <= index < len(ids):
            del ids[index]
            write_featured(head, ids)
            self.fill_banners()
            self.fill_apps()

    def promote(self, bundle_id):
        head, ids = read_featured()
        if bundle_id in ids:
            self.toast("%s уже на баннере" % bundle_id)
            return
        ids.append(bundle_id)
        write_featured(head, ids)
        self.fill_banners()
        self.fill_apps()
        self.toast("%s поставлен на баннер" % bundle_id)

    # -- страница «Публикация» ----------------------------------------------

    def build_log_page(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Состояние",
            description="Поколение растёт при каждой сборке. Устройство качает "
                        "каталог, только когда оно разошлось с сохранённым.")
        self.gen_row = Adw.ActionRow(title="Поколение", subtitle="—")
        group.add(self.gen_row)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          valign=Gtk.Align.CENTER)
        for label, fn in (("Собрать", self.build_only),
                          ("Отправить", self.push_only),
                          ("Проверить раскатку", self.check_deploy)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, f=fn: f())
            actions.append(b)
        act_row = Adw.ActionRow(title="Действия")
        act_row.add_suffix(actions)
        group.add(act_row)
        page.add(group)

        log_group = Adw.PreferencesGroup(title="Журнал")
        self.log_view = Gtk.TextView(editable=False, monospace=True,
                                     left_margin=8, right_margin=8,
                                     top_margin=8, bottom_margin=8)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=320)
        scroll.set_child(self.log_view)
        scroll.add_css_class("card")
        log_group.add(scroll)
        page.add(log_group)
        return page

    # -- служебное ----------------------------------------------------------

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    def log(self, text):
        def append():
            buf = self.log_view.get_buffer()
            buf.insert(buf.get_end_iter(), text.rstrip("\n") + "\n")
            self.log_view.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0, 0)
            return False
        GLib.idle_add(append)

    def set_busy(self, busy):
        self.busy = busy
        GLib.idle_add(lambda: self.publish_btn.set_sensitive(not busy))

    def run_bg(self, fn):
        """Долгие операции — в поток: окно должно оставаться живым."""
        if self.busy:
            self.toast("Подождите, идёт другая операция")
            return
        self.set_busy(True)

        def wrapper():
            try:
                fn()
            except Exception as e:                      # noqa: BLE001
                self.log("ОШИБКА: %s" % e)
            finally:
                self.set_busy(False)
                GLib.idle_add(self.reload_all)
        threading.Thread(target=wrapper, daemon=True).start()

    def run_cmd(self, args, cwd=HERE):
        self.log("$ " + " ".join(args))
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            self.log(line)
        p.wait()
        if p.returncode != 0:
            self.log("— код возврата %d" % p.returncode)
        return p.returncode

    def reload_all(self):
        self.fill_apps()
        self.fill_storage()
        self.fill_banners()
        gen = local_generation()
        self.gen_row.set_subtitle("локально: %s" % (gen or "нет сборки"))
        return False

    # -- действия -----------------------------------------------------------

    def pick_ipa(self):
        dialog = Gtk.FileDialog(title="Выберите .ipa")
        flt = Gtk.FileFilter()
        flt.set_name("Пакеты .ipa")
        flt.add_pattern("*.ipa")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.ipa_path = f.get_path()
            self.ipa_row.set_title(os.path.basename(self.ipa_path))
            self.ipa_row.set_subtitle(human(os.path.getsize(self.ipa_path)))
        dialog.open(self, None, done)

    def pick_shots(self):
        dialog = Gtk.FileDialog(title="Выберите скриншоты")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.shot_paths = [files.get_item(i).get_path()
                               for i in range(files.get_n_items())]
            self.shots_row.set_subtitle("%d выбрано" % len(self.shot_paths))
        dialog.open_multiple(self, None, done)

    def source_url(self):
        if self.url_entry.get_text().strip():
            return self.url_entry.get_text().strip()
        if self.ipa_path:
            return "file://" + self.ipa_path
        return None

    def run_check(self):
        url = self.source_url()
        if not url:
            self.toast("Сначала выберите файл или укажите ссылку")
            return

        def work():
            local = self.ipa_path
            tmp = None
            if not local:
                self.log("качаю %s" % url)
                tmp = os.path.join(GLib.get_tmp_dir(), "studio_check.ipa")
                urllib.request.urlretrieve(url, tmp)
                local = tmp
            args = ["python3", os.path.join(TOOLS, "ipacheck.py"), local]
            bundle = self.bundle_entry.get_text().strip()
            if bundle:
                args += ["--bundle", bundle]
            p = subprocess.run(args, capture_output=True, text=True)
            self.log(p.stdout or p.stderr)
            if p.returncode != 0:
                GLib.idle_add(lambda: self.facts_row.set_title("Отклонено"))
                GLib.idle_add(lambda: self.facts_row.set_subtitle(p.stderr.strip()))
                return
            facts = json.loads(p.stdout)
            self.checked_facts = facts

            def show():
                self.facts_row.set_title("%s %s" % (facts["title"], facts["version"]))
                self.facts_row.set_subtitle(
                    "%s · %s · minOS %d · %s"
                    % (facts["bundleId"], "/".join(facts["arch"]),
                       facts["minOS"], human(facts["size"])))
                if not self.bundle_entry.get_text().strip():
                    self.bundle_entry.set_text(facts["bundleId"])
                return False
            GLib.idle_add(show)
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        self.run_bg(work)

    def run_add(self):
        url = self.source_url()
        if not url:
            self.toast("Сначала выберите файл или укажите ссылку")
            return
        genre = GENRES[self.genre_row.get_selected()]
        args = ["python3", os.path.join(TOOLS, "add_app.py"), "--url", url,
                "--genre", genre]
        for flag, value in (("--bundle", self.bundle_entry.get_text().strip()),
                            ("--author", self.author_entry.get_text().strip()),
                            ("--quote", self.quote_entry.get_text().strip()),
                            ("--by", self.by_entry.get_text().strip()),
                            ("--issue", self.issue_entry.get_text().strip())):
            if value:
                args += [flag, value]
        if self.keep_row.get_active():
            args.append("--keep")
        for s in self.shot_paths:
            args += ["--shot", s]

        def work():
            if self.run_cmd(args) == 0:
                self.log("готово — не забудьте «Собрать и отправить»")
        self.run_bg(work)
        self.stack.set_visible_child_name("log")

    def confirm_delete(self, path, card, shard_id):
        bid = card.get("bundleId", "")
        dialog = Adw.MessageDialog(
            transient_for=self, heading="Убрать %s?" % (card.get("title") or bid),
            body="Карточка, иконка, скриншоты и файл в шарде будут удалены. "
                 "Каталог пересоберётся при следующей сборке.")
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("delete", "Убрать")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response != "delete":
                return
            shard = shards().get(shard_id)
            os.remove(path)
            for rel in (card.get("icon"), card.get("url")):
                if rel and not str(rel).startswith("http"):
                    full = os.path.join(shard, rel)
                    if os.path.exists(full):
                        os.remove(full)
            shots_dir = os.path.join(shard, "shots", bid)
            if os.path.isdir(shots_dir):
                for n in os.listdir(shots_dir):
                    os.remove(os.path.join(shots_dir, n))
                os.rmdir(shots_dir)
            head, ids = read_featured()
            if bid in ids:
                write_featured(head, [i for i in ids if i != bid])
            self.log("удалено: %s" % bid)
            self.reload_all()
        dialog.connect("response", answered)
        dialog.present()

    def build_only(self):
        self.run_bg(lambda: self.run_cmd(
            ["python3", os.path.join(TOOLS, "build_relay.py")]))
        self.stack.set_visible_child_name("log")

    def push_only(self):
        def work():
            for path in [HERE] + list(shards().values()):
                name = os.path.basename(path)
                self.log("— %s" % name)
                self.run_cmd(["git", "add", "-A"], cwd=path)
                self.run_cmd(["git", "commit", "-m", "Обновление каталога"], cwd=path)
                self.run_cmd(["git", "push"], cwd=path)
        self.run_bg(work)
        self.stack.set_visible_child_name("log")

    def check_deploy(self):
        def work():
            want = local_generation()
            self.log("жду поколение %s на %s" % (want, SITE))
            for attempt in range(1, 13):
                try:
                    body = urllib.request.urlopen(SITE + "version.txt", timeout=20).read()
                except Exception as e:                  # noqa: BLE001
                    self.log("попытка %d: %s" % (attempt, e))
                    continue
                text = body.decode("utf-8", "replace")
                # Pages отвечает 200 и HTML-заглушкой на несуществующий файл,
                # поэтому смотрим на содержимое, а не на код ответа.
                if "generation=" not in text:
                    self.log("попытка %d: ещё заглушка" % attempt)
                else:
                    got = text.split("generation=", 1)[1].split("\n", 1)[0].strip()
                    if got == want:
                        self.log("раскатано: поколение %s" % got)
                        self.verify_catalog(text)
                        return
                    self.log("попытка %d: на сайте %s" % (attempt, got))
                threading.Event().wait(20)
            self.log("не дождался — Pages обычно раскатывает за пару минут")
        self.run_bg(work)
        self.stack.set_visible_child_name("log")

    def verify_catalog(self, version_text):
        """Сумма из version.txt против того, что реально отдаёт сайт."""
        want = None
        for line in version_text.split("\n"):
            if line.startswith("catalog_sha256="):
                want = line.split("=", 1)[1].strip()
        if not want:
            return
        body = urllib.request.urlopen(SITE + "catalog-all.tsv", timeout=30).read()
        got = hashlib.sha256(body).hexdigest()
        self.log("каталог: %s" % ("сумма сошлась" if got == want else "СУММА НЕ СОШЛАСЬ"))

    def publish_all(self):
        def work():
            if self.run_cmd(["python3", os.path.join(TOOLS, "build_relay.py")]) != 0:
                return
            for path in [HERE] + list(shards().values()):
                self.log("— %s" % os.path.basename(path))
                self.run_cmd(["git", "add", "-A"], cwd=path)
                self.run_cmd(["git", "commit", "-m", "Обновление каталога"], cwd=path)
                if self.run_cmd(["git", "push"], cwd=path) != 0:
                    self.log("push не прошёл, раскатку не жду")
                    return
            self.log("отправлено, жду Pages…")
        self.run_bg(work)
        self.stack.set_visible_child_name("log")


class StudioApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="ru.fsrgteam.LegacyStoreStudio")

    def do_activate(self):
        win = self.props.active_window or Studio(self)
        win.present()


if __name__ == "__main__":
    StudioApp().run(None)
