#!/usr/bin/env python3
"""
把已经在电脑上的照片导入时光手帐的回忆数据包（只用 Python 3 标准库 + 同目录的 memlib.py）

  python3 import_photos.py --from ~/Pictures/旅行 --root memories
  python3 import_photos.py --from a.jpg b.png 相册.zip 另一个文件夹 --root memories   # 文件、文件夹、ZIP 可以混着给
  python3 import_photos.py --from ~/Downloads/百度网盘/猫 --root memories --source netdisk
  python3 import_photos.py --from ./猫猫 --root memories --group photo               # 每张照片单独一段回忆

适用于：本地相册文件夹、手机/「照片」App 导出的原图、网盘客户端下载或同步到本地的文件夹
（百度网盘、阿里云盘、iCloud 云盘、OneDrive…）、Google 相册 Takeout 的 ZIP、聊天里收到后另存的图片。
--source 只决定照片放在 photos/<来源>/ 下面、网页上显示的来源：photos（默认，显示“相册”）/ netdisk（“网盘”）/ 其他任意词。

规则：
  - 只读源文件，复制到数据包；按内容去重（同一张照片导入两次只保留一份）。ZIP 不解压到别处，直接逐张读取。
  - 拍摄时间：JPEG 的 EXIF（DateTimeOriginal）→ Google Takeout 旁边的 .json 里的 photoTakenTime。
    都读不到就记为“日期未知”，绝不拿文件修改时间 / 上传时间 / 下载时间冒充拍摄时间。
  - HEIC/HEIF（iPhone 照片）：macOS 上用系统自带的 sips 转成 JPEG（保留拍摄时间），其他系统跳过并提示导出 JPEG。
  - iCloud 等云盘“只在云端”的占位文件不是照片本身，跳过并提示先下载到本地。
  - 默认同一天拍的照片合成一段回忆（--group day）；日期未知的照片每张单独一段。几乎纯色的空白图不导入。
  - 标题和正文留空，之后由用户补写，或由 AI 看图写草稿（写入时在 generated 里标明）。
  - --manifest（可选）：相册说明 JSON。netdisk_fetch.py 会自动生成；也可以由 Agent 按用户的话写：
      {"title": "2019 杭州", "place": "杭州", "tags": ["旅行"], "text": "",          ← 整个相册的默认值
       "files": {"IMG_0001.JPG": {"date": "2019-05-20", "time": "14:30", "text": "…", "preview": true}}}
    files 的键是原文件名。date 只在照片自己没有拍摄时间时使用，且只能写用户确认过的日期
    （YYYY-MM-DD / YYYY-MM / YYYY），绝不写网盘上传或修改时间。preview=true 表示这是缩略图而非原图。

输出给 Agent 的进度行：FOUND <n> / CONVERTED <n> / COPIED <n> / SKIPPED <原因> <文件> / DONE <memories.json> <回忆数> <照片数>
"""
import argparse, io, json, os, platform, re, shutil, subprocess, sys, tempfile, zipfile
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memlib

HEIC = ('.heic', '.heif')
UNSUPPORTED = ('.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.tif', '.tiff', '.bmp', '.psd')
MAX_ZIP_FILE = 80 << 20            # ZIP 里单张超过 80MB 不读（防止异常压缩包）
MAX_ZIP_TOTAL = 8 << 30


def say(tag, *parts):
    print(tag, *parts, flush=True)


class Item:
    """一张待导入的照片：可能在磁盘上，也可能在 ZIP 里。"""
    def __init__(self, name, path=None, zf=None, member=None, sidecar=None):
        self.name, self.path, self.zf, self.member, self.sidecar = name, path, zf, member, sidecar

    def read(self):
        if self.path:
            with open(self.path, 'rb') as f: return f.read()
        return self.zf.read(self.member)


def takeout_time(raw):
    """Google 相册 Takeout 旁边 json 里的拍摄时间（photoTakenTime 是拍摄时间；creationTime 是上传时间，不用）。"""
    try:
        ts = int(((json.loads(raw).get('photoTakenTime') or {}).get('timestamp')) or 0)
        return datetime.fromtimestamp(ts) if ts > 0 else None
    except Exception:
        return None


def valid_date(s):
    return bool(s) and bool(re.fullmatch(r'\d{4}(-\d{2}(-\d{2})?)?', s))


def sidecar_names(name):
    return [name + '.json', name + '.supplemental-metadata.json', os.path.splitext(name)[0] + '.json']


