#!/usr/bin/env python3
"""
微博导出（只读、只导出登录者自己发的带图微博）

只依赖 Python 3 标准库（以及同目录的 memlib.py / qzone_export.py）。两种方式，结果一样：

A. 扫码（默认）
   python3 weibo_export.py --root ./memories
   1. 生成微博官方登录二维码，保存为 <root>/qr.png 并尝试打开；Agent 把图发给用户，
      用户用手机微博 App 扫码并确认（脚本拿不到密码）。
   2. 登录后逐页读取【自己】的微博，只保留自己原创、带图片的；长微博补全文，超过 9 张图补全图片。
   3. 图片下载到 <root>/photos/weibo/，写进 <root>/memories.json。

B. 导入浏览器里保存下来的接口数据（扫码失败 / 被要求验证时的备选）
   python3 weibo_export.py --root ./memories --from-json ~/Downloads/weibo-pages
   读取一个 .json 文件或一个文件夹里的所有 .json：支持 weibo.com 的
   /ajax/statuses/mymblog、/ajax/statuses/show，以及 m.weibo.cn 的 /api/container/getIndex 返回。
   只导入这些文件里作者是同一个人（--uid，或文件里出现最多的作者）的原创带图微博。

安全与边界：
  - 登录凭证只存在本进程内存里，不写文件，进程结束即失效；不需要也不接受密码或 Cookie。
  - 只读：不发微博、不评论、不点赞、不关注；不导出评论和转发别人的内容（转发里的图属于原作者）。
  - 翻页间隔约 2.5 秒、图片间隔约 0.3 秒；可断点续跑（已下载的图片跳过，同一条微博重复导入会覆盖不会重复）。
  - 日期用微博的发布时间（北京时间）。照片原本的拍摄时间微博不提供，不去猜。

其他参数：--since 2018-01-01（只要这天以后的）  --max-posts 300  --delay 2.5  --open（同时在本机打开二维码）  --timeout 300

输出给 Agent 的进度行：
  QR_READY <png路径>（随后 QR_TEXT_BEGIN … QR_TEXT_END 是同一个码的文字版）/ QR_SCANNED / QR_EXPIRED / LOGIN_OK <昵称或uid> / PAGE <页> <累计条数>
  DETAIL <已补>/<需补> / IMAGE <已下载>/<总数> / SKIPPED <原因> / DONE <memories.json> <回忆数> <照片数> / ERROR <原因>
"""
import argparse, collections, glob, html, json, os, random, re, sys, time
import urllib.parse
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memlib
from qzone_export import Session, open_file, say
import qrshow

PASSPORT = 'https://passport.weibo.com'
BJ = timezone(timedelta(hours=8))
SOURCE = 'weibo'


# ------------------------------------------------------------------ login
def get_json(s, url, referer, timeout=20):
    raw = s.get(url, referer=referer, timeout=timeout).decode('utf-8', 'replace').strip()
    if raw[:1] not in '{[':                                  # JSONP：取括号里的部分
        raw = raw[raw.find('(') + 1: raw.rfind(')')]
    return json.loads(raw)


def login(s, out, open_qr=True, max_refresh=3, timeout=300):
    qr_path = os.path.join(out, 'qr.png'); deadline = time.time() + timeout
    for _ in range(max_refresh + 1):
        info = get_json(s, f'{PASSPORT}/sso/v2/qrcode/image?entry=miniblog&size=180', PASSPORT + '/')
        if str(info.get('retcode')) != '20000000':
            raise RuntimeError(f"获取二维码失败：{info.get('msg')}")
        qrid, img = info['data']['qrid'], info['data']['image']
        png = s.get(img, referer=PASSPORT + '/')
        qrshow.publish(png, os.path.abspath(qr_path), say)
        if open_qr: open_file(qr_path)
        scanned = False
        while time.time() < deadline:
            time.sleep(2)
            r = get_json(s, f'{PASSPORT}/sso/v2/qrcode/check?' + urllib.parse.urlencode({
                'entry': 'miniblog', 'source': 'miniblog', 'url': 'https://weibo.com/', 'qrid': qrid, 'disp': 'popup'}), PASSPORT + '/')
            code, msg = str(r.get('retcode')), r.get('msg') or ''
            if code == '20000000':
                follow_login(s, (r.get('data') or {}).get('url') or (r.get('data') or {}).get('alt') or '')
                try: os.remove(qr_path)
                except OSError: pass
                return
            if code == '50114001': continue                  # 还没扫
            if code == '50114002':                           # 已扫码，等手机上确认
                if not scanned: scanned = True; say('QR_SCANNED', '已扫码，请在手机上确认登录')
                continue
            if '过期' in msg or code in ('50114003', '50114004'):
                say('QR_EXPIRED', '二维码已过期，正在刷新'); break
            raise RuntimeError(f'登录失败：{msg or code}')
        if time.time() >= deadline: break
    raise RuntimeError('等待扫码超时')


