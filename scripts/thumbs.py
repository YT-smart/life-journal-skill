#!/usr/bin/env python3
"""
给回忆数据包里的照片生成缩略图（只用 Python 3 标准库 + 系统自带的图片工具）

  python3 thumbs.py --root memories          # 补齐缺的缩略图（已有的跳过）；旧项目跑一次就会变快

网页里 3D 照片球、螺旋塔、答案之书用的都是缩略图（长边 640 像素，每张几十 KB），
点开大图时才加载原图。几百张照片也能几秒内打开。

  - macOS：用系统自带的 sips。
  - Windows：用系统自带的 PowerShell（.NET System.Drawing），不用另外安装任何东西；
    会按 EXIF 方向把照片转正。WebP 读不了，这类照片继续用原图。
  - 其他系统：不生成，网页继续用原图（能用，只是慢）。

顺便检查 JPEG / WebP 是不是几乎纯色的空白图（memlib 不解码 JPEG），是的话在 memories.json 里标 "blank": true，
网页不显示，`memlib.py clean` 会删掉。

各导入脚本保存 memories.json 时会自动调用这里（memlib.save），一般不用手动跑。
输出：THUMBS <新生成数> <总照片数> [<失败数> 张用原图]
"""
import base64, os, platform, shutil, struct, subprocess, sys, tempfile, zlib
from concurrent.futures import ThreadPoolExecutor

MAX_SIDE, QUALITY = 640, 78
BLANK_STD = 10


def thumb_rel(file_rel):
    """photos/qzone/ab12.png → thumbs/qzone/ab12.jpg"""
    p = file_rel[len('photos/'):] if file_rel.startswith('photos/') else file_rel
    return 'thumbs/' + os.path.splitext(p)[0] + '.jpg'


def backend():
    s = platform.system()
    if s == 'Darwin' and shutil.which('sips'): return 'sips'
    if s == 'Windows' and (shutil.which('powershell') or shutil.which('pwsh')): return 'ps'
    return None


# ------------------------------------------------------------ 亮度离散度（判断空白图）
def _png_std(path):
    """sips 输出的 24×24 PNG（8 位、不隔行）→ 亮度标准差。"""
    with open(path, 'rb') as f: data = f.read()
    pos, idat = 8, b''
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]; typ = data[pos + 4:pos + 8]; body = data[pos + 8:pos + 8 + ln]; pos += 12 + ln
        if typ == b'IHDR': w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif typ == b'IDAT': idat += body
        elif typ == b'IEND': break
    if depth != 8 or inter or ctype not in (0, 2, 4, 6): return 999
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]; stride = w * bpp
    raw = zlib.decompress(idat); prev = bytearray(stride); lum = []
    for y in range(h):
        ft = raw[y * (stride + 1)]; line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0; b = prev[x]; c = prev[x - bpp] if x >= bpp else 0
            if ft == 1: line[x] = (line[x] + a) & 255
            elif ft == 2: line[x] = (line[x] + b) & 255
            elif ft == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        for x in range(w):
            i = x * bpp
            lum.append(line[i] if ctype in (0, 4) else (line[i] + line[i + 1] + line[i + 2]) / 3)
    n = len(lum); mean = sum(lum) / n
    return (sum((v - mean) ** 2 for v in lum) / n) ** .5


# ------------------------------------------------------------ macOS：sips
def exif_orientation(path):
    """JPEG 的 EXIF 方向（1–8），读不到返回 1。"""
    try:
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8': return 1
            while True:
                b = f.read(1)
                while b and b != b'\xff': b = f.read(1)
                while b == b'\xff': b = f.read(1)
                if not b or b[0] in (0xD9, 0xDA): return 1
                ln = struct.unpack('>H', f.read(2))[0]; seg = f.read(ln - 2)
                if b[0] == 0xE1 and seg[:6] == b'Exif\x00\x00':
                    t = seg[6:]; E = '<' if t[:2] == b'II' else '>'
                    off = struct.unpack(E + 'I', t[4:8])[0]
                    for i in range(struct.unpack(E + 'H', t[off:off + 2])[0]):
                        e = t[off + 2 + i * 12: off + 14 + i * 12]
                        if struct.unpack(E + 'H', e[:2])[0] == 0x0112:
                            v = struct.unpack(E + 'H', e[8:10])[0]
                            return v if 1 <= v <= 8 else 1
                    return 1
    except Exception:
        return 1


# 缩略图丢了 EXIF 方向时，用这个把像素转正（sips -r 顺时针旋转，-f 翻转）
TURN = {2: [['-f', 'horizontal']], 3: [['-r', '180']], 4: [['-f', 'vertical']], 5: [['-r', '90'], ['-f', 'horizontal']],
        6: [['-r', '90']], 7: [['-r', '270'], ['-f', 'horizontal']], 8: [['-r', '270']]}


