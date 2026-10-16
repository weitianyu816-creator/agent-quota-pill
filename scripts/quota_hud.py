#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota_hud.py —— Windows 终端「AI CLI 套餐额度聚合面板」

聚合五家套餐额度/用量，零第三方依赖（纯 Python 3.12 标准库），单文件。

数据源与认证方式
  1) opencode-go
       凭据：~/.local/share/opencode/auth.json（顶层 dict，取第一个键名含
             "opencode"（不分大小写）的条目 ['key'] 作 Bearer）。
       端点：GET https://opencode.ai/zen/go/v1/usage
       响应：usage.{rolling,weekly,monthly}.{status,percent,resetsAt}
  2) codex（ChatGPT Plus）
       凭据：~/.codex/auth.json → tokens.access_token（Bearer）+
             tokens.account_id（请求头 chatgpt-account-id）。
       端点：GET https://chatgpt.com/backend-api/wham/usage
       响应：plan_type、rate_limit.primary_window / secondary_window
             {used_percent, reset_after_seconds}
  3) cmdc（Command Code GOAT）
       凭据：环境变量 COMMAND_CODE_API_KEY（进程内直接取，用户级已设）。
       端点（国内直连，写死不走代理）：
         GET https://api.commandcode.ai/alpha/billing/credits
         GET https://api.commandcode.ai/alpha/usage/summary（可选增强：累计）
       响应：credits.monthlyCredits（月度余额）+ windowLimits.fiveHour/weekly
             {used,cap,exceeded,resetAt(毫秒时间戳，0=未开始)}。
       注意：这里展示「已用/上限」，与 opencode 的「剩余百分比」口径相反。
  4) claude（bb-api 中转站）
       凭据顺序：① 文件 ~/.bbapi_key（即用户主目录下 .bbapi_key，
             单行纯 key，读后 strip）；② 环境变量 BB_API_KEY；③ 环境变量
             ANTHROPIC_API_KEY。都拿不到则降级提示「未找到 key（~/.bbapi_key）」。
       端点（国内直连，写死不走代理）：GET https://www.bb-api.com/v1/usage
       响应：balance（余额，单位 $）+ daily_usage[]（date/requests/total_tokens/
             actual_cost）。取今日（本地日期）一行；今日无记录则显示最近一天并注明。
       补充：仍流式扫描 ~/.claude/projects/**/*.jsonl，统计今天（本地时区）消息数
             与总 token 数（timestamp；message.usage 的 input/output/cache_read/
             cache_creation），作为余额行下方的补充行（今日有中转数据时可省）。
  5) deepseek（官方余额）
       凭据：环境变量 DEEPSEEK_API_KEY（用户级已设，进程内直接取；缺失则降级
             一行原因）。绝不打印 key。
       端点（国内直连，写死不走代理）：GET https://api.deepseek.com/user/balance
       响应：is_available + balance_infos[]{currency,total_balance,granted_balance,
             topped_up_balance}。取第一条的 total_balance 作为余额（保留 2 位）。
       展示：余额 ¥29.79（CNY）· 可用；is_available=false 时红字提示。

代理
  opencode 与 chatgpt 两请求默认走 http://127.0.0.1:7897；
  可用环境变量 QUOTA_HUD_PROXY 或 --proxy 覆盖，--no-proxy 关闭。超时统一 20 秒。
  注意：cmdc 与 bb-api（claude）为国内直连源，--proxy / --no-proxy 对其不生效，
        代码内写死不走代理（见 fetch_cmdc / fetch_bbapi 内的 ProxyHandler({})）。

维护方式
  · 凭据只在运行时从本地文件读取，绝不硬编码、绝不打印任何密钥/token。
  · 单个 provider 失败只显示一行原因，绝不打印 traceback。
  · 换套餐 / 接新源：照抄本文件里对应 fetch_* 函数 + PROVIDERS 注册即可。
  · 设计参照 jazzenchen/agent-hud-open 的 docs/providers.md（端点/路径全景）。
