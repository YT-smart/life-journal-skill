# 回忆数据包格式（memories/）

网页只读一个地方：项目里的 **`memories/` 文件夹**。不管照片来自 QQ 空间、本地相册、微博、网盘还是手写，都整理成同一种格式，网页就能做出同样的全景、近景和答案之书。

```
<项目>/
├── index.html  flip.mp3
├── memories/
│   ├── memories.json          ← 唯一的数据文件
│   └── photos/                ← 所有照片
│       ├── qzone/             ← QQ 空间
│       ├── weibo/             ← 微博
│       ├── netdisk/           ← 网盘
│       ├── photos/            ← 电脑 / 手机相册
│       └── manual/            ← 对话里直接给的图
├── music/
│   ├── playlist.json
│   └── track-01.mp3 …
└── inbox/                     ← 中转区（网盘下载、浏览器保存的微博数据），网页不读，导入后可删
    └── netdisk/<相册>/          照片 + manifest.json（相册说明）+ .retrieval.json（取图索引：fsid、云端路径、状态；不存链接和凭据）
```

网址加 `?data=其他文件夹` 可以读另一份数据包（默认 `memories`）。没有数据包时网页显示内置示例（picsum 示例图 + 虚构文案）。

## memories.json

```json
{
  "format": "life-journal/memories",
  "version": 1,
  "updated_at": "2026-11-11T02:10:00",
  "sources": { "qzone": { "posts": 103, "photos": 159, "imported_at": "…" } },
  "memories": [
    {
      "id": "qzone:4c709ba7e2e2da",
      "source": "qzone",
      "date": "2017-05-20",
      "time": "14:03",
      "title": "",
      "text": "今天去看了展……",
      "place": "",
      "tags": [],
      "photos": [
        { "file": "photos/qzone/4c709ba7e2e2da_1.jpg", "width": 1280, "height": 960 },
        { "file": "photos/qzone/4c709ba7e2e2da_2.gif", "width": 340,  "height": 320 }
      ],
      "generated": []
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一编号，建议 `来源:原始编号`。同一个 id 重复导入会覆盖，不会出现两份。 |
| `source` | 是 | `qzone` / `photos` / `weibo` / `netdisk` / `manual`，网页据此显示来源（其他值原样显示）。 |
| `date` | 否 | `YYYY-MM-DD`、`YYYY-MM`、`YYYY`，或 `null`。不知道就写 `null`，网页显示“日期未知”。上传时间、文件修改时间都不是拍摄时间，不要拿来顶替。 |
| `time` | 否 | `HH:MM` |
| `title` | 否 | 卡片上手写的一行短标题（约 10 个字以内）。留空时网页从 `text` 的第一句截取。 |
| `text` | 否 | 正文，照片背面显示的文字。用户自己写过的原文原样保留。 |
| `place` | 否 | 地点，没有就留空。 |
| `tags` | 否 | 标签数组。 |
| `photos` | 是 | 至少一张。`file` 是**相对 `memories/` 的路径**；`width` / `height` 可选（判断横竖构图）。`preview: true` 表示这张是网盘缩略图而非原图（之后拿到原图可重新导入替换）。第一张是封面，其余挂在它旁边。 |
| `generated` | 否 | 哪些字段是 AI 写的，例如 `["title", "tags"]`。重新导入时，用户补写过的字段不会被空值覆盖。 |

照片格式：JPEG / PNG / GIF / WebP（浏览器能直接显示的）。HEIC、RAW、TIFF 需要先导出成 JPEG。

## 用脚本写入

所有脚本只用 Python 3 标准库，在 skill 的 `scripts/` 里，都通过 `memlib.py` 读写数据包：

- `qzone_export.py --root <项目>/memories` —— QQ 空间扫码导出
- `weibo_export.py --root <项目>/memories [--from-json <文件夹>]` —— 微博扫码导出 / 导入浏览器保存的数据
- `build_share.py --project <项目> --budget-mb 19` —— 生成发布用的分享副本（去 GPS 等元数据、重新编号、不带本机文件、按大小上限自动缩图）
- `publish_feishu.py --project <项目> [--title …] [--scope …] [--resume]` —— 用 lark-cli 发布到飞书妙搭、设置可见范围，见 publishing.md
- `qrshow.py <二维码.png> [--invert]` —— 把二维码图片转成可在终端里扫的文字二维码（扫码类脚本会自动调用）
- `netdisk_fetch.py --list <清单> --inbox <项目>/inbox/netdisk` —— 下载网盘连接器（MCP）给出的链接到 inbox，记录取图状态，生成 `manifest.json`
- `import_photos.py --from <文件 文件夹 ZIP …> --root <项目>/memories [--source netdisk] [--manifest <相册说明>]` —— 电脑、手机、网盘下载的照片
- `add_memory.py --root <项目>/memories --photo … --date … --text …` —— 手动加一段，或用 `--id` 修改已有的一段
- `memlib.py check --root <项目>/memories` —— 检查：缺失文件、空白图、没有照片的回忆
- `memlib.py clean --root <项目>/memories` —— 清理：删除空白 / 占位图，去掉因此没有照片的回忆

每种来源怎么接、能做到什么，见 `sources.md`。

## 手写或从其他来源转换

上面的脚本都覆盖不到的情况，按上面的格式自己写即可：

1. 照片复制到 `memories/photos/<来源>/`，文件名随意但不要重复（建议用内容 md5 前 16 位）。
2. 在 Python 里 `import memlib`，对每段回忆调用 `memlib.upsert(data, {...})`，最后 `memlib.save(root, data)`——它会排序、原子写入，并保证 id 不重复。
3. 运行 `memlib.py check`，确认 `problems` 为空。

多个来源可以导进同一个数据包，网页会自动合并、按时间排好。
