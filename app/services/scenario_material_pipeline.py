from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import bz2
import gzip
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import requests

from app.core.config import settings
from app.data.discovery_content import PACKS
from app.services.card_service import normalize_card_content
from app.services.public_material_importer import PublicMaterialItemImport, PublicMaterialPackImport


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = ROOT_DIR / "app" / "data" / "scenario_material_sources.json"
DEFAULT_CACHE_DIR = ROOT_DIR / "data" / "scenario-material-cache"
WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ATTRIBUTION_RE = re.compile(
    r"^CC-BY 2\.0 \(France\) Attribution: tatoeba\.org "
    r"#(?P<english_id>\d+) \((?P<english_author>[^)]+)\) & "
    r"#(?P<chinese_id>\d+) \((?P<chinese_author>[^)]+)\)$"
)
ALLOWED_ENGLISH_RE = re.compile(r'^[A-Za-z0-9 ,.!?;:\'"()/-]+$')
SCENARIO_RULES_VERSION = "scenario-rules-v1"

SCENE_CODES = (
    "daily-life",
    "workplace",
    "travel",
    "film-tv",
    "natural-spoken",
    "common-phrases",
    "useful-sentences",
    "social-communication",
    "campus-study",
    "dining-shopping",
    "internet-social-media",
)

SPECIFIC_ASSIGNMENT_ORDER = (
    "workplace",
    "travel",
    "film-tv",
    "campus-study",
    "dining-shopping",
    "internet-social-media",
    "social-communication",
)
BROAD_ASSIGNMENT_ORDER = (
    "daily-life",
    "natural-spoken",
    "common-phrases",
    "useful-sentences",
)

KEYWORDS: dict[str, tuple[str, ...]] = {
    "daily-life": (
        "home", "house", "room", "family", "morning", "tonight", "today", "tomorrow",
        "yesterday", "weekend", "sleep", "wake", "shower", "bath", "laundry", "clean",
        "weather", "rain", "cold", "hot", "busy", "tired", "ready", "late", "early",
        "free time", "take a break", "on my way", "call it a day", "take your time",
    ),
    "workplace": (
        "work", "office", "job", "boss", "manager", "colleague", "coworker", "client",
        "customer", "meeting", "project", "deadline", "report", "contract", "company",
        "business", "salary", "interview", "hire", "fired", "career", "team", "schedule",
        "shift", "presentation", "budget", "department", "resume", "application",
    ),
    "travel": (
        "travel", "trip", "airport", "flight", "plane", "train", "station", "bus", "taxi",
        "hotel", "hostel", "reservation", "book a room", "ticket", "passport", "luggage",
        "suitcase", "map", "tour", "tourist", "vacation", "holiday", "departure", "arrival",
        "platform", "gate", "boarding", "destination", "abroad", "lost", "directions",
    ),
    "film-tv": (
        "movie", "film", "cinema", "television", "tv", "tv show", "episode", "movie series",
        "actor", "actress", "film director", "movie scene", "subtitle", "documentary",
        "comedy", "drama", "cartoon", "tv channel", "watch a movie", "watching tv", "trailer",
    ),
    "social-communication": (
        "friend", "meet", "invite", "invitation", "party", "visit", "welcome", "thank",
        "thanks", "sorry", "apologize", "congratulations", "birthday", "call me", "text me",
        "talk", "chat", "nice to meet", "see you", "miss you", "help me", "join us",
        "get along", "keep in touch", "relationship", "neighbor", "guest",
    ),
    "campus-study": (
        "school", "campus", "class", "classroom", "teacher", "student", "professor", "lesson",
        "homework", "assignment", "exam", "test", "quiz", "study", "learn", "course",
        "university", "college", "library", "book", "paper", "degree", "graduate", "lecture",
        "grade", "semester", "education", "english",
    ),
    "dining-shopping": (
        "eat", "food", "meal", "breakfast", "lunch", "dinner", "restaurant", "menu", "order",
        "drink", "coffee", "tea", "water", "bread", "rice", "meat", "vegetable", "fruit",
        "shop", "store", "market", "buy", "sell", "price", "cost", "pay", "cash", "credit card",
        "receipt", "change", "size", "discount", "expensive", "cheap", "bill", "checkout",
    ),
    "internet-social-media": (
        "internet", "online", "website", "web site", "browser", "mobile app", "user account", "password",
        "username", "log in", "sign in", "email", "e-mail", "text message", "texted", "posted online",
        "leave a comment", "share online", "follow me online", "upload", "download", "web link", "video call",
        "social media", "wifi", "wi-fi", "computer", "smartphone", "mobile phone", "cell phone",
    ),
}

DIRECT_PATTERNS = (
    "can you", "could you", "would you", "will you", "do you", "did you", "are you", "have you",
    "how do", "how can", "how much", "how long", "how about", "what do", "what are", "what is",
    "where is", "where can", "when is", "why do", "please", "let's", "i need", "i want", "i'd like",
    "i'm", "i'll", "i can't", "we need", "we can", "thank you", "excuse me", "sorry", "no problem",
)
REJECT_TERMS = (
    "tom", "mary", "john", "jack", "jane", "bob", "alice", "david", "peter", "nancy", "lucy",
    "murder", "kill", "suicide", "gun", "rifle", "bomb", "naked", "sex", "porn", "rape",
    "jail", "prison", "police", "breast-feeding", "breastfeeding",
    "god", "jesus", "bible", "quran", "hitler", "trump", "biden", "covid", "coronavirus",
    "this sentence", "translate this", "in esperanto", "in french", "in japanese", "in german",
)
PROFANITY_RE = re.compile(r"\b(?:fuck|shit|bitch|bastard|asshole)\b", re.IGNORECASE)
URL_OR_HANDLE_RE = re.compile(r"(?:https?://|www\.|\S+@\S+|#[A-Za-z0-9_]+)", re.IGNORECASE)
REJECT_RE = re.compile(r"\b(?:" + "|".join(re.escape(term) for term in REJECT_TERMS) + r")\b", re.IGNORECASE)
ALL_KEYWORD_TERMS = tuple(term for terms in KEYWORDS.values() for term in terms)
ALLOWED_INTERNAL_CAPITALS = {
    "I", "English", "Chinese", "Mandarin", "TV", "Wi-Fi", "Internet",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
}
WEAK_KEYWORDS = {"work", "team", "schedule", "application", "company", "lost", "english"}
USEFUL_PATTERNS = (
    "can you", "could you", "would you", "will you", "do you", "are you", "have you",
    "where is", "where can", "what time", "how much", "how long", "how about", "is there",
    "are there", "i'd like", "i need", "may i", "should i", "please", "let's", "excuse me",
)


