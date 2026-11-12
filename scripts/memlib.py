#!/usr/bin/env python3
"""
时光手帐 · 回忆数据包工具库（只用 Python 3 标准库）

数据包结构（网页只读这里）：
    memories/
    ├── memories.json      唯一的数据文件
    └── photos/<来源>/...   所有照片（qzone / photos / weibo / netdisk / manual …）

所有导入脚本都通过这里读写，保证格式一致。也可以直接在命令行检查数据包：
    python3 memlib.py check --root memories
    python3 memlib.py clean --root memories     # 删除空白 / 占位图和失效记录

memories.json 的格式见 references/data-format.md。
"""
import hashlib, json, os, struct, sys, tempfile, zlib
from datetime import datetime

FORMAT, VERSION = 'life-journal/memories', 1
IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
# QQ 空间给“已删除 / 已失效”图片返回的占位图（内容指纹）
KNOWN_PLACEHOLDERS = {
    '3d39d24f2854caf57d365d7e8aa4f5f9', '2450935108902c7f7dbbfcff90930248',
    'ccb7f9e45d5c8e56b424221d75e5fa66', '6fc46c67597b30d6f0552ecb8a3849e0',
}


# ---------------------------------------------------------------- store
def store_file(root):
    return os.path.join(root, 'memories.json')


def load(root):
    p = store_file(root)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        assert data.get('format') == FORMAT, f'{p} 不是时光手帐的数据文件'
        return data
    return {'format': FORMAT, 'version': VERSION, 'updated_at': None, 'sources': {}, 'memories': []}


def _sort_key(m):
    d = m.get('date') or ''
    return (1 if d else 0, d, m.get('time') or '')


def save(root, data):
    os.makedirs(root, exist_ok=True)
    data['format'], data['version'] = FORMAT, VERSION
    data['updated_at'] = datetime.now().isoformat(timespec='seconds')
    data['memories'].sort(key=_sort_key, reverse=True)          # 最新在前，日期未知的放最后
    fd, tmp = tempfile.mkstemp(dir=root, suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, store_file(root))                           # 原子写入，中途断掉也不会写坏


def upsert(data, mem):
    """同一个 id 只保留一条（重复导入会覆盖旧的，不会重复）。"""
    for i, m in enumerate(data['memories']):
        if m['id'] == mem['id']:
            keep = {k: m[k] for k in ('title', 'text', 'place', 'tags') if m.get(k) and k not in (mem.get('generated') or []) and not mem.get(k)}
            data['memories'][i] = {**mem, **keep}               # 用户补写过的内容不被空值覆盖
            return
    data['memories'].append(mem)


def note_source(data, source, **info):
    data.setdefault('sources', {})[source] = {**data.get('sources', {}).get(source, {}), **info,
                                              'imported_at': datetime.now().isoformat(timespec='seconds')}


