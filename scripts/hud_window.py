#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hud_window.py —— 桌面额度竖版胶囊（v3 · 灵动岛质感）

与 v2 的核心区别：胶囊与详情卡**全部改为 PIL 高分辨率渲染成位图**（3x 超采样后 LANCZOS 缩回），
tkinter Canvas 不再用 create_arc / create_text 画任何 UI（无抗锯齿是旧版“发糊”的根因），
只负责显示 PhotoImage 与承接鼠标事件。外观对齐 iPhone 灵动岛：纯黑胶囊、全平滑边缘、
圆头进度弧；交互改为**指针轮询**防闪烁（窗口滑动/卡片弹出的 Leave 事件风暴是旧版“一闪一闪”的根因）。

停靠屏幕右缘的窄胶囊（宽 56px，自上而下 OC/GPT/GOAT/CLD/DSK），平时只露 12px 竖条，
鼠标移入滑出、离开 600ms 收回；每格 = 圆形头像 + 环形进度圈 + 文字（OC/GPT/GOAT=百分比，
CLD/DSK=余额）；悬停弹左侧详情卡。
数据：同目录 `import quota_hud`，后台 daemon 线程 collect(proxy='http://127.0.0.1:7897')，
结果放 Lock 保护的 dict，主线程 root.after(200) 轮询刷新（任何 tk 操作只在主线程）。
交互：<Enter> 立即滑出；视觉轮询（150ms）按指针位置算 hover/收回；拖动只改 y；
右键菜单＝刷新/置顶/停靠开关/终端面板/装·卸自启/退出；位置存 ~/.quotalite_pos。
自启：--install-startup 写 %APPDATA%\...\Startup\QuotaHUD.vbs（wscript+pythonw 静默，无黑窗）。
命令行：--no-topmost / --interval N / --install-startup / --uninstall-startup / --dock 0 /
        --pos X,Y / --regen-avatars。
红线：无任何密钥；零新增第三方依赖（tkinter + PIL + 标准库，numpy 仅用于边缘去紫边的可选加速）；
      异常一律捕获不崩。
