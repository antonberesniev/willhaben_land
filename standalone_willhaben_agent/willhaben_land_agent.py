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
HTML_REPORT_PATH = DATA_DIR / "suitable_proposals.html"
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
LAND_KEYWORD_PATTERN = re.compile(
    r"\b(grundstück|grundstueck|baugrund|baugrundstück|baugrundstueck)\b",
    re.IGNORECASE,
)


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
        comma_parts = cleaned.split(",")
        if len(comma_parts) > 1 and all(part.isdigit() for part in comma_parts):
            if len(comma_parts[-1]) == 3 and all(
                len(part) == 3 for part in comma_parts[1:]
            ):
                cleaned = "".join(comma_parts)
            else:
                cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", ".")

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


def remove_eur_per_m2_fragments(text: str) -> str:
    cleaned = text
    for pattern in PRICE_PER_M2_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned


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
    if not LAND_KEYWORD_PATTERN.search(text):
        return None

    title = link.get_text(strip=True) or "(untitled)"
    location = ""
    for cls in ["location", "address", "region"]:
        node = card.find(attrs={"class": re.compile(cls, re.IGNORECASE)})
        if node:
            location = node.get_text(" ", strip=True)
            break

    eur_per_m2 = extract_number(PRICE_PER_M2_PATTERNS, text)
    total_price = extract_number(PRICE_PATTERNS, remove_eur_per_m2_fragments(text))
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
            invalidated_utc TEXT,
            hit_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    # Keep backward compatibility with DB files created before invalidation tracking existed.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(suitable_proposals)").fetchall()
    }
    if "invalidated_utc" not in columns:
        conn.execute("ALTER TABLE suitable_proposals ADD COLUMN invalidated_utc TEXT")
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
            invalidated_utc = NULL,
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


def invalidate_missing_suitable(
    conn: sqlite3.Connection, seen_urls: set[str], now_utc: str
) -> int:
    if seen_urls:
        placeholders = ",".join("?" for _ in seen_urls)
        result = conn.execute(
            f"""
            UPDATE suitable_proposals
            SET invalidated_utc = ?
            WHERE invalidated_utc IS NULL
              AND url NOT IN ({placeholders})
            """,
            (now_utc, *seen_urls),
        )
    else:
        result = conn.execute(
            """
            UPDATE suitable_proposals
            SET invalidated_utc = ?
            WHERE invalidated_utc IS NULL
            """,
            (now_utc,),
        )
    return int(result.rowcount or 0)


def render_report(conn: sqlite3.Connection, threshold: float) -> str:
    active_rows = conn.execute(
        """
        SELECT title, url, location, total_price_eur, area_m2, eur_per_m2,
               first_seen_utc, last_seen_utc, hit_count
        FROM suitable_proposals
        WHERE eur_per_m2 <= ? AND invalidated_utc IS NULL
        ORDER BY eur_per_m2 ASC, last_seen_utc DESC
        """,
        (threshold,),
    ).fetchall()

    invalidated_rows = conn.execute(
        """
        SELECT title, url, location, total_price_eur, area_m2, eur_per_m2,
               first_seen_utc, last_seen_utc, invalidated_utc, hit_count
        FROM suitable_proposals
        WHERE eur_per_m2 <= ? AND invalidated_utc IS NOT NULL
        ORDER BY invalidated_utc DESC, eur_per_m2 ASC
        """,
        (threshold,),
    ).fetchall()

    lines = [
        "# Suitable Grundstück Proposals",
        "",
        f"Threshold: <= {threshold:.2f} EUR/m²",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]

    lines.append("## Active suitable proposals")
    lines.append("")

    if not active_rows:
        lines.append("No active suitable proposals found.")
    else:
        lines.extend(
            [
                "| Title | EUR/m² | Price (EUR) | Area (m²) | Location | First Seen | Last Seen | Hits | URL |",
                "|---|---:|---:|---:|---|---|---|---:|---|",
            ]
        )

        for r in active_rows:
            title, url, location, total_price, area_m2, eur_m2, first_seen, last_seen, hits = r
            eur_m2_txt = f"{eur_m2:.3f}" if eur_m2 is not None else "n/a"
            price_txt = f"{total_price:.2f}" if total_price is not None else "n/a"
            area_txt = f"{area_m2:.2f}" if area_m2 is not None else "n/a"
            lines.append(
                f"| {title.replace('|', '/')} | {eur_m2_txt} | {price_txt} | {area_txt} | "
                f"{(location or '').replace('|', '/')} | {first_seen} | {last_seen} | {hits} | "
                f"{(url or '').replace('|', '/')} |"
            )

    lines.append("")
    lines.append("## Invalidated (no longer present)")
    lines.append("")

    if not invalidated_rows:
        lines.append("No invalidated suitable proposals yet.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Title | EUR/m² | Price (EUR) | Area (m²) | Location | First Seen | Last Seen | Invalidated | Hits | URL |",
            "|---|---:|---:|---:|---|---|---|---|---:|---|",
        ]
    )

    for r in invalidated_rows:
        title, url, location, total_price, area_m2, eur_m2, first_seen, last_seen, invalidated_utc, hits = r
        eur_m2_txt = f"{eur_m2:.3f}" if eur_m2 is not None else "n/a"
        price_txt = f"{total_price:.2f}" if total_price is not None else "n/a"
        area_txt = f"{area_m2:.2f}" if area_m2 is not None else "n/a"
        lines.append(
            f"| {title.replace('|', '/')} | {eur_m2_txt} | {price_txt} | {area_txt} | "
            f"{(location or '').replace('|', '/')} | {first_seen} | {last_seen} | {invalidated_utc} | {hits} | "
            f"{(url or '').replace('|', '/')} |"
        )

    return "\n".join(lines) + "\n"


