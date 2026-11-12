#!/usr/bin/env python3
"""
QQ 空间说说导出（只读、只导出登录者自己的数据）

给 AI Agent 使用的独立脚本，只依赖 Python 3 标准库（以及同目录的 memlib.py）。流程：
  1. 生成 QQ 官方登录二维码，保存为 <out>/qr.png 并尝试用系统看图软件打开；
     Agent 应把这张图展示给用户，由用户用手机 QQ 扫码确认（脚本永远拿不到密码）。
  2. 登录成功后，逐页读取【登录者自己】的说说，默认只保留带图片的。
  3. 把图片下载到 <root>/photos/qzone/，删除空白和已删除的占位图，写进 <root>/memories.json。

安全与边界：
  - 登录凭证（cookie）只存在本进程内存里，不写入任何文件，进程结束即失效。
  - 只读接口：不发说说、不评论、不删除；不读取其他人的空间；评论内容不导出。
  - 翻页和下载都有间隔，降低触发风控的概率；可断点续跑（已下载的图片会跳过）。

用法：
  python3 qzone_export.py --root ./memories
  python3 qzone_export.py --root ./memories --all-posts      # 连同纯文字说说一起导出
  python3 qzone_export.py --root ./memories --max-posts 300  # 只导最近 300 条

输出给 Agent 的进度行都以大写标签开头，便于解析：
  QR_READY <png路径>（随后 QR_TEXT_BEGIN … QR_TEXT_END 是同一个码的文字版）/ QR_SCANNED / QR_EXPIRED / LOGIN_OK <昵称> / PAGE <n> <累计条数>
  IMAGE <已下载>/<总数> / CLEANED <说明> / DONE <memories.json路径> <回忆数> <照片数> / ERROR <原因>
"""
import argparse, http.cookiejar, json, os, platform, random, re, struct, subprocess, sys, time
import urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memlib                                              # 回忆数据包的读写（同目录）
import qrshow
from datetime import datetime

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
APPID, DAID = '549000912', '5'                      # QQ 空间网页版的登录应用标识
S_URL = 'https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone'
LIST_URL = 'https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6'


def say(tag, *parts):
    print(tag, *parts, flush=True)


def hash33(s):                                        # 二维码轮询需要的 ptqrtoken
    e = 0
    for c in s:
        e += (e << 5) + ord(c)
    return 2147483647 & e


def gtk(p_skey):                                      # 空间接口需要的 g_tk
    h = 5381
    for c in p_skey:
        h += (h << 5) + ord(c)
    return h & 2147483647


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()          # 只在内存里
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, referer=None, timeout=20):
        req = urllib.request.Request(url, headers={'User-Agent': UA, **({'Referer': referer} if referer else {})})
        with self.op.open(req, timeout=timeout) as r:
            return r.read()

    def cookie(self, name, domain_hint=None):
        val = None
        for c in self.jar:
            if c.name == name and (domain_hint is None or domain_hint in c.domain):
                val = c.value
        return val