"""
import argparse, ctypes, datetime as _dt, io, math, os, socket, subprocess, sys
import threading, time, tkinter as tk
from tkinter import messagebox
import quota_hud                                 # 同目录数据层，复用五家 collect()
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageTk

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(THIS_DIR), "assets")
PROXY, LOCK_PORT, POS_FILE = "http://127.0.0.1:7897", 19877, os.path.expanduser("~/.quotalite_pos")
CHROMA = "#ff00fe"                               # 透明色键（让圆角外露出桌面）

# ---- 几何（严格按任务书 5）----
S = 3                                            # 3x 超采样
CELL_H, TOP_PAD = 80, 14
WIN_W = 56
WIN_H = 5 * CELL_H + 2 * TOP_PAD                 # 428
CX = WIN_W // 2
RADIUS = WIN_W // 2                              # 28 → 真半圆端头
RING_R, RING_W = 19, 3.2
AVATAR_DISP = 34                                 # 运行时头像显示尺寸
AVATAR_SRC_SZ = 96                               # 头像缓存主图尺寸
PILL_BG, PILL_BORDER, PILL_HILITE = "#000000", "#1c1c1e", "#26262a"
RING_TRACK, AVATAR_RING, AVATAR_RING_HOVER = "#2c2c2e", "#3a3a3c", "#6e6e73"
CARD_W, CARD_R, CARD_BG, CARD_BORDER = 200, 16, "#1c1c1e", "#333336"
DOCK_REVEAL, DOCK_MARGIN = 12, 4
ANIM_STEPS, ANIM_MS, DOCK_POLL_MS, LEAVE_MS = 10, 12, 150, 600
GREEN, YELLOW, RED, GRAY = "#30d158", "#ffd60a", "#ff453a", "#48484a"
TXT, DIM = "#f2f2f7", "#98989d"
KEY_IDX = ["opencode", "codex", "cmdc", "bbapi", "deepseek"]
THRESHOLDS = {"percent": (50, 80), "bbapi_full": 6.0, "deepseek_full": 30.0}
AVATAR_SPECS = [
    {"code": "OC", "title": "opencode-go", "url": "https://opencode.ai/favicon.ico",
     "proxy": PROXY, "badge": "O", "color": "#007ACC"},
    {"code": "GPT", "title": "codex (ChatGPT Plus)",
     "url": "https://www.google.com/s2/favicons?domain=openai.com&sz=128",
     "proxy": PROXY, "badge": "G", "color": "#10a37f"},
    {"code": "GOAT", "title": "cmdc (Command Code GOAT)", "url": "https://commandcode.ai/favicon.ico",
     "proxy": None, "badge": "C", "color": "#5E5CE6"},
    {"code": "CLD", "title": "claude (bb-api 中转站)", "local": "claude_logo.png",
     "sharpen": True, "badge": "A", "color": "#d97757"},
    {"code": "DSK", "title": "deepseek (官方)",
     "url": "https://www.google.com/s2/favicons?domain=deepseek.com&sz=128",
     "proxy": PROXY, "badge": "D", "color": "#4D6BFE"},
]

# ---------------------------------------------------------------- 通用工具

def _num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0

def _fmt2(v):
    try: return "%.2f" % float(v)
    except (TypeError, ValueError): return "0.00"

def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)

def _rgba(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)

def _reset_abs(w):
    """窗口重置时刻 → '9月30日 13:33'；无法解析返回 None。"""
    ts = w.get("resets_at")
    dt = quota_hud.parse_iso(ts) if isinstance(ts, str) and ts else None
    if dt is None and isinstance(ts, (int, float)) and ts > 0:
        try: dt = _dt.datetime.fromtimestamp(ts / 1000.0).astimezone()
        except (OverflowError, OSError, ValueError): dt = None
    if dt is None and w.get("reset_after_seconds"):
        dt = _dt.datetime.now().astimezone() + _dt.timedelta(seconds=int(w["reset_after_seconds"]))
    return "%d月%d日 %02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute) if dt else None

def used_pct(idx, p):
    """按任务书口径归一 used%（0-100）；不可用返回 None。"""
    d = (p or {}).get(KEY_IDX[idx], {})
    if not d.get("ok"):
        return None
    if idx in (0, 1, 2):                                    # OC/GPT：最满窗；GOAT：5h/周窗取大
        ids = ("fiveHour", "weekly") if idx == 2 else None
        vals = [_num(w.get("percent")) for w in (d.get("windows") or [])
                if ids is None or w.get("id") in ids]
        return max(vals) if vals else None
    if d.get("balance") is None:                            # CLD/DSK：余额反推
        return None
    full = THRESHOLDS["bbapi_full"] if idx == 3 else THRESHOLDS["deepseek_full"]
    return (1.0 - min(max(_num(d["balance"]), 0.0) / full, 1.0)) * 100.0

def ring_color(pct):
    if pct is None: return GRAY
    g, y = THRESHOLDS["percent"]
    return GREEN if pct < g else (YELLOW if pct < y else RED)

def _has_cjk(s):
    return any(ord(ch) > 0x2E7F for ch in str(s))

def _font(px, semibold=False, cjk=False):
    """Segoe UI（拉丁）与 微软雅黑（含中文，PIL truetype 无自动回退，中文必须显式换字体）。"""
    if cjk:
        names = ("msyhbd.ttc", "simhei.ttf") if semibold else ("msyh.ttc", "simhei.ttf")
        tail = ("msyh.ttc",)
    else:
        names = ("seguisb.ttf", "segoeuib.ttf") if semibold else ("segoeui.ttf",)
        tail = ("arialbd.ttf",)
    for name in names + tail:
        try: return ImageFont.truetype("C:/Windows/Fonts/" + name, int(round(px)))
        except Exception: continue
    return ImageFont.load_default()

def _text(d, xy, s, px, semibold, color, anchor="la"):
    """按字符串是否含中文自动选字体后绘制一行文本。"""
    d.text(xy, str(s), font=_font(px, semibold, _has_cjk(s)), fill=_rgba(color), anchor=anchor)

def _fit_text(d, xy, s, sizes, semibold, color, max_w, anchor="la"):
    """在候选字号里选第一个放得下的；都放不下则最小字号截断加省略号。"""
    s = str(s)
    for px in sizes:
        f = _font(px, semibold, _has_cjk(s))
        if d.textlength(s, font=f) <= max_w:
            d.text(xy, s, font=f, fill=_rgba(color), anchor=anchor); return
    px = sizes[-1]; f = _font(px, semibold, _has_cjk(s)); t = s
    while t and d.textlength(t + "…", font=f) > max_w: t = t[:-1]
    d.text(xy, (t + "…") if t != s else s, font=f, fill=_rgba(color), anchor=anchor)

def _snap_chroma(im, chroma=(255, 0, 254)):
    """把 3x 降采样后在胶囊边缘产生的“洋红混色”硬切回色键色（-transparentcolor 无法表示半透明，
    不切会留一圈紫边）。numpy 可用时向量化，否则逐像素兜底。"""
    try:
        import numpy as np
        a = np.array(im)
        r, g, b = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
        a[(r > 70) & (b > 70) & (g < 70)] = (chroma[0], chroma[1], chroma[2], 255)
        return Image.fromarray(a, "RGBA")
    except Exception:
        px = im.load()
        for yy in range(im.size[1]):
            for xx in range(im.size[0]):
                r, g, b, al = px[xx, yy]
                if r > 70 and b > 70 and g < 70:
                    px[xx, yy] = (chroma[0], chroma[1], chroma[2], 255)
        return im

# ---------------------------------------------------------------- 头像（采集与合成）

def _badge(letter, color, size=AVATAR_SRC_SZ):
    """品牌色圆 + 白字母兜底徽章（圆形 RGBA，size×size）。"""
    ss = size * 4
    im = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im); d.ellipse((0, 0, ss - 1, ss - 1), fill=_rgba(color))
    try:
        f = _font(int(ss * 0.46), True); bb = d.textbbox((0, 0), letter, font=f)
        d.text(((ss - bb[2] + bb[0]) / 2.0 - bb[0], (ss - bb[3] + bb[1]) / 2.0 - bb[1]),
               letter, font=f, fill=(255, 255, 255, 255))
    except Exception:
        d.text((ss / 2 - 8, ss / 2 - 10), letter, fill=(255, 255, 255, 255))
    return im.resize((size, size), Image.LANCZOS)

def _compose_avatar(img, size=AVATAR_SRC_SZ):
    """方形化 → 浅底 #22272e 圆盘 + 缩放 logo + 圆蒙版收边 → size×size RGBA。"""
    img = img.convert("RGBA"); w, h = img.size; s = max(w, h)
    square = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    square.paste(img, ((s - w) // 2, (s - h) // 2))
    inner = max(8, int(size * 0.72))
    logo = square.resize((inner, inner), Image.LANCZOS)
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(base).ellipse((0, 0, size - 1, size - 1), fill=(34, 39, 46, 255))
    base.alpha_composite(logo, ((size - inner) // 2, (size - inner) // 2))
    big = base.resize((size * 4, size * 4), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    out = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    out.paste(big, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)

def _http_bytes(url, proxy, timeout):
    import urllib.request
    h = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    req = urllib.request.Request(url, headers={"User-Agent": "quota-hud/3.0"}, method="GET")
    with urllib.request.build_opener(h).open(req, timeout=timeout) as resp:
        return resp.read()

def _ensure_avatar(spec, force=False):
    """确保 assets/avatar_XX.png（96px 主图）存在；缓存优先 → 下载 → 兜底徽章；返回路径。"""
    path = os.path.join(ASSETS, "avatar_%s.png" % spec["code"])
    if os.path.isfile(path) and not force:
        return path
    img = None
    try:
        local = os.path.join(ASSETS, spec["local"]) if spec.get("local") else None
        if local and os.path.isfile(local):
            img = Image.open(local).convert("RGBA")
            if spec.get("sharpen"):
                img = ImageEnhance.Sharpness(img).enhance(1.4)
        elif spec.get("url"):
            img = Image.open(io.BytesIO(_http_bytes(spec["url"], spec.get("proxy"), 8)))
            if hasattr(img, "ico"):
                try:
                    sizes = list(img.ico.sizes())
                    if sizes: img.size = max(sizes, key=lambda sz: sz[0] * sz[1])
                except Exception: pass
            img = img.convert("RGBA")
    except Exception:
        img = None
    out = _compose_avatar(img) if img is not None else _badge(spec["badge"], spec["color"])
    try: out.save(path)
    except Exception: pass
    return path

def regen_avatars(force=True):
    """重生成全部头像缓存（--regen-avatars / 首次启动缺图时用）。返回 {code: 来源}。"""
    if not os.path.isdir(ASSETS):
        try: os.makedirs(ASSETS)
        except OSError: pass
    src = {}
    for spec in AVATAR_SPECS:
        path = _ensure_avatar(spec, force)
        if spec.get("local") and os.path.isfile(os.path.join(ASSETS, spec["local"])):
            src[spec["code"]] = "local:%s" % spec["local"]
        elif spec.get("url"):
            src[spec["code"]] = "url:%s" % spec["url"].split("/")[2]
        else:
            src[spec["code"]] = "badge"
        if not os.path.isfile(path):
            src[spec["code"]] += " (failed→badge)"
    return src

# ---------------------------------------------------------------- PIL 渲染层

def _ring_endpoints(cx, cy, r, a):  # 角度（PIL 约定：0=右，顺时针为正）→ 端点坐标
    return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))

def _draw_ring(d, cx, cy, pct):
    """环形轨道 + 圆头进度弧（从 12 点顺时针）。"""
    r, lw = RING_R * S, RING_W * S
    bbox = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(bbox, outline=_rgba(RING_TRACK), width=int(round(lw)))
    if not pct or pct <= 0:
        return
    color = _rgba(ring_color(pct))
    start, extent = -90.0, min(pct, 100.0) * 3.6
    d.arc(bbox, start=start, end=start + extent, fill=color, width=int(round(lw)))
    cap = lw / 2.0
    for a in (start, start + extent):                       # 两端圆头
        px, py = _ring_endpoints(cx, cy, r, a)
        d.ellipse((px - cap, py - cap, px + cap, py + cap), fill=color)

def _draw_avatar(img, d, cx, cy, avatar, hover):
    """34px 头像 + 外圈描边（hover 换色）。"""
    r = AVATAR_DISP * S / 2.0
    box = (cx - r, cy - r, cx + r, cy + r)
    if avatar is not None:
        a = avatar.resize((int(AVATAR_DISP * S), int(AVATAR_DISP * S)), Image.LANCZOS)
        img.alpha_composite(a, (int(round(cx - r)), int(round(cy - r))))
    else:
        d.ellipse(box, fill=_rgba("#22272e"))
    d.ellipse(box, outline=_rgba(AVATAR_RING_HOVER if hover else AVATAR_RING),
              width=max(1, int(round(1.5 * S))))

def _draw_text(d, cx, cy, text):
    """头像正下方 8px，居中，Segoe UI Semibold 12px #f2f2f7。"""
    f = _font(12 * S, True, _has_cjk(text))
    bb = d.textbbox((0, 0), text, font=f)
    d.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0], cy + AVATAR_DISP * S / 2.0 + 8 * S - bb[1]),
           text, font=f, fill=_rgba(TXT))

