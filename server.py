#!/usr/bin/env python3
"""创新意电竞馆 财务报表 Web 服务"""

import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from zoho_fetcher import fetch_sheet, normalize_period

PORT = 8000
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
MONTHS_FILE = os.path.join(WORKSPACE, "months.json")
PIN = os.environ.get("APP_PIN", "3")
PIN_RATE_LIMIT = {}  # {ip: [timestamp, ...]}

_valid_months = {}
_data_cache = {}       # {period: {"data": {...}, "ts": float}}
_cache_lock = threading.Lock()
_months_lock = threading.RLock()
_file_lock = threading.Lock()  # protects months.json reads/writes
_refresh_interval = 30     # seconds — current month cadence
_historical_period = 10   # refresh historical every N cycles (10*30s=5min)
_available_cache = None
_priming = False
server_instance = None    # for graceful shutdown
def _prime_cache_async():
    """Pre-load cache in background after server starts."""
    global _priming
    _priming = True
    time.sleep(1)
    try:
        _prime_cache()
    finally:
        _priming = False

def _wait_for_prime(timeout=10):
    """Block until priming completes or timeout."""
    start = time.time()
    while _priming and time.time() - start < timeout:
        time.sleep(0.1)


def load_months():
    with _file_lock:
        if os.path.exists(MONTHS_FILE):
            with open(MONTHS_FILE) as f:
                return json.load(f)
        return {}


def save_months(data):
    with _file_lock:
        tmp = MONTHS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MONTHS_FILE)


def build_available(urls):
    global _available_cache
    with _months_lock:
        if _available_cache is not None:
            return _available_cache
        av = {}
        for k in urls:
            parts = k.split("-")
            if len(parts) == 2:
                av.setdefault(parts[0], []).append(parts[1])
        _available_cache = av
        return av


def _invalidate_available():
    global _available_cache
    with _months_lock:
        _available_cache = None


def default_month(urls):
    cur = datetime.now().strftime("%Y-%m")
    if cur in urls:
        return cur
    lst = sorted(urls.keys(), reverse=True)
    return lst[0] if lst else None


def _is_zero(val):
    try:
        return float(val) == 0
    except (ValueError, TypeError):
        return True


def validate_month(url, timeout=10):
    try:
        data = fetch_sheet(url, timeout=timeout)
        rows = data.get("daily", [])
        period_raw = data.get("period", "")
        period = normalize_period(period_raw)
        if not period or "-" not in period or len(rows) == 0:
            return None
        summary = data.get("summary", {})
        key_fields = ["网费", "售货", "美团", "现金结余"]
        if all(_is_zero(summary.get(f, "")) for f in key_fields):
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
    with _months_lock:
        _valid_months = valid
        _invalidate_available()
    if not valid:
        print("WARNING: no valid months!")


def _prime_cache():
    """Pre-load all valid months into cache on startup."""
    global _data_cache
    with _months_lock:
        items = list(_valid_months.items())
    for period, url in items:
        try:
            data = fetch_sheet(url, timeout=15)
            data["period"] = normalize_period(data.get("period", ""))
            h = _data_hash(data)
            with _cache_lock:
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
        try:
            cycle += 1
            cur = datetime.now().strftime("%Y-%m")
            if cur != current_month:
                current_month = cur
                cycle = 0  # force full refresh on month rollover

            with _months_lock:
                snapshot = list(_valid_months.items())
            for period, url in snapshot:
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
                except Exception as e:
                    print(f"  refresh fail {period}: {e}", flush=True)
        except Exception as e:
            print(f"  background refresh error: {e}", flush=True)
            time.sleep(60)


def _data_hash(data):
    """Return a hash of the actual payload (summary + daily) for change detection."""
    s = json.dumps({
        "summary": data.get("summary", {}),
        "daily": data.get("daily", []),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode()).hexdigest()


def _build_trends():
    """Build trends dict for all cached months."""
    trends = {}
    with _months_lock:
        keys = sorted(_valid_months.keys())
    with _cache_lock:
        for p in keys:
            c = _data_cache.get(p)
            if c:
                trends[p] = c["data"].get("summary", {})
    return trends


_rate_lock = threading.Lock()

def _check_rate_limit(ip, window=60, max_attempts=10):
    """Simple sliding-window rate limiter. Returns True if allowed."""
    now = time.time()
    with _rate_lock:
        attempts = PIN_RATE_LIMIT.get(ip, [])
        attempts = [t for t in attempts if now - t < window]
        PIN_RATE_LIMIT[ip] = attempts
        if len(attempts) >= max_attempts:
            return False
        attempts.append(now)
        return True


_git_hash = ""
try:
    _git_hash = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=WORKSPACE, stderr=subprocess.DEVNULL
    ).decode().strip()
except Exception:
    pass  # git not available, skip version hash


