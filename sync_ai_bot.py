#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_ai_bot.py
每天从 https://ai-bot.cn/ 同步 AI 工具到 index.html。

做法：
  1. 抓取首页，提取「分类名 -> 收藏页 slug」映射（分类名直接来自链接文字，稳定可靠）。
  2. 逐个抓取每个分类的收藏页（含 /page/N/ 分页），解析出 (名称, 简介, 官网 data-url, 详情 href)。
  3. 与基线比对，只把 ai-bot.cn 新增的工具增量合并进 index.html：
     - 官网地址通过跟随 data-url 重定向解析为真实官网主页（绝写 ai-bot.cn 中转页）；
       data-url 本身是 ai-bot.cn 时改抓详情页找官方外链。
     - 插入到对应分类 id（sec-write 等）的 subs[0].tools 末尾，保留原有排版缩进。
  4. 更新基线（ai-bot-baseline.json，存 "分类|名称" 列表）并提交。

首次运行（基线不存在）只建基线、不改动 index.html。
设置环境变量 SYNC_CATS=AI写作工具,AI图像工具 可只处理指定分类（用于测试）。
"""
import os
import re
import sys
import json
import time
import urllib.parse
from bs4 import BeautifulSoup

HOME_URL = "https://ai-bot.cn/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
INDEX = "index.html"
BASELINE = "ai-bot-baseline.json"

CAT2ID = {
    "AI写作工具": "sec-write", "AI图像工具": "sec-image", "AI视频工具": "sec-video",
    "AI办公工具": "sec-office", "AI聊天助手": "sec-chat", "AI智能体": "sec-agent",
    "AI编程工具": "sec-code", "AI开发平台": "sec-dev", "AI设计工具": "sec-design",
    "AI音频工具": "sec-audio", "AI搜索引擎": "sec-search", "AI学习网站": "sec-learn",
    "AI训练模型": "sec-model", "AI模型评测": "sec-eval", "AI内容检测": "sec-detect",
    "AI提示指令": "sec-prompt", "AI副业工具": "sec-side",
}
# 首页收藏导航未覆盖、但从 sitemap 确认存在的两个分类
FAV_EXTRA = {"AI模型评测": "llm-benchmarks", "AI副业工具": "ai-side-business-tools"}


def clean(u):
    if not u:
        return ""
    p = urllib.parse.urlparse(u)
    return f"{p.scheme}://{p.netloc}"


def fetch_text(url, session):
    r = session.get(url, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def resolve_final(url, session):
    try:
        r = session.get(url, allow_redirects=True, timeout=25)
        return r.url
    except Exception:
        return url


def get_html(session):
    return fetch_text(HOME_URL, session)


def get_categories(session):
    html = get_html(session)
    soup = BeautifulSoup(html, "html.parser")
    cats = []
    seen = set()
    for a in soup.select('a[href*="/favorites/"]'):
        href = a.get("href", "")
        m = re.search(r"/favorites/([a-z0-9\-]+)/?", href)
        if not m:
            continue
        slug = m.group(1)
        name = a.get_text(strip=True)
        if name in CAT2ID and slug not in seen:
            seen.add(slug)
            cats.append((name, slug))
    for name, slug in FAV_EXTRA.items():
        if slug not in seen:
            seen.add(slug)
            cats.append((name, slug))
    return cats


def fetch_category(session, slug):
    items = []
    page = 1
    while True:
        url = "https://ai-bot.cn/favorites/" + slug + "/" + (f"page/{page}/" if page > 1 else "")
        try:
            html = fetch_text(url, session)
        except Exception as e:
            print(f"  收藏页 {slug} 第{page}页失败：{e}")
            break
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("a[data-url]")
        if not cards:
            break
        for c in cards:
            img = c.find("img")
            name = (img.get("alt") if img else "").strip()
            if not name:
                info = c.find("div", class_="url-info")
                if info:
                    d = info.find("div", class_="text-sm")
                    name = d.get_text(strip=True) if d else ""
            desc = ""
            info = c.find("div", class_="url-info")
            if info:
                ps = info.find_all("p")
                if ps:
                    desc = ps[0].get_text(strip=True)
            desc = (desc or "").replace("\n", " ").strip()
            items.append({
                "name": name,
                "desc": desc,
                "data_url": c.get("data-url", ""),
                "href": c.get("href", ""),
            })
        nxt = None
        for a in soup.select(f'a[href*="/favorites/{slug}/page/"]'):
            mm = re.search(r"/page/(\d+)/", a.get("href", ""))
            if mm and int(mm.group(1)) == page + 1:
                nxt = mm.group(1)
                break
        if nxt is None:
            break
        page += 1
        time.sleep(0.4)
    return items


def official_url(item, session):
    du = item.get("data_url", "")
    if du and "ai-bot.cn" not in urllib.parse.urlparse(du).netloc:
        return clean(resolve_final(du, session))
    href = item.get("href", "")
    if href and "ai-bot.cn" in urllib.parse.urlparse(href).netloc:
        try:
            h2 = fetch_text(href, session)
            s2 = BeautifulSoup(h2, "html.parser")
            for a in s2.find_all("a", href=True):
                u = a["href"]
                if u.startswith("http") and "ai-bot.cn" not in urllib.parse.urlparse(u).netloc:
                    return clean(resolve_final(u, session))
        except Exception:
            pass
    return ""


def find_insert_pos(html, catid):
    idx = html.find(f'id:"{catid}"')
    if idx < 0:
        return None
    t = html.find("tools:[", idx)
    if t < 0:
        return None
    i = t + len("tools:[") - 1
    depth = 0
    j = i
    while j < len(html):
        ch = html[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def entry_str(name, desc, url):
    return "[" + ",".join(json.dumps(x, ensure_ascii=False) for x in [name, desc, url]) + "]"


def _entry_indent(html, pos):
    """返回 tools 数组里最后一个工具条目的缩进（2 空格或 4 空格）。"""
    m = None
    for mm in re.finditer(r"\n([ \t]*)\[", html[:pos]):
        m = mm
    return m.group(1) if m else "  "


def insert_tool(html, catid, name, desc, url):
    pos = find_insert_pos(html, catid)
    if pos is None:
        return html, False
    entry = entry_str(name, desc, url)
    # 空数组 tools:[] 或 tools:[\n  ] -> 直接放入，不要加逗号
    k = pos - 1
    while k > 0 and html[k] in " \t\r\n":
        k -= 1
    if html[k] == "[":
        return html[:pos] + entry + html[pos:], True
    indent = _entry_indent(html, pos)
    # 取数组闭合 ] 之前的内容，去掉末尾空白与最多一个末尾逗号，
    # 再统一补一个逗号 + 新条目（这样无论原数组有没有末尾逗号都只会有恰好一个逗号）。
    body = html[:pos].rstrip()
    if body.endswith(","):
        body = body[:-1].rstrip()
    new_body = body + ",\n" + indent + entry
    return new_body + "\n" + html[pos:], True


def load_baseline():
    if not os.path.exists(BASELINE):
        return None
    try:
        with open(BASELINE, encoding="utf-8") as f:
            return set(json.load(f).get("pairs", []))
    except Exception:
        return set()


def save_baseline(pairs):
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump({"pairs": sorted(pairs)}, f, ensure_ascii=False, indent=0)


def main():
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print("== 获取分类列表 ==")
    try:
        cats = get_categories(session)
    except Exception as e:
        print(f"获取分类失败，跳过：{e}")
        return 0
    # 可选：只处理指定分类（测试用）
    only = os.environ.get("SYNC_CATS")
    if only:
        want = set(x.strip() for x in only.split(",") if x.strip())
        cats = [c for c in cats if c[0] in want]
    print(f"处理分类数：{len(cats)} -> {[c[0] for c in cats]}")

    all_items = []
    for name, slug in cats:
        try:
            its = fetch_category(session, slug)
        except Exception as e:
            print(f"  分类 {name} 抓取异常：{e}")
            its = []
        for it in its:
            it["cat"] = name
        all_items.extend(its)
        print(f"  {name}: {len(its)} 个")
        time.sleep(0.4)

    print(f"共解析 {len(all_items)} 个工具卡片")

    baseline = load_baseline()
    if baseline is None:
        pairs = {f"{it['cat']}|{it['name']}" for it in all_items if it["name"]}
        save_baseline(pairs)
        print(f"已初始化同步基线，共 {len(pairs)} 个工具，本次不改动 index.html。")
        return 0

    new_items = [it for it in all_items if it["name"] and f"{it['cat']}|{it['name']}" not in baseline]
    if not new_items:
        print("ai-bot.cn 本次无新增工具，index.html 保持不变。")
        return 0

    with open(INDEX, encoding="utf-8") as f:
        page = f.read()

    added, skipped = [], []
    for it in new_items:
        catid = CAT2ID.get(it["cat"])
        if not catid:
            skipped.append((it["name"], "无对应分类"))
            continue
        pos = find_insert_pos(page, catid)
        block = page[pos - 2000:pos] if pos else ""
        if pos and f'["{it["name"]}"' in block:
            skipped.append((it["name"], "index.html 该分类已存在"))
            continue
        url = official_url(it, session)
        if not url:
            skipped.append((it["name"], "解析不到官网地址"))
            continue
        page, ok = insert_tool(page, catid, it["name"], it["desc"], url)
        if ok:
            added.append((it["name"], it["cat"], url))
            baseline.add(f"{it['cat']}|{it['name']}")
        else:
            skipped.append((it["name"], "未找到插入位置"))
        time.sleep(0.3)

    if added:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(page)
        save_baseline(baseline)
        print(f"已新增 {len(added)} 个工具：")
        for n, c, u in added:
            print(f"  + [{c}] {n} -> {u}")
    if skipped:
        print(f"跳过 {len(skipped)} 个：")
        for n, r in skipped:
            print(f"  - {n} ({r})")
    if not added:
        print("本次没有可写入官网的工具，index.html 未改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
