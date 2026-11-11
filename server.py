#!/usr/bin/env python3
"""Claude Code / Codex トークン消費モニター（依存ゼロ / Python 3.8+ 標準ライブラリのみ）

  Claude Code: ~/.claude/projects/**/*.jsonl
  Codex      : ~/.codex/sessions/**/*.jsonl
の会話ログを差分読みして集計し、ブラウザ向けに JSON と静的ページを返す。
外部への通信は一切しない。

使い方: python3 server.py [--host 127.0.0.1] [--port 18931] [--open]
"""
import argparse
import calendar
import json
import os
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
ROOT = os.path.join(CONFIG_DIR, "projects")
CODEX_ROOT = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
# Claude Code はパスの英数字以外を "-" に置き換えてフォルダ名にする（例: /Users/foo.bar → -Users-foo-bar）
HOME_PREFIX = re.sub(r"[^A-Za-z0-9]", "-", HOME) + "-"
SCAN_DAYS = 8
REFRESH_SEC = 1.0

lock = threading.Lock()
offsets = {}        # path -> 読み済みバイト位置
seen = {}           # (message.id, requestId) -> record（Claude のストリーミング重複の統合用）
codex_files = {}    # path -> {"proj", "model", "last_total"}（Codex のファイルごとの状態）
# record: [ts, model, project, input, output, cache_create, cache_read, source]
records = []
state = {"ready": False, "scanned_files": 0, "last_scan": 0.0, "version": 0, "codex_limit": None}
sorted_cache = {"version": -1, "recs": []}

# range -> (表示秒数, 刻み秒, 平滑化窓秒)。刻み×窓で「tokens/分」の移動平均を作る
RANGES = {
    "60s": (60, 1, 10),
    "10m": (600, 5, 30),
    "1h": (3600, 30, 120),
    "6h": (6 * 3600, 180, 600),
    "24h": (24 * 3600, 600, 1800),
    "7d": (7 * 86400, 3600, 7200),
}
METRICS = ("input", "output", "cache_create", "cache_read")
# 「合計」の積み上げ用: ツール別の 入力（入力+キャッシュ作成）/ 出力
SPLIT = ("claude_in", "claude_out", "codex_in", "codex_out")
ALLKEYS = METRICS + SPLIT
SOURCES = ("all", "claude", "codex")
ALLOWED_HOSTS = None   # None = Host ヘッダを検査しない（0.0.0.0 で待ち受ける場合）。main() で設定


def parse_ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def short(name, n=34):
    return name if len(name) <= n else name[: n - 1] + "…"


def project_name(path):
    """Claude のログのフォルダ名から、表示用のプロジェクト名を作る。"""
    top = os.path.relpath(path, ROOT).split(os.sep)[0]
    if "scratch" in top:
        return "(scratch)"
    if top.startswith("-private-var-folders"):
        return "(tmp)"
    if top + "-" == HOME_PREFIX:
        return "(home)"
    if top.startswith(HOME_PREFIX):
        top = top[len(HOME_PREFIX):]
    if "--claude-worktrees-" in top:
        base, wt = top.split("--claude-worktrees-", 1)
        top = base + " ⎇ " + wt.rsplit("-", 1)[0]
    return short(top) or "(home)"


def codex_project(cwd):
    """Codex が記録した作業ディレクトリから、表示用のプロジェクト名を作る。"""
    norm = os.path.normpath(cwd)
    if norm == os.path.normpath(HOME):
        return "(home)"
    if "scratch" in norm:
        return "(scratch)"
    return short(os.path.basename(norm) or norm)


# ---------------------------------------------------------------- Claude

def ingest_line(line, project):
    if b'"output_tokens"' not in line or b'"assistant"' not in line:
        return
    try:
        d = json.loads(line)
    except Exception:
        return
    msg = d.get("message")
    if not isinstance(msg, dict):
        return
    u = msg.get("usage")
    model = msg.get("model") or "unknown"
    if not isinstance(u, dict) or model == "<synthetic>":
        return
    ts = parse_ts(d.get("timestamp") or "")
    if ts is None:
        return
    i = u.get("input_tokens") or 0
    o = u.get("output_tokens") or 0
    cc = u.get("cache_creation_input_tokens") or 0
    cr = u.get("cache_read_input_tokens") or 0
    if not (i or o or cc or cr):
        return
    mid, rid = msg.get("id"), d.get("requestId")
    key = (mid, rid) if (mid or rid) else None
    if key is not None and key in seen:
        r = seen[key]          # 同じ応答の途中経過 → 大きい方を採用
        if o > r[4]:
            r[4] = o
            state["version"] += 1
        return
    r = [ts, model, project, i, o, cc, cr, "claude"]
    records.append(r)
    state["version"] += 1
    if key is not None:
        seen[key] = r