def collect(sources):
    items, zips = [], []
    def add_file(path, sibling_names):
        n = os.path.basename(path); low = n.lower()
        if n.startswith('.') and low.endswith('.icloud'):
            say('SKIPPED', 'iCloud 只在云端，请先在访达里右键“立即下载”', path); return
        if n.startswith('.'): return
        if low.endswith(UNSUPPORTED):
            say('SKIPPED', '格式浏览器无法显示，请先导出为 JPEG', path); return
        if not low.endswith(memlib.IMAGE_EXT + HEIC): return
        side = next((os.path.join(os.path.dirname(path), s) for s in sidecar_names(n) if s in sibling_names), None)
        items.append(Item(n, path=path, sidecar=side))
    for src in sources:
        src = os.path.expanduser(src)
        if not os.path.exists(src):
            say('SKIPPED', '找不到', src); continue
        if os.path.isdir(src):
            if src.rstrip('/').endswith('.photoslibrary'):
                say('SKIPPED', '这是「照片」App 的图库包，不能直接读：请在「照片」里选中照片 → 文件 → 导出 → 导出未修改的原件，再给导出的文件夹', src); continue
            for dp, dirs, names in os.walk(src):
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.photoslibrary')]
                ns = set(names)
                for n in sorted(names): add_file(os.path.join(dp, n), ns)
        elif src.lower().endswith('.zip'):
            try: zf = zipfile.ZipFile(src)
            except zipfile.BadZipFile:
                say('SKIPPED', '压缩包损坏或不是 ZIP', src); continue
            zips.append(zf); names = {i.filename for i in zf.infolist()}; total = 0
            for info in zf.infolist():
                n = info.filename; base = os.path.basename(n); low = base.lower()
                if info.is_dir() or base.startswith('.') or '__MACOSX' in n: continue
                if low.endswith(UNSUPPORTED): say('SKIPPED', '格式浏览器无法显示，请先导出为 JPEG', f'{src}:{n}'); continue
                if not low.endswith(memlib.IMAGE_EXT + HEIC): continue
                if info.file_size > MAX_ZIP_FILE or total + info.file_size > MAX_ZIP_TOTAL:
                    say('SKIPPED', '压缩包里的文件过大', f'{src}:{n}'); continue
                total += info.file_size
                side = next((os.path.dirname(n) + '/' + s if os.path.dirname(n) else s for s in sidecar_names(base)
                             if ((os.path.dirname(n) + '/' + s) if os.path.dirname(n) else s) in names), None)
                items.append(Item(base, zf=zf, member=n, sidecar=side))
        else:
            add_file(src, set(os.listdir(os.path.dirname(src) or '.')))
    return items, zips


def heic_to_jpeg(raw, tmpdir):
    """macOS 自带 sips 把 HEIC 转成 JPEG（EXIF 拍摄时间会保留）。其他系统返回 None。"""
    if platform.system() != 'Darwin' or not shutil.which('sips'): return None
    src = os.path.join(tmpdir, 'in.heic'); dst = os.path.join(tmpdir, 'out.jpg')
    with open(src, 'wb') as f: f.write(raw)
    try:
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '88', src, '--out', dst],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        with open(dst, 'rb') as f: return f.read()
    except Exception:
        return None
    finally:
        for p in (src, dst):
            try: os.remove(p)
            except OSError: pass