def follow_login(s, url):
    """扫码确认后，按官方流程跟随跳转，让 weibo.com / weibo.cn 都拿到登录状态（只在内存里）。"""
    if not url: return
    try:
        r = get_json(s, url, PASSPORT + '/')                 # login.sina.com.cn 返回 crossDomainUrlList
        for u in r.get('crossDomainUrlList') or []:
            try: s.get(u, referer=PASSPORT + '/')
            except Exception: pass
    except Exception:
        s.get(url, referer=PASSPORT + '/')                   # 普通跳转页：跟过去即可


def my_uid(s):
    for url, ref in (('https://m.weibo.cn/api/config', 'https://m.weibo.cn/'),):
        try:
            d = get_json(s, url, ref).get('data') or {}
            if d.get('login') and d.get('uid'): return str(d['uid'])
        except Exception: pass
    try:
        page = s.get('https://weibo.com/', referer='https://weibo.com/').decode('utf-8', 'replace')
        m = re.search(r"\$CONFIG\['uid'\]\s*=\s*'(\d+)'", page) or re.search(r'"uid"\s*:\s*"?(\d{5,})', page)
        if m: return m.group(1)
    except Exception: pass
    raise RuntimeError('登录后没有拿到微博账号信息，请重新扫码')


# ------------------------------------------------------------------ parsing（两种接口的返回都整理成同一种结构）
def plain(text):
    text = re.sub(r'<br\s*/?>', '\n', text or '')
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r'\1', text)        # 表情图 → [笑]
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text).replace('​', '')
    text = re.sub(r'\s*全文$', '', text.strip())
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def bj_time(created):
    try: return datetime.strptime(created, '%a %b %d %H:%M:%S %z %Y').astimezone(BJ)
    except Exception: return None


def pic_url(p):
    """一张图的最大可用尺寸。weibo.com 用 pic_infos（largest/original），m.weibo.cn 用 pics（large）。"""
    for k in ('largest', 'original', 'mw2000', 'large', 'bmiddle'):
        u = (p.get(k) or {}).get('url') if isinstance(p.get(k), dict) else None
        if u: return u
    return p.get('url') or ''


def norm(mb):
    """把一条微博整理成 {id, uid, created, text, long, pic_num, pics[], repost}；不认识的返回 None。"""
    if not isinstance(mb, dict) or not (mb.get('idstr') or mb.get('id')): return None
    user = mb.get('user') or {}
    pics = []
    if mb.get('pic_infos'):
        order = mb.get('pic_ids') or list(mb['pic_infos'])
        pics = [pic_url(mb['pic_infos'][k]) for k in order if k in mb['pic_infos']]
    elif mb.get('pics'):
        pics = [pic_url(p) for p in mb['pics'] if isinstance(p, dict)]
    pics = [u.replace('http://', 'https://', 1) if '.sinaimg.cn/' in u else u for u in pics if u]   # 微博图床支持 https
    text = mb.get('text_raw') if mb.get('text_raw') is not None else plain(mb.get('text', ''))
    long_txt = (mb.get('longText') or {}).get('longTextContent') or mb.get('longTextContent')
    return {
        'id': str(mb.get('idstr') or mb.get('id')), 'mid': mb.get('mblogid') or mb.get('bid') or '',
        'uid': str(user.get('idstr') or user.get('id') or ''), 'nick': user.get('screen_name') or '',
        'created': bj_time(mb.get('created_at') or ''), 'text': plain(long_txt) if long_txt else (text or '').strip(),
        'long': bool(mb.get('isLongText')) and not long_txt, 'pic_num': int(mb.get('pic_num') or len(pics)),
        'pics': pics, 'repost': bool(mb.get('retweeted_status')),
    }


def posts_in(obj):
    """从任意一页接口返回里找出所有微博。"""
    out = []
    d = obj.get('data', obj) if isinstance(obj, dict) else {}
    if isinstance(d, dict):
        for mb in d.get('list') or []: out.append(mb)                               # weibo.com mymblog
        for c in d.get('cards') or []:                                              # m.weibo.cn getIndex
            if c.get('mblog'): out.append(c['mblog'])
            for cc in c.get('card_group') or []:
                if cc.get('mblog'): out.append(cc['mblog'])
        if d.get('idstr') or (d.get('id') and d.get('created_at')): out.append(d)     # statuses/show
    if isinstance(obj, dict) and (obj.get('idstr') and obj.get('created_at')) and obj not in out: out.append(obj)
    return [p for p in (norm(m) for m in out) if p]


