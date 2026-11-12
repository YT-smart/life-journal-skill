#!/usr/bin/env python3
"""
新建一个时光手帐项目（只用 Python 3 标准库）

  python3 new_project.py --dir ~/时光手帐
  python3 new_project.py --dir ~/时光手帐-示例 --demo      # 带上示例数据（41 张示例照片 + 虚构文案），先看看效果

会在目标文件夹里放好：
  index.html      3D 回忆网页（来自本 skill 的 assets/site）
  memories/       回忆数据包（memories.json + photos/），各导入脚本写这里
  music/          背景音乐（用 set_music.py 放进来）
  inbox/          中转区：网盘下载的原图、浏览器保存的微博数据等，导入后可删（网页不读）

目标文件夹已有 memories/、music/、inbox/ 时不会动里面的内容，只会更新网页文件（index.html、flip.mp3）。
--demo 只在 memories/ 里还没有数据时才放入示例数据；示例照片来自 Unsplash，文案均为虚构（见 examples/demo/CREDITS.md）。
输出：PROJECT <绝对路径>
"""
import argparse, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, '..', 'assets', 'site')
DEMO = os.path.join(HERE, '..', 'examples', 'demo', 'memories')


def main():
    ap = argparse.ArgumentParser(description='新建时光手帐项目')
    ap.add_argument('--dir', required=True, help='项目文件夹（不要放在 skill 目录里面）')
    ap.add_argument('--demo', action='store_true', help='放入示例数据（memories/ 为空时）')
    a = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(a.dir))
    os.makedirs(os.path.join(root, 'memories', 'photos'), exist_ok=True)
    os.makedirs(os.path.join(root, 'music'), exist_ok=True)
    os.makedirs(os.path.join(root, 'inbox'), exist_ok=True)
    pl = os.path.join(root, 'music', 'playlist.json')
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
    print('PROJECT', root, flush=True)


if __name__ == '__main__':
    main()
