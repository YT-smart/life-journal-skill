#!/usr/bin/env python3
"""
把网盘连接器（MCP）查到的照片取到本地 inbox（只用 Python 3 标准库）

本脚本不是网盘客户端：不登录、不要令牌、不调用网盘接口。它只下载 Agent 从网盘连接器
**结果里原样拿到的** 链接（原图链接或缩略图地址），并把每个文件的取图状态记下来，方便续跑和补取。

  1) Agent 用连接器列目录 / 检索后，把选中的文件写成一份清单（list.json）：
     {
       "album": "2019 杭州",                    # 相册名 = 本地文件夹名 = 默认标题
       "cloud_dir": "/我的相册/2019杭州",        # 云端目录（只做记录）
       "files": [
         {"fsid": "1234", "path": "/我的相册/2019杭州/IMG_0001.JPG", "filename": "IMG_0001.JPG",
          "md5": "…", "size": 3456789,
          "url": "<连接器返回的链接，原样粘贴>",
          "kind": "original" | "preview"}       # 原图 / 缩略图（预览）
       ]
     }
  2) python3 netdisk_fetch.py --list list.json --inbox ~/时光手帐/inbox/netdisk
  3) 失败的文件：用连接器重新查询（如 file_meta）拿新链接，写一份只含这些文件的新清单再跑一次；
     已经成功的会跳过。
  4) python3 import_photos.py --from ~/时光手帐/inbox/netdisk/<相册> --root ~/时光手帐/memories \\
         --source netdisk --manifest ~/时光手帐/inbox/netdisk/<相册>/manifest.json

落盘：
  inbox/netdisk/<相册>/<原文件名>          照片
  inbox/netdisk/<相册>/.retrieval.json      取图索引：fsid、云端路径、状态、尝试次数、最后的 HTTP 状态和错误类型。
                                            **不保存任何链接**（链接可能带签名或凭据），不进成品。
  inbox/netdisk/<相册>/manifest.json        给 import_photos.py 的说明：相册名、哪些是预览图、云端路径

规则：
  - 链接原样使用，不拼接、不改参数、不补令牌。
  - 每个文件累计最多 --max-tries 次有意义的尝试（默认 3：原链接 → 重新查询后的新链接 → 另一条已授权途径）。
  - 403 不当作限流，原样记录；429 / 503 = 限流，整批停下（RATE_LIMITED），不在这一轮反复请求。
  - 401 = 登录失效，整批停下（AUTH），交给用户处理连接器。
  - 下载结果按内容校验：不是图片（例如返回了网页）算失败；清单给了 md5 且是原图时核对 md5。

输出：GOT <fsid> <文件名> / FAIL <fsid> <HTTP> <错误类型> / RATE_LIMITED / AUTH
      NEED_RELINK <fsid …>（还能再试，需要新链接）/ GAVE_UP <fsid …>（已达上限，记为待补）
      DONE <相册目录> <成功数>/<总数> pending=<待补数> preview=<预览图数>
"""
import argparse, hashlib, json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime

UA_BROWSER = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
IMG_HEADS = (b'\xff\xd8', b'\x89PNG', b'GIF8', b'RIFF')


def say(tag, *parts):
    print(tag, *parts, flush=True)


def safe_name(s, fallback):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', (s or '').strip()).strip('. ')
    return s[:120] or fallback


def is_image(head):
    return head.startswith(IMG_HEADS) or head[4:12] in (b'ftypheic', b'ftypheix', b'ftypmif1', b'ftyphevc')


