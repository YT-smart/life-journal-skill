#!/usr/bin/env python3
"""
把分享副本发布到飞书妙搭（HTML 应用），得到一个可以分享的链接（只用 Python 3 标准库 + 飞书官方 lark-cli）

  python3 build_share.py   --project ~/时光手帐 --budget-mb 19          # 先生成分享副本（压缩后 ≤ 19MB）
  python3 publish_feishu.py --project ~/时光手帐 --title "我们的时光手帐"   # 发布 / 更新
  python3 publish_feishu.py --project ~/时光手帐 --resume                 # 继续查询上次没等完的发布
  python3 publish_feishu.py --project ~/时光手帐 --scope tenant           # 只改可见范围（用户确认后）

做的事（全部通过 lark-cli，以“用户本人”身份 --as user）：
  1. 检查 share/ 是否满足妙搭限制：index.html ≤ 10MB、打包后 ≤ 20MB、总量 ≤ 200MB。
  2. 第一次发布：apps +create --app-type html 新建应用；以后复用 publish.json 里记下的 app_id，链接不变。
  3. apps +html-publish --path ./share 上传发布 → apps +release-get 轮询，直到 finished / failed / 等待审批 / 超时。
  4. finished 时读取 online_url（只报平台真实返回的链接）。
  5. 给了 --scope 才改可见范围；默认不改（妙搭默认**只有创建者自己能打开**）。
       tenant        本企业 / 本组织的人能看
       public-login  所有人可看，但要先登录飞书
       public        所有拿到链接的人都能看，不用登录
     创意模式应用可能不允许用命令行改可见范围，这时输出 SCOPE_WEB，请用户在妙搭网页端设置。

不碰凭据：登录由 lark-cli 打开授权页、用户在浏览器里确认；本脚本不读、不存、不打印任何令牌。
记录：<项目>/publish.json 里 platform=feishu 那条：app_id、应用名、release_id、状态、链接、时间。

输出：NEED_INSTALL / NEED_LOGIN <提示> / OVER_LIMIT <说明> / APP <app_id> new|reused / RELEASE <id>
      STATUS <状态> / URL <链接> / PENDING_APPROVAL [审批链接] / FAILED <原因> / STILL_PUBLISHING
      SCOPE_SET <范围> / SCOPE_WEB <说明> / SCOPE 仅创建者可见 / ERROR <原因>
"""
import argparse, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time
from datetime import datetime

LIMIT_HTML, LIMIT_TGZ, LIMIT_TOTAL = 10 << 20, 20 << 20, 200 << 20
MARK = '.life-journal-share'


def say(tag, *parts):
    print(tag, *parts, flush=True)


class CliError(Exception):
    def __init__(self, err, raw=''):
        self.err = err or {}; self.raw = raw
        super().__init__(self.err.get('message') or raw[-400:] or 'lark-cli 调用失败')


def last_json(text):
    """lark-cli 输出里取最后一个 JSON 对象（stderr 可能夹着进度行）。"""
    text = (text or '').strip()
    try: return json.loads(text)
    except ValueError: pass
    for m in reversed(list(re.finditer(r'^\{', text, re.M))):
        try: return json.loads(text[m.start():])
        except ValueError: continue
    return None


def lark(args, cwd=None, timeout=600):
    cmd = ['lark-cli', 'apps'] + args + ['--as', 'user', '--format', 'json']
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = last_json(p.stdout); err = last_json(p.stderr)
    if p.returncode == 0 and isinstance(out, dict) and out.get('ok', True) is not False:
        return out.get('data', out) if isinstance(out.get('data', out), dict) else out
    env = err if isinstance(err, dict) else out if isinstance(out, dict) else {}
    raise CliError(env.get('error') or {}, (p.stderr or p.stdout or ''))


def find(d, key):
    """在嵌套结果里找第一个名为 key 的值。"""
    if isinstance(d, dict):
        if key in d and d[key] not in (None, ''): return d[key]
        for v in d.values():
            r = find(v, key)
            if r not in (None, ''): return r
    elif isinstance(d, list):
        for v in d:
            r = find(v, key)
            if r not in (None, ''): return r
    return None