@dataclass(frozen=True)
class ScenarioCandidate:
    english: str
    chinese: str
    normalized: str
    source_id: str
    english_id: str
    chinese_id: str
    word_count: int


@dataclass(frozen=True)
class ClassifiedScenario:
    candidate: ScenarioCandidate
    pack_code: str
    score: int
    quality_score: int
    ai_reviewed: bool = False


@dataclass(frozen=True)
class ProducedScenarioMaterials:
    production_batch: str
    packs: list[PublicMaterialPackImport]
    items_by_pack: dict[str, list[PublicMaterialItemImport]]
    report: dict[str, Any]


AiReviewer = Callable[[list[dict[str, str]], str], list[dict[str, Any]]]
Translator = Callable[[str], str]
ChineseReviewer = Callable[[list[dict[str, str]], str], list[dict[str, Any]]]


def load_source_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _download_source(source: dict[str, Any], cache_dir: Path, *, refresh: bool, offline: bool) -> bytes:
    target = cache_dir / "cmn-eng.zip"
    if target.is_file() and not refresh:
        content = target.read_bytes()
    else:
        if offline:
            raise RuntimeError(f"source is not cached for offline production: {target}")
        response = requests.get(source["url"], timeout=90)
        response.raise_for_status()
        content = response.content
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    if digest != str(source["sha256"]).lower():
        raise RuntimeError(f"source checksum mismatch: expected {source['sha256']}, got {digest}")
    return content


def _download_named_source(
    source: dict[str, Any], cache_dir: Path, filename: str, *, refresh: bool, offline: bool
) -> bytes:
    target = cache_dir / filename
    if target.is_file() and not refresh:
        content = target.read_bytes()
    else:
        if offline:
            raise RuntimeError(f"source is not cached for offline production: {target}")
        response = requests.get(source["url"], timeout=120)
        response.raise_for_status()
        content = response.content
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    if digest != str(source["sha256"]).lower():
        raise RuntimeError(f"source checksum mismatch: expected {source['sha256']}, got {digest}")
    return content


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _quality_rejection_reason(english: str, chinese: str) -> str | None:
    words = WORD_RE.findall(english)
    lowered = english.lower()
    if len(words) < 2:
        return "too_short"
    if len(words) > 16 or len(english) > 120:
        return "too_long"
    if not CJK_RE.search(chinese):
        return "missing_chinese"
    if not ALLOWED_ENGLISH_RE.fullmatch(english):
        return "unsupported_characters"
    if URL_OR_HANDLE_RE.search(english) or PROFANITY_RE.search(english):
        return "unsafe_or_web_noise"
    if REJECT_RE.search(lowered):
        return "named_or_unsuitable_topic"
    if re.search(r"\b(?:19|20)\d{2}\b", english):
        return "dated_number"
    if english.count("(") != english.count(")") or english.count('"') % 2:
        return "broken_punctuation"
    internal_capitals = [
        token for token in WORD_RE.findall(english)[1:]
        if token[:1].isupper() and token not in ALLOWED_INTERNAL_CAPITALS
    ]
    if internal_capitals:
        return "named_entity"
    if not (
        any(pattern in lowered for pattern in DIRECT_PATTERNS)
        or any(term in lowered for term in ALL_KEYWORD_TERMS)
        or len(words) <= 7
    ):
        return "low_learning_utility"
    return None


@lru_cache(maxsize=None)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b")


