#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ключи разработчиков Legacy Store: выдача, проверка, отзыв.

    python3 tools/devkeys.py init
    python3 tools/devkeys.py issue @nick --scope ru.computershik.* --years 1
    python3 tools/devkeys.py verify <токен> [--bundle ru.computershik.trubach]
    python3 tools/devkeys.py revoke @nick --reason "ключ утёк"
    python3 tools/devkeys.py list

Токен — это сертификат целиком, свёрнутый в одну строку
-----------------------------------------------------------------------------
    LSD1.<base32 полей>.<base32 подписи>

Внутри: версия формата, секрет разработчика, даты, ник и права. Подпись корня
покрывает всё это, поэтому проверка не требует обращения к реестру: ник и права
читаются из самого токена, а подделать их нельзя. В реестр смотрим только на
предмет отзыва — то есть в исключительном случае.

Из секрета детерминированно выводится пара Ed25519 (у Ed25519 приватный ключ и
есть 32-байтный seed). Отсюда главное свойство хранилища: **в реестре нет ни
одного секрета**, только публичные половины. Токен существует у разработчика и
больше нигде.

Это предъявительский ключ: кто держит строку, тот и разработчик. Для магазина,
где разработчиков десяток, это осознанный размен на простоту — разработчику не
нужно ничего запускать, он копирует строку. Если понадобится неотказуемость,
разработчик сгенерирует пару у себя и пришлёт публичную половину; формат от
этого не изменится, публичный ключ в нём уже есть.

Где что лежит
-----------------------------------------------------------------------------
Всё в ~/.legacystore, намеренно вне репозитория: секрет физически не должен
иметь возможности уехать в `git push`.

    registry.key    приватный корень, зашифрован паролем (scrypt + ChaCha20)
    registry.pub    публичный корень
    developers.tsv  ник, публичный ключ, права, сроки — без секретов
    revoked.tsv     отозванные ключи