def handle_cli_error(e, what):
    t, st = e.err.get('type', ''), e.err.get('subtype', '')
    if t in ('authentication', 'authorization') or st in ('missing_scope', 'not_logged_in', 'token_expired'):
        say('NEED_LOGIN', e.err.get('hint') or '请运行 lark-cli auth login --domain apps，把授权链接发给用户确认')
    elif st == 'rate_limit':
        say('ERROR', f'{what}：飞书限流，{e.err.get("retry_after_seconds") or "稍后"} 秒后再试')
    else:
        say('ERROR', f'{what}：{e}', ('提示：' + e.err['hint']) if e.err.get('hint') else '')
    sys.exit(1)


def load_log(project):
    try:
        with open(os.path.join(project, 'publish.json'), encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return {'sites': []}


def save_site(project, log, site):
    log['sites'] = [s for s in log.get('sites', []) if s.get('platform') != 'feishu'] + [site]
    with open(os.path.join(project, 'publish.json'), 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def check_limits(share):
    html = os.path.getsize(os.path.join(share, 'index.html'))
    total = sum(os.path.getsize(os.path.join(dp, n)) for dp, _, ns in os.walk(share) for n in ns)
    with tempfile.TemporaryFile() as f:
        with tarfile.open(fileobj=f, mode='w:gz') as t:
            for dp, _, ns in os.walk(share):
                for n in sorted(ns):
                    if n != MARK: p = os.path.join(dp, n); t.add(p, os.path.relpath(p, share))
        tgz = f.tell()
    bad = []
    if html > LIMIT_HTML: bad.append(f'index.html {html / 1048576:.1f}MB > 10MB')
    if tgz > LIMIT_TGZ: bad.append(f'打包后 {tgz / 1048576:.1f}MB > 20MB')
    if total > LIMIT_TOTAL: bad.append(f'总量 {total / 1048576:.1f}MB > 200MB')
    return bad, tgz


def poll(app_id, release_id, wait):
    deadline = time.monotonic() + wait
    while True:
        try: r = lark(['+release-get', '--app-id', app_id, '--release-id', str(release_id)], timeout=120)
        except CliError as e: handle_cli_error(e, '查询发布状态')
        rel = r.get('release', r) if isinstance(r, dict) else {}
        status = str(rel.get('status') or find(r, 'status') or '').lower()
        node = rel.get('current_node_info') or find(r, 'current_node_info') or {}
        if status in ('finished', 'failed'):
            return status, r
        if str(node.get('current_status') or '').upper() == 'PENDING':
            return 'pending_approval', r
        if time.monotonic() >= deadline:
            return 'still_publishing', r
        say('STATUS', status or 'publishing')
        time.sleep(min(10, max(1, deadline - time.monotonic())))


def set_scope(app_id, scope):
    args = {'tenant': ['--scope', 'tenant'], 'public-login': ['--scope', 'public', '--require-login=true'],
            'public': ['--scope', 'public', '--require-login=false']}[scope]
    try:
        lark(['+access-scope-set', '--app-id', app_id] + args, timeout=120)
        say('SCOPE_SET', {'tenant': '本企业/组织可见', 'public-login': '所有人可见（需登录飞书）', 'public': '所有拿到链接的人可见（无需登录）'}[scope])
        return scope
    except CliError as e:
        if e.err.get('type') in ('authentication', 'authorization') and e.err.get('subtype') == 'missing_scope':
            handle_cli_error(e, '设置可见范围')
        say('SCOPE_WEB', '命令行没能设置可见范围（' + (e.err.get('hint') or str(e))[:200] + '）。'
            '请用户打开妙搭网页端 → 我的应用 → 这个应用 → 可见范围，手动设置')
        return None


def main():
    ap = argparse.ArgumentParser(description='把时光手帐分享副本发布到飞书妙搭')
    ap.add_argument('--project', required=True)
    ap.add_argument('--title', default='时光手帐', help='应用名（第一次发布时用）')
    ap.add_argument('--new-app', action='store_true', help='不复用旧应用，新建一个（会得到新链接）')
    ap.add_argument('--resume', action='store_true', help='继续查询上次没等完的发布，不重新上传')
    ap.add_argument('--scope', choices=['tenant', 'public-login', 'public'], help='发布完成后设置可见范围（先征得用户同意）')
    ap.add_argument('--wait', type=int, default=300, help='最多等多少秒')
    a = ap.parse_args()
    project = os.path.abspath(os.path.expanduser(a.project)); share = os.path.join(project, 'share')
    if not shutil.which('lark-cli'):
        say('NEED_INSTALL', '没有找到 lark-cli：需要 Node.js，然后 npx @larksuite/cli@latest install'); sys.exit(3)
    log = load_log(project)
    site = next((s for s in log.get('sites', []) if s.get('platform') == 'feishu'), None) or {'platform': 'feishu'}

    only_scope = a.scope and not a.resume and site.get('status') == 'published' and \
        os.path.exists(os.path.join(share, 'index.html')) and site.get('built_at') == os.path.getmtime(os.path.join(share, 'index.html'))
    if a.resume or only_scope:
        if not site.get('app_id') or (a.resume and not site.get('release_id')):
            say('ERROR', 'publish.json 里没有可以继续的飞书发布'); sys.exit(1)
        app_id = site['app_id']; say('APP', app_id, 'reused')
    else:
        if not os.path.exists(os.path.join(share, 'index.html')):
            say('ERROR', '没有分享副本：先运行 build_share.py --budget-mb 19'); sys.exit(1)
        bad, tgz = check_limits(share)
        if bad:
            say('OVER_LIMIT', '；'.join(bad), '→ 用 build_share.py --budget-mb 19 重新生成，或 --years 分几次发布、--no-music'); sys.exit(1)
        say('SIZE', f'打包后约 {tgz / 1048576:.1f}MB（上限 20MB）')
        app_id = None if a.new_app else site.get('app_id')
        if app_id:
            say('APP', app_id, 'reused')
        else:
            try: r = lark(['+create', '--name', a.title, '--app-type', 'HTML'], timeout=120)
            except CliError as e:                     # 不同版本对大小写要求不同：参数错误时换小写再试一次
                if e.err.get('type') != 'validation': handle_cli_error(e, '新建妙搭应用')
                try: r = lark(['+create', '--name', a.title, '--app-type', 'html'], timeout=120)
                except CliError as e2: handle_cli_error(e2, '新建妙搭应用')
            app_id = find(r, 'app_id')
            if not (isinstance(app_id, str) and app_id.startswith('app_')):
                say('ERROR', '新建应用后没有拿到 app_id'); sys.exit(1)
            site = {'platform': 'feishu', 'app_id': app_id, 'name': a.title}
            say('APP', app_id, 'new')
        save_site(project, log, site)                       # 先记下 app_id：上传失败也能复用这个应用
        try: r = lark(['+html-publish', '--app-id', app_id, '--path', './share'], cwd=project, timeout=900)
        except CliError as e: handle_cli_error(e, '上传发布')
        rid = find(r, 'release_id')
        if not rid: say('ERROR', '上传后没有拿到 release_id'); sys.exit(1)
        site.update(status='publishing', release_id=str(rid), built_at=os.path.getmtime(os.path.join(share, 'index.html')),
                    published_at=datetime.now().isoformat(timespec='seconds'))
        save_site(project, log, site)
        say('RELEASE', rid)

    if not only_scope:
        status, r = poll(app_id, site['release_id'], a.wait)
        if status == 'finished':
            url = find(r, 'online_url')
            site['status'] = 'published'
            if isinstance(url, str) and url.startswith('https://'):
                site['url'] = url; say('URL', url)
            else:
                say('STATUS', 'finished（平台没有返回访问链接，不要编造；可在妙搭网页端“我的应用”里查看）')
        elif status == 'failed':
            logs = find(r, 'error_logs')
            site['status'] = 'failed'; say('FAILED', json.dumps(logs, ensure_ascii=False)[:600] if logs else '平台没有给出原因')
        elif status == 'pending_approval':
            site['status'] = 'pending_approval'
            say('PENDING_APPROVAL', find(r, 'approval_url') or '', '等待飞书里的审批负责人处理；处理后用 --resume 继续')
        else:
            site['status'] = 'publishing'; say('STILL_PUBLISHING', '还在部署，稍后用 --resume 继续查询')
        save_site(project, log, site)
        if site['status'] != 'published': return

    if a.scope:
        got = set_scope(app_id, a.scope)
        if got: site['scope'] = got; save_site(project, log, site)
    elif not site.get('scope'):
        say('SCOPE', '仅创建者可见（妙搭默认）。要分享给别人，先问用户想让谁能看，再用 --scope 设置')


if __name__ == '__main__':
    main()
