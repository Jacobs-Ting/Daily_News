#!/usr/bin/env python3
"""
抓取 feeds.json 裡所有 RSS / Atom 來源，整理成 news.json 給網頁讀取。
由 GitHub Actions 定時執行，只用 Python 標準函式庫，不需安裝任何套件。

本機測試：python scripts/fetch_news.py
"""
import html
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"
OUT_FILE = ROOT / "news.json"

MAX_PER_FEED = 15        # 每個來源最多取幾篇
MAX_AGE_DAYS = 3         # 只保留最近幾天的新聞
DESC_LEN = 200           # 摘要最多幾個字
TIMEOUT = 25             # 單一來源逾時秒數
WORKERS = 8              # 同時抓幾個來源
RETRIES = 2              # 失敗重試次數

TW = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 DailyNewsBot/1.0")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    last = None
    for i in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def local(tag: str) -> str:
    """去掉 XML 命名空間，{http://...}title → title；dc:date 之類保留 local name。"""
    return tag.rsplit("}", 1)[-1]


def parse_date(s: str, local_time: bool) -> int:
    """回傳毫秒時間戳。沒有時區資訊時：localTime 來源視為台灣時間，其餘視為 UTC。"""
    now_ms = int(time.time() * 1000)
    s = (s or "").strip()
    if not s:
        return now_ms
    dt = None
    try:
        dt = parsedate_to_datetime(s)          # RFC 822：Sat, 24 Oct 2026 07:00:00 +0000
    except Exception:  # noqa: BLE001
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
        except Exception:  # noqa: BLE001
            return now_ms
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW if local_time else timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return min(ms, now_ms)                     # 防呆：時間跑到未來就視為現在


def parse_xml(raw: bytes):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # 有些來源會夾帶沒跳脫的 &，修一下再試
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"&(?!#?\w+;)", "&amp;", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        root = ET.fromstring(text)

    items = [el for el in root.iter() if local(el.tag) in ("item", "entry")]
    out = []
    for it in items:
        f = {}
        link = ""
        for ch in it:
            name = local(ch.tag)
            txt = "".join(ch.itertext()).strip()
            if name == "link":
                # Atom：<link rel="alternate" href="..."/>；RSS：<link>...</link>
                href = ch.get("href")
                if href and ch.get("rel", "alternate") == "alternate" and not link:
                    link = href
                elif txt and not link:
                    link = txt
            elif name not in f and txt:
                f[name] = txt
        out.append({
            "title": f.get("title", ""),
            "link": link or (f["guid"] if f.get("guid", "").startswith("http") else ""),
            "desc": f.get("description") or f.get("summary") or f.get("encoded") or f.get("content") or "",
            "date": f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date") or "",
        })
    return out


def fetch_feed(feed: dict):
    items = parse_xml(fetch(feed["url"]))
    arts = []
    for it in items[:MAX_PER_FEED]:
        title = strip_html(it["title"])
        link = (it["link"] or "").strip()
        if not title or not link:
            continue
        arts.append({
            "cat": feed["cat"],
            "source": feed["name"],
            "title": title,
            "link": link,
            "desc": strip_html(it["desc"])[:DESC_LEN],
            "time": parse_date(it["date"], feed.get("localTime", False)),
        })
    if not arts:
        raise RuntimeError("沒有解析到任何文章")
    return arts


def main():
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))

    # 讀上一次的結果：某來源這次失敗時，沿用它上次抓到的文章，畫面不會突然少一塊
    prev = []
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8")).get("articles", [])
        except Exception:  # noqa: BLE001
            prev = []

    def run(feed):
        try:
            return feed, fetch_feed(feed), None
        except Exception as e:  # noqa: BLE001
            return feed, None, f"{type(e).__name__}: {e}"[:200]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run, feeds))

    articles, errors = [], []
    for feed, arts, err in results:
        if arts:
            articles += arts
            print(f"  ✓ {feed['name']}: {len(arts)} 篇")
        else:
            errors.append(f"{feed['name']}：{err}")
            stale = [a for a in prev if a.get("source") == feed["name"]]
            articles += stale
            print(f"  ✗ {feed['name']}: {err}（沿用上次 {len(stale)} 篇）")

    # 去重（同連結或同標題）、過濾太舊、依時間新到舊排序
    cutoff = int((time.time() - MAX_AGE_DAYS * 86400) * 1000)
    seen, uniq = set(), []
    for a in sorted(articles, key=lambda a: a["time"], reverse=True):
        key = re.sub(r"[?#].*$", "", a["link"]) or a["title"]
        if key in seen or a["title"] in seen or a["time"] < cutoff:
            continue
        seen.add(key)
        seen.add(a["title"])
        uniq.append(a)

    # 全部失敗通常代表網路出問題：不覆寫檔案，回傳錯誤碼讓 Actions 顯示紅燈
    if len(errors) == len(feeds):
        print("所有來源都失敗，保留上次的 news.json")
        sys.exit(1)

    payload = {
        "at": int(time.time() * 1000),
        "articles": uniq,
        "errors": errors,
        "sources": len(feeds),
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"完成：{len(uniq)} 篇，{len(feeds) - len(errors)}/{len(feeds)} 個來源成功")


if __name__ == "__main__":
    main()
