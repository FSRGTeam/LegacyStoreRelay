#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение iOS-иконок формата CgBI.

Apple прогоняет PNG в бандле через pngcrush -iphone, и получается файл, который
формально PNG, но не читается ничем на десктопе: у него есть чанк CgBI, поток
IDAT лежит без zlib-заголовка, каналы переставлены в BGRA и умножены на альфу.
Устройство декодирует это нативно — а нам нужно показать иконку в студии, и
поэтому здесь ровно эти четыре отличия и разбираются.

Обычный PNG эта функция не трогает и возвращает None: пусть его читает GdkPixbuf,
он делает это лучше.

    w, h, rgba = load_cgbi_rgba("icon.png")
"""

import struct
import zlib


def _chunks(data):
    pos = 8                                   # сигнатура PNG
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        yield kind.decode("ascii", "replace"), body
        pos += 12 + length                    # длина + тип + тело + CRC


def _unfilter(raw, width, height, bpp):
    """Снимает построчные фильтры PNG (типы 0-4)."""
    stride = width * bpp
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if ftype == 1:                        # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:                      # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                      # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                      # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def load_cgbi_rgba(path):
    """(ширина, высота, RGBA-байты) для CgBI-PNG, иначе None."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    is_cgbi = False
    width = height = 0
    bit_depth = color_type = 0
    idat = bytearray()
    for kind, body in _chunks(data):
        if kind == "CgBI":
            is_cgbi = True
        elif kind == "IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif kind == "IDAT":
            idat += body
        elif kind == "IEND":
            break

    # Всё, что не CgBI, отдаём штатному загрузчику.
    if not is_cgbi:
        return None
    # Иконки iOS всегда 8-битные RGBA; ничего другого сюда не приезжает, и
    # угадывать остальные комбинации значило бы писать полный декодер PNG.
    if bit_depth != 8 or color_type != 6:
        return None

    raw = zlib.decompressobj(-zlib.MAX_WBITS).decompress(bytes(idat))
    pixels = _unfilter(raw, width, height, 4)

    # BGRA с премультиплированной альфой -> RGBA.
    for i in range(0, len(pixels), 4):
        b, g, r, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if a and a != 255:
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        pixels[i], pixels[i + 1], pixels[i + 2] = r, g, b
    return width, height, bytes(pixels)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        got = load_cgbi_rgba(p)
        print(p, "CgBI %dx%d" % got[:2] if got else "обычный PNG")
