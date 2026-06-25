#!/usr/bin/env python3
"""Zoho Sheet 通用解析器。

独立模块，可被任何项目导入使用。
入口: fetch_sheet(url) → {"period": "2026-06", "summary": {...}, "headers": [...], "daily": [...]}
"""

import re
import urllib.request
import urllib.error


def fetch_sheet(url, timeout=15):
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
    """按行解析 HTML 表格，返回每行的单元格文本列表。"""
    blocks = re.split(r"<div class='row' style='top:\d+px;", html)[1:]
    rows = []
    for block in blocks:
        cells = re.findall(r"<div class\s*=\s*'w100'>([^<]*)</div>", block)
        texts = [c.strip() for c in cells if c.strip()]
        if texts:
            rows.append(texts)
    return rows


def _build_data(rows):
    """将行列表构建为结构化数据。"""
    if len(rows) < 4:
        return {"error": "表格行数不足"}

    period = rows[0][0] if rows[0] else ""

    summary = {}
    if len(rows) >= 3:
        labels = rows[1]
        values = rows[2]
        for j in range(min(len(labels), len(values))):
            summary[labels[j]] = values[j]

    headers = rows[3]
    idx_expense = None
    for _j, _h in enumerate(headers):
        if _h == "支出":
            idx_expense = _j
            break

    daily = []
    for row in rows[4:]:
        entry = {}
        for j, h in enumerate(headers):
            val = row[j] if j < len(row) else ""
            if h in ("支出明细", "退款明细") and val and val != "0":
                expected = None
                if idx_expense is not None and idx_expense < len(row):
                    try:
                        expected = float(row[idx_expense])
                    except (ValueError, TypeError):
                        pass
                val = _split_items(val, expected)
            entry[h] = val
        daily.append(entry)

    return {
        "period": period,
        "summary": summary,
        "headers": headers,
        "daily": daily,
    }


def _split_items(text, expected=None):
    """拆分无分隔符拼接的支出项目, 并通过支出总额校验修正"""
    import re as _re
    pattern = _re.compile(r'([\u4e00-\u9fff\d][\u4e00-\u9fff]*)(\d+(?:\.\d+)?)')
    matches = pattern.findall(text)
    if not matches:
        return text

    if expected is not None:
        matches = _fix_splits(text, matches, expected)

    return '，'.join(name + price for name, price in matches)


def _fix_splits(text, matches, expected):
    """校验解析出的价格总和是否匹配支出总额, 不匹配则调整小数点边界"""
    prices = []
    for _name, _price in matches:
        try:
            prices.append(float(_price))
        except ValueError:
            return matches

    total = sum(prices)
    if abs(total - expected) < 0.005:
        return matches

    diff = round(total - expected, 2)
    for i in range(len(matches)):
        name, price = matches[i]
        if '.' not in price:
            continue
        dot_pos = price.index('.')
        frac = price[dot_pos + 1:]
        if len(frac) >= 2:
            new_price = price[:dot_pos + 2]
            migrated = price[dot_pos + 2:]
            try:
                np = float(new_price)
            except ValueError:
                continue
            new_prices = prices[:]
            new_prices[i] = np
            if abs(sum(new_prices) - expected) < 0.005:
                if i + 1 < len(matches):
                    matches[i] = (name, new_price)
                    matches[i + 1] = (migrated + matches[i + 1][0], matches[i + 1][1])
                return matches

    return matches


def normalize_period(raw):
    """将 "2026/6" 或 "2026/12" 归一化为 "2026-06" 格式。"""
    m = re.match(r"(\d{4})/(\d+)", raw)
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
