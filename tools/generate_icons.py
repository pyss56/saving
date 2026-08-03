# -*- coding: utf-8 -*-
"""生成 PWA 图标（纯标准库，无第三方依赖）。
用法: python tools/generate_icons.py
输出: public/icons/icon-192.png, icon-512.png, icon.svg
"""
import os
import struct
import zlib


def _png_chunk(tag, data):
    c = struct.pack('>I', len(data)) + tag + data
    c += struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    return c


def write_png(path, width, height, pixels):
    """pixels: list of rows, each row is list of (r,g,b)"""
    raw = b''.join(b'\x00' + b''.join(bytes(p) for p in row) for row in pixels)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    png = b'\x89PNG\r\n\x1a\n'
    png += _png_chunk(b'IHDR', ihdr)
    png += _png_chunk(b'IDAT', zlib.compress(raw, 9))
    png += _png_chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)


def lerp(a, b, t):
    return int(a + (b - a) * t)


def make_icon(size):
    """柔和暖色渐变圆角底 + 白色硬币 + 棕色 ¥ 符号"""
    img = [[(255, 247, 240)] * size for _ in range(size)]
    top = (255, 214, 165)
    bottom = (253, 152, 114)
    # 圆角矩形底
    radius = size * 0.22
    for y in range(size):
        t = y / (size - 1)
        bg = tuple(lerp(top[i], bottom[i], t) for i in range(3))
        for x in range(size):
            # 圆角判定
            cx = min(max(x, radius), size - radius)
            cy = min(max(y, radius), size - radius)
            if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                continue
            img[y][x] = bg

    def in_circle(x, y, cxp, cyp, rad):
        return (x - cxp) ** 2 + (y - cyp) ** 2 <= rad * rad

    # 白色硬币
    coin_r = size * 0.30
    cx0, cy0 = size / 2, size / 2
    white = (255, 255, 255)
    for y in range(size):
        for x in range(size):
            if in_circle(x, y, cx0, cy0, coin_r):
                img[y][x] = white
    # 硬币内圈
    inner_r = size * 0.235
    ring = (255, 214, 165)
    for y in range(size):
        for x in range(size):
            d = (x - cx0) ** 2 + (y - cy0) ** 2
            if coin_r * coin_r * 0.16 <= d <= inner_r * inner_r:
                img[y][x] = ring

    # 绘制 ¥ 符号（粗线段）
    brown = (91, 74, 65)

    def put(x, y):
        if 0 <= int(x) < size and 0 <= int(y) < size:
            img[int(y)][int(x)] = brown

    def line(x0, y0, x1, y1, w=0.045 * size):
        steps = max(1, int(abs(x1 - x0) + abs(y1 - y0)))
        for i in range(steps + 1):
            t = i / steps
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            for dx in range(-int(w), int(w) + 1):
                for dy in range(-int(w), int(w) + 1):
                    if dx * dx + dy * dy <= w * w:
                        put(x + dx, y + dy)

    s = size
    # 顶部两斜线（Y 上半部）
    line(0.50 * s, 0.24 * s, 0.34 * s, 0.46 * s)
    line(0.50 * s, 0.24 * s, 0.66 * s, 0.46 * s)
    # 竖线（Y 下半部到底）
    line(0.50 * s, 0.44 * s, 0.50 * s, 0.74 * s)
    # 两条横杠
    line(0.28 * s, 0.54 * s, 0.72 * s, 0.54 * s)
    line(0.28 * s, 0.66 * s, 0.72 * s, 0.66 * s)
    return img


def make_svg():
    return '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffd6a5"/>
      <stop offset="1" stop-color="#fd9872"/>
    </linearGradient>
  </defs>
  <rect x="20" y="20" width="472" height="472" rx="112" fill="url(#g)"/>
  <circle cx="256" cy="256" r="154" fill="#ffffff"/>
  <circle cx="256" cy="256" r="122" fill="none" stroke="#ffd6a5" stroke-width="18"/>
  <g stroke="#5b4a41" stroke-width="26" stroke-linecap="round" fill="none">
    <path d="M256 118 L174 236"/>
    <path d="M256 118 L338 236"/>
    <path d="M256 226 L256 382"/>
    <path d="M150 278 L362 278"/>
    <path d="M150 342 L362 342"/>
  </g>
</svg>'''


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'public', 'icons')
    os.makedirs(out_dir, exist_ok=True)
    write_png(os.path.join(out_dir, 'icon-192.png'), 192, 192, make_icon(192))
    write_png(os.path.join(out_dir, 'icon-512.png'), 512, 512, make_icon(512))
    with open(os.path.join(out_dir, 'icon.svg'), 'w', encoding='utf-8') as f:
        f.write(make_svg())
    print('图标已生成:', out_dir)


if __name__ == '__main__':
    main()
