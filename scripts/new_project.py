#!/usr/bin/env python3
"""
新建一个时光手帐项目（只用 Python 3 标准库）

  python3 new_project.py --dir ~/时光手帐
  python3 new_project.py --dir ~/时光手帐-示例 --demo      # 带上示例数据（41 张示例照片 + 虚构文案），先看看效果

会在目标文件夹里放好：
  index.html      3D 回忆网页（来自本 skill 的 assets/site）
  memories/       回忆数据包（memories.json + photos/），各导入脚本写这里
  music/          背景音乐：项目还没有曲子时放入内置曲目（assets/site/music，7 首）；之后用 set_music.py 换成自己的
  inbox/          中转区：网盘下载的原图、浏览器保存的微博数据等，导入后可删（网页不读）

--no-music 不放入内置曲目，music/ 保持空歌单（发布分享副本时也更干净）。
目标文件夹已有 memories/、music/、inbox/ 时不会动里面的内容，只会更新网页文件（index.html、flip.mp3）；
除非 music/ 里既没有 track-* 也没有非空歌单，才会补上内置曲目。
--demo 只在 memories/ 里还没有数据时才放入示例数据；示例照片来自 Unsplash，文案均为虚构（见 examples/demo/CREDITS.md）。
输出：PROJECT <绝对路径>
"""
import argparse, json, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..', 'assets', 'site')
MUSIC = os.path.join(SITE, 'music')
DEMO = os.path.join(HERE, '..', 'examples', 'demo', 'memories')


def has_music(mdir, pl):
    """music/ 里已经有效曲子了吗（track-* 文件，或歌单里指向的文件真实存在）"""
    if any(n.startswith('track-') for n in os.listdir(mdir)):
        return True
    if os.path.exists(pl):
        try:
            tracks = json.load(open(pl, encoding='utf-8')).get('tracks', [])
        except Exception:
            tracks = []
        if any(os.path.exists(os.path.join(mdir, str(t.get('file', '')))) for t in tracks):
            return True
    return False


def seed_music(mdir, pl):
    """项目里还没有任何曲子时，把内置曲目连同歌单复制进 music/。返回放了几首。"""
    if not os.path.isdir(MUSIC) or has_music(mdir, pl):
        return 0
    n = 0
    for name in sorted(os.listdir(MUSIC)):
        src = os.path.join(MUSIC, name)
        if os.path.isfile(src) and not name.startswith('.'):
            shutil.copy2(src, os.path.join(mdir, name))
            if name.startswith('track-'):
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description='新建时光手帐项目')
    ap.add_argument('--dir', required=True, help='项目文件夹（不要放在 skill 目录里面）')
    ap.add_argument('--demo', action='store_true', help='放入示例数据（memories/ 为空时）')
    ap.add_argument('--no-music', action='store_true', help='不放内置背景音乐，music/ 保持空歌单')
    a = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(a.dir))
    os.makedirs(os.path.join(root, 'memories', 'photos'), exist_ok=True)
    mdir = os.path.join(root, 'music')
    os.makedirs(mdir, exist_ok=True)
    os.makedirs(os.path.join(root, 'inbox'), exist_ok=True)
    pl = os.path.join(mdir, 'playlist.json')
    seeded = 0 if a.no_music else seed_music(mdir, pl)   # 内置曲目只补空缺，用户的曲子一首都不会被覆盖
    if not os.path.exists(pl):                       # 空歌单：网页不报 404，之后 set_music.py 会覆盖
        with open(pl, 'w', encoding='utf-8') as f: f.write('{"tracks": []}\n')
    for name in os.listdir(SITE):
        src = os.path.join(SITE, name)
        if os.path.isfile(src) and not name.startswith('.'):
            shutil.copy2(src, os.path.join(root, name))
    if a.demo:
        mem = os.path.join(root, 'memories')
        if os.path.exists(os.path.join(mem, 'memories.json')):
            print('SKIPPED 已有回忆数据，没有放入示例', flush=True)
        else:
            shutil.copy2(os.path.join(DEMO, 'memories.json'), os.path.join(mem, 'memories.json'))
            shutil.copytree(os.path.join(DEMO, 'photos', 'demo'), os.path.join(mem, 'photos', 'demo'), dirs_exist_ok=True)
            print('DEMO 已放入示例数据（照片来自 Unsplash，文案均为虚构）', flush=True)
    if seeded:
        print(f'MUSIC {seeded} 首内置背景音乐已放入 music/', flush=True)
    print('PROJECT', root, flush=True)


if __name__ == '__main__':
    main()