"""

import argparse
import base64
import datetime
import fnmatch
import getpass
import os
import struct
import sys

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidSignature
except ImportError:                                          # pragma: no cover
    print("нужен пакет cryptography: sudo dnf install python3-cryptography",
          file=sys.stderr)
    raise

HOME = os.path.expanduser("~/.legacystore")
ROOT_KEY = os.path.join(HOME, "registry.key")
ROOT_PUB = os.path.join(HOME, "registry.pub")
DEVELOPERS = os.path.join(HOME, "developers.tsv")
REVOKED = os.path.join(HOME, "revoked.tsv")

TOKEN_PREFIX = "LSD1"
FORMAT_VERSION = 1

# Публичный корень, вписанный в код.
#
# Пустая строка означает «ещё не вписан» — тогда он читается из registry.pub, о
# чём выводится предупреждение. Как только сюда вписано значение, файл теряет
# власть: подменивший registry.pub подменил бы и доверие, а зашитая константа
# превращает подмену в ошибку проверки. `init` печатает готовую строку.
PINNED_ROOT = ""


class DevKeyError(Exception):
    pass


# --------------------------------------------------------------------------
# Кодирование

def b32(raw):
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def unb32(text):
    pad = "=" * (-len(text) % 8)
    try:
        return base64.b32decode(text.strip().upper() + pad)
    except Exception as exc:
        raise DevKeyError("строка повреждена: %s" % exc)


def days_since_epoch(date):
    return (date - datetime.date(1970, 1, 1)).days


def date_from_days(days):
    return datetime.date(1970, 1, 1) + datetime.timedelta(days=int(days))


def pack_payload(secret, handle, scope, issued, expires):
    """Поля токена в компактный вид.

    Длины строк записаны явно, а не разделителями: ник или маска прав с
    разделителем внутри иначе разъехались бы при разборе, и токен стал бы
    значить не то, что подписан.
    """
    h = handle.encode("utf-8")
    s = scope.encode("utf-8")
    if len(h) > 255 or len(s) > 255:
        raise DevKeyError("ник или права длиннее 255 байт")
    return (struct.pack("!B", FORMAT_VERSION) + secret
            + struct.pack("!II", days_since_epoch(issued), days_since_epoch(expires))
            + struct.pack("!B", len(h)) + h
            + struct.pack("!B", len(s)) + s)


def unpack_payload(blob):
    if len(blob) < 1 + 32 + 8 + 2:
        raise DevKeyError("токен короче допустимого")
    version = blob[0]
    if version != FORMAT_VERSION:
        raise DevKeyError("версия формата %d, а я знаю только %d"
                          % (version, FORMAT_VERSION))
    secret = blob[1:33]
    issued_days, expires_days = struct.unpack("!II", blob[33:41])
    pos = 41
    handle_len = blob[pos]; pos += 1
    handle = blob[pos:pos + handle_len].decode("utf-8"); pos += handle_len
    scope_len = blob[pos]; pos += 1
    scope = blob[pos:pos + scope_len].decode("utf-8"); pos += scope_len
    if pos != len(blob):
        raise DevKeyError("в токене лишние байты")
    return {
        "secret": secret,
        "handle": handle,
        "scope": scope,
        "issued": date_from_days(issued_days),
        "expires": date_from_days(expires_days),
    }


# --------------------------------------------------------------------------
# Корневой ключ

def _derive(password, salt):
    # scrypt, а не голый хеш: подбор пароля должен стоить памяти, иначе видеокарта
    # переберёт словарь за вечер. Параметры — рекомендованные для интерактивного
    # ввода, около ста миллисекунд на этой машине.
    kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
    return kdf.derive(password.encode("utf-8"))


def save_root(private_key, password):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    raw = private_key.private_bytes_raw()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_derive(password, salt)).encrypt(nonce, raw, b"LSROOT1")

    fd = os.open(ROOT_KEY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("LSROOT1\n")
        f.write("kdf=scrypt n=32768 r=8 p=1\n")
        f.write("salt=%s\n" % base64.b64encode(salt).decode())
        f.write("nonce=%s\n" % base64.b64encode(nonce).decode())
        f.write("key=%s\n" % base64.b64encode(ct).decode())

    pub = b32(private_key.public_key().public_bytes_raw())
    with open(ROOT_PUB, "w", encoding="utf-8") as f:
        f.write(pub + "\n")
    return pub


def load_root(password):
    if not os.path.exists(ROOT_KEY):
        raise DevKeyError("корневого ключа нет — сначала devkeys.py init")
    meta = {}
    with open(ROOT_KEY, encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    salt = base64.b64decode(meta["salt"])
    nonce = base64.b64decode(meta["nonce"])
    ct = base64.b64decode(meta["key"])
    try:
        raw = ChaCha20Poly1305(_derive(password, salt)).decrypt(nonce, ct, b"LSROOT1")
    except Exception:
        raise DevKeyError("пароль не подошёл")
    return Ed25519PrivateKey.from_private_bytes(raw)


def root_public():
    """Публичный корень: сначала зашитый, потом файл.

    Расхождение между ними — не мелочь: оно означает, что либо подменили файл,
    либо ключ перевыпустили и забыли обновить код. И то и другое должно быть
    видно, а не проглочено молча.
    """
    if PINNED_ROOT:
        pinned = Ed25519PublicKey.from_public_bytes(unb32(PINNED_ROOT))
        if os.path.exists(ROOT_PUB):
            with open(ROOT_PUB, encoding="utf-8") as f:
                on_disk = f.read().strip()
            if on_disk and on_disk != PINNED_ROOT:
                raise DevKeyError(
                    "публичный корень в коде и в registry.pub разные — "
                    "либо файл подменили, либо ключ перевыпустили без правки кода")
        return pinned
    if not os.path.exists(ROOT_PUB):
        raise DevKeyError("нет ни зашитого корня, ни registry.pub")
    print("ВНИМАНИЕ: публичный корень не зашит в код, взят из registry.pub. "
          "Впиши его в PINNED_ROOT — иначе подмена файла подменит и доверие.",
          file=sys.stderr)
    with open(ROOT_PUB, encoding="utf-8") as f:
        return Ed25519PublicKey.from_public_bytes(unb32(f.read().strip()))


# --------------------------------------------------------------------------
# Выдача и проверка

def make_token(root, handle, scope, years=1, secret=None, issued=None):
    if not handle.startswith("@"):
        handle = "@" + handle
    issued = issued or datetime.date.today()
    expires = issued + datetime.timedelta(days=int(365 * years))
    # Секрет — из системного источника, в который на этой машине уже течёт
    # аппаратная энтропия. Собственный сбор «из железа» был бы шагом назад:
    # см. замер термодатчика на Pi — меньше бита на сорок чтений.
    secret = secret or os.urandom(32)
    payload = pack_payload(secret, handle, scope, issued, expires)
    signature = root.sign(payload)
    return "%s.%s.%s" % (TOKEN_PREFIX, b32(payload), b32(signature))


def parse_token(token, root_pub=None, bundle=None, today=None,
                revoked=None, check_revoked=True):
    """Разбирает токен и проверяет всё, что можно проверить.

    Возвращает словарь с полями. Любая неудача — исключение с человеческим
    текстом: молчаливый None здесь однажды стал бы «проверка прошла».
    """
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise DevKeyError("это не токен Legacy Store")
    payload = unb32(parts[1])
    signature = unb32(parts[2])

    pub = root_pub or root_public()
    try:
        pub.verify(signature, payload)
    except InvalidSignature:
        raise DevKeyError("подпись не сходится — токен подделан или не наш")

    data = unpack_payload(payload)
    key = Ed25519PrivateKey.from_private_bytes(data["secret"])
    data["pubkey"] = b32(key.public_key().public_bytes_raw())
    data["private"] = key

    today = today or datetime.date.today()
    if today > data["expires"]:
        raise DevKeyError("токен истёк %s" % data["expires"].isoformat())
    if today < data["issued"]:
        raise DevKeyError("токен выдан будущим числом %s"
                          % data["issued"].isoformat())

    if check_revoked:
        revoked = revoked if revoked is not None else read_revoked()
        if data["pubkey"] in revoked:
            raise DevKeyError("ключ отозван %s: %s"
                              % (revoked[data["pubkey"]][0],
                                 revoked[data["pubkey"]][1]))

    if bundle is not None and not scope_allows(data["scope"], bundle):
        raise DevKeyError("права «%s» не покрывают %s" % (data["scope"], bundle))
    return data


def scope_allows(scope, bundle):
    """Права — список масок через запятую: `ru.nick.*, com.other.app`.

    Маски задаём мы сами, поэтому fnmatch безопасен: посторонний влиять на них
    не может, а подпись не даст их отредактировать.
    """
    for mask in scope.split(","):
        mask = mask.strip()
        if mask and fnmatch.fnmatchcase(bundle, mask):
            return True
    return False


# --------------------------------------------------------------------------
# Реестр

def read_developers():
    rows = []
    if not os.path.exists(DEVELOPERS):
        return rows
    with open(DEVELOPERS, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            while len(parts) < 7:
                parts.append("")
            rows.append({
                "handle": parts[0], "pubkey": parts[1], "scope": parts[2],
                "issued": parts[3], "expires": parts[4],
                "status": parts[5] or "active", "note": parts[6],
            })
    return rows


def write_developers(rows):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    tmp = DEVELOPERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# ник\tпубличный ключ\tправа\tвыдан\tистекает\tстатус\tзаметка\n")
        for r in rows:
            f.write("\t".join([r["handle"], r["pubkey"], r["scope"], r["issued"],
                               r["expires"], r["status"], r["note"]]) + "\n")
    os.replace(tmp, DEVELOPERS)


def read_revoked():
    out = {}
    if not os.path.exists(REVOKED):
        return out
    with open(REVOKED, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
    return out


def add_revoked(pubkey, reason):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    with open(REVOKED, "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\n" % (pubkey, datetime.date.today().isoformat(),
                                  reason.replace("\t", " ")))


def register(data, note=""):
    """Кладёт в реестр публичную половину. Секрет сюда не попадает никогда."""
    rows = [r for r in read_developers() if r["handle"] != data["handle"]]
    rows.append({
        "handle": data["handle"],
        "pubkey": data["pubkey"],
        "scope": data["scope"],
        "issued": data["issued"].isoformat(),
        "expires": data["expires"].isoformat(),
        "status": "active",
        "note": note,
    })
    rows.sort(key=lambda r: r["handle"].lower())
    write_developers(rows)


# --------------------------------------------------------------------------
# Командная строка

def cmd_init(args):
    if os.path.exists(ROOT_KEY) and not args.force:
        print("корневой ключ уже есть: %s (--force чтобы перевыпустить, но тогда "
              "все выданные токены станут недействительны)" % ROOT_KEY)
        return 1
    password = getpass.getpass("пароль для корневого ключа: ")
    again = getpass.getpass("ещё раз: ")
    if password != again:
        print("пароли не совпали", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("пароль короче восьми символов — не пойдёт", file=sys.stderr)
        return 1
    pub = save_root(Ed25519PrivateKey.generate(), password)
    print("корень создан: %s" % ROOT_KEY)
    print("публичная половина: %s" % pub)
    print()
    print("впиши её в код, чтобы подмена файла не подменяла доверие:")
    print('    PINNED_ROOT = "%s"' % pub)
    return 0


def cmd_issue(args):
    password = getpass.getpass("пароль корневого ключа: ")
    root = load_root(password)
    token = make_token(root, args.handle, args.scope, years=args.years)
    data = parse_token(token, root_pub=root.public_key(), check_revoked=False)
    register(data, note=args.note or "")
    print("выдан %s, права «%s», до %s"
          % (data["handle"], data["scope"], data["expires"].isoformat()))
    print("публичный ключ в реестре: %s" % data["pubkey"])
    print()
    print("отдать разработчику (и больше нигде не хранить):")
    print(token)
    return 0


def cmd_verify(args):
    try:
        data = parse_token(args.token, bundle=args.bundle)
    except DevKeyError as exc:
        print("НЕ ПРОШЁЛ: %s" % exc)
        return 1
    print("в порядке: %s" % data["handle"])
    print("  права:      %s" % data["scope"])
    print("  выдан:      %s" % data["issued"].isoformat())
    print("  истекает:   %s" % data["expires"].isoformat())
    print("  публичный:  %s" % data["pubkey"])
    if args.bundle:
        print("  %s покрывается правами" % args.bundle)
    return 0


def cmd_revoke(args):
    rows = read_developers()
    found = [r for r in rows if r["handle"] in (args.handle, "@" + args.handle.lstrip("@"))]
    if not found:
        print("такого ника в реестре нет: %s" % args.handle, file=sys.stderr)
        return 1
    for r in found:
        add_revoked(r["pubkey"], args.reason)
        r["status"] = "revoked"
        print("отозван %s (%s)" % (r["handle"], args.reason))
    write_developers(rows)
    return 0


def cmd_list(args):
    rows = read_developers()
    if not rows:
        print("реестр пуст")
        return 0
    revoked = read_revoked()
    today = datetime.date.today()
    print("%-16s %-12s %-24s %s" % ("ник", "статус", "права", "истекает"))
    for r in rows:
        status = r["status"]
        if r["pubkey"] in revoked:
            status = "отозван"
        elif r["expires"] and datetime.date.fromisoformat(r["expires"]) < today:
            status = "истёк"
        print("%-16s %-12s %-24s %s" % (r["handle"], status, r["scope"], r["expires"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="ключи разработчиков Legacy Store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="создать корневой ключ")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("issue", help="выдать ключ разработчику")
    p.add_argument("handle")
    p.add_argument("--scope", default="*", help="маски bundleId через запятую")
    p.add_argument("--years", type=float, default=1.0)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("verify", help="проверить токен")
    p.add_argument("token")
    p.add_argument("--bundle", help="проверить и права на этот bundleId")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("revoke", help="отозвать ключ")
    p.add_argument("handle")
    p.add_argument("--reason", default="без причины")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("list", help="показать реестр")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args()
    try:
        return args.func(args)
    except DevKeyError as exc:
        print("ошибка: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
