#!/usr/bin/env python3
"""
生成用来发布 / 分享的“分享副本”（只用 Python 3 标准库；macOS 上用系统自带 sips 缩小照片）

  python3 build_share.py --project ~/时光手帐                    # 输出到 ~/时光手帐/share/
  python3 build_share.py --project ~/时光手帐 --years 2019 2020 --exclude qzone:4c70… --no-place
  python3 build_share.py --project ~/时光手帐 --budget-mb 48     # 按发布平台的上限（豆包 48 / 飞书妙搭 19）：超了自动逐级缩小照片
  python3 build_share.py --project ~/时光手帐 --out ~/时光手帐/share-edgeone   # EdgeOne 单独用一个目录（里面会有 .edgeone 关联）
  python3 build_share.py --project ~/时光手帐 --zip              # 另外打一个 share.zip（想单独发文件时用）

分享副本 = 一个纯静态网站文件夹，交给发布平台（豆包网页发布 / 宿主自带发布 / 飞书妙搭 / EdgeOne Makers）：
  share/index.html  flip.mp3  robots.txt
  share/memories/memories.json      清理过的数据（见下）
  share/memories/photos/0001.jpg …  重新编号的照片
  share/music/…                     背景音乐（默认带上项目里的歌单；--no-music 不带）

为了分享安全，副本和本机项目不同：
  - 照片去掉 EXIF / XMP / IPTC 等元数据：**GPS 定位、相机型号、拍摄设备等都不会带出去**
    （只保留“照片方向”，否则竖拍的照片会躺倒）。PNG 去掉文字块，WebP 去掉 EXIF/XMP 块。
  - 照片默认把长边缩到 2048 像素（--max-side，0 = 不缩；只在 macOS 上可用），网页够清楚，体积小很多。
  - 回忆和照片重新编号：不带 QQ 说说编号、微博编号、网盘路径、原文件名等能反查到账号的信息。
  - 去掉导入记录（sources）、AI 标注（generated）、预览标记等内部字段。
  - 默认带上背景音乐（项目歌单里的歌）。歌曲可能有版权，发布前会提醒用户；不想带就加 --no-music。
  - 网页加 noindex，robots.txt 禁止搜索引擎收录（只防正规搜索引擎，不是访问控制）。
  - 不带 inbox/、二维码、取图索引、publish.json 等任何本机文件。
可选：--years 只要某几年 / --exclude <回忆id …> 去掉某几段 / --no-text 不带正文和标题 / --no-place 不带地点
      --no-source 不显示“来自 QQ 空间 / 微博”

输出：CHECK <回忆id> <原因> <片段>（文字里疑似手机号、身份证号、邮箱、@别人、门牌地址，让用户决定）
      MEMORIES / PHOTOS / YEARS / TEXTS / PLACES / STRIPPED / RESIZED / MUSIC / SIZE / OUT [/ ZIP]
      （发布前把这些数字和 CHECK 原样告诉用户）
"""
import argparse, json, os, platform, re, shutil, struct, subprocess, sys, tempfile, zipfile
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memlib

MARK = '.life-journal-share'
KEEP = ('.edgeone',)          # 发布工具在副本里留下的项目关联（EdgeOne），重建时保留、不计入体积、不打包


def walk_files(d):
    for dp, dirs, ns in os.walk(d):
        dirs[:] = [x for x in dirs if x not in KEEP]
        for n in ns:
            if n != MARK: yield os.path.join(dp, n)