def _contains_term(lowered: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in lowered
    return _term_pattern(term).search(lowered) is not None


def _translation_preference(candidate: ScenarioCandidate) -> tuple[int, int, str]:
    traditional_markers = "這個們為會裡與說時還來對學習車門見過麼買賣錢體書話電網"
    return (
        sum(candidate.chinese.count(char) for char in traditional_markers),
        len(candidate.chinese),
        candidate.source_id,
    )


def _read_tatoeba_pairs(archive: bytes, source: dict[str, Any]) -> tuple[list[ScenarioCandidate], dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        raw = zipped.read(source["archive_member"]).decode("utf-8")

    rejection_counts: dict[str, int] = {}
    by_normalized: dict[str, ScenarioCandidate] = {}
    parsed_count = 0
    eligible_count = 0
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        parts = raw_line.split("\t")
        if len(parts) != 3:
            rejection_counts["invalid_columns"] = rejection_counts.get("invalid_columns", 0) + 1
            continue
        english, chinese, attribution = (_clean_text(value) for value in parts)
        match = ATTRIBUTION_RE.fullmatch(attribution)
        if match is None:
            rejection_counts["invalid_attribution"] = rejection_counts.get("invalid_attribution", 0) + 1
            continue
        parsed_count += 1
        reason = _quality_rejection_reason(english, chinese)
        if reason:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        normalized = normalize_card_content(english)
        if not normalized:
            rejection_counts["empty_normalized"] = rejection_counts.get("empty_normalized", 0) + 1
            continue
        eligible_count += 1
        details = match.groupdict()
        candidate = ScenarioCandidate(
            english=english,
            chinese=chinese,
            normalized=normalized,
            source_id=(
                f"en:{details['english_id']}:{details['english_author']};"
                f"zh:{details['chinese_id']}:{details['chinese_author']}"
            )[:160],
            english_id=details["english_id"],
            chinese_id=details["chinese_id"],
            word_count=len(WORD_RE.findall(english)),
        )
        existing = by_normalized.get(normalized)
        if existing is None or _translation_preference(candidate) < _translation_preference(existing):
            by_normalized[normalized] = candidate

    return list(by_normalized.values()), {
        "raw_pair_count": len(raw.splitlines()),
        "parsed_pair_count": parsed_count,
        "clean_candidate_count": len(by_normalized),
        "duplicate_english_removed": eligible_count - len(by_normalized),
        "rejections": rejection_counts,
    }


def _read_cc0_english(archive: bytes, excluded: set[str]) -> tuple[list[ScenarioCandidate], dict[str, int]]:
    raw = bz2.decompress(archive).decode("utf-8")
    candidates: list[ScenarioCandidate] = []
    rejected = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            rejected += 1
            continue
        sentence_id, language, english = parts[:3]
        english = _clean_text(english)
        normalized = normalize_card_content(english)
        lowered = english.lower()
        conversational = bool(re.search(r"\b(?:i|i'm|i'll|i'd|we|you|your)\b", lowered)) or english.endswith("?")
        # Apply the English half of the established quality gate. A temporary
        # Chinese marker avoids weakening the bilingual parser's rules. The
        # English-only supplement is intentionally stricter: translation is
        # reserved for short conversational material, not factual corpus noise.
        if (
            language != "eng"
            or normalized in excluded
            or _quality_rejection_reason(english, "中")
            or len(WORD_RE.findall(english)) > 12
            or (not conversational and len(WORD_RE.findall(english)) > 10)
        ):
            rejected += 1
            continue
        candidates.append(ScenarioCandidate(
            english=english,
            chinese="",
            normalized=normalized,
            source_id=f"cc0:{sentence_id}",
            english_id=sentence_id,
            chinese_id="",
            word_count=len(WORD_RE.findall(english)),
        ))
    return candidates, {"raw_english_count": len(raw.splitlines()), "clean_english_count": len(candidates), "rejected_count": rejected}


def _open_english_candidate(english: str, source_id: str, excluded: set[str]) -> ScenarioCandidate | None:
    english = _clean_text(english)
    normalized = normalize_card_content(english)
    if normalized in excluded or _quality_rejection_reason(english, "中"):
        return None
    words = WORD_RE.findall(english)
    if len(words) > 12:
        return None
    excluded.add(normalized)
    numeric_id = re.sub(r"\D", "", source_id) or str(len(excluded))
    return ScenarioCandidate(english, "", normalized, source_id[:160], numeric_id[-18:], "", len(words))


def _append_sourced_candidate(
    output: dict[str, list[ClassifiedScenario]],
    *,
    pack_code: str,
    english: str,
    source_id: str,
    excluded: set[str],
) -> bool:
    candidate = _open_english_candidate(english, source_id, excluded)
    if candidate is None:
        return False
    score = _scores(candidate).get(pack_code, 0) + 4
    output[pack_code].append(ClassifiedScenario(candidate, pack_code, score, _quality_score(candidate)))
    return True


def _read_sgd_dialogues(archive: bytes, excluded: set[str]) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, int]]:
    output = {code: [] for code in SCENE_CODES}
    domain_map = {
        "Movies": "film-tv", "Media": "film-tv", "Messaging": "internet-social-media",
        "Flights": "travel", "Hotels": "travel", "Buses": "travel", "RentalCars": "travel",
        "RideSharing": "travel", "Travel": "travel", "Trains": "travel",
    }
    raw_count = kept_count = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        members = sorted(name for name in zipped.namelist() if re.search(r"/(?:train|dev|test)/dialogues_\d+\.json$", name))
        for member in members:
            for dialogue in json.loads(zipped.read(member)):
                domains = [service.split("_", 1)[0] for service in dialogue.get("services", [])]
                pack_code = next((domain_map[domain] for domain in domains if domain in domain_map), None)
                if pack_code is None:
                    continue
                for turn_index, turn in enumerate(dialogue.get("turns", [])):
                    raw_count += 1
                    if len(output[pack_code]) >= 2000:
                        continue
                    kept_count += _append_sourced_candidate(
                        output, pack_code=pack_code, english=str(turn.get("utterance") or ""),
                        source_id=f"sgd:{dialogue['dialogue_id']}:{turn_index}", excluded=excluded,
                    )
    return output, {"raw_relevant_utterance_count": raw_count, "clean_candidate_count": kept_count}


def _read_abcd_dialogues(archive: bytes, excluded: set[str]) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, int]]:
    output = {code: [] for code in SCENE_CODES}
    payload = json.loads(gzip.decompress(archive))
    online_flows = {"account_access", "troubleshoot_site", "manage_account"}
    raw_count = kept_count = 0
    for split in ("train", "dev", "test"):
        for dialogue in payload[split]:
            flow = str(dialogue["scenario"]["flow"])
            personal_values = {
                str(value).strip().lower() for value in dialogue["scenario"].get("personal", {}).values() if value
            }
            for turn_index, turn in enumerate(dialogue.get("original", [])):
                speaker, english = str(turn[0]), str(turn[1])
                raw_count += 1
                lowered = english.lower()
                if any(value and value in lowered for value in personal_values):
                    continue
                if speaker == "customer" and flow in online_flows:
                    pack_code = "internet-social-media"
                elif speaker == "agent":
                    pack_code = "workplace"
                else:
                    scores = _scores(ScenarioCandidate(english, "", "", "", "0", "", len(WORD_RE.findall(english))))
                    pack_code = "useful-sentences" if scores["useful-sentences"] >= 5 else "social-communication"
                if len(output[pack_code]) >= 2000:
                    continue
                kept_count += _append_sourced_candidate(
                    output, pack_code=pack_code, english=english,
                    source_id=f"abcd:{dialogue['convo_id']}:{turn_index}", excluded=excluded,
                )
    return output, {"raw_utterance_count": raw_count, "clean_candidate_count": kept_count}


def _read_clinc150(archive: bytes, excluded: set[str]) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, int]]:
    output = {code: [] for code in SCENE_CODES}
    intent_groups = {
        "internet-social-media": {
            "are_you_a_bot", "change_accent", "change_ai_name", "change_language", "change_speed",
            "change_user_name", "change_volume", "find_phone", "make_call", "reset_settings",
            "share_location", "smart_home", "sync_device", "text", "user_name", "what_can_i_ask_you",
        },
        "workplace": {
            "application_status", "calendar", "calendar_update", "direct_deposit", "income",
            "meeting_schedule", "payday", "pto_balance", "pto_request", "pto_request_status", "pto_used",
            "rollover_401k", "schedule_meeting", "taxes", "w2",
        },
        "travel": {
            "book_flight", "book_hotel", "car_rental", "carry_on", "current_location", "directions",
            "distance", "flight_status", "international_visa", "lost_luggage", "plug_type", "traffic",
            "travel_alert", "travel_notification", "travel_suggestion", "uber",
        },
        "social-communication": {"goodbye", "greeting", "maybe", "no", "thank_you", "yes"},
        "useful-sentences": {"cancel", "date", "reminder", "reminder_update", "repeat", "time", "timezone", "todo_list", "todo_list_update"},
    }
    intent_to_pack = {intent: code for code, intents in intent_groups.items() for intent in intents}
    raw_count = kept_count = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        payload = json.loads(zipped.read("clinc150_uci/data_full.json"))
    for split in ("train", "val", "test"):
        for row_index, (english, intent) in enumerate(payload[split]):
            pack_code = intent_to_pack.get(intent)
            if pack_code is None:
                continue
            raw_count += 1
            kept_count += _append_sourced_candidate(
                output, pack_code=pack_code, english=english,
                source_id=f"clinc150:{split}:{row_index}:{intent}", excluded=excluded,
            )
    return output, {"raw_relevant_utterance_count": raw_count, "clean_candidate_count": kept_count}


