"""Pure utilities with no network or legacy-system dependencies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "from",
    "source",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(*parts: object, prefix: str = "") -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonicalize_url(url: str, *, keep_query: bool = False) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL 不能为空")
    raw = url.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"不是有效的 HTTP(S) URL: {url}")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = ""
    if keep_query:
        pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_QUERY_KEYS
        ]
        query = urlencode(sorted(pairs))
    return urlunsplit(("https", netloc, path, query, ""))


def extract_urls(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    candidates = re.findall(r"https?://[^\s<>\]\[\"']+", text)
    return [candidate.rstrip(".,;:!?，。；：！？)") for candidate in candidates]


def item_identity(snapshot) -> tuple[str, str]:
    if snapshot.item_id:
        return f"native:{snapshot.item_id}", "native_item_id"
    if snapshot.item_url:
        return f"url:{canonicalize_url(snapshot.item_url, keep_query=True)}", "canonical_url"
    if snapshot.title and snapshot.image_url:
        fallback = stable_hash(
            snapshot.platform,
            snapshot.seller_key,
            " ".join(snapshot.title.lower().split()),
            canonicalize_url(snapshot.image_url, keep_query=False),
        )
        return f"fallback:{fallback}", "fallback_title_image"
    raise ValueError("商品缺少 item_id、item_url，且无法构造可靠 fallback identity")


def event_key(
    platform: str,
    identity_key: str,
    event_type: str,
    *,
    new_price: Optional[int] = None,
    term_type: Optional[str] = None,
) -> str:
    parts = [platform, identity_key, event_type]
    if term_type is not None:
        parts.append(term_type)
    if new_price is not None:
        parts.append(str(new_price))
    return stable_hash(*parts, prefix="evt_")


def atomic_write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
