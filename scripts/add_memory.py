#!/usr/bin/env python3
"""
手动添加 / 修改一段回忆（只用 Python 3 标准库 + 同目录的 memlib.py）

适合：用户在对话里直接给了几张图片并讲了那天的事；或者想给已导入的回忆补日期、地点、文字。

  # 新建一段：几张图 + 用户自己说的话
  python3 add_memory.py --root memories --photo ~/Desktop/a.jpg ~/Desktop/b.jpg \\
      --date 2019-05-20 --place 杭州 --title 西湖边 --text "那天下了一点雨……" --tags 旅行 雨

  # 修改已有的一段（id 见 memories.json；只改给出的字段）
  python3 add_memory.py --root memories --id photos:2019-05-20 --date 2019-05 --text "补一句话"

规则：
  - 照片复制到 memories/photos/manual/（按内容命名，原图不动）；HEIC 在 macOS 上自动转 JPEG。
  - --date 写用户确认过的日期：YYYY-MM-DD、YYYY-MM 或 YYYY；不知道就不写（网页显示“日期未知”）。
  - 文字写用户自己的原话；AI 代写的标题/标签要加 --generated title tags，方便以后区分。
输出：SAVED <id> <照片数> / ERROR <原因>
"""
import argparse, os, re, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memlib
from import_photos import heic_to_jpeg, HEIC


def main():
    ap = argparse.ArgumentParser(description='手动添加或修改一段回忆')
    ap.add_argument('--root', default='memories')
    ap.add_argument('--id', help='要修改的回忆 id；不给就新建一段（source=manual）')
    ap.add_argument('--photo', nargs='*', default=[], help='照片文件（新建时至少一张；修改时会追加）')
    ap.add_argument('--date'); ap.add_argument('--time'); ap.add_argument('--title'); ap.add_argument('--text')
    ap.add_argument('--place'); ap.add_argument('--tags', nargs='*')
    ap.add_argument('--generated', nargs='*', default=None, help='哪些字段是 AI 写的，例如 title tags')
    a = ap.parse_args()
    try:
        if a.date and not re.fullmatch(r'\d{4}(-\d{2}(-\d{2})?)?', a.date): raise ValueError('--date 格式应为 YYYY-MM-DD、YYYY-MM 或 YYYY')
        if a.time and not re.fullmatch(r'\d{2}:\d{2}', a.time): raise ValueError('--time 格式应为 HH:MM')
        data = memlib.load(a.root)
        old = memlib.find(data, a.id) if a.id else None
        if a.id and not old: raise ValueError(f'没有找到 {a.id}')
        d = os.path.join(a.root, 'photos', 'manual'); os.makedirs(d, exist_ok=True)
        photos = list(old['photos']) if old else []
        tmp = tempfile.mkdtemp(prefix='lj-')
        try:
            for p in a.photo:
                p = os.path.expanduser(p)
                raw = open(p, 'rb').read()
                if p.lower().endswith(HEIC) and not memlib.sniff_ext(raw[:12]):
                    raw = heic_to_jpeg(raw, tmp)
                    if not raw: raise ValueError(f'{p} 是 HEIC，无法转换，请先导出为 JPEG')
                ext = memlib.sniff_ext(raw[:12])
                if not ext: raise ValueError(f'{p} 不是浏览器能显示的图片（JPEG/PNG/GIF/WebP）')
                rel = f'photos/manual/{memlib.md5_bytes(raw)[:16]}{ext}'; dst = os.path.join(a.root, rel)
                if not os.path.exists(dst):
                    with open(dst, 'wb') as f: f.write(raw)
                if any(x['file'] == rel for x in photos): continue
                wh = memlib.image_size(dst)
                photos.append({'file': rel, **({'width': wh[0], 'height': wh[1]} if wh else {}), 'name': os.path.basename(p)})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not photos: raise ValueError('一段回忆至少要有一张照片（--photo）')
        mem = dict(old) if old else {'id': f"manual:{memlib.md5_bytes(photos[0]['file'].encode())[:12]}", 'source': 'manual',
                                     'date': None, 'time': None, 'title': '', 'text': '', 'place': '', 'tags': [], 'generated': []}
        for k in ('date', 'time', 'title', 'text', 'place'):
            v = getattr(a, k)
            if v is not None: mem[k] = v
        if a.tags is not None: mem['tags'] = a.tags
        if a.generated is not None: mem['generated'] = sorted(set(mem.get('generated') or []) | set(a.generated))
        else:   # 用户亲自给的字段不再算 AI 写的
            mem['generated'] = [g for g in mem.get('generated') or [] if getattr(a, g, None) is None]
        mem['photos'] = photos
        data['memories'] = [m for m in data['memories'] if m['id'] != mem['id']] + [mem]
        memlib.save(a.root, data)
        print('SAVED', mem['id'], len(photos), flush=True)
    except Exception as e:
        print('ERROR', e, flush=True); sys.exit(1)


if __name__ == '__main__':
    main()