def fetch(url, timeout):
    host = urllib.request.urlparse(url).hostname or ''
    # 百度网盘原图下载域名要求这个 UA（官方文档约定）；其他链接用普通浏览器 UA
    ua = 'pan.baidu.com' if host.endswith(('pcs.baidu.com', 'baidupcs.com')) else UA_BROWSER
    req = urllib.request.Request(url, headers={'User-Agent': ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def load_json(p, default):
    try:
        with open(p, encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(p, obj):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def main():
    ap = argparse.ArgumentParser(description='下载网盘连接器给出的照片链接到本地 inbox，并记录取图状态')
    ap.add_argument('--list', required=True, help='Agent 写的清单 JSON')
    ap.add_argument('--inbox', required=True, help='例如 ~/时光手帐/inbox/netdisk')
    ap.add_argument('--max-tries', type=int, default=3, help='每个文件累计最多尝试几次（默认 3）')
    ap.add_argument('--delay', type=float, default=0.6, help='两次下载之间的间隔秒数')
    ap.add_argument('--timeout', type=float, default=60)
    a = ap.parse_args()

    spec = load_json(os.path.expanduser(a.list), None)
    if not spec or not isinstance(spec.get('files'), list):
        say('ERROR', '清单格式不对：需要 {"album": …, "files": [ … ]}'); sys.exit(1)
    album = safe_name(spec.get('album'), 'netdisk')
    out = os.path.join(os.path.expanduser(a.inbox), album); os.makedirs(out, exist_ok=True)
    idx_p, man_p = os.path.join(out, '.retrieval.json'), os.path.join(out, 'manifest.json')
    idx = load_json(idx_p, {'album': album, 'cloud_dir': spec.get('cloud_dir', ''), 'files': {}})
    if spec.get('cloud_dir'): idx['cloud_dir'] = spec['cloud_dir']

    stop = None
    for i, f in enumerate(spec['files']):
        fsid = str(f.get('fsid') or f.get('path') or f.get('filename') or i)
        rec = idx['files'].setdefault(fsid, {'tries': 0})
        rec.update({k: f[k] for k in ('path', 'filename', 'md5', 'size') if f.get(k) is not None})
        kind = 'preview' if f.get('kind') == 'preview' else 'original'
        if rec.get('status') == 'done' and (rec.get('kind') == 'original' or kind == 'preview'):
            continue                                   # 已经有了（预览图之后可以用原图替换，反之不行）
        if stop:
            rec.setdefault('status', 'pending'); continue
        url = (f.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')):
            rec.update(status='pending', last_error='清单里没有可用链接'); continue
        if rec['tries'] >= a.max_tries:
            rec['status'] = 'failed'; continue
        rec['tries'] += 1; rec['last_try'] = datetime.now().isoformat(timespec='seconds')
        name = safe_name(f.get('filename') or os.path.basename(f.get('path') or ''), f'{fsid}.jpg')
        try:
            code, body = fetch(url, a.timeout)
            if not is_image(body[:12]):
                raise ValueError('返回的不是图片（可能是网页或错误信息）')
            if kind == 'original' and f.get('md5') and len(f['md5']) == 32 and hashlib.md5(body).hexdigest() != f['md5'].lower():
                # 百度网盘的 md5 字段对大文件不一定是整文件 md5，只记录不判失败
                rec['md5_note'] = '内容 md5 与网盘记录不同（网盘对部分文件不给整文件 md5，通常无碍）'
            stem, ext = os.path.splitext(name)
            if kind == 'preview': name = f'{stem}.preview{ext or ".jpg"}'
            dst = os.path.join(out, name)
            if os.path.exists(dst) and rec.get('local') != name:
                dst = os.path.join(out, f'{stem}_{fsid}{os.path.splitext(name)[1]}'); name = os.path.basename(dst)
            with open(dst, 'wb') as w: w.write(body)
            old = rec.get('local')
            if old and old != name and kind == 'original' and rec.get('kind') == 'preview':
                try: os.remove(os.path.join(out, old))       # 原图到了，删掉旧的预览图
                except OSError: pass
            rec.update(status='done', kind=kind, local=name, bytes=len(body), last_http=code, last_error=None)
            say('GOT', fsid, name)
        except urllib.error.HTTPError as e:
            rec.update(status='pending', last_http=e.code, last_error={401: '登录失效', 403: '拒绝访问（403，不等于限流）',
                       404: '文件不存在或链接失效', 429: '限流', 503: '服务暂不可用/限流'}.get(e.code, f'HTTP {e.code}'))
            say('FAIL', fsid, e.code, rec['last_error'])
            if e.code in (429, 503):
                rec['tries'] -= 1                        # 限流不算这个文件的尝试
                stop = 'RATE_LIMITED'
            elif e.code == 401:
                stop = 'AUTH'
        except Exception as e:
            rec.update(status='pending', last_http=None, last_error=type(e).__name__ + ': ' + str(e)[:120])
            say('FAIL', fsid, '-', rec['last_error'])
        if rec['status'] == 'pending' and rec['tries'] >= a.max_tries: rec['status'] = 'failed'
        write_json(idx_p, idx)
        time.sleep(a.delay)

    write_json(idx_p, idx)
    files = idx['files']
    done = [k for k, r in files.items() if r.get('status') == 'done']
    relink = [k for k, r in files.items() if r.get('status') == 'pending']
    gave = [k for k, r in files.items() if r.get('status') == 'failed']
    previews = [k for k in done if files[k].get('kind') == 'preview']
    manifest = load_json(man_p, {})
    manifest.setdefault('title', album)
    manifest['files'] = {r['local']: {'cloud_path': r.get('path', ''), **({'preview': True} if r.get('kind') == 'preview' else {})}
                         for r in files.values() if r.get('status') == 'done'}
    write_json(man_p, manifest)
    if stop == 'RATE_LIMITED': say('RATE_LIMITED', '网盘限流：这一轮停下，过一段时间再跑同一条命令')
    if stop == 'AUTH': say('AUTH', '网盘连接器登录失效：请用户重新连接网盘后再继续')
    if relink: say('NEED_RELINK', *relink)
    if gave: say('GAVE_UP', *gave)
    say('DONE', out, f'{len(done)}/{len(files)}', f'pending={len(relink) + len(gave)}', f'preview={len(previews)}')


if __name__ == '__main__':
    main()