def keep(p, uid):
    if p['repost'] or not p['created']: return 'repost' if p['repost'] else 'no-date'
    if uid and p['uid'] and p['uid'] != uid: return 'other-author'
    if not p['pics'] and not p['pic_num']: return 'no-photo'
    return None


# ------------------------------------------------------------------ online fetch
def fetch_online(s, uid, since, max_posts, delay):
    posts, seen, page, empty = [], set(), 1, 0
    ref = f'https://weibo.com/u/{uid}'
    while True:
        try:
            d = get_json(s, f'https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0', ref)
        except Exception as e:
            raise RuntimeError(f'读取第 {page} 页失败（{e}）。可以稍后用同一命令续跑，或换用 --from-json 方式')
        if d.get('ok') not in (1, '1', True):
            if d.get('ok') == -100 or 'login' in json.dumps(d)[:300].lower():
                raise RuntimeError('登录状态失效或需要验证，请重新扫码；仍不行就换用 --from-json 方式')
            raise RuntimeError(f"读取第 {page} 页失败：{d.get('msg') or d.get('message') or d.get('ok')}")
        batch = posts_in(d)
        if not batch:
            empty += 1
            if empty >= 2: break                              # 连续两页没有内容 = 到底了
        else: empty = 0
        older = False
        for p in batch:
            if p['id'] in seen: continue
            seen.add(p['id'])
            if since and p['created'] and p['created'].date() < since: older = True; continue
            if keep(p, uid) is None: posts.append(p)
        say('PAGE', page, len(posts))
        if (max_posts and len(posts) >= max_posts) or (older and batch and all(x['created'] and x['created'].date() < since for x in batch[-3:])):
            break
        page += 1
        time.sleep(delay + random.random() * .8)
    return posts[:max_posts] if max_posts else posts


def complete_details(s, posts, delay):
    """长微博补全文；图片多于列表里给出的（超过 9 张时常见）补全图片。失败的保留已有内容。"""
    need = [p for p in posts if p['long'] or p['pic_num'] > len(p['pics'])]
    for k, p in enumerate(need, 1):
        key = p['mid'] or p['id']
        try:
            if p['pic_num'] > len(p['pics']):
                d = get_json(s, f'https://weibo.com/ajax/statuses/show?id={key}', 'https://weibo.com/')
                full = norm(d)
                if full and len(full['pics']) > len(p['pics']): p['pics'] = full['pics']
            if p['long']:
                d = get_json(s, f'https://weibo.com/ajax/statuses/longtext?id={key}', 'https://weibo.com/')
                t = (d.get('data') or {}).get('longTextContent')
                if t: p['text'] = plain(t); p['long'] = False
        except Exception:
            pass
        say('DETAIL', f'{k}/{len(need)}')
        time.sleep(delay * .5 + random.random() * .5)


# ------------------------------------------------------------------ store
def download(s, posts, root):
    d = os.path.join(root, 'photos', SOURCE); os.makedirs(d, exist_ok=True)
    total = sum(len(p['pics']) for p in posts); done = failed = 0
    for p in posts:
        p['files'] = []
        for k, u in enumerate(p['pics'], 1):
            base, path = f"{p['id']}_{k}", None
            for ext in memlib.IMAGE_EXT:
                c = os.path.join(d, base + ext)
                if os.path.exists(c) and os.path.getsize(c) > 0: path = c; break
            if not path:
                for attempt in range(3):
                    try:
                        data = s.get(u, referer='https://weibo.com/', timeout=40)
                        ext = memlib.sniff_ext(data[:12])
                        if not ext: raise ValueError('不是图片')
                        path = os.path.join(d, base + ext)
                        with open(path, 'wb') as f: f.write(data)
                        break
                    except Exception:
                        time.sleep(1.5 * (attempt + 1))
                time.sleep(.25 + random.random() * .2)
            done += 1
            if path: p['files'].append(path)
            else: failed += 1
            if done % 5 == 0 or done == total: say('IMAGE', f'{done}/{total}')
    if failed: say('SKIPPED', f'{failed} 张图片下载失败（可能已被删除），重跑同一命令会再试')