def open_file(path):
    try:
        if platform.system() == 'Darwin':
            subprocess.Popen(['open', path])
        elif platform.system() == 'Windows':
            os.startfile(path)                          # type: ignore[attr-defined]
        else:
            subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def login(s, out, open_qr=True, max_refresh=3, timeout=300):
    s.get('https://xui.ptlogin2.qq.com/cgi-bin/xlogin?' + urllib.parse.urlencode({
        'proxy_url': 'https://qzs.qq.com/qzone/v6/portal/proxy.html', 'daid': DAID, 'hide_title_bar': 1,
        'low_login': 0, 'qlogin_auto_login': 1, 'no_verifyimg': 1, 'link_target': 'blank',
        'appid': APPID, 'style': 22, 'target': 'self', 's_url': S_URL}))
    login_sig = s.cookie('pt_login_sig') or ''
    qr_path = os.path.join(out, 'qr.png')
    deadline = time.time() + timeout
    for attempt in range(max_refresh + 1):
        png = s.get('https://ssl.ptlogin2.qq.com/ptqrshow?' + urllib.parse.urlencode({
            'appid': APPID, 'e': 2, 'l': 'M', 's': 3, 'd': 72, 'v': 4, 't': random.random(),
            'daid': DAID, 'pt_3rd_aid': 0}), referer='https://xui.ptlogin2.qq.com/')
        qrshow.publish(png, os.path.abspath(qr_path), say)    # 写出清晰大图 + QR_READY + 文字二维码
        if open_qr:
            open_file(qr_path)
        token = hash33(s.cookie('qrsig') or '')
        scanned = False
        while time.time() < deadline:
            time.sleep(2)
            txt = s.get('https://ssl.ptlogin2.qq.com/ptqrlogin?' + urllib.parse.urlencode({
                'u1': S_URL, 'ptqrtoken': token, 'ptredirect': 0, 'h': 1, 't': 1, 'g': 1, 'from_ui': 1,
                'ptlang': 2052, 'action': f'0-0-{int(time.time() * 1000)}', 'js_ver': 24051315, 'js_type': 1,
                'login_sig': login_sig, 'pt_uistyle': 40, 'aid': APPID, 'daid': DAID}),
                referer='https://xui.ptlogin2.qq.com/').decode('utf-8', 'replace')
            args = re.findall(r"'([^']*)'", txt)
            code = args[0] if args else ''
            if code == '0':                              # 登录成功 → 跟随跳转拿到空间的 cookie
                s.get(args[2], referer='https://xui.ptlogin2.qq.com/')
                nick = args[5] if len(args) > 5 else ''
                try: os.remove(qr_path)
                except OSError: pass
                return nick
            if code == '67' and not scanned:
                scanned = True; say('QR_SCANNED', '已扫码，请在手机上确认登录')
            if code == '65':
                say('QR_EXPIRED', '二维码已过期，正在刷新'); break
            if code not in ('0', '65', '66', '67'):
                raise RuntimeError(f'登录失败：{args[4] if len(args) > 4 else txt[:120]}')
        if time.time() >= deadline:
            break
    raise RuntimeError('等待扫码超时')


def clean(text):
    text = re.sub(r'\[em\][^\[]*\[/em\]', '', text or '')
    text = re.sub(r'@\{uin:\d+,nick:([^,}]*)[^}]*\}', r'@\1', text)
    return text.strip()


def fetch_posts(s, uin, g_tk, all_posts, max_posts, delay):
    pos, num, items, page = 0, 20, [], 0
    while True:
        raw = s.get(LIST_URL + '?' + urllib.parse.urlencode({
            'uin': uin, 'hostUin': uin, 'ftype': 0, 'sort': 0, 'pos': pos, 'num': num, 'replynum': 0,
            'g_tk': g_tk, 'callback': '_preloadCallback', 'code_version': 1, 'format': 'jsonp',
            'need_private_comment': 1}), referer=f'https://user.qzone.qq.com/{uin}').decode('utf-8', 'replace')
        body = raw[raw.find('(') + 1: raw.rfind(')')] if '(' in raw else raw
        data = json.loads(body)
        if data.get('code', 0) != 0:
            raise RuntimeError(f"读取说说失败（code={data.get('code')} {data.get('message', '')}）")
        msgs = data.get('msglist') or []
        page += 1
        for m in msgs:
            pics = []
            for p in m.get('pic') or []:
                u = p.get('url2') or p.get('url3') or p.get('url1') or p.get('url') or ''
                if u:
                    pics.append(u.replace('http://', 'https://', 1))
            if not pics and not all_posts:
                continue
            ts = int(m.get('created_time') or 0)
            lbs = m.get('lbs') or {}
            items.append({
                'id': str(m.get('tid', '')),
                'created': datetime.fromtimestamp(ts).isoformat(timespec='seconds') if ts else None,
                'date': datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts else None,
                'text': clean(m.get('content', '')),
                'place': (lbs.get('name') or lbs.get('idname') or '').strip(),
                'remote_images': pics,
            })
        say('PAGE', page, len(items))
        total = int(data.get('total') or 0)
        pos += num
        if not msgs or (total and pos >= total) or (max_posts and len(items) >= max_posts):
            break
        time.sleep(delay + random.random() * 0.6)
    return items[:max_posts] if max_posts else items