def claude_lines(path, lines):
    proj = project_name(path)
    for line in lines:
        ingest_line(line, proj)


# ---------------------------------------------------------------- Codex

def codex_lines(path, lines):
    st = codex_files.setdefault(path, {"proj": "(codex)", "model": "codex", "last_total": -1})
    for line in lines:
        if b'"token_count"' not in line and b'"turn_context"' not in line and b'"session_meta"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        t = d.get("type")
        if t in ("turn_context", "session_meta"):
            if p.get("cwd"):
                st["proj"] = codex_project(p["cwd"])
            if p.get("model"):
                st["model"] = p["model"]
            continue
        if t != "event_msg" or p.get("type") != "token_count":
            continue
        ts = parse_ts(d.get("timestamp") or "")
        if ts is None:
            continue
        # 利用上限（最新のスナップショットを保持）
        rl = p.get("rate_limits")
        prim = rl.get("primary") if isinstance(rl, dict) else None
        if isinstance(prim, dict) and prim.get("used_percent") is not None:
            cur = state["codex_limit"]
            if cur is None or ts >= cur["ts"]:
                state["codex_limit"] = {"ts": ts, "used": prim.get("used_percent"),
                                        "window_min": prim.get("window_minutes"),
                                        "resets_at": prim.get("resets_at")}
        # トークン数（累計が増えたときだけ、そのリクエスト分を加算）
        info = p.get("info")
        if not isinstance(info, dict):
            continue
        tot = (info.get("total_token_usage") or {}).get("total_tokens")
        last = info.get("last_token_usage") or {}
        if tot is None or tot <= st["last_total"]:
            continue
        st["last_total"] = tot
        inp = last.get("input_tokens") or 0           # Codex は入力にキャッシュ分を含む
        cached = last.get("cached_input_tokens") or 0
        out = last.get("output_tokens") or 0           # 推論トークンを含む
        cw = last.get("cache_write_input_tokens") or 0
        r = [ts, st["model"], st["proj"], max(0, inp - cached), out, cw, cached, "codex"]
        if r[3] or r[4] or r[5] or r[6]:
            records.append(r)
            state["version"] += 1


# ---------------------------------------------------------------- 走査

def scan_tree(root, handler, cutoff):
    n = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            n += 1
            off = offsets.get(path, 0)
            if st.st_size <= off:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(off)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue                     # 書きかけの行は次回
            lines = [ln for ln in data[: end + 1].split(b"\n") if ln]
            with lock:
                handler(path, lines)
                offsets[path] = off + end + 1
    return n


def scan_once():
    cutoff = time.time() - SCAN_DAYS * 86400
    n = scan_tree(ROOT, claude_lines, cutoff) + scan_tree(CODEX_ROOT, codex_lines, cutoff)
    with lock:
        if len(records) > 80000:
            lim = time.time() - SCAN_DAYS * 86400
            records[:] = [r for r in records if r[0] >= lim]
            state["version"] += 1
        state["scanned_files"] = n
        state["last_scan"] = time.time()
        state["ready"] = True


def scan_loop():
    while True:
        try:
            scan_once()
        except Exception as e:  # noqa
            print("scan error:", e, flush=True)
        time.sleep(REFRESH_SEC)


# ---------------------------------------------------------------- 集計

def billable(r):
    return r[3] + r[4] + r[5]


def get_sorted():
    with lock:
        if sorted_cache["version"] != state["version"]:
            sorted_cache["recs"] = sorted(records, key=lambda r: r[0])
            sorted_cache["version"] = state["version"]
        return (sorted_cache["recs"], state["scanned_files"], state["ready"],
                state["last_scan"], state["codex_limit"])


def build_blocks(recs):
    """先頭メッセージの時刻を UTC の正時に切り捨て、5時間で1ブロックとする（Claude 用）。"""
    blocks = []
    cur = None
    for r in recs:
        ts = r[0]
        if cur is None or ts >= cur["start"] + 5 * 3600 or ts - cur["last"] >= 5 * 3600:
            start = ts - (ts % 3600)
            cur = {"start": start, "last": ts, "tokens": 0, "cache_read": 0}
            blocks.append(cur)
        cur["last"] = ts
        cur["tokens"] += billable(r)
        cur["cache_read"] += r[6]
    return blocks


