#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверки для devkeys.py.

    python3 tools/test_devkeys.py

Каждый тест отвечает на вопрос «что именно этот механизм должен не пропустить».
Проверка, которая пропускает подделку, хуже отсутствия проверки: она создаёт
уверенность там, где её нет.
"""

import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import devkeys                                              # noqa: E402
from devkeys import DevKeyError                             # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.root = Ed25519PrivateKey.generate()
        self.pub = self.root.public_key()

    def issue(self, handle="@nick", scope="ru.nick.*", years=1, issued=None):
        return devkeys.make_token(self.root, handle, scope, years=years,
                                  issued=issued)

    def parse(self, token, **kw):
        kw.setdefault("root_pub", self.pub)
        kw.setdefault("check_revoked", False)
        return devkeys.parse_token(token, **kw)

    # --- то, что должно работать ---

    def test_round_trip_keeps_every_field(self):
        token = self.issue("@computershik", "ru.computershik.*", years=2)
        data = self.parse(token)
        self.assertEqual(data["handle"], "@computershik")
        self.assertEqual(data["scope"], "ru.computershik.*")
        self.assertEqual(data["expires"] - data["issued"],
                         datetime.timedelta(days=730))

    def test_handle_gets_at_sign(self):
        self.assertEqual(self.parse(self.issue("nick"))["handle"], "@nick")

    def test_public_key_derives_from_the_token_itself(self):
        """Реестру не нужен секрет: публичная половина выводится из токена."""
        token = self.issue()
        first = self.parse(token)["pubkey"]
        second = self.parse(token)["pubkey"]
        self.assertEqual(first, second)
        self.assertEqual(len(devkeys.unb32(first)), 32)

    def test_scope_matching(self):
        self.assertTrue(devkeys.scope_allows("ru.nick.*", "ru.nick.app"))
        self.assertTrue(devkeys.scope_allows("*", "com.anything.at.all"))
        self.assertTrue(devkeys.scope_allows("a.b.*, c.d.app", "c.d.app"))
        self.assertFalse(devkeys.scope_allows("ru.nick.*", "ru.other.app"))
        # Точка в маске — настоящая точка, а не «любой символ»: иначе
        # `ru.nick.*` покрыло бы `runick.app` постороннего человека.
        self.assertFalse(devkeys.scope_allows("ru.nick.*", "runickXapp"))

    # --- то, что должно не пройти ---

    def test_signature_from_a_different_root_is_rejected(self):
        stranger = Ed25519PrivateKey.generate()
        token = devkeys.make_token(stranger, "@nick", "*")
        with self.assertRaises(DevKeyError) as e:
            self.parse(token)
        self.assertIn("подпись", str(e.exception))

    def test_tampering_with_the_payload_is_caught(self):
        """Главное свойство: права нельзя расширить, не сломав подпись."""
        token = self.issue("@nick", "ru.nick.*")
        prefix, payload, sig = token.split(".")
        raw = bytearray(devkeys.unb32(payload))
        raw[-1] ^= 0x01                       # правим последний байт прав
        forged = "%s.%s.%s" % (prefix, devkeys.b32(bytes(raw)), sig)
        with self.assertRaises(DevKeyError):
            self.parse(forged)

    def test_swapping_the_secret_is_caught(self):
        """Нельзя взять чужой сертификат и подставить свой секрет."""
        token = self.issue()
        prefix, payload, sig = token.split(".")
        raw = bytearray(devkeys.unb32(payload))
        raw[1:33] = os.urandom(32)
        forged = "%s.%s.%s" % (prefix, devkeys.b32(bytes(raw)), sig)
        with self.assertRaises(DevKeyError):
            self.parse(forged)

    def test_expired_token(self):
        old = datetime.date.today() - datetime.timedelta(days=800)
        token = self.issue(issued=old, years=1)
        with self.assertRaises(DevKeyError) as e:
            self.parse(token)
        self.assertIn("истёк", str(e.exception))

    def test_token_issued_in_the_future(self):
        ahead = datetime.date.today() + datetime.timedelta(days=5)
        token = self.issue(issued=ahead)
        with self.assertRaises(DevKeyError) as e:
            self.parse(token)
        self.assertIn("будущим", str(e.exception))

    def test_bundle_outside_scope(self):
        token = self.issue("@nick", "ru.nick.*")
        with self.assertRaises(DevKeyError) as e:
            self.parse(token, bundle="com.chuzhoi.app")
        self.assertIn("права", str(e.exception))
        self.parse(token, bundle="ru.nick.trubach")          # свой — проходит

    def test_revoked_key(self):
        token = self.issue()
        data = self.parse(token)
        revoked = {data["pubkey"]: ("2026-08-16", "утёк")}
        with self.assertRaises(DevKeyError) as e:
            self.parse(token, revoked=revoked, check_revoked=True)
        self.assertIn("отозван", str(e.exception))

    def test_garbage_input(self):
        for junk in ("", "мусор", "LSD1.не.base32",
                     "LSD9.AAAA.BBBB", "LSD1.AAAA", "LSD1.AAAA.BBBB.CCCC"):
            with self.assertRaises(DevKeyError):
                self.parse(junk)

    def test_trailing_bytes_are_rejected(self):
        """Лишний хвост в полях — признак подгонки, а не совместимости."""
        token = self.issue()
        prefix, payload, _ = token.split(".")
        raw = devkeys.unb32(payload) + b"\x00"
        with self.assertRaises(DevKeyError):
            devkeys.unpack_payload(raw)


class RootKeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lsdev-")
        self._saved = (devkeys.HOME, devkeys.ROOT_KEY, devkeys.ROOT_PUB,
                       devkeys.DEVELOPERS, devkeys.REVOKED, devkeys.PINNED_ROOT)
        devkeys.HOME = self.dir
        devkeys.ROOT_KEY = os.path.join(self.dir, "registry.key")
        devkeys.ROOT_PUB = os.path.join(self.dir, "registry.pub")
        devkeys.DEVELOPERS = os.path.join(self.dir, "developers.tsv")
        devkeys.REVOKED = os.path.join(self.dir, "revoked.tsv")
        devkeys.PINNED_ROOT = ""

    def tearDown(self):
        (devkeys.HOME, devkeys.ROOT_KEY, devkeys.ROOT_PUB,
         devkeys.DEVELOPERS, devkeys.REVOKED, devkeys.PINNED_ROOT) = self._saved

    def test_root_survives_password_round_trip(self):
        key = Ed25519PrivateKey.generate()
        devkeys.save_root(key, "правильный пароль")
        back = devkeys.load_root("правильный пароль")
        self.assertEqual(back.private_bytes_raw(), key.private_bytes_raw())

    def test_wrong_password_is_refused(self):
        devkeys.save_root(Ed25519PrivateKey.generate(), "правильный")
        with self.assertRaises(DevKeyError) as e:
            devkeys.load_root("неправильный")
        self.assertIn("пароль", str(e.exception))

    def test_key_file_is_not_world_readable(self):
        devkeys.save_root(Ed25519PrivateKey.generate(), "пароль подлиннее")
        mode = os.stat(devkeys.ROOT_KEY).st_mode & 0o777
        self.assertEqual(mode, 0o600, "приватный ключ доступен не только владельцу")

    def test_secret_never_reaches_the_registry(self):
        """Ключевое свойство хранилища — проверяем, а не надеемся."""
        root = Ed25519PrivateKey.generate()
        token = devkeys.make_token(root, "@nick", "ru.nick.*")
        data = devkeys.parse_token(token, root_pub=root.public_key(),
                                   check_revoked=False)
        devkeys.register(data)
        with open(devkeys.DEVELOPERS, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("@nick", body)
        self.assertIn(data["pubkey"], body)
        self.assertNotIn(devkeys.b32(data["secret"]), body)
        self.assertNotIn(token, body)

    def test_pinned_root_mismatch_is_loud(self):
        """Подменённый registry.pub должен ломать проверку, а не проходить."""
        devkeys.save_root(Ed25519PrivateKey.generate(), "пароль подлиннее")
        stranger = Ed25519PrivateKey.generate().public_key()
        devkeys.PINNED_ROOT = devkeys.b32(stranger.public_bytes_raw())
        with self.assertRaises(DevKeyError) as e:
            devkeys.root_public()
        self.assertIn("подменили", str(e.exception))

    def test_revocation_is_recorded_and_read_back(self):
        devkeys.add_revoked("AAAA", "утёк в чат")
        revoked = devkeys.read_revoked()
        self.assertIn("AAAA", revoked)
        self.assertEqual(revoked["AAAA"][1], "утёк в чат")


if __name__ == "__main__":
    unittest.main(verbosity=2)