def render_pill(spec):
    """整条胶囊（3x → LANCZOS 缩回 1x）。spec = {"cells":[{pct,text,avatar,hover},...]}。"""
    w, h = WIN_W * S, WIN_H * S
    img = Image.new("RGBA", (w, h), _rgba(CHROMA))
    d = ImageDraw.Draw(img)
    rad = min(RADIUS * S, (w - 1) // 2, (h - 1) // 2)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=rad, fill=_rgba(PILL_BG))
    bw = max(1, int(round(1.5 * S)))
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=rad, outline=_rgba(PILL_BORDER), width=bw)
    r2 = rad - bw - 1                                       # 顶部内侧玻璃高光（上半个圆）
    hb = max(1, int(round(1.0 * S)))
    d.arc((w / 2.0 - r2, rad - r2, w / 2.0 + r2, rad + r2), start=180, end=360,
          fill=_rgba(PILL_HILITE), width=hb)
    for i, cell in enumerate(spec.get("cells", [])):
        cy = (TOP_PAD + CELL_H * i + CELL_H // 2) * S
        ccx = CX * S
        _draw_ring(d, ccx, cy, cell.get("pct"))
        _draw_avatar(img, d, ccx, cy, cell.get("avatar"), cell.get("hover"))
        _draw_text(d, ccx, cy, cell.get("text", "--"))
    return _snap_chroma(img.resize((WIN_W, WIN_H), Image.LANCZOS))

def _card_bar(d, x0, y0, width, height, pct, color):
    """圆头进度条：轨道 + 填充。"""
    d.rounded_rectangle((x0, y0, x0 + width, y0 + height), radius=height / 2.0,
                        fill=_rgba(RING_TRACK))
    if pct and pct > 0:
        fw = max(height, width * min(pct, 100.0) / 100.0)
        d.rounded_rectangle((x0, y0, x0 + fw, y0 + height), radius=height / 2.0, fill=_rgba(color))

def render_card(idx, providers, avatars):
    """详情卡（PIL 渲染）。返回 (Image, height)。"""
    d = (providers or {}).get(KEY_IDX[idx], {})
    pct = used_pct(idx, providers or {})
    windows = d.get("windows") or []
    ok = bool(d.get("ok"))
    y = 40
    if not ok:
        y += 24
    elif idx in (3, 4):
        y += 18
        if idx == 3 and d.get("today"): y += 15
        y += 18
    else:
        y += 34 * max(1, len(windows))
    height = max(56, y + 12)
    w, h = CARD_W * S, height * S
    img = Image.new("RGBA", (w, h), _rgba(CHROMA))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=CARD_R * S, fill=_rgba(CARD_BG))
    dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=CARD_R * S,
                         outline=_rgba(CARD_BORDER), width=S)
    av = avatars.get(idx)
    if av is not None:
        img.alpha_composite(av.resize((20 * S, 20 * S), Image.LANCZOS), (12 * S, 10 * S))
    else:
        dr.ellipse((12 * S, 10 * S, 32 * S - 1, 30 * S - 1), fill=_rgba("#22272e"))
    _fit_text(dr, (38 * S, 20 * S), AVATAR_SPECS[idx]["title"], (12 * S, 11 * S, 10 * S),
              True, TXT, (CARD_W - 12 - 38) * S, "lm")
    y = 40 * S
    if not ok:
        _text(dr, (12 * S, y), "不可用: %s" % (d.get("error") or "未知错误"),
              10 * S, False, RED, "la")
    elif idx in (3, 4):
        sym = "$" if idx == 3 else ("¥" if d.get("currency") == "CNY"
                                    else ("$" if d.get("currency") == "USD" else ""))
        _text(dr, (12 * S, y), "余额 %s%s" % (sym, _fmt2(d.get("balance"))),
              12 * S, True, TXT, "la")
        y += 18 * S
        t = d.get("today")
        if idx == 3 and t:
            _text(dr, (12 * S, y), "今日 %d 次请求 · $%s"
                  % (t.get("requests", 0), _fmt2(t.get("actual_cost"))),
                  10 * S, False, DIM, "la")
            y += 15 * S
        _card_bar(dr, 12 * S, y, (CARD_W - 24) * S, 4 * S, pct, ring_color(pct))
    else:
        for win in windows:
            _text(dr, (12 * S, y), win.get("label", ""), 11 * S, False, DIM, "la")
            lab = _reset_abs(win)
            _text(dr, ((CARD_W - 12) * S, y + 1 * S), "重置于 %s" % (lab if lab else "未知"),
                  10 * S, False, DIM, "ra")
            y += 16 * S
            wp = _num(win.get("percent"))
            _card_bar(dr, 12 * S, y, (CARD_W - 24) * S, 4 * S, wp, ring_color(wp))
            y += 18 * S
    return _snap_chroma(img.resize((CARD_W, height), Image.LANCZOS)), height

