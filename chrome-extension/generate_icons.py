#!/usr/bin/env python3
"""Erstellt die Icons für die Chrome Extension (nur einmal ausführen)."""
import struct, zlib, math, os

def make_png(size):
    r, g, b = 30, 58, 138  # Dunkelblau
    er, eg, eb = 255, 255, 255  # Weiß (Briefumschlag)
    radius = size * 0.18
    pad = size * 0.07

    def in_rounded_rect(x, y):
        x1, y1 = pad, pad
        x2, y2 = size - 1 - pad, size - 1 - pad
        if x < x1 or x > x2 or y < y1 or y > y2:
            return False
        # Ecken prüfen
        for cx, cy in [(x1+radius, y1+radius), (x2-radius, y1+radius),
                       (x1+radius, y2-radius), (x2-radius, y2-radius)]:
            if x < cx and y < cy or x < cx and y > size-1-cy or \
               x > size-1-cx and y < cy or x > size-1-cx and y > size-1-cy:
                dist = math.sqrt((x - cx)**2 + (y - cy)**2)
                if dist > radius:
                    return False
        return True

    def envelope_pixel(x, y):
        """Einfaches Briefumschlag-Symbol."""
        ex1 = size * 0.18
        ex2 = size * 0.82
        ey1 = size * 0.28
        ey2 = size * 0.72
        if not (ex1 <= x <= ex2 and ey1 <= y <= ey2):
            return False
        # Diagonale Linien oben (V-Form)
        mid_x = size * 0.5
        slope = (ey2 - ey1) / (ex2 - ex1) * 0.6
        y_line_left = ey1 + slope * (x - ex1)
        y_line_right = ey1 + slope * (ex2 - x)
        thickness = max(1, size * 0.06)
        if abs(y - min(y_line_left, y_line_right)) < thickness and y < ey1 + (ey2-ey1)*0.5:
            return True
        # Rahmen
        border = max(1, size * 0.05)
        if (x - ex1 < border or ex2 - x < border or
                ey2 - y < border):
            return True
        return False

    rows = []
    for y in range(size):
        row = b'\x00'
        for x in range(size):
            if in_rounded_rect(x, y):
                if envelope_pixel(x, y):
                    row += bytes([er, eg, eb, 255])
                else:
                    row += bytes([r, g, b, 255])
            else:
                row += bytes([0, 0, 0, 0])
        rows.append(row)

    raw = b''.join(rows)
    compressed = zlib.compress(raw, 9)

    def chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack('>II', size, size) + bytes([8, 6, 0, 0, 0])
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')

os.makedirs('icons', exist_ok=True)
for size in [16, 48, 128]:
    with open(f'icons/icon{size}.png', 'wb') as f:
        f.write(make_png(size))
    print(f'  icons/icon{size}.png erstellt')
print('Fertig!')