def main():
    ap = argparse.ArgumentParser(description='把电脑上的照片（文件、文件夹、ZIP）导入时光手帐的回忆数据包')
    ap.add_argument('--from', dest='src', nargs='+', required=True, help='照片文件、文件夹（递归）或 ZIP，可以给多个')
    ap.add_argument('--root', default='memories', help='回忆数据包目录（网页读取这里）')
    ap.add_argument('--group', choices=['day', 'photo'], default='day', help='同一天合成一段（day）或每张一段（photo）')
    ap.add_argument('--source', default='photos', help='来源标识：photos（相册）/ netdisk（网盘）/ 其他')
    ap.add_argument('--manifest', help='可选的相册说明 JSON（netdisk_fetch.py 会自动生成；Agent 也可以按用户的话写）')
    a = ap.parse_args()
    man = {}
    if a.manifest:
        try:
            with open(os.path.expanduser(a.manifest), encoding='utf-8') as f: man = json.load(f)
        except (OSError, ValueError) as e:
            say('ERROR', '读不了 --manifest：', e); sys.exit(1)
    man_files = man.get('files') or {}

    items, zips = collect(a.src)
    orig_of = lambda n: re.sub(r'\.preview(?=\.[^.]+$)', '', n, flags=re.I)
    names = {it.name for it in items}
    for it in [it for it in items if orig_of(it.name) != it.name and orig_of(it.name) in names]:
        items.remove(it); say('SKIPPED', '已有原图，跳过预览图', it.path or it.name)
    say('FOUND', len(items))
    dst_dir = os.path.join(a.root, 'photos', a.source); os.makedirs(dst_dir, exist_ok=True)
    data = memlib.load(a.root)
    groups = defaultdict(list)
    copied = converted = 0; seen = set()
    tmpdir = tempfile.mkdtemp(prefix='lj-')
    try:
        for it in items:
            label = it.path or f'{it.zf.filename}:{it.member}'
            raw = it.read()
            if it.name.lower().endswith(HEIC) or (raw[4:12] in (b'ftypheic', b'ftypheix', b'ftypmif1', b'ftyphevc')):
                if memlib.sniff_ext(raw[:12]):                  # 扩展名叫 HEIC，其实已经是 JPEG/PNG
                    pass
                else:
                    jpg = heic_to_jpeg(raw, tmpdir)
                    if not jpg: say('SKIPPED', 'HEIC 无法转换，请在相册里导出为 JPEG', label); continue
                    raw = jpg; converted += 1
            ext = memlib.sniff_ext(raw[:12])
            if not ext: say('SKIPPED', '不是可识别的图片', label); continue
            h = memlib.md5_bytes(raw)
            if h in seen: say('SKIPPED', '重复照片', label); continue
            seen.add(h)
            rel = f'photos/{a.source}/{h[:16]}{ext}'; dst = os.path.join(a.root, rel)
            if not os.path.exists(dst):
                with open(dst, 'wb') as f: f.write(raw)
                copied += 1
            if memlib.is_blank(dst):
                os.remove(dst); say('SKIPPED', '空白图', label); continue
            shot = memlib.exif_datetime(dst)
            if not shot and it.sidecar:
                try: shot = takeout_time(open(it.sidecar, 'rb').read() if it.path else it.zf.read(it.sidecar))
                except Exception: shot = None
            meta = man_files.get(it.name) or {}
            wh = memlib.image_size(dst)
            entry = {'file': rel, **({'width': wh[0], 'height': wh[1]} if wh else {}), 'name': it.name,
                     **({'preview': True} if meta.get('preview') else {})}
            day = shot.strftime('%Y-%m-%d') if shot else (meta.get('date') if valid_date(meta.get('date')) else None)
            key = day if (day and a.group == 'day') else h[:16]
            groups[key].append((shot, entry, meta))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for z in zips: z.close()
    if converted: say('CONVERTED', converted)
    say('COPIED', copied)

    # 之前导入的网盘缩略图，这次拿到了同名原图：把旧的预览图换掉（删掉预览、空了的回忆一并删掉）
    originals = {e['name'] for g in groups.values() for _, e, _ in g if not e.get('preview')}
    replaced = 0
    for m in list(data['memories']):
        if m.get('source') != a.source: continue
        keep = []
        for p in m.get('photos') or []:
            if p.get('preview') and orig_of(p.get('name', '')) in originals and orig_of(p['name']) != p['name']:
                replaced += 1
                if not any(p['file'] == e['file'] for g in groups.values() for _, e, _ in g):
                    try: os.remove(os.path.join(a.root, p['file']))
                    except OSError: pass
            else: keep.append(p)
        if len(keep) != len(m.get('photos') or []):
            if keep: m['photos'] = keep
            else: data['memories'].remove(m)
    if replaced: say('REPLACED', replaced, '张预览图换成了原图')

    n_photo = 0
    for key, group in groups.items():
        group.sort(key=lambda t: (t[0] is None, t[0] or datetime.min))
        shot = group[0][0]
        metas = [m for _, _, m in group if m]
        pick = lambda k: next((m[k] for m in metas if m.get(k)), None) or man.get(k) or ([] if k == 'tags' else '')
        mid = f'{a.source}:{key}'; old = memlib.find(data, mid)
        if old:                                            # 同一天之前导入过别的照片：合并，不覆盖；保留更早的时刻
            have = {e['file'] for _, e, _ in group}
            group = [(None, e, {}) for e in old.get('photos') or [] if e['file'] not in have] + group
            if shot and old.get('time') and old.get('date') == shot.strftime('%Y-%m-%d'):
                shot = min(shot, datetime.strptime(old['date'] + ' ' + old['time'], '%Y-%m-%d %H:%M'))
        # 日期优先用照片自己的拍摄时间；没有时才用清单里用户确认过的日期（绝不用网盘上传/修改时间）
        mdate = next((m['date'] for m in metas if valid_date(m.get('date'))), None)
        mtime = next((m['time'] for m in metas if re.fullmatch(r'\d{2}:\d{2}', m.get('time') or '')), None)
        memlib.upsert(data, {
            'id': mid, 'source': a.source,
            'date': shot.strftime('%Y-%m-%d') if shot else mdate,
            'time': shot.strftime('%H:%M') if shot else (mtime if mdate else None),
            'title': pick('title'), 'text': pick('text'), 'place': pick('place'), 'tags': pick('tags'),
            'photos': [e for _, e, _ in group],
        })
        n_photo += len(group)
    memlib.note_source(data, a.source, memories=len(groups), photos=n_photo)
    memlib.save(a.root, data)
    say('DONE', os.path.abspath(memlib.store_file(a.root)), len(groups), n_photo)


if __name__ == '__main__':
    main()