"""

import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 基础配置

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_PROXY = "http://127.0.0.1:7897"
TIMEOUT = 20
BAR_WIDTH = 10

COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


# ---------------------------------------------------------------- 通用工具

def _activate_vt():
    """Windows 终端激活 ANSI VT 转义。"""
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass


def _local_now():
    return _dt.datetime.now().astimezone()


def humanize_seconds(sec):
    """秒 → 人性化字符串，如 '2小时13分'、'3天'。"""
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "未知"
    if sec < 0:
        sec = 0
    if sec < 60:
        return "%d秒" % sec
    if sec < 3600:
        return "%d分" % (sec // 60)
    if sec < 86400:
        h, m = sec // 3600, (sec % 3600) // 60
        return "%d小时" % h if m == 0 else "%d小时%d分" % (h, m)
    d, h = sec // 86400, (sec % 86400) // 3600
    return "%d天" % d if h == 0 else "%d天%d小时" % (d, h)


def parse_iso(ts):
    """ISO8601 字符串 → aware datetime（UTC 落回本地时区）；失败返回 None。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        s = ts.strip().replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def humanize_until(ts):
    """ISO8601 重置时刻 → '还有 2小时13分'；解析失败返回 '未知'。"""
    dt = parse_iso(ts)
    if dt is None:
        return "未知"
    delta = (dt - _local_now()).total_seconds()
    return humanize_seconds(delta)


