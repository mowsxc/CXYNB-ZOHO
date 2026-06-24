#!/usr/bin/env python3
"""创新意电竞馆 财务报表 Web 服务"""

import http.server
import json
import os
import urllib.error
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from zoho_fetcher import fetch_sheet, normalize_period

PORT = 8000
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
MONTHS_FILE = os.path.join(WORKSPACE, "months.json")


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
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        urls = load_months()
        avail = build_available(urls)

        if path == "/api/months":
            json_response(self, {"available": avail, "list": sorted(urls.keys(), reverse=True)})
            return

        if path == "/api/data":
            period = qs.get("period", [None])[0]
            url = urls.get(period) or urls.get(default_month(urls)) if urls else None
            if not url:
                json_response(self, {"error": "无可用数据"}, 404)
                return
            try:
                data = fetch_sheet(url)
                raw = data.get("period", "")
                data["period"] = normalize_period(raw)
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
                period = normalize_period(data.get("period", ""))
                if not period or "-" not in period:
                    json_response(self, {"error": f"无法识别月份: {data.get('period')}"}, 400)
                    return
                urls[period] = url_to_add
                save_months(urls)
                avail = build_available(urls)
                json_response(self, {"ok": True, "period": period, "available": avail, "list": sorted(urls.keys(), reverse=True)})
            except urllib.error.URLError as e:
                json_response(self, {"error": f"抓取失败: {e}"}, 502)
            except Exception as e:
                json_response(self, {"error": str(e)}, 500)
            return

        serve_static(self.path, self)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Server running on http://0.0.0.0:{PORT}")
    server.serve_forever()