def _merge_sourced_options(parts: list[dict[str, list[ClassifiedScenario]]]) -> tuple[dict[str, list[ClassifiedScenario]], int]:
    output = {code: [] for code in SCENE_CODES}
    fingerprints: set[str] = set()
    removed = 0
    for code in SCENE_CODES:
        rows = [row for part in parts for row in part[code]]
        rows.sort(key=lambda row: (-(row.score * 2 + row.quality_score), row.candidate.word_count, row.candidate.source_id))
        for row in rows:
            fingerprint = _near_duplicate_fingerprint(row.candidate)
            if fingerprint in fingerprints:
                removed += 1
                continue
            fingerprints.add(fingerprint)
            output[code].append(row)
    return output, removed


def _cached_source_adapter(
    *,
    prefix: str,
    archive: bytes,
    cache_path: Path,
    adapter: Callable[[bytes, set[str]], tuple[dict[str, list[ClassifiedScenario]], dict[str, int]]],
    excluded: set[str],
) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, Any]]:
    if cache_path.is_file():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        raw_rows = payload["rows"]
        report: dict[str, Any] = dict(payload["report"])
        report["adapter_cache_hit"] = True
    else:
        produced, source_report = adapter(archive, set())
        raw_rows = {
            code: [{
                "english": row.candidate.english,
                "normalized": row.candidate.normalized,
                "source_id": row.candidate.source_id,
                "english_id": row.candidate.english_id,
                "word_count": row.candidate.word_count,
                "score": row.score,
                "quality_score": row.quality_score,
            } for row in rows]
            for code, rows in produced.items()
        }
        report = dict(source_report)
        report["adapter_cache_hit"] = False
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"report": source_report, "rows": raw_rows}, ensure_ascii=False) + "\n", encoding="utf-8")
    output = {code: [] for code in SCENE_CODES}
    duplicate_count = 0
    for code in SCENE_CODES:
        for item in raw_rows.get(code, []):
            normalized = str(item["normalized"])
            if normalized in excluded:
                duplicate_count += 1
                continue
            excluded.add(normalized)
            candidate = ScenarioCandidate(
                str(item["english"]), "", normalized, str(item["source_id"]),
                str(item["english_id"]), "", int(item["word_count"]),
            )
            output[code].append(ClassifiedScenario(
                candidate, code, int(item["score"]), int(item["quality_score"])
            ))
    report["excluded_duplicate_count"] = duplicate_count
    report["selected_from_adapter_cache"] = sum(len(rows) for rows in output.values())
    report["prefix"] = prefix
    return output, report


