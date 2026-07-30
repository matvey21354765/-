from __future__ import annotations

import hashlib
import re


def extract_source_id(canonical_url: str) -> str:
    path = canonical_url.split("?", 1)[0].rstrip("/")
    match = re.search(r"_(\d{6,})$", path)
    if not match:
        match = re.search(r"/(\d{6,})(?:/|$)", path)
    if match:
        return match.group(1)
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def parse_int(text: str | None) -> int | None:
    digits = re.sub(r"\D+", "", text or "")
    return int(digits) if digits else None


def normalize_photo_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith("https://") else None
