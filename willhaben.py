#!/usr/bin/env python3
"""Download willhaben.at advertisements for the search URLs from config.json.

Every advertisement is stored in its own folder inside ``data_path``:

    <data_path>/<yyyyMMdd>-<advertisement id>/advertisement.json
    <data_path>/<yyyyMMdd>-<advertisement id>/images/001.jpg, 002.jpg, ...

The date is the day the advertisement was first found. ``<data_path>/index.json``
maps every advertisement id to its folder, price per m2 and status
(``active`` or ``removed``). Saved advertisements are never overwritten; only
their status in the index is updated. After every run the index is also exported
to ``<data_path>/index.csv`` (a derived file, regenerated each time). The ``checked`` flag is set to false
only when an advertisement is first inserted and is never changed by the script
afterwards, so it can be edited by hand.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
BASE_URL = "https://www.willhaben.at"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 30
REQUEST_DELAY_SECONDS = 1.0
INDEX_FILENAME = "index.json"
CSV_FILENAME = "index.csv"
CSV_COLUMNS = [
    "id",
    "folder",
    "url",
    "price_per_m2",
    "status",
    "checked",
    "found_utc",
    "status_changed_utc",
]

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not config.get("search_urls"):
        raise ValueError("config.json: 'search_urls' is empty or missing")
    if not config.get("data_path"):
        raise ValueError("config.json: 'data_path' is missing")
    return config


def fetch_next_data(url: str) -> dict:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    script = BeautifulSoup(response.text, "html.parser").find(
        "script", id="__NEXT_DATA__"
    )
    if script is None or not script.string:
        raise ValueError(f"No __NEXT_DATA__ JSON found on {url}")
    return json.loads(script.string)


def with_page(url: str, page: int) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def attributes_to_dict(advert: dict) -> dict[str, list[str]]:
    return {
        attr["name"]: attr.get("values", [])
        for attr in advert.get("attributes", {}).get("attribute", [])
    }


def advert_url(advert: dict) -> str | None:
    seo = attributes_to_dict(advert).get("SEO_URL")
    if seo:
        return urljoin(BASE_URL + "/iad/", seo[0])
    return None


def collect_search_adverts(search_url: str, max_pages: int | None) -> list[dict]:
    adverts: list[dict] = []
    page = 1
    while True:
        page_url = with_page(search_url, page)
        print(f"[search] page {page}: {page_url}")
        result = fetch_next_data(page_url)["props"]["pageProps"]["searchResult"]
        summaries = result.get("advertSummaryList", {}).get("advertSummary", [])
        adverts.extend(summaries)
        rows_found = int(result.get("rowsFound") or 0)
        rows_requested = int(result.get("rowsRequested") or len(summaries) or 1)
        print(f"[search] {len(summaries)} advertisements, total found: {rows_found}")
        if not summaries or page * rows_requested >= rows_found:
            break
        if max_pages is not None and page >= max_pages:
            print(f"[search] stopping at max_pages={max_pages}")
            break
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)
    return adverts


def fetch_advert_details(summary: dict) -> dict:
    url = advert_url(summary)
    if not url:
        return summary
    try:
        return fetch_next_data(url)["props"]["pageProps"]["advertDetails"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"[warn] detail page failed ({exc}); using search summary", file=sys.stderr)
        return summary


def image_urls(advert: dict, summary: dict) -> list[str]:
    urls: list[str] = []
    for source in (advert, summary):
        for image in source.get("advertImageList", {}).get("advertImage", []):
            url = (
                image.get("referenceImageUrl")
                or image.get("mainImageUrl")
                or image.get("thumbnailImageUrl")
            )
            if url and url not in urls:
                urls.append(url)
        if urls:
            break
    return urls


def download_images(urls: list[str], images_dir: Path) -> list[str]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for index, url in enumerate(urls, start=1):
        extension = Path(urlparse(url).path).suffix or ".jpg"
        target = images_dir / f"{index:03d}{extension}"
        if not target.exists():
            try:
                response = session.get(url, timeout=TIMEOUT)
                response.raise_for_status()
            except requests.RequestException as exc:
                print(f"[warn] image failed {url}: {exc}", file=sys.stderr)
                continue
            target.write_bytes(response.content)
            print(f"[image] {url} -> {target}")
        saved.append(target.name)
    return saved


def first_value(advert: dict, name: str) -> str | None:
    values = attributes_to_dict(advert).get(name)
    return values[0] if values else None


def price_per_m2(*adverts: dict) -> float | None:
    for advert in adverts:
        try:
            value = first_value(advert, "PRICE/SQUARE_METER")
            if value:
                return round(float(value), 2)
            price = first_value(advert, "ESTATE_PRICE/PRICE_SUGGESTION") or first_value(
                advert, "PRICE"
            )
            area = first_value(advert, "PLOT/AREA")
            if price and area and float(area) > 0:
                return round(float(price) / float(area), 2)
        except ValueError:
            continue
    return None


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_index(data_path: Path) -> dict:
    path = data_path / INDEX_FILENAME
    if not path.exists():
        return {"advertisements": {}}
    index = json.loads(path.read_text(encoding="utf-8"))
    index.setdefault("advertisements", {})
    return index


def save_index(data_path: Path, index: dict) -> None:
    path = data_path / INDEX_FILENAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def export_csv(data_path: Path, index: dict) -> None:
    path = data_path / CSV_FILENAME
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for advert_id, entry in index["advertisements"].items():
            writer.writerow({"id": advert_id, **entry})
    temporary.replace(path)


def save_advert(summary: dict, search_url: str, data_path: Path, index: dict) -> None:
    advert_id = str(summary["id"])
    found = datetime.now(timezone.utc)
    folder_name = f"{found:%Y%m%d}-{advert_id}"
    folder = data_path / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    advert = fetch_advert_details(summary)
    saved_images = download_images(image_urls(advert, summary), folder / "images")
    url = advert_url(summary)

    document = {
        "id": advert_id,
        "url": url,
        "search_url": search_url,
        "found_utc": now_utc(),
        "images": saved_images,
        "attributes": attributes_to_dict(advert),
        "advert": advert,
    }
    (folder / "advertisement.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    previous = index["advertisements"].get(advert_id, {})
    index["advertisements"][advert_id] = {
        "folder": folder_name,
        "url": url,
        "price_per_m2": price_per_m2(summary, advert),
        "status": "active",
        "checked": previous.get("checked", False),
        "found_utc": document["found_utc"],
        "status_changed_utc": document["found_utc"],
    }
    save_index(data_path, index)
    print(f"[saved] {folder_name}: {url} ({len(saved_images)} images)")


def set_status(index: dict, advert_id: str, status: str) -> bool:
    entry = index["advertisements"][advert_id]
    if entry.get("status") == status:
        return False
    entry["status"] = status
    entry["status_changed_utc"] = now_utc()
    print(f"[status] {advert_id}: {status}")
    return True


def run(config_path: Path, max_pages: int | None) -> int:
    config = load_config(config_path)
    data_path = Path(config["data_path"])
    if not data_path.is_absolute():
        data_path = config_path.resolve().parent / data_path
    data_path.mkdir(parents=True, exist_ok=True)
    max_pages = max_pages or config.get("max_pages")

    index = load_index(data_path)
    saved = index["advertisements"]
    # Backfill only: an existing value (possibly edited by hand) is never changed.
    if any("checked" not in entry for entry in saved.values()):
        for entry in saved.values():
            entry.setdefault("checked", False)
        save_index(data_path, index)
    seen: set[str] = set()
    new_count = 0
    failures = 0
    complete_scan = max_pages is None

    for search_url in config["search_urls"]:
        try:
            summaries = collect_search_adverts(search_url, max_pages)
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"[error] search failed {search_url}: {exc}", file=sys.stderr)
            failures += 1
            complete_scan = False
            continue
        for summary in summaries:
            advert_id = str(summary.get("id", ""))
            if not advert_id or advert_id in seen:
                continue
            seen.add(advert_id)
            entry = saved.get(advert_id)
            if entry and (data_path / entry["folder"]).is_dir():
                if set_status(index, advert_id, "active"):
                    save_index(data_path, index)
                continue
            try:
                save_advert(summary, search_url, data_path, index)
                new_count += 1
            except (requests.RequestException, OSError) as exc:
                print(f"[error] advertisement {advert_id} failed: {exc}", file=sys.stderr)
                failures += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    # Mark as removed only after a full, error-free scan of every search.
    if complete_scan:
        changed = [
            advert_id
            for advert_id in saved
            if advert_id not in seen and set_status(index, advert_id, "removed")
        ]
        if changed:
            save_index(data_path, index)
    else:
        print("[info] partial scan: removed advertisements were not checked")

    export_csv(data_path, index)
    print(
        f"Done: {len(seen)} advertisements found, {new_count} new, "
        f"{failures} failures, data in {data_path}"
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download willhaben.at advertisements")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--max-pages", type=int, help="Limit result pages per search URL")
    args = parser.parse_args()
    return run(args.config, args.max_pages)


if __name__ == "__main__":
    raise SystemExit(main())