def _translate_supplements(
    classified: dict[str, list[ClassifiedScenario]],
    *,
    base: dict[str, list[ClassifiedScenario]],
    target_per_pack: int,
    max_items: int,
    cache_path: Path,
    translator: Translator,
    requests_per_second: float = 4.0,
    workers: int = 4,
) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, Any]]:
    # Raise the smallest packs first, which avoids spending the 20% translation
    # allowance on categories already close to the target.
    selected: list[ClassifiedScenario] = []
    offsets = {code: 0 for code in SCENE_CODES}
    counts = {code: len(base[code]) for code in SCENE_CODES}
    while len(selected) < max_items:
        available = [code for code in SCENE_CODES if counts[code] < target_per_pack and offsets[code] < len(classified[code])]
        if not available:
            break
        code = min(available, key=lambda item: (counts[item], SCENE_CODES.index(item)))
        row = classified[code][offsets[code]]
        offsets[code] += 1
        selected.append(ClassifiedScenario(row.candidate, code, row.score, row.quality_score, True))
        counts[code] += 1

    cache: dict[str, str] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    misses = [row for row in selected if row.candidate.source_id not in cache]
    if misses:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        interval = 1.0 / requests_per_second
        rate_lock = threading.Lock()
        next_request_at = [time.monotonic()]

        def translate_one(row: ClassifiedScenario) -> tuple[str, str]:
            for attempt in range(5):
                try:
                    with rate_lock:
                        scheduled = max(time.monotonic(), next_request_at[0])
                        next_request_at[0] = scheduled + interval
                    time.sleep(max(0.0, scheduled - time.monotonic()))
                    translated = _clean_text(translator(row.candidate.english))
                    if not CJK_RE.search(translated):
                        raise RuntimeError(f"translation has no Chinese text: {row.candidate.source_id}")
                    return row.candidate.source_id, translated
                except Exception as exc:
                    if "RequestLimitExceeded" not in str(exc) or attempt == 4:
                        raise
                    time.sleep(1.0 + attempt)
            raise RuntimeError(f"translation retry exhausted: {row.candidate.source_id}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(translate_one, row) for row in misses]
            for index, future in enumerate(as_completed(futures), start=1):
                source_id, translated = future.result()
                cache[source_id] = translated
                if index % 25 == 0 or index == len(misses):
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    output = {code: list(rows) for code, rows in base.items()}
    for row in selected:
        candidate = row.candidate
        translated = ScenarioCandidate(
            english=candidate.english, chinese=cache[candidate.source_id], normalized=candidate.normalized,
            source_id=candidate.source_id, english_id=candidate.english_id, chinese_id="", word_count=candidate.word_count,
        )
        output[row.pack_code].append(ClassifiedScenario(translated, row.pack_code, row.score, row.quality_score, True))
    return output, {"enabled": True, "provider": "tencent-tmt", "selected_count": len(selected), "fresh_translation_count": len(misses), "translated_by_pack": {code: offsets[code] for code in SCENE_CODES}}


def _keyword_score(lowered: str, terms: tuple[str, ...]) -> int:
    return sum(
        (3 if " " in term else 1 if term in WEAK_KEYWORDS else 2)
        for term in terms
        if _contains_term(lowered, term)
    )


def _scores(candidate: ScenarioCandidate) -> dict[str, int]:
    lowered = candidate.english.lower()
    scores = {code: _keyword_score(lowered, KEYWORDS.get(code, ())) for code in KEYWORDS}
    direct_count = sum(1 for pattern in DIRECT_PATTERNS if pattern in lowered)
    question = candidate.english.endswith("?")
    first_or_second_person = bool(re.search(r"\b(?:i|i'm|i'll|i'd|we|you|your)\b", lowered))
    contraction = bool(re.search(r"\b\w+'(?:m|re|ve|ll|d|s|t)\b", lowered))

    scores["natural-spoken"] = direct_count * 3 + int(question) * 2 + int(contraction) * 2 + int(first_or_second_person)
    common_formula = direct_count > 0 or contraction
    scores["common-phrases"] = (
        (7 - candidate.word_count if 2 <= candidate.word_count <= 6 and common_formula else 0)
        + direct_count * 2
        + int(contraction)
    )
    useful_count = sum(1 for pattern in USEFUL_PATTERNS if pattern in lowered)
    scores["useful-sentences"] = (
        (5 if 3 <= candidate.word_count <= 8 and useful_count else 0)
        + useful_count * 2
        + int(question)
    )
    if scores["daily-life"]:
        scores["daily-life"] += direct_count + int(first_or_second_person)
    if scores["social-communication"]:
        scores["social-communication"] += direct_count + int(first_or_second_person)
    return scores


def _quality_score(candidate: ScenarioCandidate) -> int:
    lowered = candidate.english.lower()
    score = 20 - abs(candidate.word_count - 6)
    score += 3 if candidate.english.endswith("?") else 0
    score += 5 if any(pattern in lowered for pattern in DIRECT_PATTERNS) else 0
    score += 2 if re.search(r"\b(?:i|we|you|your)\b", lowered) else 0
    score += 1 if re.search(r"\b\w+'(?:m|re|ve|ll|d|s|t)\b", lowered) else 0
    score -= 5 if re.match(r"^(?:he|she|they|many people|some people)\b", lowered) else 0
    return score


def _near_duplicate_fingerprint(candidate: ScenarioCandidate) -> str:
    tokens = [token.lower() for token in WORD_RE.findall(candidate.english)]
    aliases = {"television": "tv", "wi-fi": "wifi"}
    tokens = [aliases.get(token, token) for token in tokens]
    if len(tokens) > 3:
        tokens = [token for token in tokens if token not in {"please", "now", "really", "just"}]
    return " ".join(tokens)


def _classify_candidates(
    candidates: list[ScenarioCandidate], *, target_per_pack: int, reserve_per_pack: int
) -> tuple[dict[str, list[ClassifiedScenario]], int]:
    limit = target_per_pack + reserve_per_pack
    options: dict[str, list[ClassifiedScenario]] = {code: [] for code in SCENE_CODES}
    minimum_scores = {
        "workplace": 2, "travel": 2, "film-tv": 2, "campus-study": 2,
        "dining-shopping": 2, "internet-social-media": 2, "social-communication": 2,
        "daily-life": 2, "natural-spoken": 5, "common-phrases": 5, "useful-sentences": 5,
    }
    for candidate in candidates:
        scores = _scores(candidate)
        quality = _quality_score(candidate)
        for code, score in scores.items():
            if score >= minimum_scores[code]:
                options[code].append(ClassifiedScenario(candidate, code, score, quality))

    assigned: set[str] = set()
    assigned_fingerprints: set[str] = set()
    near_duplicate_count = 0
    result: dict[str, list[ClassifiedScenario]] = {code: [] for code in SCENE_CODES}
    for code in SPECIFIC_ASSIGNMENT_ORDER + BROAD_ASSIGNMENT_ORDER:
        ranked = sorted(
            options[code],
            key=lambda row: (-(row.score * 2 + row.quality_score), -row.score, row.candidate.word_count, int(row.candidate.english_id)),
        )
        for row in ranked:
            if row.candidate.normalized in assigned:
                continue
            fingerprint = _near_duplicate_fingerprint(row.candidate)
            if fingerprint in assigned_fingerprints:
                near_duplicate_count += 1
                continue
            result[code].append(row)
            assigned.add(row.candidate.normalized)
            assigned_fingerprints.add(fingerprint)
            if len(result[code]) >= limit:
                break
    return result, near_duplicate_count


def _ollama_reviewer(rows: list[dict[str, str]], model: str) -> list[dict[str, Any]]:
    schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "keep": {"type": "boolean"},
                        "category": {"type": "string", "enum": list(SCENE_CODES)},
                        "reason": {"type": "string", "maxLength": 48},
                    },
                    "required": ["id", "keep", "category", "reason"],
                },
            }
        },
        "required": ["decisions"],
    }
    prompt = (
        "You audit authentic bilingual English-learning examples. For every row, keep it only if the English is "
        "natural, self-contained, broadly useful, safe, and correctly assigned to the proposed category. "
        "Do not rewrite or generate text. Return one decision for every id. If another listed category is clearly "
        "better, return that category and keep=true. Reject obscure, context-dependent, dated, unnatural, or trivial rows. "
        "Keep every reason under six words.\n"
        + json.dumps(rows, ensure_ascii=False)
    )
    response = requests.post(
        f"{str(settings.ollama_base_url).rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0, "num_predict": 128},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=max(300, int(settings.ollama_timeout_seconds)),
    )
    response.raise_for_status()
    payload = response.json()
    parsed = json.loads(payload["message"]["content"])
    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        raise RuntimeError("Ollama scenario review returned no decisions")
    return decisions