# ---------------------------------------------------------------- images
def sniff_ext(head):
    if head[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
    if head[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP': return '.webp'
    if head[:2] == b'\xff\xd8': return '.jpg'
    return None


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def find(data, mem_id):
    return next((m for m in data['memories'] if m['id'] == mem_id), None)


def image_size(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(30)
            if head[:8] == b'\x89PNG\r\n\x1a\n': return struct.unpack('>II', head[16:24])
            if head[:6] in (b'GIF87a', b'GIF89a'): return struct.unpack('<HH', head[6:10])
            if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                if head[12:16] == b'VP8X': return (1 + int.from_bytes(head[24:27], 'little'), 1 + int.from_bytes(head[27:30], 'little'))
                if head[12:16] == b'VP8 ': f.seek(26); w, h = struct.unpack('<HH', f.read(4)); return (w & 0x3fff, h & 0x3fff)
                if head[12:16] == b'VP8L': f.seek(21); b = f.read(4); return (1 + (((b[1] & 0x3f) << 8) | b[0]), 1 + (((b[3] & 0xf) << 10) | (b[2] << 2) | ((b[1] & 0xc0) >> 6)))
                return None
            if head[:2] != b'\xff\xd8': return None
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b'\xff': b = f.read(1)
                while b == b'\xff': b = f.read(1)
                if not b: return None
                m = b[0]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    f.read(3); h, w = struct.unpack('>HH', f.read(4)); return (w, h)
                ln = struct.unpack('>H', f.read(2))[0]; f.seek(ln - 2, 1)
    except Exception:
        return None


def _std(values):
    if not values: return 999
    n = len(values); mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** .5


def _png_lum(data):
    """解码 PNG（8 位、非隔行），返回采样亮度；不支持的格式返回 None。"""
    pos, w = 8, None; idat = b''; palette = None
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]; typ = data[pos + 4:pos + 8]; body = data[pos + 8:pos + 8 + ln]; pos += 12 + ln
        if typ == b'IHDR': w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE': palette = body
        elif typ == b'IDAT': idat += body
        elif typ == b'IEND': break
    if w is None or depth != 8 or inter != 0 or ctype not in (0, 2, 3, 4, 6): return None
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]; stride = w * bpp
    raw = zlib.decompress(idat); prev = bytearray(stride); lum = []; step = max(1, h // 24)
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
        prev = line
        if y % step: continue
        for x in range(0, w, max(1, w // 24)):
            i = x * bpp
            if ctype == 3 and palette: k = line[i] * 3; r, g, b = palette[k:k + 3] if k + 3 <= len(palette) else (0, 0, 0)
            elif ctype in (0, 4): r = g = b = line[i]
            else: r, g, b = line[i:i + 3]
            lum.append((r + g + b) / 3)
    return lum


def _gif_lum(data):
    """解码 GIF 第一帧（LZW），返回采样亮度。"""
    w, h, flags = struct.unpack('<HHB', data[6:11]); pos = 13; gct = None
    if flags & 0x80: n = 3 * (2 << (flags & 7)); gct = data[pos:pos + n]; pos += n
    while pos < len(data):
        b = data[pos]
        if b == 0x21:                                           # 扩展块：跳过
            pos += 2
            while data[pos]: pos += data[pos] + 1
            pos += 1
        elif b == 0x2C:
            iw, ih, lf = struct.unpack('<4xHHB', data[pos + 1:pos + 10]); pos += 10; table = gct
            if lf & 0x80: n = 3 * (2 << (lf & 7)); table = data[pos:pos + n]; pos += n
            if lf & 0x40 or not table: return None              # 隔行或没有调色板：不判断
            mcs = data[pos]; pos += 1; buf = bytearray()
            while data[pos]: buf += data[pos + 1:pos + 1 + data[pos]]; pos += data[pos] + 1
            clear, eoi = 1 << mcs, (1 << mcs) + 1; size = mcs + 1
            dic = [[i] for i in range(clear)] + [[], []]; out = []; prev = None; bitpos = 0; total = len(buf) * 8
            while bitpos + size <= total and len(out) < iw * ih:
                code = 0
                for k in range(size):
                    if buf[(bitpos + k) >> 3] >> ((bitpos + k) & 7) & 1: code |= 1 << k
                bitpos += size
                if code == clear: dic = dic[:clear + 2]; size = mcs + 1; prev = None; continue
                if code == eoi: break
                if code < len(dic): entry = dic[code]
                elif prev is not None: entry = prev + prev[:1]
                else: break
                out += entry
                if prev is not None:
                    dic.append(prev + entry[:1])
                    if len(dic) == 1 << size and size < 12: size += 1
                prev = entry
            lum = []
            for i in range(0, len(out), max(1, len(out) // 600)):
                k = out[i] * 3; r, g, b = table[k:k + 3] if k + 3 <= len(table) else (255, 255, 255); lum.append((r + g + b) / 3)
            return lum
        else:
            break
    return None


def is_blank(path):
    """几乎纯色的图片（空白贴图、灰色占位图）。JPEG/WebP 不解码，返回 False。"""
    try:
        with open(path, 'rb') as f: data = f.read()
        ext = sniff_ext(data[:12])
        lum = _png_lum(data) if ext == '.png' else _gif_lum(data) if ext == '.gif' else None
        return lum is not None and _std(lum) <= 10
    except Exception:
        return False


# ---------------------------------------------------------------- EXIF
def exif_datetime(path):
    """读取 JPEG 的拍摄时间（DateTimeOriginal，其次 DateTime）。读不到返回 None，不拿文件修改时间冒充。"""
    try:
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8': return None
            while True:
                b = f.read(1)
                while b and b != b'\xff': b = f.read(1)
                while b == b'\xff': b = f.read(1)
                if not b or b[0] in (0xD9, 0xDA): return None
                ln = struct.unpack('>H', f.read(2))[0]; seg = f.read(ln - 2)
                if b[0] == 0xE1 and seg[:6] == b'Exif\x00\x00': return _parse_exif(seg[6:])
    except Exception:
        return None


def _parse_exif(t):
    e = '<' if t[:2] == b'II' else '>'
    u16 = lambda o: struct.unpack(e + 'H', t[o:o + 2])[0]; u32 = lambda o: struct.unpack(e + 'I', t[o:o + 4])[0]
    def ifd(off):
        tags = {}
        for i in range(u16(off)):
            p = off + 2 + i * 12; tag, typ, cnt = u16(p), u16(p + 2), u32(p + 4)
            if typ == 2:
                vo = p + 8 if cnt <= 4 else u32(p + 8); tags[tag] = t[vo:vo + cnt].rstrip(b'\x00').decode('ascii', 'ignore')
            elif typ == 4: tags[tag] = u32(p + 8)
        return tags
    ifd0 = ifd(u32(4)); val = None
    if 0x8769 in ifd0: val = ifd(ifd0[0x8769]).get(0x9003)
    val = val or ifd0.get(0x0132)
    try: return datetime.strptime(val.strip(), '%Y:%m:%d %H:%M:%S') if val else None
    except ValueError: return None


# ---------------------------------------------------------------- check / clean
def check(root, fix=False):
    data = load(root); problems = []; photos = 0; removed = 0
    seen_hash = {}
    for m in list(data['memories']):
        ok = []
        for ph in m.get('photos') or []:
            p = os.path.join(root, ph['file'])
            if not os.path.exists(p): problems.append(f"缺少文件 {ph['file']}"); continue
            hsh = md5(p)
            if hsh in KNOWN_PLACEHOLDERS or is_blank(p):
                problems.append(f"空白/占位图 {ph['file']}")
                if fix: os.remove(p); removed += 1
                continue
            seen_hash.setdefault(hsh, []).append(m['id']); ok.append(ph)
        if fix: m['photos'] = ok
        photos += len(ok)
        if not ok:
            problems.append(f"没有照片的回忆 {m['id']}")
            if fix: data['memories'].remove(m)
    if fix: save(root, data)
    years = {}
    for m in data['memories']: y = (m.get('date') or '日期未知')[:4]; years[y] = years.get(y, 0) + 1
    return {'memories': len(data['memories']), 'photos': photos, 'years': dict(sorted(years.items())), 'problems': problems, 'removed_files': removed}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='检查 / 清理时光手帐的回忆数据包')
    ap.add_argument('cmd', choices=['check', 'clean'])
    ap.add_argument('--root', default='memories')
    a = ap.parse_args()
    r = check(a.root, fix=(a.cmd == 'clean'))
    print(json.dumps(r, ensure_ascii=False, indent=1))
