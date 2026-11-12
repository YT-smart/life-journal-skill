#!/usr/bin/env python3
"""
把二维码图片（PNG）变成可以直接贴在对话里扫的文字二维码（只用 Python 3 标准库）

  python3 qrshow.py <qr.png>            # 深色块 = 二维码的黑点（适合浅色背景）
  python3 qrshow.py <qr.png> --invert   # 反色（适合深色背景；扫不出来时换这个试）

导出脚本出现 QR_READY 时会自动附带一份（QR_TEXT_BEGIN … QR_TEXT_END），这个命令用来单独重新生成。
文字二维码只在行距接近 1 的地方（终端）能扫；聊天窗口行距大，请发图片。
原理：解码 PNG → 按定位角估出模块大小 → 每个模块取中心点 → 用半格字符 ▀ ▄ █ 两行并一行输出。
"""
import struct, sys, zlib


def png_gray(data):
    """解码非隔行 PNG（位深 1/2/4/8，灰度/RGB/调色板/带透明），返回 (宽, 高, 亮度二维列表)。"""
    if data[:8] != b'\x89PNG\r\n\x1a\n': raise ValueError('不是 PNG')
    pos, idat, pal, trns = 8, b'', None, None
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]; typ = data[pos + 4:pos + 8]; body = data[pos + 8:pos + 8 + ln]; pos += 12 + ln
        if typ == b'IHDR': w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE': pal = body
        elif typ == b'IDAT': idat += body
        elif typ == b'IEND': break
    if inter or depth == 16: raise ValueError('不支持的 PNG 格式')
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, ch * depth // 8); stride = (w * ch * depth + 7) // 8
    raw = zlib.decompress(idat); prev = bytearray(stride); rows = []
    for y in range(h):
        ft = raw[y * (stride + 1)]; line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0; b = prev[x]; c = prev[x - bpp] if x >= bpp else 0
            if ft == 1: line[x] = (line[x] + a) & 255
            elif ft == 2: line[x] = (line[x] + b) & 255
            elif ft == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line; out = []
        for x in range(w):
            if depth < 8:
                v = (line[x * depth // 8] >> (8 - depth - (x * depth) % 8)) & ((1 << depth) - 1)
                if ctype == 3 and pal: out.append(sum(pal[v * 3:v * 3 + 3]) / 3)
                else: out.append(v * 255 / ((1 << depth) - 1))
            else:
                i = x * ch
                if ctype == 3: k = line[i] * 3; out.append(sum(pal[k:k + 3]) / 3 if pal else 0)
                elif ctype in (0, 4): out.append(line[i] if ctype == 0 or line[i + 1] > 127 else 255)
                else: out.append(sum(line[i:i + 3]) / 3 if ctype == 2 or line[i + 3] > 127 else 255)
        rows.append(out)
    return w, h, rows


def qr_modules(png):
    """从二维码图片里取出模块矩阵（True = 黑点）。"""
    w, h, g = png_gray(png)
    dark = [[v < 128 for v in r] for r in g]
    ys = [y for y in range(h) if any(dark[y])]
    if not ys: raise ValueError('图里没有二维码')
    top, bottom = ys[0], ys[-1]
    xs = [x for x in range(w) if any(dark[y][x] for y in range(top, bottom + 1))]
    left, right = xs[0], xs[-1]
    # 左上定位角横穿过去是 黑1:白1:黑3:白1:黑1 = 7 个模块；圆角定位角也适用
    ests = []
    for y in range(top, top + (bottom - top) // 3):
        runs, x, cur = [], left, True
        while x <= right and len(runs) < 5:
            k = x
            while k <= right and dark[y][k] == cur: k += 1
            runs.append(k - x); x = k; cur = not cur
        if len(runs) == 5 and x - left > 0:
            u = sum(runs) / 7
            if all(abs(r - e * u) <= max(1.0, u * .6) for r, e in zip(runs, (1, 1, 3, 1, 1))): ests.append(u)
    if not ests: raise ValueError('找不到二维码定位角')
    ests.sort(); mod = ests[len(ests) // 2]
    n = round(((right - left + 1) / mod - 17) / 4) * 4 + 17          # 对齐到合法尺寸 21、25、29 …
    if not 21 <= n <= 177: raise ValueError('识别不出二维码尺寸')
    mod = (right - left + 1) / n; modv = (bottom - top + 1) / n
    return [[dark[min(h - 1, int(top + (r + .5) * modv))][min(w - 1, int(left + (c + .5) * mod))] for c in range(n)] for r in range(n)]


def qr_png(png, scale=10, quiet=4):
    """重新画一张清晰的大二维码（原图太小或带 logo 时更好扫）；内容与原图逐模块一致。"""
    m = qr_modules(png); n = len(m); W = (n + 2 * quiet) * scale
    rows = []
    for y in range(W):
        r = y // scale - quiet
        rows.append(b'\x00' + bytes(0 if (0 <= r < n and 0 <= x // scale - quiet < n and m[r][x // scale - quiet]) else 255
                                    for x in range(W)))
    ck = lambda t, b: struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + ck(b'IHDR', struct.pack('>IIBBBBB', W, W, 8, 0, 0, 0, 0)) +
            ck(b'IDAT', zlib.compress(b''.join(rows), 9)) + ck(b'IEND', b''))


def publish(png, path, say):
    """导出脚本用：写出二维码图片（尽量换成清晰大图），输出 QR_READY 和一份文字二维码。"""
    try: big = qr_png(png)
    except Exception: big = None
    with open(path, 'wb') as f: f.write(big or png)
    say('QR_READY', path)
    try:
        say('QR_TEXT_BEGIN'); print(qr_text(png), flush=True); say('QR_TEXT_END')
    except Exception:
        pass


def qr_text(png, invert=False, quiet=2):
    m = qr_modules(png); n = len(m)
    grid = [[False] * (n + 2 * quiet) for _ in range(quiet)] + \
           [[False] * quiet + row + [False] * quiet for row in m] + \
           [[False] * (n + 2 * quiet) for _ in range(quiet + 1)]
    if invert: grid = [[not v for v in r] for r in grid]
    lines = []
    for y in range(0, len(grid) - 1, 2):
        a, b = grid[y], grid[y + 1]
        lines.append(''.join('█' if p and q else '▀' if p else '▄' if q else ' ' for p, q in zip(a, b)).rstrip() or ' ')
    return '\n'.join(lines)


if __name__ == '__main__':
    if len(sys.argv) < 2: print(__doc__); sys.exit(1)
    print(qr_text(open(sys.argv[1], 'rb').read(), invert='--invert' in sys.argv))