def _sips_one(job):
    src, dst, tmpdir = job
    tiny = os.path.join(tmpdir, os.path.basename(dst) + '.png')
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        part = dst + '.part.jpg'
        import memlib
        wh = memlib.image_size(src)
        z = ['-Z', str(MAX_SIDE)] if not wh or max(wh) > MAX_SIDE else []          # sips -Z 会把小图放大，小图不缩放
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', str(QUALITY)] + z + [src, '--out', part],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if exif_orientation(part) != exif_orientation(src):               # sips 一般会保留 EXIF 方向；万一丢了就把像素转正
            for op in TURN.get(exif_orientation(src), []):
                subprocess.run(['sips'] + op + [part], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        subprocess.run(['sips', '-s', 'format', 'png', '-z', '24', '24', part, '--out', tiny],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        std = _png_std(tiny)
        os.replace(part, dst)
        return std
    except Exception:
        try: os.remove(dst + '.part.jpg')
        except OSError: pass
        return None
    finally:
        try: os.remove(tiny)
        except OSError: pass


def _run_sips(jobs, progress):
    tmpdir = tempfile.mkdtemp(prefix='lj-thumb-')
    out = []
    try:
        with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4))) as ex:
            for k, std in enumerate(ex.map(_sips_one, [(s, d, tmpdir) for s, d in jobs]), 1):
                out.append(std); progress(k)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# ------------------------------------------------------------ Windows：PowerShell + System.Drawing
