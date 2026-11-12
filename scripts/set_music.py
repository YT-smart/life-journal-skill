#!/usr/bin/env python3
"""
把用户自己的音乐文件放进时光手帐项目，并生成 music/playlist.json（只用 Python 3 标准库）

  python3 set_music.py --project ~/时光手帐 --from ~/Downloads/歌1.mp3 ~/Downloads/歌2.m4a
  python3 set_music.py --project ~/时光手帐 --from ~/Music/回忆歌单        # 也可以给一个文件夹
  python3 set_music.py --project ~/时光手帐 --clear                        # 清空歌单

规则：
  - 只复制，不改原文件；按内容去重；统一改名为 track-01.xxx、track-02.xxx…（网页不显示曲名）。
  - 只用用户自己提供、有权使用的音频文件；本脚本不联网下载任何歌曲。
  - 网页在“启动时光机”时随机挑一首开始，音量约 8 秒渐入；一轮之内不重复。
输出：TRACKS <数量> / SKIPPED <原因> <文件>
"""
import argparse, hashlib, json, os, shutil

AUDIO_EXT = ('.mp3', '.m4a', '.aac', '.ogg', '.oga', '.wav', '.flac', '.webm', '.opus')


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''): h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description='设置时光手帐的背景音乐')
    ap.add_argument('--project', required=True, help='时光手帐项目文件夹')
    ap.add_argument('--from', dest='src', nargs='*', default=[], help='音乐文件或文件夹')
    ap.add_argument('--clear', action='store_true', help='先清空已有歌单')
    a = ap.parse_args()
    mdir = os.path.join(os.path.expanduser(a.project), 'music'); os.makedirs(mdir, exist_ok=True)
    pl_path = os.path.join(mdir, 'playlist.json')
    tracks = []
    if not a.clear and os.path.exists(pl_path):
        tracks = [t for t in json.load(open(pl_path, encoding='utf-8')).get('tracks', []) if os.path.exists(os.path.join(mdir, t['file']))]
    if a.clear:
        for t in os.listdir(mdir):
            if t.startswith('track-'): os.remove(os.path.join(mdir, t))
        tracks = []

    files = []
    for s in a.src:
        s = os.path.expanduser(s)
        if os.path.isdir(s):
            for dp, _, names in os.walk(s):
                files += [os.path.join(dp, n) for n in sorted(names)]
        else:
            files.append(s)
    seen = {t.get('md5') for t in tracks}
    for f in files:
        if not os.path.isfile(f): print('SKIPPED', '找不到文件', f); continue
        ext = os.path.splitext(f)[1].lower()
        if ext not in AUDIO_EXT: print('SKIPPED', '不是浏览器能播放的音频格式', f); continue
        h = md5(f)
        if h in seen: print('SKIPPED', '重复', f); continue
        seen.add(h)
        name = f'track-{len(tracks) + 1:02d}{ext}'
        shutil.copy2(f, os.path.join(mdir, name))
        tracks.append({'file': name, 'md5': h})
    with open(pl_path, 'w', encoding='utf-8') as fp:
        json.dump({'tracks': tracks}, fp, ensure_ascii=False, indent=1)
    print('TRACKS', len(tracks), flush=True)


if __name__ == '__main__':
    main()
