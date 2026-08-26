"""Download the pinned upstream ECDICT CSV and build the runtime SQLite database."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path


UPSTREAM_COMMIT = "bc015ed2e24a7abef49fc6dbbb7fe32c1dadaf8b"
UPSTREAM_CSV_URL = (
    "https://raw.githubusercontent.com/skywind3000/ECDICT/"
    f"{UPSTREAM_COMMIT}/ecdict.csv"
)
REQUIRED_COLUMNS = {"word", "phonetic", "translation"}


def validate_database(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"database does not exist: {path}"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(stardict)")
            }
            missing = REQUIRED_COLUMNS - columns
            if missing:
                return False, "stardict is missing columns: " + ", ".join(sorted(missing))
            row = connection.execute(
                "SELECT phonetic, translation FROM stardict "
                "WHERE lower(word) = 'hello' LIMIT 1"
            ).fetchone()
            if not row or not str(row[0] or "").strip() or not str(row[1] or "").strip():
                return False, "known word lookup failed: hello"
    except (OSError, sqlite3.Error) as exc:
        return False, f"SQLite validation failed: {exc}"
    return True, "SQLite schema and hello lookup are valid"


def download_csv(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "English-analyzer-ecdict-setup"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"download returned HTTP {response.status}")
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def build_database(csv_path: Path, database_path: Path) -> int:
    with csv_path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise RuntimeError("upstream CSV does not contain word, phonetic, and translation columns")
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE stardict (
                  id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL UNIQUE,
                  word VARCHAR(64) COLLATE NOCASE NOT NULL UNIQUE,
                  sw VARCHAR(64) COLLATE NOCASE NOT NULL,
                  phonetic VARCHAR(64), definition TEXT, translation TEXT,
                  pos VARCHAR(16), collins INTEGER DEFAULT 0, oxford INTEGER DEFAULT 0,
                  tag VARCHAR(64), bnc INTEGER, frq INTEGER, exchange TEXT,
                  detail TEXT, audio TEXT
                );
                CREATE INDEX sd_1 ON stardict (word COLLATE NOCASE);
                """
            )
            rows: list[tuple[str, ...]] = []
            total = 0
            for item in reader:
                word = (item.get("word") or "").strip()
                if not word:
                    continue
                rows.append((
                    word, "".join(character for character in word if character.isalnum()).lower(),
                    item.get("phonetic") or "", item.get("definition") or "",
                    item.get("translation") or "", item.get("pos") or "",
                    item.get("collins") or 0, item.get("oxford") or 0, item.get("tag") or "",
                    item.get("bnc") or None, item.get("frq") or None, item.get("exchange") or "",
                    item.get("detail") or "", item.get("audio") or "",
                ))
                if len(rows) >= 2000:
                    connection.executemany(
                        "INSERT OR IGNORE INTO stardict "
                        "(word, sw, phonetic, definition, translation, pos, collins, oxford, tag, bnc, frq, exchange, detail, audio) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
                    )
                    total += len(rows)
                    rows.clear()
            if rows:
                connection.executemany(
                    "INSERT OR IGNORE INTO stardict "
                    "(word, sw, phonetic, definition, translation, pos, collins, oxford, tag, bnc, frq, exchange, detail, audio) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
                total += len(rows)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "ecdict" / "ecdict.db")
    parser.add_argument("--source-url", default=UPSTREAM_CSV_URL, help="fixed upstream URL by default; intended for isolated testing only")
    parser.add_argument("--force", action="store_true", help="replace an existing valid target")
    parser.add_argument("--validate-only", action="store_true", help="validate target without downloading or writing")
    args = parser.parse_args()
    target = args.target.resolve()
    valid, message = validate_database(target)
    if args.validate_only:
        print(("ECDICT READY: " if valid else "ECDICT SETUP FAILED: ") + message)
        return 0 if valid else 1
    if valid and not args.force:
        print(f"ECDICT READY: existing database kept ({message})")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ecdict-", dir=target.parent) as temporary:
        temp_dir = Path(temporary)
        csv_path = temp_dir / "ecdict.csv"
        database_path = temp_dir / "ecdict.db"
        print(f"Downloading ECDICT source: {args.source_url}")
        download_csv(args.source_url, csv_path)
        print("Building SQLite database...")
        count = build_database(csv_path, database_path)
        valid, message = validate_database(database_path)
        if not valid:
            raise RuntimeError(message)
        os.replace(database_path, target)
    print(f"ECDICT READY: {target} ({count} source rows; {message})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ECDICT SETUP FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
