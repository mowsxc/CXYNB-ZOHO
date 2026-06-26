#!/usr/bin/env python3
"""创新意电竞馆 财务报表 Web 服务"""

import http.server
import json
import os
import threading
import time
import urllib.error
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from zoho_fetcher import fetch_sheet, normalize_period

PORT = 8000
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
MONTHS_FILE = os.path.join(WORKSPACE, "months.json")

_valid_months = {}
_data_cache = {}       # {period: {"data": {...}, "ts": float}}
_cache_lock = threading.Lock()
_refresh_interval = 20     # seconds — current month cadence
_historical_period = 15   # refresh historical every N cycles (15*20s=5min)


def load_months():
    if os.path.exists(MONTHS_FILE):
        with open(MONTHS_FILE) as f:
            return json.load(f)
    return {}


def save_months(data):
    with open(MONTHS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_available(urls):
    av = {}
    for k in urls:
        parts = k.split("-")
        if len(parts) == 2:
            av.setdefault(parts[0], []).append(parts[1])
    return av


def default_month(urls):
    cur = datetime.now().strftime("%Y-%m")
    if cur in urls:
        return cur
    lst = sorted(urls.keys(), reverse=True)
    return lst[0] if lst else None


def validate_month(url, timeout=10):
    try:
        data = fetch_sheet(url, timeout=timeout)
        rows = data.get("daily", [])
        period_raw = data.get("period", "")
        period = normalize_period(period_raw)
        if not period or "-" not in period or len(rows) == 0:
            return None
        summary = data.get("summary", {})
        def is_zero(val):
            try:
                return float(val) == 0
            except (ValueError, TypeError):
                return True
        key_fields = ["网费", "售货", "美团", "现金结余"]
        if all(is_zero(summary.get(f, "")) for f in key_fields):
            return None
        return period
    except Exception:
        return None


def init_valid_months():
    global _valid_months
    stored = load_months()
    valid = {}
    print("Validating months...")
    for key, url in stored.items():
        result = validate_month(url)
        if result:
            valid[key] = url
            print(f"  {key} OK")
        else:
            print(f"  {key} SKIP (no data)")
    _valid_months = valid
    if not valid:
        print("WARNING: no valid months!")
    _prime_cache()


def _prime_cache():
    """Pre-load all valid months into cache on startup."""
    global _data_cache
    for period, url in _valid_months.items():
        try:
            data = fetch_sheet(url, timeout=15)
            data["period"] = normalize_period(data.get("period", ""))
            h = _data_hash(data)
            _data_cache[period] = {"data": data, "ts": time.time(), "hash": h}
            print(f"  cached {period}")
        except Exception as e:
            print(f"  cache fail {period}: {e}")


def _background_refresh():
    """Two-tier refresh: current month every cycle, historical every N cycles."""
    cycle = 0
    current_month = datetime.now().strftime("%Y-%m")
    while True:
        time.sleep(_refresh_interval)
        cycle += 1
        cur = datetime.now().strftime("%Y-%m")
        if cur != current_month:
            current_month = cur
            cycle = 0  # force full refresh on month rollover

        for period, url in list(_valid_months.items()):
            is_current = (period == current_month)
            if not is_current and cycle % _historical_period != 0:
                continue
            try:
                data = fetch_sheet(url, timeout=15)
                data["period"] = normalize_period(data.get("period", ""))
                h = _data_hash(data)
                with _cache_lock:
                    old = _data_cache.get(period)
                    if not old or _data_hash(old["data"]) != h:
                        _data_cache[period] = {"data": data, "ts": time.time(), "hash": h}
            except Exception:
                pass


def _data_hash(data):
    """Return a hash of the actual payload (summary + daily) for change detection."""
    import hashlib
    s = json.dumps({
        "summary": data.get("summary", {}),
        "daily": data.get("daily", []),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode()).hexdigest()


def _build_trends():
    """Build trends dict for all cached months."""
    trends = {}
    with _cache_lock:
        for p in sorted(_valid_months.keys()):
            c = _data_cache.get(p)
            if c:
                trends[p] = c["data"].get("summary", {})
    return trends


def serve_static(path, handler):
    if path in ("/", ""):
        path = "/index.html"
    filepath = os.path.join(WORKSPACE, path.lstrip("/"))
    if os.path.isfile(filepath):
        with open(filepath, "rb") as f:
            content = f.read()
        handler.send_response(200)
        ext = os.path.splitext(filepath)[1]
        ct = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        handler.send_header("Content-Type", ct)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(content)
    else:
        handler.send_response(404)
        handler.end_headers()
        handler.wfile.write(b"Not Found")


def json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _valid_months
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        avail = build_available(_valid_months)

        if path == "/api/months":
            # Lazy re-check one skipped month per call
            stored = load_months()
            for k, v in stored.items():
                if k not in _valid_months:
                    result = validate_month(v)
                    if result:
                        _valid_months[k] = v
                    break
            avail = build_available(_valid_months)
            json_response(self, {"available": avail, "list": sorted(_valid_months.keys(), reverse=True)})
            return

        if path == "/api/data":
            global _data_cache
            period = qs.get("period", [None])[0]
            targets = _valid_months
            url = targets.get(period)
            if not url:
                stored = load_months()
                url = stored.get(period)
                if url:
                    result = validate_month(url)
                    if result:
                        _valid_months[period] = url
                    else:
                        url = None
            if not url:
                url = targets.get(default_month(targets)) if targets else None
            if not url:
                json_response(self, {"error": "无可用数据"}, 404)
                return

            actual_period = period
            if not actual_period:
                for k, v in targets.items():
                    if v == url:
                        actual_period = k
                        break

            # Serve from cache if available
            avail = build_available(_valid_months)
            with _cache_lock:
                cached = _data_cache.get(actual_period) if actual_period else None
            if cached:
                data = dict(cached["data"])
                data["_cache_ts"] = cached["ts"]
                data["_data_hash"] = cached.get("hash", "")
                data["_cached"] = True
                data["available"] = avail
                data["trends"] = _build_trends()
                json_response(self, data)
                return

            # Cache miss — fetch synchronously
            try:
                data = fetch_sheet(url)
                data["period"] = normalize_period(data.get("period", ""))
                h = _data_hash(data)
                if actual_period:
                    with _cache_lock:
                        _data_cache[actual_period] = {"data": data, "ts": time.time(), "hash": h}
                data["_cache_ts"] = time.time()
                data["_data_hash"] = h
                data["_cached"] = False
                data["available"] = avail
                data["trends"] = _build_trends()
                json_response(self, data)
            except urllib.error.URLError as e:
                json_response(self, {"error": f"Zoho 请求失败: {e}"}, 502)
            except Exception as e:
                json_response(self, {"error": str(e)}, 500)
            return

        if path == "/api/add-month":
            url_to_add = qs.get("url", [None])[0]
            if not url_to_add:
                json_response(self, {"error": "缺少 url 参数"}, 400)
                return
            url_to_add = url_to_add.split("?")[0]
            data = fetch_sheet(url_to_add)
            period = normalize_period(data.get("period", ""))
            if not period or "-" not in period or len(data.get("daily", [])) == 0:
                json_response(self, {"error": "链接无有效数据或无法识别月份"}, 400)
                return
            stored = load_months()
            stored[period] = url_to_add
            save_months(stored)
            _valid_months[period] = url_to_add
            h = _data_hash(data)
            with _cache_lock:
                _data_cache[period] = {"data": data, "ts": time.time(), "hash": h}
            avail = build_available(_valid_months)
            json_response(self, {"ok": True, "period": period, "available": avail, "list": sorted(_valid_months.keys(), reverse=True)})
            return

        if path == "/api/trends":
            with _cache_lock:
                trends = {}
                for p in sorted(_valid_months.keys()):
                    c = _data_cache.get(p)
                    if c:
                        trends[p] = c["data"].get("summary", {})
            json_response(self, {"trends": trends})
            return

        if path == "/api/ping":
            with _cache_lock:
                ts_map = {p: c["ts"] for p, c in _data_cache.items()}
            json_response(self, {"ts": time.time(), "cached": list(_data_cache.keys()), "timestamps": ts_map})
            return

        serve_static(self.path, self)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    init_valid_months()
    t = threading.Thread(target=_background_refresh, daemon=True)
    t.start()
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Server running on http://0.0.0.0:{PORT}")
    server.serve_forever()
