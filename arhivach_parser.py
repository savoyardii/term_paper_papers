"""
arhivach.vc parser — потоковая выгрузка: search → fetch треда → parse → filter → CSV.
HTML по умолчанию не сохраняется (как 4plebs).

usage:
    python arhivach_parser.py --query "Ильназ Галявиев"
    python arhivach_parser.py --query "Ильназ Галявиев" \
        --start-date 2021-05-11 --end-date 2021-05-25
    python arhivach_parser.py --query "Ильназ Галявиев" \
        --from-html arhivach_threads          # переиспользовать уже скачанные HTML
    python arhivach_parser.py --query "Ильназ Галявиев" \
        --save-html arhivach_threads          # дополнительно сохранять HTML
"""

import argparse
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://arhivach.vc"
PER_PAGE = 25
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2})\s+\S+\s+(\d{2}):(\d{2}):(\d{2})")


def fetch(url, session):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def _parse_post_date(s):
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    dd, mm, yy, hh, mi, ss = map(int, m.groups())
    return datetime(2000 + yy, mm, dd, hh, mi, ss)


def parse_search_page(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for tr in soup.select('tr[id^="thread_row_"]'):
        tid = tr["id"].replace("thread_row_", "")
        a = tr.select_one('td.thread_desc a[href^="/thread/"]')
        if not a:
            continue
        b = a.find("b")
        title = b.get_text(" ", strip=True) if b else a.get_text(" ", strip=True)
        posts_el = tr.select_one("span.thread_posts_count")
        date_el = tr.select_one("td.thread_date span")
        tags = [t.get_text(strip=True) for t in tr.select("div.thread_tags span.taglabel")]
        out.append({
            "thread_id": tid,
            "title": title,
            "url": BASE + a["href"],
            "posts": int(posts_el.text.strip()) if posts_el else 0,
            "date": date_el.get_text(strip=True) if date_el else "",
            "tags": "|".join(tags),
        })
    return out


def find_last_offset(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    last = 0
    for a in soup.select("div.pagination a, ul.pagination a"):
        m = re.search(r"/(\d+)/\?q=", a.get("href", ""))
        if m:
            last = max(last, int(m.group(1)))
    return last


def parse_thread(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    posts = []
    for p in soup.select("div.post"):
        pid = p.get("postid") or p.get("id", "").replace("post", "")
        name = p.select_one(".poster_name")
        time_el = p.select_one(".post_time")
        num_el = p.select_one(".post_num")
        subj = p.select_one(".post_subject")
        msg = p.select_one(".post_comment")
        date_str = time_el.get_text(" ", strip=True) if time_el else ""
        posts.append({
            "post_id": pid,
            "num": num_el.get_text(strip=True) if num_el else "",
            "name": name.get_text(" ", strip=True) if name else "",
            "date_str": date_str,
            "date": _parse_post_date(date_str),
            "subject": subj.get_text(" ", strip=True) if subj else "",
            "text": msg.get_text("\n", strip=True) if msg else "",
        })
    return posts


def search_threads(query, session, max_pages, delay):
    q_enc = quote_plus(query)
    first_url = f"{BASE}/index/?q={q_enc}"
    print(f"[*] поиск: {query!r}")
    txt = fetch(first_url, session)
    last_offset = find_last_offset(txt)
    total_pages = last_offset // PER_PAGE + 1
    print(f"[*] страниц: {total_pages}")
    threads = parse_search_page(txt)
    print(f"    стр 1: +{len(threads)}")
    for offset in range(PER_PAGE, last_offset + 1, PER_PAGE):
        page = offset // PER_PAGE + 1
        if page > max_pages:
            break
        url = f"{BASE}/index/relevant/alltime/{offset}/?q={q_enc}"
        time.sleep(delay)
        try:
            txt = fetch(url, session)
        except Exception as e:
            print(f"    стр {page}: ошибка {e}")
            continue
        rows = parse_search_page(txt)
        threads.extend(rows)
        print(f"    стр {page}: +{len(rows)}, итого {len(threads)}")
    seen, uniq = set(), []
    for t in threads:
        if t["thread_id"] not in seen:
            seen.add(t["thread_id"])
            uniq.append(t)
    return uniq


def keep_post(p, kw_lower, start_dt, end_dt):
    if start_dt and (not p["date"] or p["date"] < start_dt):
        return False
    if end_dt and (not p["date"] or p["date"] > end_dt):
        return False
    if kw_lower:
        text = (p["text"] or "").lower()
        if not any(w in text for w in kw_lower):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help='ключевая фраза для search и (по умолчанию) для фильтра постов')
    ap.add_argument("--keywords", default=None,
                    help='отдельный фильтр текста постов через запятую; '
                         'если не задан — берутся слова из --query')
    ap.add_argument("--listing-out", default="arhivach_listing.csv",
                    help="CSV списка найденных тредов (только в сетевом режиме)")
    ap.add_argument("--posts-out", default="arhivach_posts.csv")
    ap.add_argument("--from-html", default=None,
                    help="папка с уже скачанными HTML тредов: пропустить search+fetch")
    ap.add_argument("--save-html", default=None,
                    help="папка для сохранения HTML каждого треда (по умолчанию не сохраняем)")
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD, включительно")
    ap.add_argument("--end-date", default=None, help="YYYY-MM-DD, включительно")
    ap.add_argument("--max-pages", type=int, default=999)
    ap.add_argument("--delay", type=float, default=1.5, help="секунд между сетевыми запросами")
    args = ap.parse_args()

    start_dt = datetime.strptime(args.start_date, "%Y-%m-%d") if args.start_date else None
    end_dt = (datetime.strptime(args.end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
              if args.end_date else None)

    if args.keywords:
        kw_lower = [w.strip().lower() for w in args.keywords.split(",") if w.strip()]
    else:
        kw_lower = [w.lower() for w in args.query.split() if w.strip()]

    print(f"[*] kw={kw_lower}  даты={args.start_date or '...'}..{args.end_date or '...'}")

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"})

    if args.from_html:
        src = Path(args.from_html)
        threads = [{"thread_id": p.stem, "url": ""} for p in sorted(src.glob("*.html"))]
        print(f"[*] from-html: {len(threads)} файлов в {src}/")

        def loader(t):
            return (src / f"{t['thread_id']}.html").read_text(encoding="utf-8")
    else:
        threads = search_threads(args.query, s, args.max_pages, args.delay)
        pd.DataFrame(threads).to_csv(args.listing_out, index=False)
        print(f"[=] {len(threads)} тредов → {args.listing_out}")
        save_dir = Path(args.save_html) if args.save_html else None
        if save_dir:
            save_dir.mkdir(exist_ok=True)
            print(f"[*] HTML тредов сохраняем в {save_dir}/")

        def loader(t, _save=save_dir):
            html = fetch(t["url"], s)
            if _save:
                (_save / f"{t['thread_id']}.html").write_text(html, encoding="utf-8")
            time.sleep(args.delay)
            return html

    rows = []
    seen_total = 0
    for i, t in enumerate(threads, 1):
        try:
            html = loader(t)
        except Exception as e:
            print(f"  {i}/{len(threads)} {t['thread_id']}: ошибка {e}")
            continue
        kept = 0
        for p in parse_thread(html):
            seen_total += 1
            if keep_post(p, kw_lower, start_dt, end_dt):
                p["thread_id"] = t["thread_id"]
                rows.append(p)
                kept += 1
        print(f"  {i}/{len(threads)} {t['thread_id']}: +{kept}")

    df = pd.DataFrame(rows)
    df.to_csv(args.posts_out, index=False)
    print(f"[=] {len(df)}/{seen_total} постов прошли фильтр → {args.posts_out}")
    if not df.empty:
        print(f"    тредов с подходящими постами: {df['thread_id'].nunique()}")


if __name__ == "__main__":
    main()