# ---------------------------------------------------------------- 单实例 / 自启 / DPI

def _acquire_lock(port=LOCK_PORT):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port)); sock.listen(1)
    except OSError:
        print("已有 QuotaHUD 实例在运行（127.0.0.1:%d 被占用），退出。" % port)
        sys.exit(0)
    return sock

def _set_dpi_aware():
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception: pass

def _startup_vbs_path():
    ad = os.environ.get("APPDATA")
    return (os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs",
                         "Startup", "QuotaHUD.vbs") if ad else None)

def install_startup():
    vbs = _startup_vbs_path()
    if not vbs: return False, "未找到 %APPDATA%"
    pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    pw = pw if os.path.isfile(pw) else "pythonw.exe"
    content = ('Set sh = CreateObject("WScript.Shell")\r\nsh.CurrentDirectory = "%s"\r\n'
               'sh.Run """%s"" ""%s""", 0, False\r\n'
               % (THIS_DIR, pw, os.path.join(THIS_DIR, "hud_window.py")))
    try:
        with open(vbs, "w", encoding="ascii", errors="replace") as fh: fh.write(content)
    except OSError as e:
        return False, "写入失败: %s" % e
    return True, vbs

def uninstall_startup():
    vbs = _startup_vbs_path()
    if not (vbs and os.path.isfile(vbs)): return True, "未安装"
    try:
        os.remove(vbs); return True, vbs
    except OSError as e:
        return False, "删除失败: %s" % e