SENSITIVE = [                                   # 只是提醒：命中后让用户决定这段要不要分享（--exclude / --no-text）
    ('手机号', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')),
    ('身份证号', re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')),
    ('邮箱', re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')),
    ('提到了别人（@）', re.compile(r'(?<![\w.+-])@[\w一-鿿-]{1,20}')),
    ('门牌地址', re.compile(r'[一-鿿]{2,}(路|街|巷|弄)\d+号|\d+(栋|幢|单元)\d*')),
]


def say(tag, *parts):
    print(tag, *parts, flush=True)


# ------------------------------------------------------------ 去元数据
def jpeg_orientation(app1):
    """从 APP1 Exif 段里读出 Orientation（1–8），读不到返回 1。"""
    try:
        t = app1[6:]
        le = t[:2] == b'II'; E = '<' if le else '>'
        off = struct.unpack(E + 'I', t[4:8])[0]; n = struct.unpack(E + 'H', t[off:off + 2])[0]
        for i in range(n):
            e = t[off + 2 + i * 12: off + 14 + i * 12]
            if struct.unpack(E + 'H', e[:2])[0] == 0x0112:
                v = struct.unpack(E + 'H', e[8:10])[0]
                return v if 1 <= v <= 8 else 1
    except Exception:
        pass
    return 1


def minimal_exif(orient):
    tiff = b'MM\x00*' + struct.pack('>I', 8) + struct.pack('>H', 1) + \
           struct.pack('>HHIHH', 0x0112, 3, 1, orient, 0) + struct.pack('>I', 0)
    body = b'Exif\x00\x00' + tiff
    return b'\xff\xe1' + struct.pack('>H', len(body) + 2) + body


def strip_jpeg(data):
    if data[:2] != b'\xff\xd8': return data, False
    out, pos, orient, removed = [b'\xff\xd8'], 2, 1, False
    while pos + 4 <= len(data):
        if data[pos] != 0xff: return data, False                 # 结构异常：原样返回，交给调用方决定
        m = data[pos + 1]
        if m == 0xff: pos += 1; continue
        if m == 0xda:                                           # 开始扫描：后面是图像数据，原样保留
            out.append(data[pos:]); break
        if 0xd0 <= m <= 0xd9 or m == 0x01:
            out.append(data[pos:pos + 2]); pos += 2; continue
        ln = struct.unpack('>H', data[pos + 2:pos + 4])[0]; seg = data[pos:pos + 2 + ln]
        payload = seg[4:]
        if m == 0xe1:                                           # Exif / XMP
            if payload[:6] == b'Exif\x00\x00': orient = jpeg_orientation(payload)
            removed = True
        elif m in (0xed, 0xfe) or (m == 0xe2 and not payload.startswith(b'ICC_PROFILE')) or 0xe3 <= m <= 0xef and m != 0xee:
            removed = True                                       # IPTC / 注释 / 厂商私有段（保留 ICC 色彩和 Adobe 段）
        else:
            out.append(seg)
        pos += 2 + ln
    else:
        return data, False
    if orient != 1: out.insert(1, minimal_exif(orient))
    return b''.join(out), removed


def strip_png(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n': return data, False
    out, pos, removed = [data[:8]], 8, False
    while pos + 8 <= len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]; typ = data[pos + 4:pos + 8]; chunk = data[pos:pos + 12 + ln]
        if typ in (b'tEXt', b'iTXt', b'zTXt', b'eXIf', b'tIME'): removed = True
        else: out.append(chunk)
        pos += 12 + ln
        if typ == b'IEND': break
    return b''.join(out), removed


def strip_webp(data):
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP': return data, False
    out, pos, removed = [], 12, False
    while pos + 8 <= len(data):
        typ = data[pos:pos + 4]; ln = struct.unpack('<I', data[pos + 4:pos + 8])[0]; chunk = data[pos:pos + 8 + ln + (ln & 1)]
        if typ in (b'EXIF', b'XMP '): removed = True
        else:
            if typ == b'VP8X': chunk = chunk[:8] + bytes([chunk[8] & ~0x0c]) + chunk[9:]   # 清掉“有 EXIF/XMP”标志
            out.append(chunk)
        pos += 8 + ln + (ln & 1)
    body = b'WEBP' + b''.join(out)
    return b'RIFF' + struct.pack('<I', len(body)) + body, removed


def strip_meta(data, ext):
    return {'.jpg': strip_jpeg, '.png': strip_png, '.webp': strip_webp}.get(ext, lambda d: (d, False))(data)


def can_resize():
    return platform.system() == 'Darwin' and bool(shutil.which('sips'))


def tar_size(d):
    """估算发布平台打包后的 tar.gz 大小（不含分享副本标记文件）。"""
    import tarfile
    with tempfile.TemporaryFile() as f:
        with tarfile.open(fileobj=f, mode='w:gz') as t:
            for p in sorted(walk_files(d)): t.add(p, os.path.relpath(p, d))
        return f.tell()


def resize(src, dst, max_side, quality=None):
    """macOS sips 等比缩小到长边 max_side（给了 quality 时 JPEG 一律按这个质量重存）；没做任何事返回 False。"""
    if not max_side or not can_resize(): return False
    wh = memlib.image_size(src)
    jpeg = open(src, 'rb').read(2) == b'\xff\xd8'
    if not wh or (max(wh) <= max_side and not (quality and jpeg)): return False
    cmd = ['sips'] + (['-s', 'formatOptions', str(quality)] if quality and jpeg else []) + \
          (['-Z', str(max_side)] if max(wh) > max_side else [])
    try:
        subprocess.run(cmd + [src, '--out', dst], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return os.path.exists(dst)
    except Exception:
        return False


# ------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description='生成用于发布 / 分享的时光手帐静态副本')
    ap.add_argument('--project', required=True)
    ap.add_argument('--out', help='默认 <项目>/share')
    ap.add_argument('--years', nargs='*', help='只要这些年份（日期未知的写 unknown）')
    ap.add_argument('--exclude', nargs='*', default=[], help='不分享的回忆 id')
    ap.add_argument('--no-text', action='store_true'); ap.add_argument('--no-place', action='store_true')
    ap.add_argument('--no-source', action='store_true')
    ap.add_argument('--no-music', action='store_true', help='不带背景音乐（默认带上项目歌单里的歌）')
    ap.add_argument('--with-music', action='store_true', help=argparse.SUPPRESS)   # 旧参数，现在默认就带
    ap.add_argument('--max-side', type=int, default=2048)
    ap.add_argument('--budget-mb', type=float, default=0, help='压缩后体积上限（豆包 48、飞书妙搭 19，其他按平台）；超了自动逐级缩小照片')
    ap.add_argument('--zip', action='store_true', help='另外打包 share.zip')
    a = ap.parse_args()
    project = os.path.abspath(os.path.expanduser(a.project))

    root = os.path.join(project, 'memories')
    if not os.path.exists(os.path.join(project, 'index.html')): say('ERROR', '项目里没有 index.html'); sys.exit(1)
    data = memlib.load(root)
    out = os.path.abspath(os.path.expanduser(a.out or os.path.join(project, 'share')))
    if out == project or out.startswith(root + os.sep) or project.startswith(out + os.sep):
        say('ERROR', '--out 不能是项目目录本身、上级目录或 memories 里面'); sys.exit(1)
    if os.path.exists(out):
        if os.listdir(out) and not os.path.exists(os.path.join(out, MARK)):
            say('ERROR', f'{out} 已存在且不是之前生成的分享副本，换一个 --out'); sys.exit(1)
        kept = tempfile.mkdtemp(prefix='lj-keep-')
        for k in KEEP:
            if os.path.isdir(os.path.join(out, k)): shutil.move(os.path.join(out, k), os.path.join(kept, k))
        shutil.rmtree(out); os.makedirs(out)
        for k in os.listdir(kept): shutil.move(os.path.join(kept, k), os.path.join(out, k))
        shutil.rmtree(kept, ignore_errors=True)
    os.makedirs(os.path.join(out, 'memories', 'photos')); os.makedirs(os.path.join(out, 'music'))
    open(os.path.join(out, MARK), 'w').close()

    years = set(a.years or [])
    mems = [m for m in data['memories'] if m.get('photos') and m['id'] not in set(a.exclude)
            and (not years or (m.get('date') or 'unknown')[:4] in years or (not m.get('date') and 'unknown' in years))]
    mems.sort(key=lambda m: (m.get('date') is None, m.get('date') or '', m.get('time') or ''))
    plan = [(i, m) for i, m in enumerate(mems, 1)]

    def build_photos(max_side, quality, report):
        pdir = os.path.join(out, 'memories', 'photos'); shutil.rmtree(pdir, ignore_errors=True); os.makedirs(pdir)
        tmp = tempfile.mkdtemp(prefix='lj-share-')
        st = {'clean': [], 'n': 0, 'stripped': 0, 'resized': 0, 'texts': 0, 'places': 0, 'yrs': {}}
        try:
            for i, m in plan:
                photos = []
                for ph in m['photos']:
                    src = os.path.join(root, ph['file'])
                    if not os.path.exists(src) or memlib.is_blank(src): continue
                    use = src; small = os.path.join(tmp, 'r' + os.path.splitext(src)[1])
                    if resize(src, small, max_side, quality): use = small; st['resized'] += 1
                    raw = open(use, 'rb').read(); ext = memlib.sniff_ext(raw[:12])
                    if not ext: continue
                    raw, rm = strip_meta(raw, ext); st['stripped'] += rm
                    st['n'] += 1; rel = f'photos/{st["n"]:04d}{ext}'
                    with open(os.path.join(out, 'memories', rel), 'wb') as f: f.write(raw)
                    wh = memlib.image_size(os.path.join(out, 'memories', rel))
                    photos.append({'file': rel, **({'width': wh[0], 'height': wh[1]} if wh else {})})
                if not photos: continue
                c = {'id': f'm{i:04d}', 'source': '' if a.no_source else m.get('source', ''),
                     'date': m.get('date'), 'time': m.get('time'),
                     'title': '' if a.no_text else (m.get('title') or ''), 'text': '' if a.no_text else (m.get('text') or ''),
                     'place': '' if a.no_place else (m.get('place') or ''), 'tags': list(m.get('tags') or []), 'photos': photos}
                if report:
                    for why, rx in SENSITIVE:
                        for k in ('title', 'text', 'place'):
                            hit = rx.search(c[k] or '')
                            if hit: say('CHECK', m['id'], why, '「' + (c[k][max(0, hit.start() - 8):hit.end() + 8]).replace('\n', ' ') + '」'); break
                st['texts'] += bool(c['text'] or c['title']); st['places'] += bool(c['place'])
                y = (c['date'] or '日期未知')[:4]; st['yrs'][y] = st['yrs'].get(y, 0) + 1
                st['clean'].append(c)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return st

    st = build_photos(a.max_side, None, True)
    clean, n_photo, stripped, resized, texts, places, yrs = (st[k] for k in ('clean', 'n', 'stripped', 'resized', 'texts', 'places', 'yrs'))
    if not clean: shutil.rmtree(out); say('ERROR', '筛选后没有可以分享的回忆'); sys.exit(1)
    with open(os.path.join(out, 'memories', 'memories.json'), 'w', encoding='utf-8') as f:
        json.dump({'format': 'life-journal/memories', 'version': 1, 'memories': clean}, f, ensure_ascii=False, indent=1)

    # 网页、翻书声、noindex
    html = open(os.path.join(project, 'index.html'), encoding='utf-8').read()
    if 'name="robots"' not in html:
        html = re.sub(r'(<head[^>]*>)', r'\1\n<meta name="robots" content="noindex, nofollow">', html, count=1)
    with open(os.path.join(out, 'index.html'), 'w', encoding='utf-8') as f: f.write(html)
    for extra in ('flip.mp3',):
        if os.path.exists(os.path.join(project, extra)): shutil.copy2(os.path.join(project, extra), os.path.join(out, extra))
    with open(os.path.join(out, 'robots.txt'), 'w') as f: f.write('User-agent: *\nDisallow: /\n')

    tracks = []
    if not a.no_music:
        try: tracks = json.load(open(os.path.join(project, 'music', 'playlist.json'), encoding='utf-8')).get('tracks') or []
        except (OSError, ValueError): tracks = []
        kept = []
        for t in tracks:
            fn = t.get('file') if isinstance(t, dict) else t
            if fn and os.path.exists(os.path.join(project, 'music', fn)):
                shutil.copy2(os.path.join(project, 'music', fn), os.path.join(out, 'music', os.path.basename(fn)))
                kept.append({**(t if isinstance(t, dict) else {}), 'file': os.path.basename(fn)})
        tracks = kept
    with open(os.path.join(out, 'music', 'playlist.json'), 'w', encoding='utf-8') as f:
        json.dump({'tracks': tracks}, f, ensure_ascii=False)

    used_side, used_q = a.max_side, None
    if a.budget_mb:
        steps = [(1600, 80), (1280, 75), (1024, 70), (800, 65)]
        while tar_size(out) > a.budget_mb * 1048576 and steps and can_resize():
            used_side, used_q = steps.pop(0)
            say('SHRINK', f'超过 {a.budget_mb}MB，照片改为长边 {used_side}、质量 {used_q} 重新生成')
            st = build_photos(used_side, used_q, False)
            n_photo, stripped, resized, clean = st['n'], st['stripped'], st['resized'], st['clean']
            with open(os.path.join(out, 'memories', 'memories.json'), 'w', encoding='utf-8') as f:
                json.dump({'format': 'life-journal/memories', 'version': 1, 'memories': clean}, f, ensure_ascii=False, indent=1)
        tz = tar_size(out)
        mb = lambda d: sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d)) / 1048576
        say('BUDGET', 'ok' if tz <= a.budget_mb * 1048576 else 'over', f'压缩后约 {tz / 1048576:.1f}MB / 上限 {a.budget_mb}MB',
            f'（照片 {mb(os.path.join(out, "memories", "photos")):.1f}MB，音乐 {mb(os.path.join(out, "music")):.1f}MB）')
    files = list(walk_files(out))
    size = sum(os.path.getsize(p) for p in files)
    biggest = max((os.path.getsize(p), os.path.basename(p)) for p in files)
    say('MEMORIES', len(clean)); say('PHOTOS', n_photo)
    say('YEARS', ' '.join(f'{k}:{v}' for k, v in sorted(yrs.items())))
    say('TEXTS', texts, '段带文字' + ('（已按 --no-text 去掉）' if a.no_text else ''))
    say('PLACES', places, '段带地点' + ('（已按 --no-place 去掉）' if a.no_place else ''))
    say('STRIPPED', stripped, '张照片去掉了元数据（含 GPS 定位）')
    say('RESIZED', resized, (f'张缩到长边 {used_side}' + (f'、质量 {used_q}' if used_q else '')) if used_side else '（未缩小）')
    say('MUSIC', len(tracks), '首' + ('（已按 --no-music 去掉）' if a.no_music else '（提醒用户：歌曲可能有版权）' if tracks else ''))
    say('SIZE', f'{size / 1048576:.1f}MB', f'文件 {len(files)} 个，最大 {biggest[1]} {biggest[0] / 1048576:.1f}MB')
    say('OUT', out)
    if a.zip:
        zp = out.rstrip('/') + '.zip'
        with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in walk_files(out): z.write(p, os.path.relpath(p, out))
        say('ZIP', zp)


if __name__ == '__main__':
    main()
