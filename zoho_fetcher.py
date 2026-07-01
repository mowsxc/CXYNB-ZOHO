#!/usr/bin/env python3
"""Zoho Sheet 通用解析器。

独立模块，可被任何项目导入使用。
入口: fetch_sheet(url) → {"period": "2026-06", "summary": {...}, "headers": [...], "daily": [...]}
"""

import re
import urllib.request
import urllib.error


def fetch_sheet(url, timeout=30):
    """从 Zoho 发布的 HTML 表格抓取数据，返回结构化 dict。

    参数:
        url: Zoho Sheet publishedrange 链接 (去掉 ?type=grid 等参数)
        timeout: 请求超时秒数

    返回:
        {
            "period": "2026/6",           # 原始月份标识
            "summary": {"现金结余": "74679.48", ...},
            "headers": ["日期", "班次", ...],
            "daily": [{"日期": "30", "班次": "白班", ...}, ...]
        }
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    rows = _parse_grid(html)
    return _build_data(rows)


def _parse_grid(html):
    """按行解析 HTML 表格，通过 left 位置对齐空单元格，返回每行完整列数据。"""
    blocks = re.split(r"<div class='row' style='top:\d+px;", html)[1:]
    rows = []
    column_positions = None  # list of left pixel values from header row

    for block in blocks:
        col_divs = re.findall(
            r"<div style='[^']*left:(\d+)px[^']*'[^>]*>"
            r"(?:<div class\s*=\s*'w100'>([^<]*)</div>)?"
            r"</div>",
            block,
        )
        if not col_divs:
            continue

        if column_positions is None:
            # First row with content: track position sequence from period row
            column_positions = [int(p) for p, _ in col_divs]

        # Build position→text map for this row
        pos_map = {int(p): (v or "").strip() for p, v in col_divs}

        texts = [pos_map.get(pos, "") for pos in column_positions]
        # Keep internal empty cells; only add row if it has at least one non-empty cell
        if any(t != "" for t in texts):
            rows.append(texts)

    return rows


def _build_data(rows):
    """将行列表构建为结构化数据。"""
    if len(rows) < 4:
        return {"error": "表格行数不足"}

    period = rows[0][0] if rows[0] else ""
    if not re.match(r"^\d{4}/\d+$", period):
        return {"error": f"无法识别月份标识: {period}"}

    summary = {}
    if len(rows) >= 3:
        labels = rows[1]
        values = rows[2]
        for j in range(min(len(labels), len(values))):
            label = labels[j]
            # Normalize variant labels to consistent key
            if label in ("退货", "入账", "退款金额"):
                label = "退款"
            summary[label] = values[j]

    headers = [("退款" if h in ("退货", "入账", "退款金额") else h) for h in rows[3]]

    daily = []
    for row in rows[4:]:
        entry = {}
        for j, h in enumerate(headers):
            entry[h] = row[j] if j < len(row) else ""
        daily.append(entry)

    return {
        "period": period,
        "summary": summary,
        "headers": headers,
        "daily": daily,
    }


def normalize_period(raw):
    """将 "2026/6" 或 "2026/12" 归一化为 "2026-06" 格式。"""
    m = re.match(r"^(\d{4})/(\d+)$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return raw


if __name__ == "__main__":
    import sys
    import json
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        print("Usage: python zoho_fetcher.py <url>")
        sys.exit(1)
    data = fetch_sheet(url)
    print(json.dumps(data, ensure_ascii=False, indent=2))