def _ollama_chinese_reviewer(rows: list[dict[str, str]], model: str) -> list[dict[str, Any]]:
    model_rows = [{"id": str(index), "english": row["english"], "chinese": row["chinese"]} for index, row in enumerate(rows)]
    schema = {
        "type": "object",
        "properties": {
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "chinese": {"type": "string"},
                        "reason": {"type": "string", "maxLength": 48},
                    },
                    "required": ["id", "chinese", "reason"],
                },
            }
        },
        "required": ["corrections"],
    }
    prompt = (
        "You are the final Chinese translation quality auditor for English-learning material. "
        "Review every English/Chinese pair. Preserve a good Chinese translation exactly as supplied. "
        "Only include an item in corrections when its Chinese is clearly inaccurate, awkward, overly literal, "
        "or wrong for the conversational context. Never alter English, add explanations, or embellish meaning. "
        "Use concise natural Simplified Chinese. Return {corrections: []} when no change is needed. "
        "Never return unchanged items. Every corrected Chinese string must differ from its input. "
        "For example, English 'The website is down.' with Chinese '网站是向下的。' must be corrected to "
        "'网站宕机了。'. Keep reason under six Chinese words.\n"
        + json.dumps(model_rows, ensure_ascii=False)
    )
    response = requests.post(
        f"{str(settings.ollama_base_url).rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0, "num_predict": 4096},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=max(300, int(settings.ollama_timeout_seconds)),
    )
    response.raise_for_status()
    payload = json.loads(response.json()["message"]["content"])
    corrections = payload.get("corrections")
    if not isinstance(corrections, list):
        raise RuntimeError("Ollama Chinese review returned no corrections array")
    by_id = {str(item.get("id")): item for item in corrections if isinstance(item, dict)}
    expected = {row["id"] for row in model_rows}
    if not set(by_id).issubset(expected) or len(by_id) != len(corrections):
        raise RuntimeError("Ollama Chinese review returned unknown or duplicate ids")
    originals = {row["id"]: row["chinese"] for row in model_rows}
    by_id = {
        source_id: item for source_id, item in by_id.items()
        if _clean_text(str(item.get("chinese") or "")) != originals[source_id]
    }
    return [{
        "id": original["id"],
        "changed": model_row["id"] in by_id,
        "chinese": str(by_id[model_row["id"]].get("chinese") or "") if model_row["id"] in by_id else original["chinese"],
        "reason": str(by_id[model_row["id"]].get("reason") or "") if model_row["id"] in by_id else "保持原译",
    } for original, model_row in zip(rows, model_rows)]


def _chinese_review_cache_key(
    row: ClassifiedScenario, *, model: str, prompt_version: str
) -> str:
    candidate = row.candidate
    raw = f"{prompt_version}|{model}|{candidate.source_id}|{candidate.english}|{candidate.chinese}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _apply_chinese_quality_review(
    classified: dict[str, list[ClassifiedScenario]],
    *,
    translated_prefixes: set[str],
    model: str,
    prompt_version: str,
    batch_size: int,
    cache_path: Path,
    reviewer: ChineseReviewer,
) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    selected = [
        row for rows in classified.values() for row in rows
        if row.candidate.source_id.split(":", 1)[0] in translated_prefixes
    ]
    misses = [
        row for row in selected
        if cache.get(_chinese_review_cache_key(row, model=model, prompt_version=prompt_version), {}).get("status") != "success"
    ]
    fresh_reviewed = 0
    fresh_failures = 0
    cache_hits = len(selected) - len(misses)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    for offset in range(0, len(misses), batch_size):
        batch = misses[offset:offset + batch_size]
        request_rows = [{
            "id": row.candidate.source_id,
            "english": row.candidate.english,
            "chinese": row.candidate.chinese,
        } for row in batch]
        try:
            decisions = reviewer(request_rows, model)
            by_id = {str(item.get("id")): item for item in decisions if isinstance(item, dict)}
            expected_ids = {row.candidate.source_id for row in batch}
            if set(by_id) != expected_ids:
                raise RuntimeError("Chinese review did not return exactly one result for every source row")
            for row in batch:
                decision = by_id[row.candidate.source_id]
                changed = decision.get("changed")
                reviewed_chinese = _clean_text(str(decision.get("chinese") or ""))
                if not isinstance(changed, bool) or not CJK_RE.search(reviewed_chinese):
                    raise RuntimeError(f"invalid Chinese review result: {decision}")
                if not changed and reviewed_chinese != row.candidate.chinese:
                    raise RuntimeError("unchanged Chinese review did not preserve the original translation")
                key = _chinese_review_cache_key(row, model=model, prompt_version=prompt_version)
                cache[key] = {
                    "status": "success",
                    "source_id": row.candidate.source_id,
                    "changed": changed,
                    "chinese": reviewed_chinese,
                    "reason": str(decision.get("reason") or "")[:160],
                }
                fresh_reviewed += 1
        except Exception as exc:
            for row in batch:
                key = _chinese_review_cache_key(row, model=model, prompt_version=prompt_version)
                cache[key] = {
                    "status": "failed",
                    "source_id": row.candidate.source_id,
                    "error": str(exc)[:300],
                }
                fresh_failures += 1
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    output: dict[str, list[ClassifiedScenario]] = {}
    modified_count = reviewed_count = failure_count = 0
    for code, rows in classified.items():
        reviewed_rows: list[ClassifiedScenario] = []
        for row in rows:
            prefix = row.candidate.source_id.split(":", 1)[0]
            if prefix not in translated_prefixes:
                reviewed_rows.append(row)
                continue
            entry = cache.get(_chinese_review_cache_key(row, model=model, prompt_version=prompt_version), {})
            if entry.get("status") != "success":
                failure_count += 1
                reviewed_rows.append(row)
                continue
            reviewed_count += 1
            modified_count += int(bool(entry["changed"]))
            candidate = row.candidate
            reviewed_candidate = ScenarioCandidate(
                candidate.english, str(entry["chinese"]), candidate.normalized, candidate.source_id,
                candidate.english_id, candidate.chinese_id, candidate.word_count,
            )
            reviewed_rows.append(ClassifiedScenario(
                reviewed_candidate, row.pack_code, row.score, row.quality_score, row.ai_reviewed
            ))
        output[code] = reviewed_rows
    return output, {
        "enabled": True,
        "model": model,
        "prompt_version": prompt_version,
        "batch_size": batch_size,
        "eligible_count": len(selected),
        "reviewed_count": reviewed_count,
        "modified_count": modified_count,
        "failure_count": failure_count,
        "cache_hit_count": cache_hits,
        "fresh_reviewed_count": fresh_reviewed,
        "fresh_failure_count": fresh_failures,
    }