def rate_series(recs, now, span, step, win):
    """各時刻 t について、(t-win, t] の合計を 1 分あたりに換算した値を返す。"""
    k = max(1, win // step)
    last = int(now // step) * step
    n = span // step
    ts = [last - (n - 1 - i) * step for i in range(n)]
    pre = k - 1                                 # 窓の立ち上がり用に手前も集計
    first = ts[0] - pre * step
    size = n + pre
    sums = {m: [0] * size for m in ALLKEYS}
    for r in recs:
        if r[0] <= first - step:
            continue
        idx = (r[0] - first) // step
        if idx < 0 or idx >= size:
            continue
        sums["input"][idx] += r[3]
        sums["output"][idx] += r[4]
        sums["cache_create"][idx] += r[5]
        sums["cache_read"][idx] += r[6]
        sums[r[7] + "_in"][idx] += r[3] + r[5]
        sums[r[7] + "_out"][idx] += r[4]
    scale = 60.0 / (k * step)
    out = {}
    for m in ALLKEYS:
        s = sums[m]
        run = sum(s[:k])
        vals = []
        for i in range(n):
            j = i + pre                          # このバケットを窓の末尾にする
            if i > 0:
                run += s[j] - s[j - k]
            vals.append(round(run * scale, 1))
        out[m] = vals
    out["total"] = [round(a + b + c, 1) for a, b, c in zip(out["input"], out["output"], out["cache_create"])]
    return ts, out, k * step


def api_data(rng, source):
    rng = rng if rng in RANGES else "60s"
    source = source if source in SOURCES else "all"
    span, step, win = RANGES[rng]
    now = time.time()
    allrecs, scanned, ready, last_scan, codex_limit = get_sorted()
    recs = allrecs if source == "all" else [r for r in allrecs if r[7] == source]

    ts, series, win_sec = rate_series(recs, now, span, step, win)

    # 内訳（直近24時間）
    by_model, by_project = {}, {}
    cut24 = now - 86400
    for r in recs:
        if r[0] >= cut24:
            by_model[r[1]] = by_model.get(r[1], 0) + billable(r)
            by_project[r[2]] = by_project.get(r[2], 0) + billable(r)
    top_models = sorted(by_model.items(), key=lambda kv: -kv[1])[:6]
    tp = sorted(by_project.items(), key=lambda kv: -kv[1])
    other = sum(v for _, v in tp[8:])
    tp = tp[:8] + ([("その他", other)] if other else [])

    # 5時間ブロック（Claude のみ）
    block = None
    if source != "codex":
        blocks = build_blocks([r for r in allrecs if r[7] == "claude" and r[0] >= now - 24 * 3600])
        if blocks:
            b = blocks[-1]
            end = b["start"] + 5 * 3600
            if now < end and now - b["last"] < 5 * 3600:
                block = {"start": b["start"], "end": end, "tokens": b["tokens"], "cache_read": b["cache_read"]}

    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    today = [r for r in recs if r[0] >= midnight]
    today_by_source = {"claude": 0, "codex": 0}
    for r in allrecs:
        if r[0] >= midnight:
            today_by_source[r[7]] += billable(r)

    return {
        "now": now, "ready": ready, "scanned_files": scanned, "last_scan": last_scan,
        "range": rng, "source": source, "span": span, "step": step, "window": win_sec,
        "t": ts, "series": series,
        "models": top_models, "projects": tp,
        "block": block,
        "codex_limit": codex_limit if source != "claude" else None,
        "today": {"tokens": sum(billable(r) for r in today), "cache_read": sum(r[6] for r in today),
                  "messages": len(today), "by_source": today_by_source},
        "last_message": recs[-1][0] if recs else None,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # DNS リバインディング対策: 想定外の Host ヘッダは拒否する
        if ALLOWED_HOSTS is not None:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ALLOWED_HOSTS:
                self._send(403, b"forbidden host", "text/plain")
                return
        u = urlparse(self.path)
        if u.path == "/api/data":
            q = parse_qs(u.query)
            rng = (q.get("range") or ["60s"])[0]
            src = (q.get("source") or ["all"])[0]
            self._send(200, json.dumps(api_data(rng, src)).encode(), "application/json")
        elif u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description="Claude Code / Codex トークン消費モニター")
    ap.add_argument("--host", default=os.environ.get("BIND_HOST", "127.0.0.1"),
                    help="待ち受けアドレス（既定: 127.0.0.1 = 自分の Mac からのみ）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "18931")))
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    a = ap.parse_args()
    if not os.path.isdir(ROOT) and not os.path.isdir(CODEX_ROOT):
        print(f"ログのフォルダが見つかりません: {ROOT} / {CODEX_ROOT}\n"
              "Claude Code か Codex を一度でも使うと作られます。", flush=True)
    global ALLOWED_HOSTS
    if a.host not in ("0.0.0.0", "::"):
        extra = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
        ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", a.host, *extra}
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        raise SystemExit(f"{a.host}:{a.port} を開けませんでした（{e}）。すでに起動済みか、--port で別のポートを指定してください。")
    threading.Thread(target=scan_loop, daemon=True).start()
    url = f"http://{'localhost' if a.host == '127.0.0.1' else a.host}:{a.port}"
    print(f"token-monitor: {url}  (Ctrl+C で終了)", flush=True)
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
