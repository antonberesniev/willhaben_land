#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "proposals.db"
REPORT_PATH = DATA_DIR / "suitable_proposals.md"
DEFAULT_CONFIG_PATH = ROOT / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PRICE_PER_M2_PATTERNS = [
    re.compile(r"(\d[\d\.,\s]*)\s*€\s*/\s*m[²2]", re.IGNORECASE),
    re.compile(r"(\d[\d\.,\s]*)\s*eur\s*/\s*m[²2]", re.IGNORECASE),
]
PRICE_PATTERNS = [
    re.compile(r"(\d[\d\.,\s]*)\s*€", re.IGNORECASE),
    re.compile(r"(\d[\d\.,\s]*)\s*eur", re.IGNORECASE),
]
AREA_PATTERNS = [
    re.compile(r"(\d[\d\.,\s]*)\s*m[²2]", re.IGNORECASE),
]


@dataclass
class Candidate:
    title: str
    url: str
    location: str
    total_price_eur: float | None
    area_m2: float | None
    eur_per_m2: float | None
    source_url: str


def parse_number(raw: str) -> float | None:
    cleaned = raw.replace("\xa0", " ").strip()
    cleaned = re.sub(r"[^\d,\.\s]", "", cleaned)
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_number(patterns: Iterable[re.Pattern], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = parse_number(match.group(1))
            if value is not None:
                return value
    return None


def extract_cards(soup: BeautifulSoup) -> List[BeautifulSoup]:
    selectors = [
        "article",
        "li",
        "div[data-testid*='result']",
        "div[class*='search-result']",
        "div[class*='Result']",
    ]
    cards: List[BeautifulSoup] = []
    seen = set()
    for selector in selectors:
        for el in soup.select(selector):
            key = id(el)
            if key not in seen:
                seen.add(key)
                cards.append(el)
    return cards


def parse_candidate_from_card(card: BeautifulSoup, source_url: str) -> Candidate | None:
    link = card.find("a", href=True)
    if not link:
        return None

    href = link.get("href", "").strip()
    if not href:
        return None

    full_url = urljoin("https://www.willhaben.at", href)
    text = " ".join(card.stripped_strings)
    lower_text = text.lower()
    if "grund" not in lower_text and "grundstück" not in lower_text:
        return None

    title = link.get_text(strip=True) or "(untitled)"
    location = ""
    for cls in ["location", "address", "region"]:
        node = card.find(attrs={"class": re.compile(cls, re.IGNORECASE)})
        if node:
            location = node.get_text(" ", strip=True)
            break

    eur_per_m2 = extract_number(PRICE_PER_M2_PATTERNS, text)
    total_price = extract_number(PRICE_PATTERNS, text)
    area_m2 = extract_number(AREA_PATTERNS, text)

    if eur_per_m2 is None and total_price and area_m2 and area_m2 > 0:
        eur_per_m2 = total_price / area_m2

    return Candidate(
        title=title,
        url=full_url,
        location=location,
        total_price_eur=total_price,
        area_m2=area_m2,
        eur_per_m2=eur_per_m2,
        source_url=source_url,
    )


def scrape_candidates(search_url: str, timeout: int = 30) -> List[Candidate]:
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(search_url, headers=headers, timeout=timeout)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    parsed: List[Candidate] = []
    seen_urls = set()

    for card in extract_cards(soup):
        candidate = parse_candidate_from_card(card, search_url)
        if candidate and candidate.url not in seen_urls:
            seen_urls.add(candidate.url)
            parsed.append(candidate)

    return parsed


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suitable_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            location TEXT,
            total_price_eur REAL,
            area_m2 REAL,
            eur_per_m2 REAL,
            source_url TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()


def upsert_suitable(conn: sqlite3.Connection, c: Candidate, now_utc: str) -> None:
    conn.execute(
        """
        INSERT INTO suitable_proposals (
            url, title, location, total_price_eur, area_m2, eur_per_m2,
            source_url, first_seen_utc, last_seen_utc, hit_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            location = excluded.location,
            total_price_eur = excluded.total_price_eur,
            area_m2 = excluded.area_m2,
            eur_per_m2 = excluded.eur_per_m2,
            source_url = excluded.source_url,
            last_seen_utc = excluded.last_seen_utc,
            hit_count = suitable_proposals.hit_count + 1
        """,
        (
            c.url,
            c.title,
            c.location,
            c.total_price_eur,
            c.area_m2,
            c.eur_per_m2,
            c.source_url,
            now_utc,
            now_utc,
        ),
    )


def render_report(conn: sqlite3.Connection, threshold: float) -> str:
    rows = conn.execute(
        """
        SELECT title, url, location, total_price_eur, area_m2, eur_per_m2,
               first_seen_utc, last_seen_utc, hit_count
        FROM suitable_proposals
        ORDER BY eur_per_m2 ASC, last_seen_utc DESC
        """
    ).fetchall()

    lines = [
        "# Suitable Grundstück Proposals",
        "",
        f"Threshold: <= {threshold:.2f} EUR/m²",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]

    if not rows:
        lines.append("No suitable proposals found yet.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Title | EUR/m² | Price (EUR) | Area (m²) | Location | First Seen | Last Seen | Hits | URL |",
            "|---|---:|---:|---:|---|---|---|---:|---|",
        ]
    )

    for r in rows:
        title, url, location, total_price, area_m2, eur_m2, first_seen, last_seen, hits = r
        eur_m2_txt = f"{eur_m2:.3f}" if eur_m2 is not None else "n/a"
        price_txt = f"{total_price:.2f}" if total_price is not None else "n/a"
        area_txt = f"{area_m2:.2f}" if area_m2 is not None else "n/a"
        lines.append(
            f"| {title.replace('|', '/')} | {eur_m2_txt} | {price_txt} | {area_txt} | "
            f"{(location or '').replace('|', '/')} | {first_seen} | {last_seen} | {hits} | {url} |"
        )

    return "\n".join(lines) + "\n"


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.example.json to config.json and edit it."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def run(config_path: Path, dry_run: bool = False) -> int:
    cfg = load_config(config_path)
    search_urls = cfg.get("search_urls", [])
    threshold = float(cfg.get("max_eur_per_m2", 2.0))

    if not search_urls:
        raise ValueError("config search_urls is empty")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    now_utc = datetime.now(timezone.utc).isoformat()
    suitable_count = 0
    scanned_count = 0

    try:
        for search_url in search_urls:
            try:
                candidates = scrape_candidates(search_url)
            except requests.RequestException as exc:
                print(f"Failed to fetch {search_url}: {exc}", file=sys.stderr)
                continue
            scanned_count += len(candidates)
            for c in candidates:
                if c.eur_per_m2 is None:
                    continue
                if c.eur_per_m2 <= threshold:
                    suitable_count += 1
                    if not dry_run:
                        upsert_suitable(conn, c, now_utc)

        if not dry_run:
            conn.commit()
            REPORT_PATH.write_text(render_report(conn, threshold), encoding="utf-8")
    finally:
        conn.close()

    print(f"Scanned candidates: {scanned_count}")
    print(f"Suitable candidates (<= {threshold} EUR/m²): {suitable_count}")
    if not dry_run:
        print(f"Report updated: {REPORT_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily willhaben Grundstück checker with <= EUR/m² filter"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config JSON (default: standalone_willhaben_agent/config.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write DB/report")
    args = parser.parse_args()
    return run(args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