def _message(title, text):
    root = tk.Tk(); root.withdraw(); messagebox.showinfo(title, text); root.destroy()

# ---------------------------------------------------------------- 应用

class HudApp(object):
    def __init__(self, args):
        self.args, self.providers = args, None
        self.lock = threading.Lock()
        self.state = {"providers": None, "last": None, "refreshing": False, "error": None}
        self._hover = self._drag = None
        self._card = None; self._card_idx = None
        self._anim_job = self._anim_target = self._anim_start = None
        self._anim_i = 0; self._animating = False
        self._last_inside = time.time()
        self._aimg, self._anew, self._alock = {}, {}, threading.Lock()
        self._pill_photo = self._card_photo = None
        self._pill_pil = None
        self._rendered = False
        self.root = tk.Tk()
        self.root.title("Quota HUD")
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        try:
            self.root.attributes("-transparentcolor", CHROMA); self.bgcol = CHROMA
        except Exception:
            self.bgcol = PILL_BG
        self.root.configure(bg=self.bgcol)
        self.topmost = not args.no_topmost
        self.root.attributes("-topmost", self.topmost)
        self._restore_pos()
        self.cv = tk.Canvas(self.root, width=WIN_W, height=WIN_H, bg=self.bgcol,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        for fn in (self._build_items, self._bind_events, self._build_menu, self._start_avatars,
                   self.refresh, self._poll, self._visual_poll, self._schedule_auto):
            fn()
    # -------- 停靠坐标 --------
    def _ex(self):
        return self.root.winfo_screenwidth() - WIN_W - DOCK_MARGIN
    def _rx(self):
        return self.root.winfo_screenwidth() - DOCK_REVEAL
    # -------- 布局 / 头像 --------
    def _build_items(self):
        self._pill_item = self.cv.create_image(0, 0, anchor="nw")
        for i in range(5):
            self._aimg[i] = self._load_avatar(i)
        self._render_pill()
        self._rendered = True
    def _load_avatar(self, i):
        path = os.path.join(ASSETS, "avatar_%s.png" % AVATAR_SPECS[i]["code"])
        if os.path.isfile(path):
            try: return Image.open(path).convert("RGBA")
            except Exception: pass
        return _badge(AVATAR_SPECS[i]["badge"], AVATAR_SPECS[i]["color"])
    def _cell_text(self, i):
        p = self.providers or {}
        d = p.get(KEY_IDX[i], {})
        if i not in (0, 1, 2) and d.get("ok") and d.get("balance") is not None:
            if i == 3:
                return "$%.2f" % float(d["balance"])
            cur = d.get("currency")
            sym = "¥" if cur == "CNY" else ("$" if cur == "USD" else "")
            return "%s%.2f" % (sym, float(d["balance"]))
        pct = used_pct(i, p)
        return ("%d%%" % int(round(pct))) if pct is not None else "--"
    def _cell_specs(self):
        p = self.providers or {}
        return [{"pct": used_pct(i, p), "text": self._cell_text(i),
                 "avatar": self._aimg.get(i), "hover": (self._hover == i)} for i in range(5)]
    def _render_pill(self):
        try:
            self._pill_pil = render_pill({"cells": self._cell_specs()})
            self._pill_photo = ImageTk.PhotoImage(self._pill_pil)
            self.cv.itemconfig(self._pill_item, image=self._pill_photo)
        except Exception:
            pass
    def _start_avatars(self):
        if not os.path.isdir(ASSETS):
            try: os.makedirs(ASSETS)
            except OSError: return
        todo = [i for i in range(5) if not os.path.isfile(
            os.path.join(ASSETS, "avatar_%s.png" % AVATAR_SPECS[i]["code"]))]
        if not todo: return
        def work():
            for i in todo:
                try: path = _ensure_avatar(AVATAR_SPECS[i])
                except Exception: continue
                with self._alock: self._anew[i] = path
        threading.Thread(target=work, daemon=True).start()
    def _bind_events(self):
        for seq, fn in (("<Enter>", self._on_enter), ("<Button-1>", self._on_press),
                        ("<B1-Motion>", self._on_drag), ("<ButtonRelease-1>", self._on_release),
                        ("<Button-3>", self._on_menu)):
            self.cv.bind(seq, fn)
    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self._topmost_var = tk.BooleanVar(value=self.topmost)
        self._dock_var = tk.BooleanVar(value=self.docked)
        self.menu.add_command(label="立即刷新", command=self.refresh)
        self.menu.add_checkbutton(label="置顶", variable=self._topmost_var, command=self._toggle_topmost)
        self.menu.add_checkbutton(label="停靠右缘", variable=self._dock_var, command=self._toggle_dock)
        self.menu.add_separator()
        self.menu.add_command(label="打开终端面板", command=self._open_terminal)
        self.menu.add_command(label="安装开机自启", command=self._install_startup)
        self.menu.add_command(label="移除开机自启", command=self._uninstall_startup)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._quit)
    def _restore_pos(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.docked, x, y = bool(self.args.dock), sw - WIN_W - DOCK_MARGIN, (sh - WIN_H) // 2
        src = self.args.pos
        if not src and os.path.isfile(POS_FILE):
            try: src = open(POS_FILE, encoding="utf-8").read().strip()
            except OSError: src = None
        if src:
            try:
                p = src.split(",")
                if p[0] == "dock": self.docked, y = True, int(p[1])
                elif p[0] == "free": self.docked, x, y = False, int(p[1]), int(p[2])
                elif len(p) >= 2: self.docked, x, y = False, int(p[0]), int(p[1])  # --pos X,Y
                else: src = None  # 旧版格式，忽略，保持默认停靠
            except (ValueError, IndexError): pass
        y = _clamp(y, 0, max(0, sh - WIN_H))
        x = self._rx() if self.docked else _clamp(x, 0, max(0, sw - WIN_W))
        self.root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, x, y))
    def _save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as fh:
                fh.write("dock,%d" % self.root.winfo_y() if self.docked
                         else "free,%d,%d" % (self.root.winfo_x(), self.root.winfo_y()))
        except OSError: pass
    # -------- 停靠伸缩（ease-out cubic）--------
    def _slide_to(self, target):
        if not self.docked: return
        if self.root.winfo_x() == target and not self._animating: return
        if self._animating and self._anim_target == target: return
        self._anim_start, self._anim_i, self._anim_target = self.root.winfo_x(), 0, target
        self._animating = True
        if self._anim_job is None:
            self._anim_job = self.root.after(0, self._anim_step)
    def _anim_step(self):
        self._anim_job = None
        target = self._anim_target
        if target is None:
            self._animating = False; return
        self._anim_i += 1
        t = min(self._anim_i / float(ANIM_STEPS), 1.0)
        frac = 1.0 - (1.0 - t) ** 3
        x = int(round(self._anim_start + (target - self._anim_start) * frac))
        self.root.geometry("+%d+%d" % (x, self.root.winfo_y()))
        if self._anim_i >= ANIM_STEPS:
            self.root.geometry("+%d+%d" % (target, self.root.winfo_y()))
            self._anim_target = None; self._animating = False
            self._post_anim(); return
        self._anim_job = self.root.after(ANIM_MS, self._anim_step)
    def _post_anim(self):
        try:
            px, py = self.root.winfo_pointerxy()
            if self._in_window(self.root, px, py) or self._in_window(self._card, px, py):
                self._last_inside = time.time()
        except Exception:
            pass
    def _cancel_anim(self):
        if self._anim_job is not None:
            try: self.root.after_cancel(self._anim_job)
            except Exception: pass
        self._anim_job, self._anim_target, self._animating = None, None, False
    def _on_enter(self, _e):
        self._last_inside = time.time()
        if self.docked: self._slide_to(self._ex())
    # -------- 指针轮询（防闪烁核心）--------
    def _hit(self, y):
        return int((y - TOP_PAD) // CELL_H) if TOP_PAD <= y < TOP_PAD + 5 * CELL_H else None
    def _in_window(self, win, px, py):
        if win is None: return False
        try:
            x, y = win.winfo_rootx(), win.winfo_rooty()
            return x <= px <= x + win.winfo_width() and y <= py <= y + win.winfo_height()
        except Exception:
            return False
    def _visual_poll(self):
        try:
            self._tick_visual()
        except Exception:
            pass
        self.root.after(DOCK_POLL_MS, self._visual_poll)
    def _tick_visual(self):
        px, py = self.root.winfo_pointerxy()
        in_pill = self._in_window(self.root, px, py)
        in_card = self._in_window(self._card, px, py)
        now = time.time()
        if in_pill or in_card:
            self._last_inside = now
            if self.docked and not self._animating and self.root.winfo_x() != self._ex():
                self._slide_to(self._ex())                  # 指针回到条内 → 立即展开
            if in_pill:
                self._set_hover(self._hit(py - self.root.winfo_rooty()))
        elif not self._animating and now - self._last_inside >= LEAVE_MS / 1000.0:
            self._set_hover(None)
            if self.docked and self.root.winfo_x() != self._rx():
                self._slide_to(self._rx())
    def _set_hover(self, idx):
        if idx == self._hover: return
        self._hover = idx
        if idx is None:
            self._destroy_card()
        elif 0 <= idx < 5:
            self._show_card(idx)
        self._render_pill()                                 # hover 描边换色
    # -------- 详情卡片 --------
    def _new_top(self):
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True); top.configure(bg=self.bgcol)
        try: top.attributes("-transparentcolor", CHROMA)
        except Exception: pass
        return top
    def _place_card(self, idx, height):
        sx = max(8, self.root.winfo_x() - 12 - CARD_W)
        cy = TOP_PAD + CELL_H * idx + CELL_H // 2
        sy = _clamp(self.root.winfo_y() + cy - height // 2, 8,
                    max(8, self.root.winfo_screenheight() - height - 8))
        self._card.geometry("+%d+%d" % (sx, sy))
    def _show_card(self, idx):
        self._destroy_card()
        img, height = render_card(idx, self.providers, self._aimg)
        self._card_photo = ImageTk.PhotoImage(img)
        top = self._new_top()
        cv = tk.Canvas(top, width=CARD_W, height=height, bg=self.bgcol,
                       highlightthickness=0, bd=0)
        cv.pack()
        cv.create_image(0, 0, anchor="nw", image=self._card_photo)
        top.bind("<Enter>", lambda _e: setattr(self, "_last_inside", time.time()))
        self._card, self._card_idx, self._card_cv = top, idx, cv
        self._place_card(idx, height)
    def _refresh_card(self):
        if self._card is None or self._card_idx is None: return
        try:
            img, height = render_card(self._card_idx, self.providers, self._aimg)
            self._card_photo = ImageTk.PhotoImage(img)
            self._card_cv.config(height=height)
            self._card_cv.itemconfig(1, image=self._card_photo)   # item 1 = 卡片位图
            self._place_card(self._card_idx, height)
        except Exception:
            pass
    def _destroy_card(self):
        if self._card is not None:
            try: self._card.destroy()
            except Exception: pass
        self._card = self._card_idx = None
    # -------- 拖动 --------
    def _on_press(self, e):
        self._cancel_anim()
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
    def _on_drag(self, e):
        if not self._drag: return
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        ny = _clamp(e.y_root - self._drag[1], 0, max(0, sh - WIN_H))
        nx = self._ex() if self.docked else _clamp(e.x_root - self._drag[0], 0, max(0, sw - WIN_W))
        self.root.geometry("+%d+%d" % (nx, ny))
    def _on_release(self, _e):
        if self._drag:
            self._drag = None; self._save_pos(); self._last_inside = time.time()
    # -------- 刷新 / 轮询 --------
    def refresh(self):
        with self.lock: self.state["refreshing"] = True
        threading.Thread(target=self._worker, daemon=True).start()
    def _worker(self):
        err, providers = None, None
        try: providers = quota_hud.collect(proxy=PROXY)
        except Exception as e:                          # noqa: BLE001 —— 绝不崩
            err = str(e) or type(e).__name__
        with self.lock:
            self.state.update(providers=providers, last=time.time(), refreshing=False, error=err)
    def _poll(self):
        with self.lock: snap = dict(self.state)
        try: self._update_ui(snap)
        except Exception: pass
        with self._alock:
            new, self._anew = dict(self._anew), {}
        for i, path in new.items():
            try: self._aimg[i] = Image.open(path).convert("RGBA")
            except Exception: continue
        if new:
            try:
                self._render_pill()
                if self._hover is not None: self._refresh_card()
            except Exception: pass
        self.root.after(200, self._poll)
    def _update_ui(self, snap):
        new_prov = snap.get("providers")
        changed = (new_prov is not None and new_prov is not self.providers) or (not self._rendered)
        if new_prov is not None or self.providers is None:
            self.providers = new_prov
        if changed:
            self._render_pill(); self._rendered = True
            if self._hover is not None and self._card is not None:
                self._refresh_card()
    def _schedule_auto(self):
        self.root.after(max(30, self.args.interval) * 1000,
                        lambda: (self.refresh(), self._schedule_auto()))
    # -------- 菜单动作 --------
    def _toggle_topmost(self):
        self.topmost = bool(self._topmost_var.get())
        self.root.attributes("-topmost", self.topmost)
    def _toggle_dock(self):
        self.docked = bool(self._dock_var.get())
        self._cancel_anim()
        if self.docked: self._slide_to(self._rx())
        elif self._card is not None: self._destroy_card()
        self._save_pos()
    def _on_menu(self, e):
        try: self.menu.tk_popup(e.x_root, e.y_root)
        finally: self.menu.grab_release()
    def _open_terminal(self):
        try:
            subprocess.Popen('start "" cmd /k "%s" "%s" --watch'
                             % (sys.executable, os.path.join(THIS_DIR, "quota_hud.py")), shell=True)
        except Exception: pass
    def _install_startup(self):
        ok, info = install_startup()
        _message("QuotaHUD", ("已安装开机自启:\n%s" % info) if ok else ("安装失败: %s" % info))
    def _uninstall_startup(self):
        ok, info = uninstall_startup()
        _message("QuotaHUD", "已移除开机自启" if ok else ("移除失败: %s" % info))
    def _quit(self):
        self._save_pos()
        try:
            self._destroy_card()
            self.root.destroy()
        except Exception: pass
    def run(self):
        self.root.mainloop()