PS_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$max = [int]$env:LJ_MAX; $q = [long]$env:LJ_Q
$enc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = New-Object System.Drawing.Imaging.EncoderParameters 1
$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), $q
$attr = New-Object System.Drawing.Imaging.ImageAttributes
$attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
$flips = @{2='RotateNoneFlipX';3='Rotate180FlipNone';4='Rotate180FlipX';5='Rotate90FlipX';6='Rotate90FlipNone';7='Rotate270FlipX';8='Rotate270FlipNone'}
foreach ($line in [System.IO.File]::ReadAllLines($env:LJ_LIST, [System.Text.Encoding]::UTF8)) {
  if (-not $line) { continue }
  $parts = $line -split "`t"; $img = $null; $bmp = $null; $sm = $null
  try {
    $bytes = [System.IO.File]::ReadAllBytes($parts[0])
    $ms = New-Object System.IO.MemoryStream (,$bytes)
    $img = [System.Drawing.Image]::FromStream($ms)
    if ($img.PropertyIdList -contains 274) {
      $o = [System.BitConverter]::ToUInt16($img.GetPropertyItem(274).Value, 0)
      if ($flips.ContainsKey([int]$o)) { $img.RotateFlip([System.Drawing.RotateFlipType]$flips[[int]$o]) }
    }
    $s = [Math]::Min(1.0, $max / [double][Math]::Max($img.Width, $img.Height))
    $w = [Math]::Max(1, [int][Math]::Round($img.Width * $s)); $h = [Math]::Max(1, [int][Math]::Round($img.Height * $s))
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = 'HighQualityBicubic'; $g.PixelOffsetMode = 'HighQuality'; $g.Clear([System.Drawing.Color]::White)
    $g.DrawImage($img, (New-Object System.Drawing.Rectangle 0, 0, $w, $h), 0, 0, $img.Width, $img.Height, [System.Drawing.GraphicsUnit]::Pixel, $attr); $g.Dispose()
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($parts[1])) | Out-Null
    $bmp.Save($parts[1] + '.part', $enc, $ep)
    if (Test-Path -LiteralPath $parts[1]) { Remove-Item -LiteralPath $parts[1] -Force }
    Move-Item -LiteralPath ($parts[1] + '.part') -Destination $parts[1]
    $sm = New-Object System.Drawing.Bitmap 24, 24
    $g = [System.Drawing.Graphics]::FromImage($sm); $g.InterpolationMode = 'HighQualityBilinear'; $g.Clear([System.Drawing.Color]::White)
    $g.DrawImage($bmp, 0, 0, 24, 24); $g.Dispose()
    $sum = 0.0; $sq = 0.0
    for ($y = 0; $y -lt 24; $y++) { for ($x = 0; $x -lt 24; $x++) { $c = $sm.GetPixel($x, $y); $v = ($c.R + $c.G + $c.B) / 3.0; $sum += $v; $sq += $v * $v } }
    $mean = $sum / 576; $std = [Math]::Sqrt([Math]::Max(0, $sq / 576 - $mean * $mean))
    [Console]::Out.WriteLine('OK ' + $std.ToString([System.Globalization.CultureInfo]::InvariantCulture))
  } catch {
    [Console]::Out.WriteLine('ERR')
  } finally {
    if ($sm) { $sm.Dispose() }; if ($bmp) { $bmp.Dispose() }; if ($img) { $img.Dispose() }
  }
  [Console]::Out.Flush()
}
'''


def _run_ps_chunk(jobs, tick):
    exe = shutil.which('powershell') or shutil.which('pwsh')
    fd, lst = tempfile.mkstemp(suffix='.txt', prefix='lj-thumb-')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for s, d in jobs: f.write(os.path.abspath(s) + '\t' + os.path.abspath(d) + '\n')
    env = {**os.environ, 'LJ_LIST': lst, 'LJ_MAX': str(MAX_SIDE), 'LJ_Q': str(QUALITY)}
    enc = base64.b64encode(PS_SCRIPT.encode('utf-16-le')).decode()
    out = []
    try:
        p = subprocess.Popen([exe, '-NoProfile', '-NonInteractive', '-EncodedCommand', enc], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in p.stdout:
            line = line.strip()
            if not line.startswith(('OK', 'ERR')): continue
            try: out.append(float(line.split()[1]) if line.startswith('OK') else None)
            except (IndexError, ValueError): out.append(None)
            tick()
        p.wait()
    except Exception:
        pass
    finally:
        try: os.remove(lst)
        except OSError: pass
    return (out + [None] * len(jobs))[:len(jobs)]


def _run_ps(jobs, progress):
    """几个 PowerShell 进程并行（每个进程启动要 1 秒左右，所以一个进程处理一批）。"""
    import threading
    n = max(1, min(4, (os.cpu_count() or 2) // 2, len(jobs) // 20 or 1))
    chunks = [jobs[i::n] for i in range(n)]
    lock = threading.Lock(); done = [0]
    def tick():
        with lock: done[0] += 1; k = done[0]
        progress(k)
    with ThreadPoolExecutor(max_workers=n) as ex:
        parts = list(ex.map(lambda c: _run_ps_chunk(c, tick), chunks))
    res = [None] * len(jobs)
    for i, part in enumerate(parts):
        res[i::n] = part
    return res


# ------------------------------------------------------------ 对外接口
def memlib_size_ok(src):
    """原图长边不超过缩略图的 1.25 倍，才可以直接拿原图当缩略图。"""
    import memlib
    wh = memlib.image_size(src)
    return bool(wh) and max(wh) <= MAX_SIDE * 1.25


def ensure(root, data, quiet=False):
    """给 data 里还没有缩略图的照片生成缩略图，写回 ph['thumb'] / ph['blank']。返回新生成的张数。"""
    photos = [ph for m in data.get('memories', []) for ph in (m.get('photos') or []) if ph.get('file')]
    jobs, todo = [], []
    for ph in photos:
        t = ph.get('thumb')
        if t and os.path.exists(os.path.join(root, t)): continue
        ph.pop('thumb', None)
        src = os.path.join(root, ph['file'])
        if not os.path.exists(src): continue
        jobs.append((src, os.path.join(root, thumb_rel(ph['file'])))); todo.append(ph)
    if not jobs: return 0
    be = backend()
    if not be:
        if not quiet: print('THUMBS 0', len(photos), '这个系统不能生成缩略图，网页继续用原图', flush=True)
        return 0
    step = max(25, len(jobs) // 10)
    def progress(k):
        if not quiet and k % step == 0 and k < len(jobs): print('THUMBS_PROGRESS', k, len(jobs), flush=True)
    res = (_run_sips if be == 'sips' else _run_ps)(jobs, progress)
    made = failed = 0
    for ph, (src, dst), std in zip(todo, jobs, res):
        if std is None or not os.path.exists(dst): failed += 1; continue
        if os.path.getsize(dst) >= os.path.getsize(src) and memlib_size_ok(src):
            os.remove(dst); ph['thumb'] = ph['file']                   # 原图本来就小：直接用原图，不多存一份
        else:
            ph['thumb'] = os.path.relpath(dst, root).replace(os.sep, '/')
        if std <= BLANK_STD: ph['blank'] = True
        else: ph.pop('blank', None)
        made += 1
    if not quiet: print('THUMBS', made, len(photos), *([failed, '张用原图'] if failed else []), flush=True)
    return made


if __name__ == '__main__':
    import argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import memlib
    ap = argparse.ArgumentParser(description='给时光手帐的照片补齐缩略图')
    ap.add_argument('--root', default='memories')
    a = ap.parse_args()
    d = memlib.load(a.root)
    memlib.save(a.root, d)            # save 里会调用 ensure()