def _review_cache_key(row: ClassifiedScenario, *, model: str, prompt_version: str) -> str:
    raw = f"{prompt_version}|{model}|{row.pack_code}|{row.candidate.source_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _apply_ai_review(
    classified: dict[str, list[ClassifiedScenario]],
    *,
    target_per_pack: int,
    items_per_pack: int,
    batch_size: int,
    model: str,
    prompt_version: str,
    cache_path: Path,
    reviewer: AiReviewer,
) -> tuple[dict[str, list[ClassifiedScenario]], dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    selected: list[ClassifiedScenario] = []
    for rows in classified.values():
        review_end = min(target_per_pack, len(rows))
        review_start = max(0, review_end - items_per_pack)
        selected.extend(rows[review_start:review_end])

    misses: list[ClassifiedScenario] = []
    for row in selected:
        if _review_cache_key(row, model=model, prompt_version=prompt_version) not in cache:
            misses.append(row)

    for offset in range(0, len(misses), batch_size):
        batch = misses[offset:offset + batch_size]
        request_rows = [
            {
                "id": row.candidate.source_id,
                "english": row.candidate.english,
                "chinese": row.candidate.chinese,
                "proposed_category": row.pack_code,
            }
            for row in batch
        ]
        decisions = reviewer(request_rows, model)
        by_id = {str(item.get("id")): item for item in decisions if isinstance(item, dict)}
        expected_ids = {row.candidate.source_id for row in batch}
        if len(batch) == 1 and len(decisions) == 1 and isinstance(decisions[0], dict):
            by_id = {batch[0].candidate.source_id: decisions[0]}
        if set(by_id) != expected_ids:
            raise RuntimeError("AI review did not return exactly one decision for every source row")
        for row in batch:
            decision = by_id[row.candidate.source_id]
            category = str(decision.get("category") or "")
            if category not in SCENE_CODES or not isinstance(decision.get("keep"), bool):
                raise RuntimeError(f"invalid AI review decision: {decision}")
            cache[_review_cache_key(row, model=model, prompt_version=prompt_version)] = {
                "keep": decision["keep"],
                "category": category,
                "reason": str(decision.get("reason") or "")[:160],
            }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    reviewed_keys = {
        _review_cache_key(row, model=model, prompt_version=prompt_version)
        for row in selected
    }
    accepted = 0
    rejected = 0
    category_changes = 0
    reviewed_by_pack = {code: 0 for code in SCENE_CODES}
    output: dict[str, list[ClassifiedScenario]] = {}
    for code, rows in classified.items():
        kept: list[ClassifiedScenario] = []
        for row in rows:
            key = _review_cache_key(row, model=model, prompt_version=prompt_version)
            if key not in reviewed_keys:
                kept.append(row)
                continue
            reviewed_by_pack[code] += 1
            decision = cache[key]
            if decision["keep"] and decision["category"] == code:
                kept.append(ClassifiedScenario(row.candidate, code, row.score, row.quality_score, True))
                accepted += 1
            else:
                rejected += 1
                category_changes += int(bool(decision["keep"]) and decision["category"] != code)
        output[code] = kept[:target_per_pack]

    return output, {
        "enabled": True,
        "model": model,
        "prompt_version": prompt_version,
        "reviewed_count": len(selected),
        "fresh_review_count": len(misses),
        "accepted_count": accepted,
        "rejected_count": rejected,
        "category_change_count": category_changes,
        "reviewed_by_pack": reviewed_by_pack,
        "generated_item_count": 0,
    }


def _build_imports(
    classified: dict[str, list[ClassifiedScenario]],
    *,
    source: dict[str, Any],
    english_cc0_source: dict[str, Any] | None,
    supplement_sources: list[dict[str, Any]],
    production_batch: str,
) -> tuple[list[PublicMaterialPackImport], dict[str, list[PublicMaterialItemImport]]]:
    pack_definitions = {row[0]: row for row in PACKS}
    translated_sources = {row["prefix"]: row for row in supplement_sources}
    packs: list[PublicMaterialPackImport] = []
    items_by_pack: dict[str, list[PublicMaterialItemImport]] = {}
    for code in SCENE_CODES:
        _, title, description, kind, sort_order = pack_definitions[code]
        packs.append(PublicMaterialPackImport(code, title, description, kind, sort_order, production_batch))
        items: list[PublicMaterialItemImport] = []
        for rank, row in enumerate(classified[code], start=1):
            candidate = row.candidate
            card_type = "phrase" if candidate.word_count <= 6 and not candidate.english.endswith("?") else "sentence"
            is_ai_translation = candidate.source_id.startswith("cc0:")
            item_source = "tatoeba"
            item_license = source["license"]
            if is_ai_translation:
                if english_cc0_source is None:
                    raise RuntimeError("CC0 source metadata missing")
                item_source = "tatoeba-cc0-ai-translation"
                item_license = english_cc0_source["license"]
                review = (
                    f"Tatoeba CC0 {english_cc0_source['revision']}; {SCENARIO_RULES_VERSION}; "
                    f"ai-translated={english_cc0_source['translation_provider']}"
                )
            elif candidate.source_id.split(":", 1)[0] in translated_sources:
                translated_source = translated_sources[candidate.source_id.split(":", 1)[0]]
                item_source = translated_source["source_value"]
                item_license = translated_source["license"]
                review = (
                    f"{translated_source['name']} {translated_source['revision']}; {SCENARIO_RULES_VERSION}; "
                    f"ai-translated={translated_source['translation_provider']}"
                )
            elif row.ai_reviewed:
                review = f"ManyThings {source['revision']}; {SCENARIO_RULES_VERSION}; ai-reviewed={source['ai_model']}"
            else:
                review = f"ManyThings {source['revision']}; {SCENARIO_RULES_VERSION}; ai=not-reviewed"
            items.append(PublicMaterialItemImport(
                content=candidate.english,
                chinese=candidate.chinese,
                card_type=card_type,
                source_label=title,
                source=item_source,
                source_id=candidate.source_id,
                license=item_license,
                corpus_rank=rank,
                corpus_frequency=None,
                production_batch=production_batch,
                review_note=review,
            ))
        items_by_pack[code] = items
    return packs, items_by_pack


def produce_scenario_materials(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh_source: bool = False,
    offline: bool = False,
    ai_review: bool = True,
    ai_reviewer: AiReviewer | None = None,
    ai_translate: bool = True,
    translator: Translator | None = None,
    chinese_review: bool = True,
    chinese_reviewer: ChineseReviewer | None = None,
) -> ProducedScenarioMaterials:
    manifest = load_source_manifest(manifest_path)
    source = dict(manifest["source"])
    production_batch = str(manifest["production_batch"])
    target_per_pack = int(manifest["target_per_pack"])
    if tuple(manifest["packs"]) != SCENE_CODES:
        raise RuntimeError("scenario source manifest does not match the configured scene packs")

    archive = _download_source(source, cache_dir, refresh=refresh_source, offline=offline)
    candidates, cleaning_report = _read_tatoeba_pairs(archive, source)
    ai_spec = manifest["ai_review"]
    reserve = int(ai_spec["items_per_pack"]) if ai_review else 0
    classified, near_duplicate_count = _classify_candidates(
        candidates, target_per_pack=target_per_pack, reserve_per_pack=reserve
    )
    ai_report: dict[str, Any] = {
        "enabled": False,
        "reviewed_count": 0,
        "generated_item_count": 0,
    }
    if ai_review:
        source["ai_model"] = str(ai_spec["model"])
        classified, ai_report = _apply_ai_review(
            classified,
            target_per_pack=target_per_pack,
            items_per_pack=int(ai_spec["items_per_pack"]),
            batch_size=int(ai_spec["batch_size"]),
            model=str(ai_spec["model"]),
            prompt_version=str(ai_spec["prompt_version"]),
            cache_path=cache_dir / "ai-review.json",
            reviewer=ai_reviewer or _ollama_reviewer,
        )

    translation_report: dict[str, Any] = {"enabled": False, "selected_count": 0, "fresh_translation_count": 0}
    english_cc0_source = dict(manifest["english_cc0_source"])
    supplement_sources = [dict(row) for row in manifest.get("supplement_sources", [])]
    if ai_translate:
        cc0_archive = _download_named_source(
            english_cc0_source, cache_dir, "eng_sentences_CC0.tsv.bz2", refresh=refresh_source, offline=offline
        )
        excluded = {row.candidate.normalized for rows in classified.values() for row in rows}
        english_candidates, english_report = _read_cc0_english(cc0_archive, excluded)
        supplemental, supplemental_near_duplicates = _classify_candidates(
            english_candidates, target_per_pack=target_per_pack, reserve_per_pack=0
        )
        if translator is None:
            from app.providers.tencent_translator import TencentTranslator
            translator = TencentTranslator().translate_to_zh
        classified, translation_report = _translate_supplements(
            supplemental,
            base=classified,
            target_per_pack=target_per_pack,
            max_items=int(english_cc0_source["max_translated_items"]),
            cache_path=cache_dir / "ai-translations.json",
            translator=translator,
        )
        translation_report["source_cleaning"] = english_report
        translation_report["near_duplicate_variant_removed"] = supplemental_near_duplicates

        external_parts: list[dict[str, list[ClassifiedScenario]]] = []
        external_reports: dict[str, Any] = {}
        excluded = {row.candidate.normalized for rows in classified.values() for row in rows}
        adapters = {
            "sgd": _read_sgd_dialogues,
            "abcd": _read_abcd_dialogues,
            "clinc150": _read_clinc150,
        }
        for supplement_source in supplement_sources:
            prefix = str(supplement_source["prefix"])
            archive = _download_named_source(
                supplement_source, cache_dir, str(supplement_source["filename"]),
                refresh=refresh_source, offline=offline,
            )
            adapter_cache_path = cache_dir / (
                f"adapter-{prefix}-{str(supplement_source['sha256'])[:12]}-{SCENARIO_RULES_VERSION}.json"
            )
            part, source_report = _cached_source_adapter(
                prefix=prefix,
                archive=archive,
                cache_path=adapter_cache_path,
                adapter=adapters[prefix],
                excluded=excluded,
            )
            external_parts.append(part)
            external_reports[prefix] = source_report
        external_options, external_near_duplicates = _merge_sourced_options(external_parts)
        classified, external_translation_report = _translate_supplements(
            external_options,
            base=classified,
            target_per_pack=target_per_pack,
            max_items=target_per_pack * len(SCENE_CODES),
            cache_path=cache_dir / "ai-translations.json",
            translator=translator,
        )
        external_translation_report["sources"] = external_reports
        external_translation_report["near_duplicate_variant_removed"] = external_near_duplicates
        translation_report["supplement_phase"] = external_translation_report
        translation_report["selected_count"] += external_translation_report["selected_count"]
        translation_report["fresh_translation_count"] += external_translation_report["fresh_translation_count"]

    chinese_review_report: dict[str, Any] = {
        "enabled": False, "eligible_count": 0, "reviewed_count": 0,
        "modified_count": 0, "failure_count": 0,
    }
    if chinese_review and ai_translate:
        review_spec = manifest["chinese_review"]
        translated_prefixes = {"cc0", *(str(row["prefix"]) for row in supplement_sources)}
        classified, chinese_review_report = _apply_chinese_quality_review(
            classified,
            translated_prefixes=translated_prefixes,
            model=str(review_spec["model"]),
            prompt_version=str(review_spec["prompt_version"]),
            batch_size=int(review_spec["batch_size"]),
            cache_path=cache_dir / "ai-chinese-review.json",
            reviewer=chinese_reviewer or _ollama_chinese_reviewer,
        )

    packs, items_by_pack = _build_imports(
        classified, source=source, english_cc0_source=english_cc0_source,
        supplement_sources=supplement_sources, production_batch=production_batch
    )
    normalized = [normalize_card_content(item.content) for items in items_by_pack.values() for item in items]
    if len(normalized) != len(set(normalized)):
        raise RuntimeError("scenario pipeline left cross-pack duplicates")
    if any(not item.chinese.strip() or not item.source_id or not item.license for items in items_by_pack.values() for item in items):
        raise RuntimeError("scenario pipeline left invalid source or translation metadata")

    pack_report = {
        code: {
            "count": len(items),
            "target": target_per_pack,
            "gap": max(0, target_per_pack - len(items)),
            "phrase_count": sum(item.card_type == "phrase" for item in items),
            "sentence_count": sum(item.card_type == "sentence" for item in items),
            "ai_reviewed_count": sum("ai-reviewed=" in str(item.review_note) for item in items),
            "source_distribution": {
                source_name: sum(item.source == source_name for item in items)
                for source_name in sorted({item.source for item in items})
            },
            "license_distribution": {
                license_name: sum(item.license == license_name for item in items)
                for license_name in sorted({item.license for item in items})
            },
            "top_items": [item.content for item in items[:10]],
        }
        for code, items in items_by_pack.items()
    }
    report = {
        "production_batch": production_batch,
        "source": source,
        "supplement_sources": supplement_sources,
        "source_sha256_verified": True,
        "cleaning": cleaning_report,
        "ai_review": ai_report,
        "ai_translation": translation_report,
        "chinese_quality_review": chinese_review_report,
        "quality": {
            "final_item_count": len(normalized),
            "within_pack_duplicate_count": 0,
            "cross_pack_duplicate_count": 0,
            "near_duplicate_variant_removed": near_duplicate_count,
            "empty_translation_count": 0,
            "unattributed_item_count": 0,
            "ai_generated_item_count": 0,
            "ai_translated_item_count": sum(
                "ai-translated=" in str(item.review_note) for items in items_by_pack.values() for item in items
            ),
        },
        "packs": pack_report,
    }
    return ProducedScenarioMaterials(production_batch, packs, items_by_pack, report)