def _parse_args(argv):
    p = argparse.ArgumentParser(description="桌面额度竖版胶囊 v3（零新增依赖）")
    p.add_argument("--no-topmost", action="store_true", help="启动时不置顶")
    p.add_argument("--interval", type=int, default=300, help="刷新间隔秒，默认 300")
    p.add_argument("--install-startup", action="store_true", help="安装开机自启后退出")
    p.add_argument("--uninstall-startup", action="store_true", help="移除开机自启后退出")
    p.add_argument("--regen-avatars", action="store_true", help="重生成全部头像缓存后退出")
    p.add_argument("--dock", type=int, default=1, help="1=右缘停靠(默认)，0=自由摆放")
    p.add_argument("--pos", metavar="X,Y", help="自由模式初始位置，如 40,40")
    p.add_argument("--lock-port", type=int, default=LOCK_PORT, help="单实例锁端口，默认 19877")
    return p.parse_args(argv)

def main(argv=None):
    args = _parse_args(argv)
    if args.regen_avatars:
        print("头像缓存已重生成: %s" % regen_avatars(True))
        return 0
    if args.install_startup or args.uninstall_startup:
        ok, info = install_startup() if args.install_startup else uninstall_startup()
        _message("QuotaHUD", info if ok else "失败: %s" % info)
        return 0 if ok else 1
    _set_dpi_aware()
    lock = _acquire_lock(args.lock_port)                # noqa: F841 —— 保活至退出
    app = HudApp(args)
    try:
        app.run()
    finally:
        try: lock.close()
        except Exception: pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
