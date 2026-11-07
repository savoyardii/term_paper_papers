"""Выгрузка постов из поискового API 4plebs в CSV."""

import os
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import requests

SEARCH_URL = "https://archive.4plebs.org/_/api/chan/search/"
QUERY = "dylan butler"
BOARD = "pol"
PAGES = 10
OUTPUT = Path("results.csv")
REQUESTS_PER_MIN = 5
US_ONLY = True
POST_COLUMNS = ["num", "thread_num", "timestamp", "poster_country", "name", "comment"]


def collect_posts(session):
    pages = []
    for page in range(1, PAGES + 1):
        print(f"Страница {page}/{PAGES}", flush=True)
        try:
            response = session.get(
                SEARCH_URL, params={"boards": BOARD, "text": QUERY, "page": page}, timeout=20
            )
            response.raise_for_status()
            result = response.json()
        except requests.HTTPError as exc:
            raise ValueError(f"страница {page}: HTTP {exc.response.status_code}") from exc
        except requests.RequestException as exc:
            raise ValueError(f"страница {page}: {type(exc).__name__}") from exc

        group = None
        if isinstance(result, list):
            if not result:
                break
            group = result[0]
        elif isinstance(result, dict):
            group = result.get("0")
        posts = group.get("posts") if isinstance(group, dict) else None
        if not isinstance(posts, list) or any(not isinstance(post, dict) for post in posts):
            raise ValueError(f"страница {page}: неожиданный формат ответа API")
        if not posts:
            break

        frame = pd.json_normalize(posts)
        if US_ONLY:
            if "poster_country" not in frame.columns:
                raise ValueError(f"страница {page}: API не вернул поле poster_country")
            frame = frame[frame["poster_country"] == "US"]
        pages.append(frame)

        if page < PAGES:
            time.sleep(60 / REQUESTS_PER_MIN)

    return pd.concat(pages, ignore_index=True) if pages else pd.DataFrame(columns=POST_COLUMNS)


def write_csv(frame, output):
    with NamedTemporaryFile(dir=output.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        frame.to_csv(temporary_path, index=False)
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)


def main():
    try:
        with requests.Session() as session:
            session.headers["Accept"] = "application/json"
            if cookie := os.getenv("FOURPLEBS_COOKIE"):
                session.headers["Cookie"] = cookie
            if user_agent := os.getenv("FOURPLEBS_USER_AGENT"):
                session.headers["User-Agent"] = user_agent
            frame = collect_posts(session)
        write_csv(frame, OUTPUT)
    except (ValueError, OSError) as exc:
        print(f"Сбой выгрузки: {exc}. Файл {OUTPUT} не обновлён.", file=sys.stderr)
        return 1

    print(f"Готово: {len(frame)} постов → {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