def serve_static(path, handler):
    if path in ("/", ""):
        path = "/index.html"
    filepath = os.path.realpath(os.path.join(WORKSPACE, path.lstrip("/")))
    if not filepath.startswith(os.path.realpath(WORKSPACE)):
        handler.send_response(403)
        handler.end_headers()
        handler.wfile.write(b"Forbidden")
        return
    if os.path.isfile(filepath):
        with open(filepath, "rb") as f:
            content = f.read()
        handler.send_response(200)
        ext = os.path.splitext(filepath)[1]
        ct = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        handler.send_header("Content-Type", ct)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self' https://sheet.zohopublic.com.cn https://*.zoho.com https://*.zoho.com.cn; "
        )
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
    handler.send_header("Access-Control-Allow-Origin", handler.headers.get("Origin", "*"))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/verify-pin":
            client_ip = self.client_address[0]
            if not _check_rate_limit(client_ip):
                json_response(self, {"error": "请求过于频繁，请稍后再试"}, 429)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
                pin = str(data.get("pin", ""))
            except Exception:
                pin = ""
            if pin == PIN:
                json_response(self, {"ok": True})
            else:
                json_response(self, {"error": "PIN error"}, 403)
            return
        if path == "/api/log":
            try:
                n = int(self.headers.get("Content-Length", 0))
                if 0 < n < 65536:
                    body = json.loads(self.rfile.read(n))
                    ts = body.get("ts", "")
                    msg = body.get("msg", "")
                    ua = self.headers.get("User-Agent", "")[:80]
                    print(f"[FE:{ts}] {msg}  UA={ua}", flush=True)
            except Exception:
                pass
            json_response(self, {"ok": True})
            return
        serve_static(self.path, self)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        avail = build_available(_valid_months)

        if path == "/api/months":
            # Lazy re-check one skipped month per call
            stored = load_months()
            to_validate = None
            with _months_lock:
                for k, v in stored.items():
                    if k not in _valid_months:
                        to_validate = (k, v)
                        break
            if to_validate:
                k, v = to_validate
                result = validate_month(v)
                if result:
                    with _months_lock:
                        _valid_months[k] = v
                        _invalidate_available()
            avail = build_available(_valid_months)
            with _months_lock:
                month_list = sorted(_valid_months.keys(), reverse=True)
            json_response(self, {"available": avail, "list": month_list})
            return

        if path == "/api/data":
            period = qs.get("period", [None])[0]
            targets = _valid_months
            url = targets.get(period)
            if not url:
                stored = load_months()
                url = stored.get(period)
                if url:
                    result = validate_month(url)
                    if result:
                        with _months_lock:
                            _valid_months[period] = url
                            _invalidate_available()
                    else:
                        url = None
            if not url:
                url = targets.get(default_month(targets)) if targets else None
            if not url:
                json_response(self, {"error": "无可用数据"}, 404)
                return

            actual_period = period
            if not actual_period:
                with _months_lock:
                    for k, v in list(targets.items()):
                        if v == url:
                            actual_period = k
                            break

            # Wait for priming if cache is empty (max 15s)
            if _priming and actual_period and actual_period not in _data_cache:
                _wait_for_prime(timeout=15)

            # Serve from cache if available
            avail = build_available(_valid_months)
            with _cache_lock:
                cached = _data_cache.get(actual_period) if actual_period else None
            if cached:
                data = json.loads(json.dumps(cached["data"]))
                data["_cache_ts"] = cached["ts"]
                data["_data_hash"] = cached.get("hash", "")
                data["_cached"] = True
                data["_priming"] = _priming
                data["available"] = avail
                data["trends"] = _build_trends()
                json_response(self, data)
                return

            # Still priming and no cache yet — return priming status
            if _priming:
                json_response(self, {"priming": True, "period": actual_period}, 202)
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
            try:
                data = fetch_sheet(url_to_add)
            except Exception as e:
                json_response(self, {"error": f"链接请求失败: {e}"}, 502)
                return
            period = normalize_period(data.get("period", ""))
            if not period or "-" not in period or len(data.get("daily", [])) == 0:
                json_response(self, {"error": "链接无有效数据或无法识别月份"}, 400)
                return
            stored = load_months()
            stored[period] = url_to_add
            save_months(stored)
            with _months_lock:
                _valid_months[period] = url_to_add
                _invalidate_available()
            h = _data_hash(data)
            with _cache_lock:
                _data_cache[period] = {"data": data, "ts": time.time(), "hash": h}
            avail = build_available(_valid_months)
            with _months_lock:
                sorted_keys = sorted(_valid_months.keys(), reverse=True)
            json_response(self, {"ok": True, "period": period, "available": avail, "list": sorted_keys})
            return

        if path == "/api/trends":
            with _months_lock:
                trend_keys = sorted(_valid_months.keys())
            with _cache_lock:
                trends = {}
                for p in trend_keys:
                    c = _data_cache.get(p)
                    if c:
                        trends[p] = c["data"].get("summary", {})
            json_response(self, {"trends": trends})
            return

        if path == "/api/log":
            try:
                n = int(self.headers.get("Content-Length", 0))
                if 0 < n < 65536:
                    body = json.loads(self.rfile.read(n))
                    ts = body.get("ts", "")
                    msg = body.get("msg", "")
                    ua = self.headers.get("User-Agent", "")[:80]
                    print(f"[FE:{ts}] {msg}  UA={ua}", flush=True)
            except Exception:
                pass
            json_response(self, {"ok": True})
            return

        if path == "/ping":
            is_priming = _priming
            with _cache_lock:
                ts_map = {p: c["ts"] for p, c in _data_cache.items()}
            json_response(self, {"ts": time.time(), "cached": list(_data_cache.keys()), "timestamps": ts_map, "priming": is_priming})
            return

        serve_static(self.path, self)

    def log_message(self, format, *args):
        pass


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with graceful shutdown support."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = True

    def shutdown(self):
        self.running = False
        super().shutdown()

    def serve_forever(self, poll_interval=0.5):
        self.running = True
        super().serve_forever(poll_interval)


def _signal_handler(signum, frame):
    print(f"\nReceived signal {signum}, shutting down...", flush=True)
    if server_instance:
        server_instance.shutdown()
    sys.exit(0)


def main():
    global server_instance, _priming
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    init_valid_months()
    t = threading.Thread(target=_background_refresh, daemon=True)
    t.start()
    server_instance = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Server running on http://0.0.0.0:{PORT}")
    # Prime cache in background after server starts
    prime_thread = threading.Thread(target=_prime_cache_async, daemon=True)
    prime_thread.start()
    server_instance.serve_forever()


if __name__ == "__main__":
    main()