def store(posts, root):
    data = memlib.load(root); n_m = n_p = 0
    for p in posts:
        photos = []
        for f in p.get('files', []):
            if memlib.is_blank(f): continue
            wh = memlib.image_size(f)
            photos.append({'file': os.path.relpath(f, root).replace(os.sep, '/'), **({'width': wh[0], 'height': wh[1]} if wh else {})})
        if not photos: continue
        t = p['created']
        tags = list(dict.fromkeys(x.strip() for x in re.findall(r'#([^#\n]{1,20})#', p['text']) if x.strip()))[:6]   # 微博话题
        memlib.upsert(data, {
            'id': f"{SOURCE}:{p['id']}", 'source': SOURCE,
            'date': t.strftime('%Y-%m-%d'), 'time': t.strftime('%H:%M'),
            'title': '', 'text': p['text'], 'place': '', 'tags': tags, 'photos': photos,
        })
        n_m += 1; n_p += len(photos)
    memlib.note_source(data, SOURCE, posts=n_m, photos=n_p)
    memlib.save(root, data)
    return n_m, n_p


def load_json_files(path):
    files = sorted(glob.glob(os.path.join(path, '**', '*.json'), recursive=True)) if os.path.isdir(path) else [path]
    posts = {}
    for f in files:
        if os.path.basename(f) in ('memories.json', 'playlist.json'): continue
        try:
            with open(f, encoding='utf-8') as fh: obj = json.load(fh)
        except Exception:
            say('SKIPPED', '不是可读的 JSON', f); continue
        objs = obj if isinstance(obj, list) else [obj]
        for o in objs:
            for p in posts_in(o):
                old = posts.get(p['id'])
                if not old or len(p['pics']) > len(old['pics']) or (old['long'] and not p['long']): posts[p['id']] = p
    return list(posts.values()), len(files)


def main():
    ap = argparse.ArgumentParser(description='导出自己微博里带图片的原创微博（只读），写进时光手帐的回忆数据包')
    ap.add_argument('--root', default='memories', help='回忆数据包目录（网页读取这里）')
    ap.add_argument('--from-json', help='不扫码：导入浏览器里保存的微博接口 JSON（文件或文件夹）')
    ap.add_argument('--uid', help='只导入这个作者的微博（--from-json 时可用；默认取文件里最多的作者）')
    ap.add_argument('--since', help='只要这一天以后的，格式 YYYY-MM-DD')
    ap.add_argument('--max-posts', type=int, default=0, help='最多导出多少条带图微博（0 = 全部）')
    ap.add_argument('--delay', type=float, default=2.5, help='翻页间隔秒数')
    ap.add_argument('--open', action='store_true', help='同时在本机用看图软件打开二维码（默认不打开，由 Agent 把二维码发到对话里）')
    ap.add_argument('--no-open', action='store_true', help=argparse.SUPPRESS)   # 旧参数，保留兼容（现在默认就不打开）
    ap.add_argument('--timeout', type=int, default=300, help='等待扫码的最长秒数')
    a = ap.parse_args()
    os.makedirs(a.root, exist_ok=True)
    since = datetime.strptime(a.since, '%Y-%m-%d').date() if a.since else None
    s = Session()
    try:
        if a.from_json:
            raw, n_files = load_json_files(os.path.expanduser(a.from_json))
            uid = a.uid or (collections.Counter(p['uid'] for p in raw if p['uid'] and not p['repost']).most_common(1) or [('', 0)])[0][0]
            reasons = collections.Counter()
            posts = []
            for p in raw:
                why = keep(p, uid) or ('older' if since and p['created'].date() < since else None)
                if why: reasons[why] += 1
                else: posts.append(p)
            posts.sort(key=lambda p: p['created'], reverse=True)
            if a.max_posts: posts = posts[:a.max_posts]
            say('PAGE', n_files, len(posts))
            names = {'repost': '转发', 'no-photo': '没有图片', 'other-author': '不是本人发的', 'no-date': '缺少日期', 'older': '早于 --since'}
            if reasons: say('SKIPPED', '，'.join(f'{names.get(k, k)} {v} 条' for k, v in reasons.items()))
            if any(p['long'] for p in posts): say('SKIPPED', f"{sum(p['long'] for p in posts)} 条长微博只有开头部分（文件里没有全文）")
        else:
            login(s, a.root, open_qr=a.open and not a.no_open, timeout=a.timeout)
            uid = my_uid(s)
            say('LOGIN_OK', uid)
            posts = fetch_online(s, uid, since, a.max_posts, a.delay)
            complete_details(s, posts, a.delay)
        download(s, posts, a.root)
        n_m, n_p = store(posts, a.root)
        say('DONE', os.path.abspath(memlib.store_file(a.root)), n_m, n_p)
    except KeyboardInterrupt:
        say('ERROR', '已取消'); sys.exit(1)
    except Exception as e:
        say('ERROR', str(e)); sys.exit(1)


if __name__ == '__main__':
    main()
