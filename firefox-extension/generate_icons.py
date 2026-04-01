#!/usr/bin/env python3
"""Erstellt die Icons für die Firefox Extension (nur einmal ausführen)."""

import struct
import zlib
import math
import os


def make_png(size):
    bg = (30, 58, 95)  # #1e3a5f — Dunkelblau
    fg = (255, 255, 255)  # Weiß

    def in_rounded_rect(x, y, r=0.18):
        r = size * r
        if x < r or x > size - 1 - r or y < r or y > size - 1 - r:
            in_x_zone = x < r or x > size - 1 - r
            in_y_zone = y < r or y > size - 1 - r
            if in_x_zone and in_y_zone:
                cx = r if x < r else size - 1 - r
                cy = r if y < r else size - 1 - r
                return math.sqrt((x - cx) ** 2 + (y - cy) ** 2) <= r
            return True
        return True

    # Briefumschlag-Koordinaten (relativ zu size)
    ex1, ey1 = size * 0.13, size * 0.26
    ex2, ey2 = size * 0.87, size * 0.74
    mid_x = size * 0.5
    fold_y = size * 0.52  # V-Spitze

    stroke = max(1.5, size * 0.045)

    def on_envelope(x, y):
        # Außenrahmen
        if ex1 <= x <= ex2 and ey1 <= y <= ey2:
            on_border = x - ex1 < stroke or ex2 - x < stroke or ey2 - y < stroke
            # V-Linie oben (Klappe)
            t = (x - ex1) / (ex2 - ex1)
            y_v = ey1 + abs(t - 0.5) * 2 * (fold_y - ey1)
            on_v = abs(y - y_v) < stroke and y <= fold_y + stroke

            # Diagonalen unten
            t_left = (x - ex1) / (mid_x - ex1) if x <= mid_x else None
            t_right = (x - mid_x) / (ex2 - mid_x) if x > mid_x else None
            y_dl = ey2 - (ey2 - fold_y) * (t_left) if t_left is not None else None
            y_dr = fold_y + (ey2 - fold_y) * (t_right) if t_right is not None else None
            on_dl = y_dl is not None and abs(y - y_dl) < stroke and y >= fold_y - stroke
            on_dr = y_dr is not None and abs(y - y_dr) < stroke and y >= fold_y - stroke

            return on_border or on_v or on_dl or on_dr
        return False

    rows = []
    for y in range(size):
        row = b"\x00"
        for x in range(size):
            if in_rounded_rect(x, y):
                if on_envelope(x, y):
                    row += bytes([*fg, 255])
                else:
                    row += bytes([*bg, 255])
            else:
                row += bytes([0, 0, 0, 0])
        rows.append(row)

    raw = b"".join(rows)
    compressed = zlib.compress(raw, 9)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">II", size, size) + bytes([8, 6, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


os.makedirs("icons", exist_ok=True)
for size in [16, 48, 128]:
    with open(f"icons/icon{size}.png", "wb") as f:
        f.write(make_png(size))
    print(f"  icons/icon{size}.png erstellt")
print("Fertig!")