def html_escape(text: str) -> str:
        return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
        )


def render_html_report(conn: sqlite3.Connection, threshold: float) -> str:
        active_rows = conn.execute(
                """
                SELECT title, url, location, total_price_eur, area_m2, eur_per_m2,
                             first_seen_utc, last_seen_utc, hit_count
                FROM suitable_proposals
                WHERE eur_per_m2 <= ? AND invalidated_utc IS NULL
                ORDER BY eur_per_m2 ASC, last_seen_utc DESC
                """,
                (threshold,),
        ).fetchall()

        invalidated_rows = conn.execute(
                """
                SELECT title, url, location, total_price_eur, area_m2, eur_per_m2,
                             first_seen_utc, last_seen_utc, invalidated_utc, hit_count
                FROM suitable_proposals
                WHERE eur_per_m2 <= ? AND invalidated_utc IS NOT NULL
                ORDER BY invalidated_utc DESC, eur_per_m2 ASC
                """,
                (threshold,),
        ).fetchall()

        generated_utc = datetime.now(timezone.utc).isoformat()

        def fmt_num(value: float | None, decimals: int = 2) -> str:
                if value is None:
                        return "n/a"
                return f"{value:.{decimals}f}"

        def row_html_active(r: tuple) -> str:
                title, url, location, total_price, area_m2, eur_m2, first_seen, last_seen, hits = r
                return (
                        "<tr>"
                        f"<td>{html_escape(title)}</td>"
                        f"<td>{fmt_num(eur_m2, 3)}</td>"
                        f"<td>{fmt_num(total_price)}</td>"
                        f"<td>{fmt_num(area_m2)}</td>"
                        f"<td>{html_escape(location or '')}</td>"
                        f"<td>{html_escape(first_seen)}</td>"
                        f"<td>{html_escape(last_seen)}</td>"
                        f"<td>{hits}</td>"
                        f"<td><a href=\"{html_escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">open</a></td>"
                        "</tr>"
                )

        def row_html_invalidated(r: tuple) -> str:
                (
                        title,
                        url,
                        location,
                        total_price,
                        area_m2,
                        eur_m2,
                        first_seen,
                        last_seen,
                        invalidated_utc,
                        hits,
                ) = r
                return (
                        "<tr>"
                        f"<td>{html_escape(title)}</td>"
                        f"<td>{fmt_num(eur_m2, 3)}</td>"
                        f"<td>{fmt_num(total_price)}</td>"
                        f"<td>{fmt_num(area_m2)}</td>"
                        f"<td>{html_escape(location or '')}</td>"
                        f"<td>{html_escape(first_seen)}</td>"
                        f"<td>{html_escape(last_seen)}</td>"
                        f"<td>{html_escape(invalidated_utc or '')}</td>"
                        f"<td>{hits}</td>"
                        f"<td><a href=\"{html_escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">open</a></td>"
                        "</tr>"
                )

        active_table_rows = "\n".join(row_html_active(r) for r in active_rows)
        invalidated_table_rows = "\n".join(row_html_invalidated(r) for r in invalidated_rows)

        if not active_table_rows:
                active_table_rows = "<tr><td colspan=\"9\">No active suitable proposals found.</td></tr>"
        if not invalidated_table_rows:
                invalidated_table_rows = "<tr><td colspan=\"10\">No invalidated suitable proposals yet.</td></tr>"

        return f"""<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Suitable Grundstuck Proposals</title>
    <style>
        :root {{
            --bg: #f3f5f7;
            --card: #ffffff;
            --text: #1d2935;
            --muted: #5f7082;
            --border: #d7dde3;
            --header: #0b6bcb;
        }}
        body {{ margin: 0; font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; background: var(--bg); color: var(--text); }}
        .wrap {{ max-width: 1280px; margin: 0 auto; padding: 20px; }}
        .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
        h1, h2 {{ margin: 0 0 12px 0; }}
        p {{ margin: 6px 0; color: var(--muted); }}
        .meta strong {{ color: var(--text); }}
        .table-wrap {{ overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th, td {{ border-bottom: 1px solid var(--border); padding: 8px; text-align: left; vertical-align: top; }}
        th {{ background: #ebf3fb; color: var(--header); position: sticky; top: 0; }}
        a {{ color: #0959a5; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"card\">
            <h1>Suitable Grundstuck Proposals</h1>
            <p class=\"meta\"><strong>Status:</strong> {len(active_rows)} active, {len(invalidated_rows)} removed</p>
            <p class=\"meta\"><strong>Threshold:</strong> &lt;= {threshold:.2f} EUR/m2</p>
            <p class=\"meta\"><strong>Generated UTC:</strong> {html_escape(generated_utc)}</p>
        </div>

        <div class=\"card\">
            <h2>Active suitable proposals</h2>
            <div class=\"table-wrap\">
                <table>
                    <thead>
                        <tr>
                            <th>Title</th><th>EUR/m2</th><th>Price (EUR)</th><th>Area (m2)</th><th>Location</th><th>First Seen</th><th>Last Seen</th><th>Hits</th><th>URL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {active_table_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <div class=\"card\">
            <h2>Removed or invalidated proposals</h2>
            <div class=\"table-wrap\">
                <table>
                    <thead>
                        <tr>
                            <th>Title</th><th>EUR/m2</th><th>Price (EUR)</th><th>Area (m2)</th><th>Location</th><th>First Seen</th><th>Last Seen</th><th>Invalidated</th><th>Hits</th><th>URL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {invalidated_table_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""


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
    now_utc = datetime.now(timezone.utc).isoformat()
    suitable_count = 0
    scanned_count = 0
    skipped_without_eur_m2 = 0
    above_threshold_count = 0
    seen_suitable_urls: set[str] = set()
    invalidated_count = 0

    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        for search_url in search_urls:
            try:
                candidates = scrape_candidates(search_url)
            except requests.RequestException as exc:
                print(f"Failed to fetch {search_url}: {exc}", file=sys.stderr)
                continue
            scanned_count += len(candidates)
            for c in candidates:
                if c.eur_per_m2 is None:
                    skipped_without_eur_m2 += 1
                    continue
                if c.eur_per_m2 <= threshold:
                    suitable_count += 1
                    seen_suitable_urls.add(c.url)
                    if not dry_run:
                        upsert_suitable(conn, c, now_utc)
                else:
                    above_threshold_count += 1
            if not dry_run:
                conn.commit()

        if not dry_run:
            invalidated_count = invalidate_missing_suitable(conn, seen_suitable_urls, now_utc)
            conn.commit()
            REPORT_PATH.write_text(render_report(conn, threshold), encoding="utf-8")
            HTML_REPORT_PATH.write_text(render_html_report(conn, threshold), encoding="utf-8")

    print(f"Scanned candidates: {scanned_count}")
    print(f"Skipped without EUR/m²: {skipped_without_eur_m2}")
    print(f"Above threshold: {above_threshold_count}")
    print(f"Suitable candidates (<= {threshold} EUR/m²): {suitable_count}")
    if not dry_run:
        print(f"Newly invalidated suitable candidates: {invalidated_count}")
    if not dry_run:
        print(f"Report updated: {REPORT_PATH}")
        print(f"HTML report updated: {HTML_REPORT_PATH}")
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