def download_images(s, items, root, uin):
    """下载到 <root>/photos/qzone/，按文件内容决定后缀；已下载的跳过（断点续跑）。"""
    img_dir = os.path.join(root, 'photos', 'qzone'); os.makedirs(img_dir, exist_ok=True)
    total = sum(len(it['remote_images']) for it in items); done = 0
    for it in items:
        it['files'] = []
        for k, u in enumerate(it['remote_images']):
            base = f"{it['id']}_{k + 1}"; path = None
            for ext in memlib.IMAGE_EXT:
                cand = os.path.join(img_dir, base + ext)
                if os.path.exists(cand) and os.path.getsize(cand) > 0: path = cand; break
            if not path:
                for attempt in range(3):
                    try:
                        data = s.get(u, referer=f'https://user.qzone.qq.com/{uin}', timeout=30)
                        path = os.path.join(img_dir, base + (memlib.sniff_ext(data[:12]) or '.jpg'))
                        with open(path, 'wb') as f: f.write(data)
                        break
                    except Exception:
                        time.sleep(1.5 * (attempt + 1))
                time.sleep(0.25)
            done += 1
            if path and os.path.exists(path): it['files'].append(path)
            if done % 5 == 0 or done == total: say('IMAGE', f'{done}/{total}')
        del it['remote_images']
    return total


def store_posts(items, root):
    """写进回忆数据包：删除空白 / 已删除的占位图，每条说说一段回忆。返回 (回忆数, 照片数, 删除的空白图数)。"""
    import collections
    hashes = {f: memlib.md5(f) for it in items for f in it['files']}
    repeated = {h for h, n in collections.Counter(hashes.values()).items() if n >= 3}   # 同一张图出现在很多条说说里 = 占位图
    data = memlib.load(root); kept_m = kept_p = removed = 0
    for it in items:
        photos = []
        for f in it['files']:
            if hashes[f] in memlib.KNOWN_PLACEHOLDERS or hashes[f] in repeated or memlib.is_blank(f):
                try: os.remove(f)
                except OSError: pass
                removed += 1; continue
            wh = memlib.image_size(f)
            photos.append({'file': os.path.relpath(f, root).replace(os.sep, '/'), **({'width': wh[0], 'height': wh[1]} if wh else {})})
        if not photos and not it.get('keep_text_only'):
            continue
        created = it.get('created')
        memlib.upsert(data, {
            'id': f"qzone:{it['id']}", 'source': 'qzone',
            'date': created[:10] if created else None, 'time': created[11:16] if created else None,
            'title': '', 'text': it.get('text', ''), 'place': it.get('place', ''), 'tags': [],
            'photos': photos,
        })
        kept_m += 1; kept_p += len(photos)
    memlib.note_source(data, 'qzone', posts=kept_m, photos=kept_p)
    memlib.save(root, data)
    return kept_m, kept_p, removed


def main():
    ap = argparse.ArgumentParser(description='导出自己 QQ 空间里带图片的说说（只读），写进时光手帐的回忆数据包')
    ap.add_argument('--root', default='memories', help='回忆数据包目录（网页读取这里）')
    ap.add_argument('--all-posts', action='store_true', help='也导出没有图片的说说')
    ap.add_argument('--max-posts', type=int, default=0, help='最多导出多少条（0 = 全部）')
    ap.add_argument('--delay', type=float, default=1.5, help='翻页间隔秒数')
    ap.add_argument('--open', action='store_true', help='同时在本机用看图软件打开二维码（默认不打开，由 Agent 把二维码发到对话里）')
    ap.add_argument('--no-open', action='store_true', help=argparse.SUPPRESS)   # 旧参数，保留兼容（现在默认就不打开）
    ap.add_argument('--timeout', type=int, default=300, help='等待扫码的最长秒数')
    a = ap.parse_args()
    os.makedirs(a.root, exist_ok=True)
    s = Session()
    try:
        nick = login(s, a.root, open_qr=a.open and not a.no_open, timeout=a.timeout)
        uin = (s.cookie('uin') or s.cookie('p_uin') or '').lstrip('o').lstrip('0')
        p_skey = s.cookie('p_skey', 'qzone') or s.cookie('p_skey') or s.cookie('skey')
        if not uin or not p_skey:
            raise RuntimeError('登录后没有拿到 QQ 空间的登录状态，请重试')
        say('LOGIN_OK', nick or '已登录')
        items = fetch_posts(s, uin, gtk(p_skey), a.all_posts, a.max_posts, a.delay)
        for it in items: it['keep_text_only'] = a.all_posts and not it['remote_images']
        n_img = download_images(s, items, a.root, uin)
        n_mem, n_photo, n_removed = store_posts(items, a.root)
        say('CLEANED', f'删除空白/已删除的占位图 {n_removed} 张')
        say('DONE', os.path.abspath(memlib.store_file(a.root)), n_mem, n_photo)
    except KeyboardInterrupt:
        say('ERROR', '已取消'); sys.exit(1)
    except Exception as e:
        say('ERROR', str(e)); sys.exit(1)


if __name__ == '__main__':
    main()
