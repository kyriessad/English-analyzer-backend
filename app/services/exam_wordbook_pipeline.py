from __future__ import annotations

import io
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from app.data.discovery_content import PACKS
from app.services.card_service import normalize_card_content
from app.services.ecdict_service import EcdictEntry, get_tagged_dictionary_entries
from app.services.public_material_importer import PublicMaterialItemImport, PublicMaterialPackImport


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = ROOT_DIR / "app" / "data" / "exam_wordbook_sources.json"
DEFAULT_CACHE_DIR = ROOT_DIR / "data" / "exam-wordbook-cache"
TOKEN_RE = re.compile(r"[a-z]+(?:['-][a-z]+)*", re.IGNORECASE)
CORPUS_BOILERPLATE_RE = re.compile(
    r"^(?:#+\s|directions?:|questions?\s+\d.*\bbased on\b|"
    r"(?:reading|listening|speaking|writing) section directions|"
    r".*\banswer sheet\b|.*\bmark the corresponding letter\b|"
    r".*\bsample tasks?\b|.*\bpractice test\s+\d*\b|"
    r"copyright\b|page\s+\d+\s+of\s+\d+|ielts\.org\b|www\.ets\.org\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProducedExamWordbooks:
    production_batch: str
    packs: list[PublicMaterialPackImport]
    items_by_pack: dict[str, list[PublicMaterialItemImport]]
    report: dict[str, Any]


def load_source_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _cache_path(cache_dir: Path, source_id: str, url: str) -> Path:
    suffix = Path(urlparse(url).path).suffix or ".bin"
    return cache_dir / f"{source_id}{suffix}"


def _download(url: str, target: Path, *, refresh: bool, offline: bool) -> bytes:
    if target.is_file() and not refresh:
        return target.read_bytes()
    if offline:
        raise RuntimeError(f"source is not cached for offline production: {target.name}")
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    content = response.content
    if not content:
        raise RuntimeError(f"downloaded source is empty: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return content


def _clean_corpus_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("_* ")
        if not line or CORPUS_BOILERPLATE_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _token_counts(texts: list[str]) -> tuple[Counter[str], int]:
    tokens = [
        token.lower()
        for text in texts
        for token in TOKEN_RE.findall(_clean_corpus_text(text))
    ]
    return Counter(tokens), len(tokens)


def _read_text_archive(
    corpus: dict[str, Any], cache_dir: Path, *, refresh: bool, offline: bool
) -> tuple[list[str], list[str]]:
    archive = _download(
        corpus["url"],
        _cache_path(cache_dir, f"{corpus['revision']}-papers", corpus["url"]),
        refresh=refresh,
        offline=offline,
    )
    texts: list[str] = []
    names: list[str] = []
    file_pattern = re.compile(str(corpus.get("file_pattern") or r"\.md$"), re.IGNORECASE)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in sorted(zipped.namelist()):
            normalized_name = "/" + name.replace("\\", "/")
            path_segment = corpus.get("path_segment")
            if path_segment and path_segment not in normalized_name:
                continue
            if not file_pattern.search(normalized_name):
                continue
            texts.append(zipped.read(name).decode("utf-8"))
            names.append(name)
    if len(texts) != int(corpus["expected_documents"]):
        raise RuntimeError(
            f"{corpus['name']} expected {corpus['expected_documents']} documents, found {len(texts)}"
        )
    return texts, names


def _read_pdf_documents(
    corpus: dict[str, Any], cache_dir: Path, *, refresh: bool, offline: bool
) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    names: list[str] = []
    for document in corpus["documents"]:
        content = _download(
            document["url"],
            _cache_path(cache_dir, document["id"], document["url"]),
            refresh=refresh,
            offline=offline,
        )
        if not content.startswith(b"%PDF"):
            raise RuntimeError(f"source is not a PDF: {document['url']}")
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise RuntimeError(f"no text could be extracted from {document['id']}")
        texts.append(text)
        names.append(document["id"])
    return texts, names


def _build_ecdict_items(
    *,
    pack_title: str,
    tag: str,
    source: dict[str, Any],
    corpus: dict[str, Any],
    counts: Counter[str],
    production_batch: str,
) -> tuple[list[PublicMaterialItemImport], dict[str, Any]]:
    entries: tuple[EcdictEntry, ...] = get_tagged_dictionary_entries(tag, limit=20_000)
    if not entries:
        raise RuntimeError(f"ECDICT tag {tag!r} is unavailable")
    ordered = sorted(
        entries,
        key=lambda entry: (
            -counts[normalize_card_content(entry.word)],
            entry.frq if entry.frq is not None else 999_999_999,
            entry.bnc if entry.bnc is not None else 999_999_999,
            normalize_card_content(entry.word),
        ),
    )
    matched = sum(1 for entry in ordered if counts[normalize_card_content(entry.word)] > 0)
    items = [
        PublicMaterialItemImport(
            content=entry.word,
            chinese=entry.meanings[0],
            card_type="word",
            source_label=pack_title,
            source="ecdict",
            source_id=f"{tag}:{normalize_card_content(entry.word)}",
            license=source["license"],
            corpus_rank=rank,
            corpus_frequency=float(counts[normalize_card_content(entry.word)]),
            production_batch=production_batch,
            review_note=(
                f"candidate=ECDICT@{source['revision'][:12]}; "
                f"corpus={corpus['name']}; scope={corpus['nature']}"
            )[:240],
        )
        for rank, entry in enumerate(ordered, start=1)
    ]
    return items, {
        "candidate_count": len(items),
        "matched_candidate_count": matched,
        "zero_frequency_count": len(items) - matched,
        "duplicate_count": 0,
    }


def produce_exam_wordbooks(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh_sources: bool = False,
    offline: bool = False,
    book_codes: set[str] | None = None,
) -> ProducedExamWordbooks:
    manifest = load_source_manifest(manifest_path)
    production_batch = str(manifest["production_batch"])
    pack_definitions = {row[0]: row for row in PACKS}
    candidate_sources = manifest["candidate_sources"]
    packs: list[PublicMaterialPackImport] = []
    items_by_pack: dict[str, list[PublicMaterialItemImport]] = {}
    report_books: dict[str, Any] = {}

    selected_codes = set(book_codes or manifest["books"])
    unknown_codes = selected_codes.difference(manifest["books"])
    if unknown_codes:
        raise ValueError(f"unknown exam word books: {', '.join(sorted(unknown_codes))}")

    for pack_code, spec in manifest["books"].items():
        if pack_code not in selected_codes:
            continue
        _, title, description, kind, sort_order = pack_definitions[pack_code]
        corpus = spec["corpus"]
        source = candidate_sources[spec["candidate_source"]]
        document_names: list[str] = []
        token_count: int | None = None

        if corpus["kind"] in {"github_markdown_archive", "github_text_archive"}:
            texts, document_names = _read_text_archive(
                corpus, cache_dir, refresh=refresh_sources, offline=offline
            )
        elif corpus["kind"] == "pdf_documents":
            texts, document_names = _read_pdf_documents(
                corpus, cache_dir, refresh=refresh_sources, offline=offline
            )
        else:
            raise RuntimeError(f"unsupported corpus kind: {corpus['kind']}")
        counts, token_count = _token_counts(texts)
        items, quality = _build_ecdict_items(
            pack_title=title,
            tag=spec["candidate_tag"],
            source=source,
            corpus=corpus,
            counts=counts,
            production_batch=production_batch,
        )

        normalized = [normalize_card_content(item.content) for item in items]
        if len(normalized) != len(set(normalized)):
            raise RuntimeError(f"pipeline left duplicate candidates in {pack_code}")
        if [item.corpus_rank for item in items] != list(range(1, len(items) + 1)):
            raise RuntimeError(f"pipeline produced invalid ranks in {pack_code}")
        if any(not item.chinese.strip() for item in items):
            raise RuntimeError(f"pipeline produced empty definitions in {pack_code}")

        packs.append(PublicMaterialPackImport(
            code=pack_code,
            title=title,
            description=description,
            kind=kind,
            sort_order=sort_order,
            content_version=production_batch,
        ))
        items_by_pack[pack_code] = items
        report_books[pack_code] = {
            **quality,
            "candidate_source": source,
            "corpus_source": corpus,
            "corpus_documents": document_names,
            "corpus_document_count": len(document_names),
            "corpus_token_count": token_count,
            "matching": "lowercase exact lexical-token matching; no lemmatization",
            "top_items": [
                {
                    "content": item.content,
                    "frequency": item.corpus_frequency,
                    "rank": item.corpus_rank,
                }
                for item in items[:10]
            ],
        }

    report = {
        "production_batch": production_batch,
        "book_count": len(items_by_pack),
        "total_candidate_count": sum(len(items) for items in items_by_pack.values()),
        "books": report_books,
    }
    return ProducedExamWordbooks(
        production_batch=production_batch,
        packs=packs,
        items_by_pack=items_by_pack,
        report=report,
    )
