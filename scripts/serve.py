#!/usr/bin/env python3
"""
在本机打开时光手帐网页（只用 Python 3 标准库）

  python3 serve.py --project ~/时光手帐            # 自动找空闲端口并打开浏览器
  python3 serve.py --project ~/时光手帐 --port 8765 --no-open

网页要通过本地服务器访问（直接双击 index.html 读不到数据包）。
只监听 127.0.0.1，局域网里的其他设备访问不到。按 Ctrl+C 停止。
输出：URL <地址>
"""
import argparse, functools, http.server, os, socket, threading, webbrowser


def free_port(start):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try: s.bind(('127.0.0.1', p)); return p
            except OSError: continue
    raise SystemExit('没有找到空闲端口')


def main():
    ap = argparse.ArgumentParser(description='本机预览时光手帐')
    ap.add_argument('--project', required=True)
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-open', action='store_true')
    a = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(a.project))
    if not os.path.exists(os.path.join(root, 'index.html')):
        raise SystemExit(f'{root} 里没有 index.html，请先运行 new_project.py')
    port = free_port(a.port)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', port), functools.partial(Quiet, directory=root))
    url = f'http://localhost:{port}/'
    print('URL', url, flush=True)
    if not a.no_open: threading.Timer(.6, lambda: webbrowser.open(url)).start()
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == '__main__':
    main()