def make_progress_bar(percent, width=BAR_WIDTH):
    """[#####-----] 56% 形式的进度条（截断取整，与示例一致）。"""
    filled = int(percent / 100.0 * width)
    filled = max(0, min(width, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "] %d%%" % percent


def colorize_bar(text, percent, use_color):
    if not use_color:
        return text
    if percent < 50:
        return COLOR_GREEN + text + COLOR_RESET
    if percent < 80:
        return COLOR_YELLOW + text + COLOR_RESET
    return COLOR_RED + text + COLOR_RESET


def _build_opener(proxy):
    """proxy 为 None 时禁用代理；为字符串时走该代理；否则用系统默认。"""
    if proxy is None:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def http_get_json(url, headers, proxy, retries=2):
    """GET → JSON。失败抛异常（调用方自行捕获，不得打印密钥）。

    瞬时网络抖动（如 commandcode.ai TLS 握手偶发超时）自动重试，
    只重试网络层异常，HTTP 4xx/5xx 不重试（重试无意义还拖慢面板）。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            opener = _build_opener(proxy)
            with opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------- 数据源

def load_auth_json(rel_path):
    path = os.path.expanduser(rel_path)
    if not os.path.isfile(path):
        raise RuntimeError("凭据文件不存在: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_opencode(proxy):
    data = load_auth_json("~/.local/share/opencode/auth.json")
    key = None
    for name, entry in data.items():
        if "opencode" in str(name).lower() and isinstance(entry, dict):
            key = entry.get("key")
            if key:
                break
    if not key:
        raise RuntimeError("未在 auth.json 找到 opencode 条目")
    headers = {
        "Authorization": "Bearer %s" % key,
        "User-Agent": "opencode-quota-hud/1.0",
        "Accept": "application/json",
    }
    payload = http_get_json(
        "https://opencode.ai/zen/go/v1/usage", headers, proxy
    )
    usage = payload.get("usage") or {}
    windows = []
    for label, wid in (("rolling", "滚动(5h)"), ("weekly", "周"), ("monthly", "月")):
        w = usage.get(label) or {}
        windows.append({
            "id": label,
            "label": wid,
            "status": w.get("status", "unknown"),
            "percent": int(w.get("percent", 0) or 0),
            "resets_at": w.get("resetsAt"),
            "reset_in": humanize_until(w.get("resetsAt")),
        })
    return {"ok": True, "windows": windows}


def fetch_codex(proxy):
    data = load_auth_json("~/.codex/auth.json")
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    account = tokens.get("account_id")
    if not token:
        raise RuntimeError("auth.json 缺少 tokens.access_token")
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if account:
        headers["chatgpt-account-id"] = account
    payload = http_get_json(
        "https://chatgpt.com/backend-api/wham/usage", headers, proxy
    )
    rl = payload.get("rate_limit") or {}
    windows = []
    for label, wid in (("primary_window", "5小时窗"), ("secondary_window", "周窗")):
        w = rl.get(label) or {}
        percent = int(round(float(w.get("used_percent", 0) or 0)))
        windows.append({
            "id": label,
            "label": wid,
            "percent": percent,
            "reset_after_seconds": w.get("reset_after_seconds"),
            "reset_in": humanize_seconds(w.get("reset_after_seconds", 0)),
        })
    return {
        "ok": True,
        "plan_type": payload.get("plan_type", "unknown"),
        "windows": windows,
    }


def _humanize_reset_ms(ms):
    """毫秒时间戳 → '还有 3天2小时'；<=0 或非法返回 '未开始'。"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "未开始"
    if ms <= 0:
        return "未开始"
    try:
        dt = _dt.datetime.fromtimestamp(ms / 1000.0).astimezone()
    except (OverflowError, OSError, ValueError):
        return "未知"
    return humanize_seconds((dt - _local_now()).total_seconds())


def _read_cmdc_key():
    key = os.environ.get("COMMAND_CODE_API_KEY")
    if key and key.strip():
        return key.strip()
    raise RuntimeError("未设置环境变量 COMMAND_CODE_API_KEY")


def _read_bbapi_key():
    """按 ① ~/.bbapi_key ② BB_API_KEY ③ ANTHROPIC_API_KEY 顺序取 key。"""
    path = os.path.expanduser("~/.bbapi_key")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                return key
        except OSError:
            pass
    for env in ("BB_API_KEY", "ANTHROPIC_API_KEY"):
        key = os.environ.get(env)
        if key and key.strip():
            return key.strip()
    raise RuntimeError("未找到 key（~/.bbapi_key）")


def fetch_cmdc(proxy):
    """Command Code GOAT：billing/credits（+ 可选 usage/summary）。国内直连。"""
    # 国内直连源：忽略上层 proxy 设置（--proxy / --no-proxy 不生效），写死不走代理。
    key = _read_cmdc_key()
    headers = {
        "Authorization": "Bearer %s" % key,
        "Accept": "application/json",
        "User-Agent": "opencode-quota-hud/1.1",
    }
    payload = http_get_json(
        "https://api.commandcode.ai/alpha/billing/credits", headers, None
    )
    credits = payload.get("credits") or {}
    window_limits = payload.get("windowLimits") or {}
    windows = []
    for label, wid in (("fiveHour", "5小时窗"), ("weekly", "周窗")):
        w = window_limits.get(label) or {}
        try:
            used = float(w.get("used", 0) or 0)
            cap = float(w.get("cap", 0) or 0)
        except (TypeError, ValueError):
            used, cap = 0.0, 0.0
        percent = int(round(used / cap * 100)) if cap else 0
        reset_at = w.get("resetAt", 0) or 0
        windows.append({
            "id": label,
            "label": wid,
            "used": round(used, 2),
            "cap": round(cap, 2),
            "percent": percent,
            "exceeded": w.get("exceeded"),
            "resets_at": reset_at,
            "reset_in": _humanize_reset_ms(reset_at),
        })
    try:
        balance = round(float(credits.get("monthlyCredits", 0) or 0), 2)
    except (TypeError, ValueError):
        balance = 0.0
    result = {"ok": True, "balance": balance, "windows": windows}
    # 可选增强：累计用量/请求数（失败不影响主数据）
    try:
        summary = http_get_json(
            "https://api.commandcode.ai/alpha/usage/summary", headers, None
        )
        result["total_cost"] = round(float(summary.get("totalCost", 0) or 0), 2)
        result["total_count"] = summary.get("totalCount")
    except Exception:  # noqa: BLE001 —— 增强项失败静默
        pass
    return result


def _today_local_date():
    return _local_now().date()


def _scan_claude_local():
    """流式扫描 ~/.claude/projects/**/*.jsonl，统计今日本地用量（永不抛异常）。"""
    root = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(root):
        return None
    today = _today_local_date()
    messages = 0
    tokens = 0
    token_parts = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    files = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            files += 1
            full = os.path.join(dirpath, fn)
            try:
                fh = open(full, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:  # 逐行流式读取，禁止一次性读入大文件
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue  # 单行解析失败跳过
                    ts = obj.get("timestamp")
                    dt = parse_iso(ts)
                    if dt is None or dt.date() != today:
                        continue
                    msg = obj.get("message")
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    messages += 1
                    for key, part in (
                        ("input_tokens", "input"),
                        ("output_tokens", "output"),
                        ("cache_read_input_tokens", "cache_read"),
                        ("cache_creation_input_tokens", "cache_creation"),
                    ):
                        val = usage.get(key, 0) or 0
                        try:
                            val = int(val)
                        except (TypeError, ValueError):
                            val = 0
                        token_parts[part] += val
                        tokens += val
    return {
        "messages": messages,
        "tokens": tokens,
        "token_parts": token_parts,
        "files_scanned": files,
    }


def fetch_bbapi(proxy):
    """claude（bb-api 中转站）：GET /v1/usage 取余额+今日用量。国内直连。"""
    # 国内直连源：忽略上层 proxy 设置（--proxy / --no-proxy 不生效），写死不走代理。
    local = _scan_claude_local()
    try:
        key = _read_bbapi_key()
    except RuntimeError as e:
        return {"ok": False, "balance": None, "windows": [],
                "error": str(e), "local": local}
    headers = {
        "Authorization": "Bearer %s" % key,
        "Accept": "application/json",
        "User-Agent": "opencode-quota-hud/1.1",
    }
    try:
        payload = http_get_json(
            "https://www.bb-api.com/v1/usage", headers, None
        )
    except urllib.error.HTTPError as e:
        return {"ok": False, "balance": None, "windows": [],
                "error": "HTTP %s" % e.code, "local": local}
    except urllib.error.URLError as e:
        return {"ok": False, "balance": None, "windows": [],
                "error": "网络错误: %s" % _short(e.reason), "local": local}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "balance": None, "windows": [],
                "error": _short(str(e)) or type(e).__name__, "local": local}

    try:
        balance = round(float(payload.get("balance", 0) or 0), 2)
    except (TypeError, ValueError):
        balance = 0.0
    daily = payload.get("daily_usage") or []
    today = _today_local_date().isoformat()
    entry = None
    exact = False
    for d in daily:
        if isinstance(d, dict) and str(d.get("date")) == today:
            entry, exact = d, True
            break
    if entry is None and daily:
        valid = [d for d in daily if isinstance(d, dict)]
        if valid:
            entry = sorted(valid, key=lambda x: str(x.get("date", "")))[-1]
    today_info = None
    if entry is not None:
        def _num(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0
        today_info = {
            "date": str(entry.get("date", "")),
            "exact": exact,
            "requests": int(_num(entry.get("requests"))),
            "actual_cost": round(_num(entry.get("actual_cost")), 2),
            "total_tokens": int(_num(entry.get("total_tokens"))),
        }
    return {
        "ok": True,
        "balance": balance,
        "windows": [],
        "today": today_info,
        "local": local,
    }


def _read_deepseek_key():
    """环境变量 DEEPSEEK_API_KEY（用户级已设，进程内直接取）。绝不打印。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key and key.strip():
        return key.strip()
    raise RuntimeError("未设置环境变量 DEEPSEEK_API_KEY")


def fetch_deepseek(proxy):
    """DeepSeek 官方余额：GET /user/balance。国内直连（写死不走代理）。"""
    # 国内直连源：忽略上层 proxy 设置（--proxy / --no-proxy 不生效），写死不走代理。
    key = _read_deepseek_key()
    headers = {
        "Authorization": "Bearer %s" % key,
        "Accept": "application/json",
        "User-Agent": "opencode-quota-hud/1.2",
    }
    payload = http_get_json(
        "https://api.deepseek.com/user/balance", headers, None
    )
    available = bool(payload.get("is_available"))
    currency = None
    balance = None
    infos = payload.get("balance_infos") or []
    if infos and isinstance(infos[0], dict):
        first = infos[0]
        currency = first.get("currency")
        try:
            balance = round(float(first.get("total_balance", 0) or 0), 2)
        except (TypeError, ValueError):
            balance = None
    return {
        "ok": True,
        "available": available,
        "currency": currency,
        "balance": balance,
        "windows": [],
    }


# ---------------------------------------------------------------- 聚合

def collect(proxy):
    providers = {}
    for name, fn in (
        ("opencode", fetch_opencode),
        ("codex", fetch_codex),
        ("cmdc", fetch_cmdc),
        ("bbapi", fetch_bbapi),
        ("deepseek", fetch_deepseek),
    ):
        try:
            providers[name] = fn(proxy)
        except urllib.error.HTTPError as e:
            providers[name] = {"ok": False, "error": "HTTP %s" % e.code}
        except urllib.error.URLError as e:
            providers[name] = {"ok": False, "error": "网络错误: %s" % _short(e.reason)}
        except Exception as e:  # noqa: BLE001 —— 单源失败不得中断整体
            providers[name] = {"ok": False, "error": _short(str(e)) or type(e).__name__}
    return providers


def _short(text, limit=160):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def overall_ok(providers):
    """五家数据源至少一个成功。"""
    return any(
        providers.get(n, {}).get("ok")
        for n in ("opencode", "codex", "cmdc", "bbapi", "deepseek")
    )


# ---------------------------------------------------------------- 渲染

def render_text(providers, use_color=True):
    lines = []
    now = _local_now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append("AI 套餐额度面板  ·  %s" % now)
    lines.append("=" * 46)

    # opencode
    lines.append("")
    lines.append("opencode-go")
    oc = providers.get("opencode", {})
    if oc.get("ok"):
        for w in oc["windows"]:
            bar = colorize_bar(make_progress_bar(w["percent"]), w["percent"], use_color)
            lines.append("  %-9s %s  重置于 %s" % (w["label"], bar, w["reset_in"]))
    else:
        lines.append("  " + _err_line(oc, use_color))

    # codex
    lines.append("")
    lines.append("codex (ChatGPT Plus)")
    cx = providers.get("codex", {})
    if cx.get("ok"):
        lines.append("  套餐: %s" % cx.get("plan_type", "unknown"))
        for w in cx["windows"]:
            bar = colorize_bar(make_progress_bar(w["percent"]), w["percent"], use_color)
            lines.append("  %-9s %s  重置于 %s" % (w["label"], bar, w["reset_in"]))
    else:
        lines.append("  " + _err_line(cx, use_color))

    # cmdc（已用/上限口径，与 opencode 的剩余百分比相反）
    lines.append("")
    lines.append("cmdc (Command Code GOAT)")
    cm = providers.get("cmdc", {})
    if cm.get("ok"):
        lines.append("  月度余额 %s credits" % _fmt2(cm.get("balance")))
        for w in cm["windows"]:
            bar = colorize_bar(make_progress_bar(w["percent"]), w["percent"], use_color)
            lines.append("  %-9s %s  已用 %s/%s  重置于 %s" % (
                w["label"], bar, _fmt2(w["used"]), _fmt2(w["cap"]), w["reset_in"],
            ))
        if cm.get("total_count") is not None:
            lines.append("  累计 %s credits / %s 次请求" % (
                _fmt2(cm.get("total_cost")), cm.get("total_count"),
            ))
    else:
        lines.append("  " + _err_line(cm, use_color))

    # claude（bb-api 中转站：余额 + 今日用量；本地 JSONL 统计作补充行）
    lines.append("")
    lines.append("claude (bb-api 中转站)")
    cl = providers.get("bbapi", {})
    if cl.get("ok"):
        lines.append("  余额 $%s" % _fmt2(cl.get("balance")))
        t = cl.get("today")
        if t:
            suffix = "" if t.get("exact") else "（非今日，最近一天）"
            lines.append("  %s%s 请求 %d 次 · 实际 $%s · tokens %s" % (
                t["date"], suffix, t["requests"], _fmt2(t["actual_cost"]),
                f"{t['total_tokens']:,}",
            ))
        else:
            lines.append("  今日无中转记录")
    else:
        lines.append("  " + _err_line(cl, use_color))
    local = cl.get("local")
    if local:
        parts = local.get("token_parts", {})
        lines.append("  本地统计补充：今日消息 %d 条 · 总 token %s" % (
            local["messages"], f"{local['tokens']:,}",
        ))
        lines.append(
            "    in %s / out %s / cache_read %s / cache_creation %s"
            % (
                f"{parts.get('input', 0):,}",
                f"{parts.get('output', 0):,}",
                f"{parts.get('cache_read', 0):,}",
                f"{parts.get('cache_creation', 0):,}",
            )
        )

    # deepseek（官方余额）
    lines.append("")
    lines.append("deepseek (官方)")
    ds = providers.get("deepseek", {})
    if ds.get("ok"):
        cur = ds.get("currency") or ""
        sym = "¥" if cur == "CNY" else ("$" if cur == "USD" else "")
        if ds.get("available"):
            lines.append("  余额 %s%s（%s）· 可用" % (
                sym, _fmt2(ds.get("balance")), cur,
            ))
        else:
            text = "  余额 %s%s（%s）· 不可用（is_available=false）" % (
                sym, _fmt2(ds.get("balance")), cur,
            )
            lines.append((COLOR_RED + text + COLOR_RESET) if use_color else text)
    else:
        lines.append("  " + _err_line(ds, use_color))

    lines.append("")
    lines.append("=" * 46)
    lines.append("提示: --watch 循环刷新  ·  --json 机器可读  ·  --tray 托盘")
    return "\n".join(lines)


def _fmt2(value):
    """数值保留两位小数；非法值返回 '0.00'。"""
    try:
        return "%.2f" % float(value)
    except (TypeError, ValueError):
        return "0.00"


def _err_line(provider, use_color):
    msg = provider.get("error", "未知错误")
    text = "不可用: %s" % msg
    return (COLOR_RED + text + COLOR_RESET) if use_color else text


def render_json(providers):
    return json.dumps(
        {
            "generated_at": _local_now().isoformat(),
            "providers": providers,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------- 托盘

def _summary_line(providers):
    parts = []
    oc = providers.get("opencode", {})
    if oc.get("ok"):
        win = {w["id"]: w for w in oc["windows"]}
        parts.append("OC 滚动%d%% 周%d%% 月%d%%" % (
            win.get("rolling", {}).get("percent", 0),
            win.get("weekly", {}).get("percent", 0),
            win.get("monthly", {}).get("percent", 0),
        ))
    cx = providers.get("codex", {})
    if cx.get("ok"):
        win = {w["id"]: w for w in cx["windows"]}
        parts.append("Codex 5h%d%% 周%d%%" % (
            win.get("primary_window", {}).get("percent", 0),
            win.get("secondary_window", {}).get("percent", 0),
        ))
    cm = providers.get("cmdc", {})
    if cm.get("ok"):
        parts.append("cmdc 余额%s" % _fmt2(cm.get("balance")))
    cl = providers.get("bbapi", {})
    if cl.get("ok"):
        parts.append("Claude $%s" % _fmt2(cl.get("balance")))
    ds = providers.get("deepseek", {})
    if ds.get("ok"):
        parts.append("DS %s%s" % (
            "¥" if (ds.get("currency") == "CNY") else "",
            _fmt2(ds.get("balance")),
        ))
    return " | ".join(parts) if parts else "额度面板：全部数据源不可用"


def run_tray(proxy):
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        print("未安装 pystray / Pillow，无法启用托盘。")
        print("安装: pip install pystray pillow")
        print("（--tray 需要可选依赖；其余模式零依赖）")
        return 2

    providers = collect(proxy)

    def make_image():
        img = Image.new("RGBA", (64, 64), (30, 30, 30, 255))
        d = ImageDraw.Draw(img)
        s = _summary_line(providers)
        n = min(3, max(1, s.count("%") // 2))
        d.rectangle([6, 40, 58, 54], outline=(200, 200, 200, 255))
        d.rectangle([6, 40, 6 + int(52 * 0.6), 54], fill=(90, 200, 90, 255))
        d.text((6, 20), "QUOTA", fill=(230, 230, 230, 255))
        return img

    icon = pystray.Icon("quota_hud", make_image(), _summary_line(providers))

    def _quit(icon_, _item):
        icon_.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(_summary_line(providers), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("opencode-go", None, enabled=False),
        pystray.MenuItem("codex", None, enabled=False),
        pystray.MenuItem("cmdc (Command Code GOAT)", None, enabled=False),
        pystray.MenuItem("claude (bb-api 中转站)", None, enabled=False),
        pystray.MenuItem("deepseek (官方)", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )
    print("托盘已启动，右键图标查看各 provider，退出请选“退出”。")
    icon.run()
    return 0


# ---------------------------------------------------------------- 主流程

def resolve_proxy(args):
    if args.no_proxy:
        return None
    if args.proxy:
        return args.proxy
    env = os.environ.get("QUOTA_HUD_PROXY")
    if env:
        return env
    return DEFAULT_PROXY


def should_color(args):
    if args.no_color:
        return False
    return sys.stdout.isatty()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Windows AI 套餐额度聚合面板（零第三方依赖）"
    )
    parser.add_argument("--once", action="store_true", help="打印一次表格退出（默认）")
    parser.add_argument(
        "--watch", nargs="?", const=300, type=int, metavar="秒",
        help="循环刷新，默认 300 秒",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--tray", action="store_true", help="托盘图标（需 pystray+PIL）")
    parser.add_argument("--proxy", metavar="URL", help="覆盖代理，如 http://127.0.0.1:7897")
    parser.add_argument("--no-proxy", action="store_true", help="关闭代理")
    parser.add_argument("--no-color", action="store_true", help="禁用颜色")
    args = parser.parse_args(argv)

    _activate_vt()
    proxy = resolve_proxy(args)

    if args.tray:
        return run_tray(proxy)

    use_color = should_color(args)

    if args.watch is not None:
        interval = max(5, args.watch)
        try:
            while True:
                providers = collect(proxy)
                if sys.stdout.isatty():
                    os.system("cls" if os.name == "nt" else "clear")
                print(render_text(providers, use_color), flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0

    providers = collect(proxy)

    if args.json:
        print(render_json(providers))
    else:
        print(render_text(providers, use_color))

    return 0 if overall_ok(providers) else 1


if __name__ == "__main__":
    sys.exit(main())
