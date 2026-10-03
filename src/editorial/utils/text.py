from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import re
from typing import Iterable
import unicodedata


MULTISPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"@\w+")
PUNCT_RE = re.compile(r"[^\w\s]")

TAG_RULES: dict[str, tuple[str, ...]] = {
    "study": ("сесс", "экзам", "зачет", "препод", "лекц", "лаба"),
    "relationships": ("парень", "девуш", "отношен", "любов", "бывш"),
    "dorm": ("общага", "общежит", "коменда"),
    "money": ("деньг", "стипенд", "работ", "зарплат"),
    "social": ("друз", "компан", "тусов", "вечерин"),
    "question": ("кто", "как", "что делать", "посоветуйте", "подскажите"),
}


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = MULTISPACE_RE.sub(" ", text).strip()
    return text


def normalize_text(text: str | None) -> str:
    cleaned = clean_text(text).lower()
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = MENTION_RE.sub(" ", cleaned)
    cleaned = PUNCT_RE.sub(" ", cleaned)
    cleaned = MULTISPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def compute_text_hash(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_raw_text_hash(text: str | None) -> str | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def detect_tags(text: str | None) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    tags: list[str] = []
    for tag, markers in TAG_RULES.items():
        if any(marker in normalized for marker in markers):
            tags.append(tag)
    return tags


def detect_language_code(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    if re.search(r"[а-яё]", normalized):
        return "ru"
    if re.search(r"[a-z]", normalized):
        return "en"
    return None


def similarity_score(left: str | None, right: str | None) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(a=left_normalized, b=right_normalized).ratio()


def pick_primary_tag(tags: Iterable[str]) -> str | None:
    for tag in tags:
        return tag
    return None


def compute_moderation_hash(text: str | None) -> str | None:
    """Punctuation-insensitive identity that retains URLs and mentions."""
    normalized = unicodedata.normalize("NFC", text or "").lower()
    normalized = re.sub(r"[\W_]+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None
