"""One-shot development capture for a Mercari seller profile.

This script is deliberately not imported by the seller monitor. It performs one
page navigation, listens to naturally generated responses, stores redacted raw
diagnostics outside the repository, and writes a fixture only when a reliable
JSON item-list response is found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Request, Response, sync_playwright


PROFILE_RE = re.compile(r"^https://jp\.mercari\.com/user/profile/(?P<seller_id>[0-9]+)$")
ITEM_ID_RE = re.compile(r"\bm[0-9]{5,}\b", re.IGNORECASE)
GET_ITEMS_PATH = "/items/get_items"
PAGINATION_QUERY_RE = re.compile(r"page|offset|cursor|pager", re.IGNORECASE)
SAFE_QUERY_KEYS = {
    "limit",
    "status",
    "with_auction",
    "exclude_archived_item",
    "page",
    "offset",
    "cursor",
    "pager_id",
}
CAPTURE_STAGES = (
    "browser_started",
    "profile_loaded",
    "initial_items_captured",
    "filter_clicked",
    "filtered_items_captured",
    "pagination_trigger_attempted",
    "next_page_captured",
    "completed",
    "failed",
)
OUTPUT_FORBIDDEN_KEY_RE = re.compile(
    r"cookie|authorization|set[_-]?cookie|dpop|token|localstorage|sessionstorage",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
SECRET_KEY_RE = re.compile(
    r"cookie|authorization|token|secret|csrf|session|credential|trace[_-]?id|"
    r"tracking|experiment|variant|amplitude|fingerprint",
    re.IGNORECASE,
)
PERSONAL_KEY_RE = re.compile(
    r"nickname|user_?name|display_?name|avatar|biography|introduction|profile_?image",
    re.IGNORECASE,
)
IMAGE_KEY_RE = re.compile(r"image|photo|thumbnail|picture", re.IGNORECASE)
PRICE_KEY_RE = re.compile(
    r"price|amount|current_?bid|start(?:ing)?_?price|buyout|bid_?amount",
    re.IGNORECASE,
)
PAGINATION_KEY_RE = re.compile(r"cursor|page_?info|has_?next|next_?page|total_?count", re.IGNORECASE)
AUCTION_KEY_RE = re.compile(r"auction|bid|buyout|bidding", re.IGNORECASE)
STATUS_KEY_RE = re.compile(r"status|sold|on_?sale|available", re.IGNORECASE)
TITLE_KEY_RE = re.compile(r"^(?:title|name|item_?name|product_?name)$", re.IGNORECASE)
ITEM_ID_KEY_RE = re.compile(r"^(?:item_?id|product_?id|listing_?id|id)$", re.IGNORECASE)
SELLER_ID_KEY_RE = re.compile(r"(?:seller|user|owner).*id|id.*(?:seller|user|owner)", re.IGNORECASE)
ANALYTICS_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "adservice",
    "analytics",
    "amplitude",
    "newrelic",
    "sentry.io",
    "clarity.ms",
    "facebook.net",
)
STANDARD_HEADER_NAMES = {
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "connection",
    "content-length",
    "content-type",
    "host",
    "origin",
    "pragma",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "user-agent",
}


@dataclass
class Candidate:
    url: str
    status: int
    method: str
    resource_type: str
    content_type: str
    score: int
    features: dict[str, Any]
    dependency_flags: dict[str, Any]
    parsed_json: Any | None = None
    text: str | None = None
    error: str | None = None

    @property
    def reliable_item_list(self) -> bool:
        return bool(
            self.parsed_json is not None
            and self.score >= 45
            and (
                self.features.get("item_like_object_count", 0) >= 2
                or self.features.get("mercari_item_id_count", 0) >= 2
            )
        )


@dataclass
class CaptureState:
    target_url: str
    seller_id: str
    request_counts: Counter = field(default_factory=Counter)
    response_counts: Counter = field(default_factory=Counter)
    candidates: list[Candidate] = field(default_factory=list)
    blocked_counts: Counter = field(default_factory=Counter)
    status_403_count: int = 0
    status_429_count: int = 0
    document_status: int | None = None
    document_navigation_count: int = 0
    browser_name: str | None = None
    load_seconds: float | None = None
    has_login_prompt: bool = False
    login_wall_detected: bool = False
    captcha_detected: bool = False
    item_cell_count: int = 0


@dataclass
class GetItemsObservation:
    phase: str
    request_url: str
    method: str
    request_sequence: int
    status: int | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    requested_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    responded_at: str | None = None


def _safe_url(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    query_names = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    redacted_query = "&".join(f"{key}=<redacted>" for key in query_names)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, "")), query_names


def _is_get_items_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname == "api.mercari.jp" and parsed.path == GET_ITEMS_PATH


def _query_multimap(url: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        result.setdefault(key, []).append(value)
    return result


def _item_payloads(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    return [item for item in payload["data"] if isinstance(item, dict)]


def _status_distribution(payload: dict[str, Any] | None) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                str(item.get("status", "<missing>"))
                for item in _item_payloads(payload)
            ).items()
        )
    )


def _meta_has_next(payload: dict[str, Any] | None) -> bool | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return None
    value = payload["meta"].get("has_next")
    return value if isinstance(value, bool) else None


def _safe_query_snapshot(
    url: str,
    seller_id: str,
    sanitizer: "FixtureSanitizer",
    opaque_values: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Keep only permitted query semantics; never persist credentials or raw IDs."""

    result: dict[str, Any] = {}
    for key, values in _query_multimap(url).items():
        if SECRET_KEY_RE.search(key) or OUTPUT_FORBIDDEN_KEY_RE.search(key):
            continue
        if key != "seller_id" and key not in SAFE_QUERY_KEYS and not PAGINATION_QUERY_RE.search(key):
            continue
        safe_values: list[str] = []
        for value in values:
            if key == "seller_id" or value == seller_id:
                safe = "example_seller_id"
            elif value in sanitizer.item_map:
                safe = sanitizer.item_map[value]
            elif key in SAFE_QUERY_KEYS or PAGINATION_QUERY_RE.search(key):
                if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
                    safe = value
                elif re.fullmatch(r"(?i:true|false|on_sale|sold_out|trading|all|active)", value):
                    safe = value
                elif re.fullmatch(r"[A-Za-z_-]{1,40}", value):
                    safe = value
                else:
                    token = (key, value)
                    if token not in opaque_values:
                        opaque_values[token] = f"example_{key}_{len(opaque_values) + 1}"
                    safe = opaque_values[token]
            else:
                safe = "<redacted>"
            safe_values.append(safe)
        result[key] = safe_values[0] if len(safe_values) == 1 else safe_values
    return result


def _atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    forbidden_values: set[str] | None = None,
) -> None:
    """Atomically write UTF-8 JSON, verify readability, then scan the result."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    forbidden = forbidden_values or set()
    _assert_sanitized_output(payload, forbidden)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        parsed = json.loads(output.read_text(encoding="utf-8"))
        _assert_sanitized_output(parsed, forbidden)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_sanitized_output(payload: Any, forbidden_values: set[str]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    forbidden_hits = sorted(
        value for value in forbidden_values if value and value in serialized
    )
    if forbidden_hits:
        raise ValueError(f"sanitized output contains {len(forbidden_hits)} forbidden value(s)")
    if re.search(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.", serialized):
        raise ValueError("sanitized output contains a JWT-like value")
    forbidden_keys = [
        key
        for obj in _walk(payload)
        for key in obj
        if OUTPUT_FORBIDDEN_KEY_RE.search(str(key))
    ]
    if forbidden_keys:
        raise ValueError(f"sanitized output contains forbidden key(s): {sorted(set(forbidden_keys))}")


def _sanitize_error_text(value: str, seller_id: str, item_map: dict[str, str]) -> str:
    result = _scrub_secret_string(str(value)).replace(seller_id, "example_seller_id")
    for item_id in ITEM_ID_RE.findall(result):
        replacement = item_map.get(item_id)
        if replacement is None:
            replacement = f"m{9_100_000_000 + len(item_map) + 1}"
            item_map[item_id] = replacement
        result = result.replace(item_id, replacement)
    return result


@dataclass
class RequestCounters:
    navigation_count: int = 0
    document_request_count: int = 0
    script_request_count: int = 0
    stylesheet_request_count: int = 0
    fetch_request_count: int = 0
    xhr_request_count: int = 0
    other_request_count: int = 0
    get_items_request_count: int = 0
    get_items_response_count: int = 0
    blocked_request_count: int = 0
    response_count: int = 0
    http_403_count: int = 0
    http_429_count: int = 0

    def record_request(self, resource_type: str, *, get_items: bool = False) -> None:
        field_name = {
            "document": "document_request_count",
            "script": "script_request_count",
            "stylesheet": "stylesheet_request_count",
            "fetch": "fetch_request_count",
            "xhr": "xhr_request_count",
        }.get(resource_type, "other_request_count")
        setattr(self, field_name, getattr(self, field_name) + 1)
        if get_items:
            self.get_items_request_count += 1

    def record_response(self, status: int, *, get_items: bool = False) -> None:
        self.response_count += 1
        if get_items:
            self.get_items_response_count += 1
        if status == 403:
            self.http_403_count += 1
        if status == 429:
            self.http_429_count += 1

    def as_dict(self) -> dict[str, int]:
        values = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }
        values["request_count"] = sum(
            values[name]
            for name in (
                "document_request_count",
                "script_request_count",
                "stylesheet_request_count",
                "fetch_request_count",
                "xhr_request_count",
                "other_request_count",
            )
        )
        return values


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _all_keys(value: Any) -> list[str]:
    keys: list[str] = []
    for obj in _walk(value):
        keys.extend(str(key) for key in obj)
    return keys


def _item_like_objects(value: Any) -> list[dict]:
    matches = []
    for obj in _walk(value):
        keys = [str(key) for key in obj]
        has_id = any(ITEM_ID_KEY_RE.search(key) for key in keys)
        has_title = any(TITLE_KEY_RE.search(key) for key in keys)
        has_price = any(PRICE_KEY_RE.search(key) for key in keys)
        has_image = any(IMAGE_KEY_RE.search(key) for key in keys)
        if has_id and sum((has_title, has_price, has_image)) >= 2:
            matches.append(obj)
    return matches


def _feature_scan(parsed_json: Any | None, text: str) -> tuple[int, dict[str, Any]]:
    item_ids = sorted(set(ITEM_ID_RE.findall(text)))
    lowered = text.lower()
    if parsed_json is not None:
        keys = _all_keys(parsed_json)
        item_objects = _item_like_objects(parsed_json)
    else:
        keys = []
        item_objects = []
    normalized_keys = [re.sub(r"[^a-z0-9]", "", key.lower()) for key in keys]

    def key_or_text(pattern: re.Pattern, words: tuple[str, ...]) -> bool:
        return any(pattern.search(key) for key in keys) or any(word in lowered for word in words)

    features = {
        "item_like_object_count": len(item_objects),
        "mercari_item_id_count": len(item_ids),
        "has_multiple_items": len(item_objects) >= 2 or len(item_ids) >= 2,
        "has_title_field": key_or_text(TITLE_KEY_RE, ('"title"', '"name"')),
        "has_price_field": key_or_text(PRICE_KEY_RE, ('"price"', '"amount"')),
        "has_image_field": key_or_text(IMAGE_KEY_RE, ('"image"', '"thumbnail"', '"photo"')),
        "has_seller_or_user_id": any(SELLER_ID_KEY_RE.search(key) for key in keys),
        "has_status_or_sold": key_or_text(STATUS_KEY_RE, ('"status"', '"sold"', 'on_sale')),
        "has_pagination": key_or_text(PAGINATION_KEY_RE, ('cursor', 'hasnextpage', 'pageinfo')),
        "has_auction_fields": key_or_text(AUCTION_KEY_RE, ('auction', 'buyout', 'currentbid')),
        "pagination_keys": sorted(
            {key for key in keys if PAGINATION_KEY_RE.search(key)}
        )[:30],
        "auction_keys": sorted({key for key in keys if AUCTION_KEY_RE.search(key)})[:30],
        "status_keys": sorted({key for key in keys if STATUS_KEY_RE.search(key)})[:30],
        "top_level_type": type(parsed_json).__name__ if parsed_json is not None else "text",
        "top_level_keys": list(parsed_json)[:50] if isinstance(parsed_json, dict) else [],
        "normalized_key_count": len(set(normalized_keys)),
    }
    score = 0
    score += 30 if features["item_like_object_count"] >= 2 else 0
    score += 20 if features["mercari_item_id_count"] >= 2 else 0
    score += 8 if features["has_title_field"] else 0
    score += 8 if features["has_price_field"] else 0
    score += 8 if features["has_image_field"] else 0
    score += 5 if features["has_seller_or_user_id"] else 0
    score += 5 if features["has_status_or_sold"] else 0
    score += 8 if features["has_pagination"] else 0
    score += 8 if features["has_auction_fields"] else 0
    return score, features


def _dependency_flags(request: Request) -> dict[str, Any]:
    try:
        headers = request.all_headers()
    except PlaywrightError:
        headers = {}
    lower_headers = {str(key).lower(): value for key, value in headers.items()}
    _, query_names = _safe_url(request.url)
    sensitive_query_names = sorted(
        key for key in query_names if SECRET_KEY_RE.search(key) or key.lower() in {"sig", "ts", "timestamp"}
    )
    custom_header_names = sorted(
        key
        for key in lower_headers
        if key not in STANDARD_HEADER_NAMES
        and key not in {"cookie", "authorization"}
        and not SECRET_KEY_RE.search(key)
    )
    post_data = request.post_data or ""
    graphql = "graphql" in request.url.lower() or "operationName" in post_data or '"query"' in post_data
    return {
        "sent_cookie": bool(lower_headers.get("cookie")),
        "sent_authorization": bool(lower_headers.get("authorization")),
        "sent_csrf_header": any("csrf" in key for key in lower_headers),
        "custom_header_names": custom_header_names,
        "query_parameter_names": query_names,
        "sensitive_or_short_lived_query_names": sensitive_query_names,
        "has_post_data": bool(post_data),
        "graphql_shape": graphql,
        "has_dynamic_path_segment": bool(re.search(r"/[A-Za-z0-9_-]{24,}(?:/|$)", urlsplit(request.url).path)),
    }


def _classify_request(request: Request) -> str:
    resource_type = request.resource_type
    if resource_type == "document":
        return "document"
    if resource_type in {"script", "stylesheet"}:
        return "js_css"
    if resource_type in {"fetch", "xhr"}:
        return "fetch_xhr"
    return "other"


def _is_capture_response(response: Response) -> tuple[bool, str]:
    request = response.request
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_data_type = request.resource_type in {"fetch", "xhr"}
    is_graphql = "graphql" in request.url.lower()
    is_json_type = content_type in {"application/json", "text/json"} or content_type.endswith("+json")
    return is_data_type or is_graphql or is_json_type, content_type


def _capture_response(state: CaptureState, response: Response) -> None:
    state.response_counts[response.request.resource_type] += 1
    if response.status == 403:
        state.status_403_count += 1
    if response.status == 429:
        state.status_429_count += 1
    should_capture, content_type = _is_capture_response(response)
    if not should_capture:
        return
    state.response_counts["candidate_checked"] += 1
    safe_url, _ = _safe_url(response.url)
    candidate = Candidate(
        url=safe_url,
        status=response.status,
        method=response.request.method,
        resource_type=response.request.resource_type,
        content_type=content_type,
        score=0,
        features={},
        dependency_flags=_dependency_flags(response.request),
    )
    try:
        body = response.body()
        if len(body) > 15_000_000:
            candidate.error = f"response body too large: {len(body)} bytes"
            state.candidates.append(candidate)
            return
        text = body.decode("utf-8", errors="replace")
        candidate.text = text
        try:
            candidate.parsed_json = json.loads(text)
            state.response_counts["json"] += 1
        except json.JSONDecodeError:
            state.response_counts["text_non_json"] += 1
        candidate.score, candidate.features = _feature_scan(candidate.parsed_json, text)
    except Exception as exc:
        candidate.error = f"{type(exc).__name__}: {exc}"
    state.candidates.append(candidate)


def _scrub_secret_string(value: str) -> str:
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "<redacted-jwt>", value)
    value = re.sub(
        r"(?i)(token|secret|signature|authorization|cookie|set-cookie|dpop)=([^&\s]+)",
        r"\1=<redacted>",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}", "Bearer <redacted>", value)
    return value


def _redact_raw_storage(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                result[key] = "<redacted>"
            elif key_text.lower() in {"name", "nickname", "avatar", "description"} and re.search(
                r"seller|user|owner|profile", parent_key, re.IGNORECASE
            ):
                result[key] = "<redacted-personal>"
            else:
                result[key] = _redact_raw_storage(child, key_text)
        return result
    if isinstance(value, list):
        return [_redact_raw_storage(child, parent_key) for child in value]
    if isinstance(value, str):
        return _scrub_secret_string(value)
    return value


def _collect_fixture_sensitive_values(payload: Any, seller_id: str) -> dict[str, set[str]]:
    values = {
        "seller_ids": {seller_id},
        "item_ids": set(ITEM_ID_RE.findall(json.dumps(payload, ensure_ascii=False))),
        "titles": set(),
        "image_urls": set(),
        "personal_values": set(),
    }
    for obj in _item_like_objects(payload):
        for key, child in obj.items():
            key_text = str(key)
            if TITLE_KEY_RE.search(key_text) and isinstance(child, str) and child.strip():
                values["titles"].add(child)
            if IMAGE_KEY_RE.search(key_text):
                for match in URL_RE.findall(json.dumps(child, ensure_ascii=False)):
                    values["image_urls"].add(match.rstrip("\\\"],}"))
    def collect_personal(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                is_contextual_name = key_text.lower() in {"name", "nickname", "description"} and re.search(
                    r"seller|user|owner|profile", parent_key, re.IGNORECASE
                )
                if (
                    (PERSONAL_KEY_RE.search(key_text) or is_contextual_name)
                    and isinstance(child, str)
                    and child.strip()
                ):
                    values["personal_values"].add(child)
                collect_personal(child, key_text)
        elif isinstance(value, list):
            for child in value:
                collect_personal(child, parent_key)

    collect_personal(payload)
    return values


class FixtureSanitizer:
    def __init__(self, seller_id: str, sensitive: dict[str, set[str]]):
        self.seller_id = seller_id
        self.sensitive = sensitive
        self.item_map = {
            item_id: f"m{9_000_000_000 + index}"
            for index, item_id in enumerate(sorted(sensitive["item_ids"]), 1)
        }
        self.title_map = {
            title: f"测试商品 {index}"
            for index, title in enumerate(sorted(sensitive["titles"]), 1)
        }
        self.image_map = {
            image_url: f"https://example.com/images/item-{index}.jpg"
            for index, image_url in enumerate(sorted(sensitive["image_urls"]), 1)
        }
        self.price_index = 0

    def _replace_string(self, value: str) -> str:
        result = _scrub_secret_string(value).replace(self.seller_id, "example_seller_id")
        for original, replacement in self.image_map.items():
            result = result.replace(original, replacement)
        for original, replacement in self.item_map.items():
            result = result.replace(original, replacement)
        for original, replacement in self.title_map.items():
            if result == original:
                result = replacement
        for original in self.sensitive["personal_values"]:
            if result == original:
                result = "example_seller"
        return result

    def sanitize(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            result = {}
            for child_key, child in value.items():
                child_key_text = str(child_key)
                if SECRET_KEY_RE.search(child_key_text):
                    continue
                if PERSONAL_KEY_RE.search(child_key_text):
                    result[child_key] = None if child is None else "example_seller"
                    continue
                if child_key_text.lower() in {"name", "nickname", "description"} and re.search(
                    r"seller|user|owner|profile", key, re.IGNORECASE
                ):
                    result[child_key] = None if child is None else "example_seller"
                    continue
                if SELLER_ID_KEY_RE.search(child_key_text):
                    result[child_key] = "example_seller_id"
                    continue
                if (
                    child_key_text.lower() == "id"
                    and re.search(r"seller|user|owner", key, re.IGNORECASE)
                    and str(child) == self.seller_id
                ):
                    result[child_key] = "example_seller_id"
                    continue
                if ITEM_ID_KEY_RE.search(child_key_text):
                    if isinstance(child, str) and child in self.item_map:
                        result[child_key] = self.item_map[child]
                        continue
                if PRICE_KEY_RE.search(child_key_text) and isinstance(child, (int, float, str)):
                    self.price_index += 1
                    replacement = 1000 + self.price_index * 500
                    result[child_key] = str(replacement) if isinstance(child, str) else replacement
                    continue
                result[child_key] = self.sanitize(child, child_key_text)
            return result
        if isinstance(value, list):
            return [self.sanitize(child, key) for child in value]
        if isinstance(value, str):
            return self._replace_string(value)
        return value


def _fixture_audit(
    sanitized: Any,
    sensitive: dict[str, set[str]],
    seller_id: str,
) -> dict[str, Any]:
    serialized = json.dumps(sanitized, ensure_ascii=False)
    forbidden_keys = [key for key in _all_keys(sanitized) if SECRET_KEY_RE.search(key)]
    non_example_image_urls: list[str] = []
    for obj in _walk(sanitized):
        for key, value in obj.items():
            if not IMAGE_KEY_RE.search(str(key)):
                continue
            for match in URL_RE.findall(json.dumps(value, ensure_ascii=False)):
                host = (urlsplit(match.rstrip("\\\"],}")).hostname or "").lower()
                if host and host != "example.com":
                    non_example_image_urls.append(host)
    residual = {
        "seller_id": seller_id in serialized,
        "item_ids": sum(1 for value in sensitive["item_ids"] if value in serialized),
        "titles": sum(1 for value in sensitive["titles"] if value and value in serialized),
        "image_urls": sum(1 for value in sensitive["image_urls"] if value and value in serialized),
        "personal_values": sum(
            1 for value in sensitive["personal_values"] if len(value) >= 3 and value in serialized
        ),
        "secret_keys": forbidden_keys,
        "jwt_shape": bool(re.search(r"\beyJ[A-Za-z0-9_-]{20,}\.", serialized)),
        "non_example_image_hosts": sorted(set(non_example_image_urls)),
    }
    passed = not any(
        (
            residual["seller_id"],
            residual["item_ids"],
            residual["titles"],
            residual["image_urls"],
            residual["personal_values"],
            residual["secret_keys"],
            residual["jwt_shape"],
            residual["non_example_image_hosts"],
        )
    )
    return {
        "passed": passed,
        "source_sensitive_counts": {key: len(values) for key, values in sensitive.items()},
        "residual": residual,
    }


def _item_ids(payload: dict[str, Any] | None) -> set[str]:
    return {
        value
        for item in _item_payloads(payload)
        if isinstance((value := item.get("id")), str) and ITEM_ID_RE.fullmatch(value)
    }


def _pager_ids(payload: dict[str, Any] | None) -> list[Any]:
    """Compatibility helper for the legacy completed-capture analyzer."""

    return [item.get("pager_id") for item in _item_payloads(payload)]


class CaptureCheckpoint:
    """Incremental, sanitized, atomic persistence for one controlled run."""

    PHASE_FILES = {
        "initial": "initial_items_sanitized.json",
        "filter": "filtered_items_sanitized.json",
        "pagination": "next_page_items_sanitized.json",
    }

    def __init__(self, run_dir: str | Path, seller_id: str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.seller_id = seller_id
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.updated_at = self.started_at
        self.current_stage: str | None = None
        self.last_successful_stage: str | None = None
        self.completed_stages: list[str] = []
        self.action_stage = "browser_started"
        self.failed_stage: str | None = None
        self.failure_kind: str | None = None
        self.failure_reason: str | None = None
        self.outcome: str | None = None
        self.observations: list[GetItemsObservation] = []
        self.counters = RequestCounters()
        self.filter_clicked = False
        self.pagination_trigger_attempted = False
        self.scroll_count = 0
        self.new_get_items_response_observed = False
        self.browser_name: str | None = None
        self.document_status: int | None = None
        self.toggle_strategy: str | None = None
        self.toggle_checked_before: bool | None = None
        self.toggle_checked_after: bool | None = None
        self.blocked_reasons: Counter = Counter()
        self._item_map: dict[str, str] = {}
        self._title_map: dict[str, str] = {}
        self._image_map: dict[str, str] = {}
        self._opaque_query_values: dict[tuple[str, str], str] = {}
        self._forbidden_values: set[str] = {seller_id}
        self._saved_phase_files: dict[str, str] = {}

    def set_action(self, stage: str) -> None:
        if stage not in CAPTURE_STAGES:
            raise ValueError(f"unknown capture stage: {stage}")
        self.action_stage = stage

    def _sensitive_and_sanitizer(self) -> tuple[dict[str, set[str]], FixtureSanitizer]:
        payloads = [record.payload for record in self.observations if record.payload is not None]
        combined = {"responses": payloads}
        sensitive = _collect_fixture_sensitive_values(combined, self.seller_id)
        for item_id in sorted(sensitive["item_ids"]):
            self._item_map.setdefault(item_id, f"m{9_000_000_000 + len(self._item_map) + 1}")
        for title in sorted(sensitive["titles"]):
            self._title_map.setdefault(title, f"测试商品 {len(self._title_map) + 1}")
        for image_url in sorted(sensitive["image_urls"]):
            self._image_map.setdefault(
                image_url,
                f"https://example.com/images/item-{len(self._image_map) + 1}.jpg",
            )
        for values in sensitive.values():
            self._forbidden_values.update(value for value in values if value)
        sanitizer = FixtureSanitizer(self.seller_id, sensitive)
        sanitizer.item_map = dict(self._item_map)
        sanitizer.title_map = dict(self._title_map)
        sanitizer.image_map = dict(self._image_map)
        return sensitive, sanitizer

    def _write(self, name: str, payload: Any) -> None:
        _atomic_write_json(
            self.run_dir / name,
            payload,
            forbidden_values=set(self._forbidden_values),
        )

    def _manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "current_stage": self.current_stage,
            "last_successful_stage": self.last_successful_stage,
            "completed_stages": list(self.completed_stages),
            "failed_stage": self.failed_stage,
            "failure_kind": self.failure_kind,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "target_url": "https://jp.mercari.com/user/profile/example_seller_id",
            "browser": self.browser_name,
            "document_status": self.document_status,
            "filter_clicked": self.filter_clicked,
            "pagination_trigger_attempted": self.pagination_trigger_attempted,
            "scroll_count": self.scroll_count,
            "new_get_items_response_observed": self.new_get_items_response_observed,
            "counters": self.counters.as_dict(),
            "blocked_reasons": dict(self.blocked_reasons),
            "files": {
                "run_manifest": "run_manifest.json",
                "request_summary": "request_summary_sanitized.json",
                "initial_items": self._saved_phase_files.get("initial"),
                "filtered_items": self._saved_phase_files.get("filter"),
                "next_page_items": self._saved_phase_files.get("pagination"),
                "error_summary": "error_summary.json" if self.failed_stage else None,
            },
        }

    def _request_summary(self) -> dict[str, Any]:
        _, sanitizer = self._sensitive_and_sanitizer()
        first_ids: set[str] | None = None
        previous_ids: set[str] | None = None
        responses: list[dict[str, Any]] = []
        for observation in self.observations:
            current_ids = _item_ids(observation.payload)
            if first_ids is None and observation.payload is not None:
                first_ids = current_ids
            responses.append(
                {
                    "sequence": observation.request_sequence,
                    "phase": observation.phase,
                    "requested_at": observation.requested_at,
                    "responded_at": observation.responded_at,
                    "method": observation.method,
                    "path": urlsplit(observation.request_url).path,
                    "http_status": observation.status,
                    "query": _safe_query_snapshot(
                        observation.request_url,
                        self.seller_id,
                        sanitizer,
                        self._opaque_query_values,
                    ),
                    "item_count": (
                        len(_item_payloads(observation.payload))
                        if observation.payload is not None
                        else None
                    ),
                    "status_distribution": _status_distribution(observation.payload),
                    "has_next": _meta_has_next(observation.payload),
                    "duplicate_with_previous": (
                        len(current_ids & previous_ids)
                        if current_ids and previous_ids is not None
                        else 0
                    ),
                    "duplicate_with_first": (
                        len(current_ids & first_ids)
                        if current_ids and first_ids is not None
                        else 0
                    ),
                    "payload_file": self._saved_phase_files.get(observation.phase),
                    "parse_error": (
                        _sanitize_error_text(
                            observation.error, self.seller_id, self._item_map
                        )
                        if observation.error
                        else None
                    ),
                }
            )
            if observation.payload is not None:
                previous_ids = current_ids
        return {
            "schema_version": 1,
            "current_stage": self.current_stage,
            "counters": self.counters.as_dict(),
            "responses": responses,
        }

    def checkpoint(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # Refresh forbidden values before any summary is serialized.
        self._sensitive_and_sanitizer()
        self._write("request_summary_sanitized.json", self._request_summary())
        self._write("run_manifest.json", self._manifest())

    def mark_stage(self, stage: str) -> None:
        if stage not in CAPTURE_STAGES or stage == "failed":
            raise ValueError(f"invalid successful capture stage: {stage}")
        self.current_stage = stage
        self.last_successful_stage = stage
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)
        self.action_stage = stage
        self.checkpoint()

    def record_request(self, resource_type: str, *, get_items: bool = False) -> None:
        self.counters.record_request(resource_type, get_items=get_items)

    def record_blocked(self, reason: str) -> None:
        self.counters.blocked_request_count += 1
        self.blocked_reasons[reason] += 1

    def record_response(self, status: int, *, get_items: bool = False) -> None:
        self.counters.record_response(status, get_items=get_items)

    def add_get_items_request(self, request_url: str, method: str, phase: str) -> GetItemsObservation:
        observation = GetItemsObservation(
            phase=phase,
            request_url=request_url,
            method=method,
            request_sequence=len(self.observations) + 1,
        )
        self.observations.append(observation)
        self.checkpoint()
        return observation

    def finish_get_items_response(
        self,
        observation: GetItemsObservation,
        *,
        status: int,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        observation.status = status
        observation.responded_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        observation.payload = payload
        observation.error = error
        if observation.phase == "pagination":
            self.new_get_items_response_observed = True
        if payload is not None and observation.phase not in self._saved_phase_files:
            sensitive, sanitizer = self._sensitive_and_sanitizer()
            sanitized = sanitizer.sanitize(payload)
            audit = _fixture_audit(sanitized, sensitive, self.seller_id)
            if not audit["passed"]:
                raise ValueError(f"checkpoint fixture audit failed: {audit['residual']}")
            filename = self.PHASE_FILES[observation.phase]
            self._write(filename, sanitized)
            self._saved_phase_files[observation.phase] = filename
        self.checkpoint()

    def record_scroll(self) -> None:
        self.pagination_trigger_attempted = True
        self.scroll_count += 1
        self.checkpoint()

    def classify_failure(self, failed_stage: str) -> str:
        filtered = next(
            (
                record
                for record in self.observations
                if record.phase == "filter" and record.payload is not None
            ),
            None,
        )
        pagination = [record for record in self.observations if record.phase == "pagination"]
        if filtered is None and failed_stage in {
            "filter_clicked",
            "filtered_items_captured",
            "pagination_trigger_attempted",
            "next_page_captured",
        }:
            return "filtered_response_not_captured"
        if filtered is not None and _meta_has_next(filtered.payload) is False:
            return "filtered_has_next_false"
        if pagination and any(record.error for record in pagination):
            return "next_page_parse_failed"
        if self.pagination_trigger_attempted and not pagination:
            if filtered is not None and _meta_has_next(filtered.payload) is True:
                return "filtered_has_next_true_but_no_next_request"
            return "pagination_not_triggered_with_unknown_has_next"
        if pagination and not any(record.payload is not None for record in pagination):
            return "next_page_response_unusable"
        return "browser_or_page_error"

    def fail(self, failed_stage: str, error: BaseException | str) -> None:
        self.failed_stage = failed_stage
        self.failure_kind = self.classify_failure(failed_stage)
        self.failure_reason = _sanitize_error_text(str(error), self.seller_id, self._item_map)
        for value in sorted(self._forbidden_values, key=len, reverse=True):
            if value:
                self.failure_reason = self.failure_reason.replace(value, "<redacted>")
        self.current_stage = "failed"
        error_summary = {
            "stage": "failed",
            "failed_stage": failed_stage,
            "failure_kind": self.failure_kind,
            "error_type": type(error).__name__ if isinstance(error, BaseException) else "Error",
            "reason": self.failure_reason,
            "last_successful_stage": self.last_successful_stage,
            "filter_clicked": self.filter_clicked,
            "pagination_trigger_attempted": self.pagination_trigger_attempted,
            "scroll_count": self.scroll_count,
            "new_get_items_response_observed": self.new_get_items_response_observed,
        }
        self._write("error_summary.json", error_summary)
        self.checkpoint()

    @contextmanager
    def failure_guard(self):
        try:
            yield self
        except BaseException as exc:
            self.fail(self.action_stage, exc)
            raise
        finally:
            self.checkpoint()


def build_pagination_capture_artifacts(
    initial: list[GetItemsObservation],
    filtered: GetItemsObservation,
    next_page: GetItemsObservation,
    seller_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build sanitized fixtures and request analysis without performing I/O."""

    if not initial or filtered.payload is None or next_page.payload is None:
        raise ValueError("pagination capture requires initial, filtered, and next-page payloads")
    combined = {
        "initial": [record.payload for record in initial if record.payload is not None],
        "filtered": filtered.payload,
        "next_page": next_page.payload,
    }
    sensitive = _collect_fixture_sensitive_values(combined, seller_id)
    sanitizer = FixtureSanitizer(seller_id, sensitive)
    filtered_sanitized = sanitizer.sanitize(filtered.payload)
    next_sanitized = sanitizer.sanitize(next_page.payload)
    opaque_values: dict[tuple[str, str], str] = {}

    initial_requests = [
        {
            "method": record.method,
            "status": record.status,
            "path": urlsplit(record.request_url).path,
            "query": _safe_query_snapshot(
                record.request_url, seller_id, sanitizer, opaque_values
            ),
            "item_count": len(_item_payloads(record.payload)),
            "status_distribution": _status_distribution(record.payload),
            "has_next": _meta_has_next(record.payload),
        }
        for record in initial
    ]
    filtered_query = _query_multimap(filtered.request_url)
    filtered_pagers = _pager_ids(filtered.payload)
    next_pagers = _pager_ids(next_page.payload)
    last_filtered_pager = filtered_pagers[-1] if filtered_pagers else None
    matching_pagination_keys = sorted(
        key
        for key, values in _query_multimap(next_page.request_url).items()
        if last_filtered_pager is not None
        and any(value == str(last_filtered_pager) for value in values)
    )
    duplicate_count = len(_item_ids(filtered.payload) & _item_ids(next_page.payload))
    filtered_statuses = _status_distribution(filtered.payload)
    next_statuses = _status_distribution(next_page.payload)
    on_sale_filter_reliable = bool(filtered_statuses) and set(filtered_statuses) <= {"on_sale"}
    on_sale_filter_reliable = (
        on_sale_filter_reliable
        and bool(next_statuses)
        and set(next_statuses) <= {"on_sale"}
    )
    second_has_next = _meta_has_next(next_page.payload)
    if second_has_next is False:
        estimated_pages: int | None = 2
        minimum_pages = 2
    else:
        estimated_pages = None
        minimum_pages = 3

    analysis = {
        "schema_version": 1,
        "endpoint": GET_ITEMS_PATH,
        "initial_requests": initial_requests,
        "filtered_request": {
            "method": filtered.method,
            "status": filtered.status,
            "path": urlsplit(filtered.request_url).path,
            "query": _safe_query_snapshot(
                filtered.request_url, seller_id, sanitizer, opaque_values
            ),
            "item_count": len(_item_payloads(filtered.payload)),
            "status_distribution": filtered_statuses,
            "has_next": _meta_has_next(filtered.payload),
        },
        "next_page_request": {
            "method": next_page.method,
            "status": next_page.status,
            "path": urlsplit(next_page.request_url).path,
            "query": _safe_query_snapshot(
                next_page.request_url, seller_id, sanitizer, opaque_values
            ),
            "item_count": len(_item_payloads(next_page.payload)),
            "status_distribution": next_statuses,
            "has_next": second_has_next,
        },
        "pagination_evidence": {
            "filtered_page_last_pager_id": last_filtered_pager,
            "next_request_matching_parameter_names": matching_pagination_keys,
            "next_page_first_pager_id": next_pagers[0] if next_pagers else None,
            "next_page_last_pager_id": next_pagers[-1] if next_pagers else None,
            "duplicate_item_count": duplicate_count,
            "pager_id_proven": bool(matching_pagination_keys),
        },
        "filter_evidence": {
            "query_parameter_names": sorted(filtered_query),
            "reliable_on_sale_only": on_sale_filter_reliable,
            "contains_sold_out": "sold_out" in filtered_statuses or "sold_out" in next_statuses,
            "contains_trading": "trading" in filtered_statuses or "trading" in next_statuses,
        },
        "page_estimate": {
            "exact_pages": estimated_pages,
            "minimum_pages": minimum_pages,
            "reason": (
                "second page is terminal"
                if second_has_next is False
                else "second page still reports has_next=true"
            ),
        },
    }
    combined_sanitized = {
        "filtered": filtered_sanitized,
        "next_page": next_sanitized,
        "analysis": analysis,
    }
    audit = _fixture_audit(combined_sanitized, sensitive, seller_id)
    serialized_analysis = json.dumps(analysis, ensure_ascii=False)
    analysis_queries = [request["query"] for request in initial_requests]
    analysis_queries.extend(
        [analysis["filtered_request"]["query"], analysis["next_page_request"]["query"]]
    )
    unsafe_sensitive_query_value = False
    for query in analysis_queries:
        for key, value in query.items():
            values = value if isinstance(value, list) else [value]
            if SECRET_KEY_RE.search(key) and any(item != "<redacted>" for item in values):
                unsafe_sensitive_query_value = True
    audit["analysis_residual"] = {
        "seller_id": seller_id in serialized_analysis,
        "sensitive_query_value": unsafe_sensitive_query_value,
        "jwt_shape": bool(re.search(r"\beyJ[A-Za-z0-9_-]{20,}\.", serialized_analysis)),
    }
    audit["passed"] = audit["passed"] and not any(audit["analysis_residual"].values())
    return filtered_sanitized, next_sanitized, analysis, audit


def _launch_context(playwright, viewport: dict[str, int]):
    strategies: list[tuple[str, dict[str, Any]]] = [
        ("chrome-channel", {"channel": "chrome"}),
        ("msedge-channel", {"channel": "msedge"}),
    ]
    common_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    strategies.extend(
        (f"executable:{path.name}:{path.parent}", {"executable_path": str(path)})
        for path in common_paths
        if path.exists()
    )
    errors = []
    for name, launch_options in strategies:
        profile = tempfile.TemporaryDirectory(prefix="mercari_capture_profile_")
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile.name,
                headless=True,
                locale="ja-JP",
                viewport=viewport,
                accept_downloads=False,
                **launch_options,
            )
            return context, profile, name, errors
        except Exception as exc:
            errors.append({"strategy": name, "error_type": type(exc).__name__})
            profile.cleanup()
    raise RuntimeError(f"无法启动本机 Chrome/Edge；尝试结果: {errors}")


def _write_raw_capture(run_dir: Path, state: CaptureState, launch_errors: list[dict]) -> tuple[list[Candidate], Path]:
    ranked = sorted(state.candidates, key=lambda candidate: candidate.score, reverse=True)
    top = ranked[:5]
    candidate_summaries = []
    for index, candidate in enumerate(top, 1):
        suffix = ".json" if candidate.parsed_json is not None else ".txt"
        raw_path = run_dir / f"candidate_{index:02d}_raw{suffix}"
        if index <= 2 and candidate.parsed_json is not None:
            sensitive = _collect_fixture_sensitive_values(candidate.parsed_json, state.seller_id)
            sanitizer = FixtureSanitizer(state.seller_id, sensitive)
            sanitized = sanitizer.sanitize(candidate.parsed_json)
            audit = _fixture_audit(sanitized, sensitive, state.seller_id)
            if audit["passed"]:
                forbidden = {state.seller_id}
                for values in sensitive.values():
                    forbidden.update(value for value in values if value)
                _atomic_write_json(raw_path, sanitized, forbidden_values=forbidden)
        candidate_summaries.append(
            {
                "rank": index,
                "url": candidate.url,
                "status": candidate.status,
                "method": candidate.method,
                "resource_type": candidate.resource_type,
                "content_type": candidate.content_type,
                "score": candidate.score,
                "features": candidate.features,
                "dependency_flags": candidate.dependency_flags,
                "is_json": candidate.parsed_json is not None,
                "reliable_item_list": candidate.reliable_item_list,
                "error": candidate.error,
                "raw_file": raw_path.name if raw_path.exists() else None,
            }
        )
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_url": "https://jp.mercari.com/user/profile/example_seller_id",
        "browser": state.browser_name,
        "browser_launch_failures": launch_errors,
        "document_navigation_count": state.document_navigation_count,
        "request_counts": dict(state.request_counts),
        "response_counts": dict(state.response_counts),
        "blocked_counts": dict(state.blocked_counts),
        "document_status": state.document_status,
        "load_seconds": state.load_seconds,
        "item_cell_count": state.item_cell_count,
        "has_login_prompt": state.has_login_prompt,
        "login_wall_detected": state.login_wall_detected,
        "captcha_detected": state.captcha_detected,
        "status_403_count": state.status_403_count,
        "status_429_count": state.status_429_count,
        "candidate_response_count": len(state.candidates),
        "json_response_count": state.response_counts.get("json", 0),
        "top_candidates": candidate_summaries,
    }
    summary_path = run_dir / "response_summary.json"
    _atomic_write_json(summary_path, summary, forbidden_values={state.seller_id})
    return ranked, summary_path


def capture(args: argparse.Namespace) -> int:
    match = PROFILE_RE.fullmatch(args.url.rstrip("/"))
    if not match:
        raise ValueError("仅接受 https://jp.mercari.com/user/profile/<numeric seller_id>")
    target_url = args.url.rstrip("/")
    seller_id = match.group("seller_id")
    repo_root = Path(__file__).resolve().parents[1]
    output_base = Path(args.output_dir).resolve()
    if output_base == repo_root or repo_root in output_base.parents:
        raise ValueError("原始捕获目录必须位于仓库外")
    run_dir = output_base / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    fixture_path = Path(args.fixture_output).resolve()
    state = CaptureState(target_url=target_url, seller_id=seller_id)
    launch_errors: list[dict] = []

    start = time.monotonic()
    with sync_playwright() as playwright:
        context, profile, browser_name, launch_errors = _launch_context(
            playwright, {"width": 1365, "height": 900}
        )
        state.browser_name = browser_name
        try:
            pages = context.pages
            page = pages[0] if pages else context.new_page()

            def route_handler(route):
                request = route.request
                url_lower = request.url.lower()
                if request.resource_type in {"image", "font", "media"}:
                    state.blocked_counts[request.resource_type] += 1
                    route.abort()
                    return
                if any(marker in url_lower for marker in ANALYTICS_MARKERS):
                    state.blocked_counts["analytics_or_ads"] += 1
                    route.abort()
                    return
                if request.resource_type == "document" and request.url.rstrip("/") != target_url:
                    state.blocked_counts["unexpected_document"] += 1
                    route.abort()
                    return
                route.continue_()

            page.route("**/*", route_handler)

            def on_request(request: Request):
                category = _classify_request(request)
                state.request_counts[f"category:{category}"] += 1
                state.request_counts[f"resource:{request.resource_type}"] += 1
                if request.resource_type == "document" and request.url.rstrip("/") == target_url:
                    state.document_navigation_count += 1

            page.on("request", on_request)
            page.on("response", lambda response: _capture_response(state, response))
            main_response = page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=args.navigation_timeout_ms,
            )
            state.document_status = main_response.status if main_response else None
            page.wait_for_timeout(args.capture_wait_ms)
            state.item_cell_count = page.locator('li[data-testid="item-cell"]').count()
            body_text = ""
            try:
                body_text = page.locator("body").inner_text(timeout=3000)
            except PlaywrightError:
                pass
            current_url = page.url.lower()
            state.has_login_prompt = "ログイン" in body_text or "login" in current_url
            state.login_wall_detected = (
                ("login" in current_url or "ログインしてください" in body_text)
                and state.item_cell_count == 0
            )
            captcha_markers = (
                "captcha",
                "ロボットではありません",
                "セキュリティチェック",
                "verify you are human",
            )
            state.captcha_detected = any(marker in body_text.lower() for marker in captcha_markers)
        finally:
            state.load_seconds = round(time.monotonic() - start, 3)
            context.close()
            profile.cleanup()

    ranked, summary_path = _write_raw_capture(run_dir, state, launch_errors)
    selected = next((candidate for candidate in ranked if candidate.reliable_item_list), None)
    fixture_written = False
    audit = None
    if selected is not None and selected.parsed_json is not None:
        sensitive = _collect_fixture_sensitive_values(selected.parsed_json, seller_id)
        sanitized = FixtureSanitizer(seller_id, sensitive).sanitize(selected.parsed_json)
        audit = _fixture_audit(sanitized, sensitive, seller_id)
        if audit["passed"]:
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            forbidden = {seller_id}
            for values in sensitive.values():
                forbidden.update(value for value in values if value)
            _atomic_write_json(fixture_path, sanitized, forbidden_values=forbidden)
            fixture_written = True

    result = {
        "browser": state.browser_name,
        "document_navigation_count": state.document_navigation_count,
        "request_counts": dict(state.request_counts),
        "response_counts": dict(state.response_counts),
        "blocked_counts": dict(state.blocked_counts),
        "document_status": state.document_status,
        "load_seconds": state.load_seconds,
        "item_cell_count": state.item_cell_count,
        "status_403_count": state.status_403_count,
        "status_429_count": state.status_429_count,
        "login_wall_detected": state.login_wall_detected,
        "captcha_detected": state.captcha_detected,
        "top_candidates": [
            {
                "rank": index,
                "url": candidate.url,
                "status": candidate.status,
                "method": candidate.method,
                "resource_type": candidate.resource_type,
                "content_type": candidate.content_type,
                "score": candidate.score,
                "features": candidate.features,
                "dependency_flags": candidate.dependency_flags,
                "reliable_item_list": candidate.reliable_item_list,
            }
            for index, candidate in enumerate(ranked[:5], 1)
        ],
        "selected_candidate_rank": (
            ranked.index(selected) + 1 if selected is not None else None
        ),
        "raw_directory": str(run_dir),
        "summary_file": str(summary_path),
        "fixture_written": fixture_written,
        "fixture_path": str(fixture_path) if fixture_written else None,
        "fixture_audit": audit,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if selected is not None and fixture_written else 3


def _wait_for_capture(page, predicate, timeout_ms: int, label: str) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(200)
    raise TimeoutError(f"timed out waiting for {label}")


def _click_on_sale_toggle_once(page) -> tuple[str, bool | None, bool | None]:
    checkbox = page.locator('input[type="checkbox"]')
    checked_before = None
    if checkbox.count():
        try:
            checked_before = checkbox.first.is_checked()
        except PlaywrightError:
            pass

    pattern = re.compile(
        r"販売中のみ表示|販売中の商品|仅显示当前在售商品|只显示当前在售商品|only show.*on sale",
        re.IGNORECASE,
    )
    target = None
    strategy = None
    text_matches = page.get_by_text(pattern)
    for index in range(text_matches.count()):
        candidate = text_matches.nth(index)
        if candidate.is_visible():
            target = candidate
            strategy = "visible-toggle-text"
            break
    if target is None:
        visible_checkboxes = [
            checkbox.nth(index)
            for index in range(checkbox.count())
            if checkbox.nth(index).is_visible()
        ]
        if len(visible_checkboxes) == 1:
            target = visible_checkboxes[0]
            strategy = "single-visible-checkbox"
    if target is None and checkbox.count() == 1:
        target = checkbox.first
        strategy = "single-checkbox"
    if target is None or strategy is None:
        raise RuntimeError("could not identify the on-sale-only toggle reliably")

    target.click(timeout=5000)
    checked_after = None
    if checkbox.count():
        try:
            checked_after = checkbox.first.is_checked()
        except PlaywrightError:
            pass
    return strategy, checked_before, checked_after


def capture_pagination_filter(args: argparse.Namespace) -> int:
    """Checkpointed controlled capture; all persisted artifacts are sanitized."""

    match = PROFILE_RE.fullmatch(args.url.rstrip("/"))
    if not match:
        raise ValueError("仅接受 https://jp.mercari.com/user/profile/<numeric seller_id>")
    target_url = args.url.rstrip("/")
    seller_id = match.group("seller_id")
    repo_root = Path(__file__).resolve().parents[1]
    output_base = Path(args.output_dir).resolve()
    if output_base == repo_root or repo_root in output_base.parents:
        raise ValueError("捕获目录必须位于仓库外")
    run_dir = output_base / f"pagination_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    checkpoint = CaptureCheckpoint(run_dir, seller_id)
    phase = {"value": "initial"}
    phase_allowed = Counter()
    request_observations: dict[int, GetItemsObservation] = {}

    with checkpoint.failure_guard():
        checkpoint.set_action("browser_started")
        with sync_playwright() as playwright:
            context, profile, browser_name, _ = _launch_context(
                playwright, {"width": 1365, "height": 700}
            )
            checkpoint.browser_name = browser_name
            checkpoint.mark_stage("browser_started")
            try:
                pages = context.pages
                page = pages[0] if pages else context.new_page()

                def route_handler(route):
                    request = route.request
                    url_lower = request.url.lower()
                    if request.resource_type in {"image", "font", "media"}:
                        checkpoint.record_blocked(request.resource_type)
                        route.abort()
                        return
                    if any(marker in url_lower for marker in ANALYTICS_MARKERS):
                        checkpoint.record_blocked("analytics_or_ads")
                        route.abort()
                        return
                    if request.resource_type == "document" and request.url.rstrip("/") != target_url:
                        checkpoint.record_blocked("unexpected_document")
                        route.abort()
                        return
                    current_phase = phase["value"]
                    if _is_get_items_url(request.url) and current_phase in {"filter", "pagination"}:
                        if phase_allowed[current_phase] >= 1:
                            checkpoint.record_blocked(f"extra_get_items:{current_phase}")
                            route.abort()
                            return
                        phase_allowed[current_phase] += 1
                    route.continue_()

                def on_request(request: Request):
                    get_items = _is_get_items_url(request.url)
                    checkpoint.record_request(request.resource_type, get_items=get_items)
                    if request.resource_type == "document" and request.url.rstrip("/") == target_url:
                        checkpoint.counters.navigation_count += 1
                    if get_items:
                        observation = checkpoint.add_get_items_request(
                            request.url,
                            request.method,
                            phase["value"],
                        )
                        request_observations[id(request)] = observation

                def on_response(response: Response):
                    get_items = _is_get_items_url(response.url)
                    checkpoint.record_response(response.status, get_items=get_items)
                    if not get_items:
                        return
                    observation = request_observations.get(id(response.request))
                    if observation is None:
                        observation = next(
                            (
                                record
                                for record in reversed(checkpoint.observations)
                                if record.request_url == response.url and record.status is None
                            ),
                            None,
                        )
                    if observation is None:
                        return
                    try:
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("get_items response is not an object")
                        checkpoint.finish_get_items_response(
                            observation,
                            status=response.status,
                            payload=payload,
                        )
                    except Exception as exc:
                        checkpoint.finish_get_items_response(
                            observation,
                            status=response.status,
                            error=f"{type(exc).__name__}: {exc}",
                        )

                page.route("**/*", route_handler)
                page.on("request", on_request)
                page.on("response", on_response)

                checkpoint.set_action("profile_loaded")
                main_response = page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=args.navigation_timeout_ms,
                )
                checkpoint.document_status = main_response.status if main_response else None
                checkpoint.mark_stage("profile_loaded")

                checkpoint.set_action("initial_items_captured")
                _wait_for_capture(
                    page,
                    lambda: any(
                        record.phase == "initial" and record.payload
                        for record in checkpoint.observations
                    ),
                    args.phase_timeout_ms,
                    "initial get_items response",
                )
                page.wait_for_timeout(1200)
                checkpoint.mark_stage("initial_items_captured")

                phase["value"] = "filter"
                checkpoint.set_action("filter_clicked")
                strategy, checked_before, checked_after = _click_on_sale_toggle_once(page)
                checkpoint.filter_clicked = True
                checkpoint.toggle_strategy = strategy
                checkpoint.toggle_checked_before = checked_before
                checkpoint.toggle_checked_after = checked_after
                checkpoint.mark_stage("filter_clicked")

                checkpoint.set_action("filtered_items_captured")
                _wait_for_capture(
                    page,
                    lambda: any(
                        record.phase == "filter" and record.payload
                        for record in checkpoint.observations
                    ),
                    args.phase_timeout_ms,
                    "filtered get_items response",
                )
                filtered = next(
                    record
                    for record in checkpoint.observations
                    if record.phase == "filter" and record.payload
                )
                checkpoint.mark_stage("filtered_items_captured")

                if _meta_has_next(filtered.payload) is False:
                    checkpoint.outcome = "filtered_has_next_false"
                    checkpoint.mark_stage("completed")
                else:
                    phase["value"] = "pagination"
                    checkpoint.pagination_trigger_attempted = True
                    checkpoint.set_action("pagination_trigger_attempted")
                    checkpoint.mark_stage("pagination_trigger_attempted")
                    for _ in range(args.max_scroll_steps):
                        page.evaluate(
                            "window.scrollBy(0, Math.max(500, Math.floor(window.innerHeight * 0.8)))"
                        )
                        checkpoint.record_scroll()
                        page.wait_for_timeout(args.scroll_step_wait_ms)
                        if any(
                            record.phase == "pagination" and record.responded_at
                            for record in checkpoint.observations
                        ):
                            break
                    next_page = next(
                        (
                            record
                            for record in checkpoint.observations
                            if record.phase == "pagination"
                        ),
                        None,
                    )
                    if next_page is None:
                        raise RuntimeError(
                            "filtered response has_next=true but scrolling triggered no next-page request"
                        )
                    if next_page.payload is None:
                        raise RuntimeError(
                            next_page.error or "next-page response could not be parsed"
                        )
                    checkpoint.set_action("next_page_captured")
                    checkpoint.mark_stage("next_page_captured")
                    checkpoint.outcome = "completed_with_next_page"
                    checkpoint.mark_stage("completed")
            finally:
                context.close()
                profile.cleanup()

    result = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    result["run_directory"] = str(run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def sanitize_saved_candidate(
    candidate_path: str | Path,
    fixture_path: str | Path,
    seller_id: str,
) -> dict[str, Any]:
    source_path = Path(candidate_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    sensitive = _collect_fixture_sensitive_values(payload, seller_id)
    sanitized = FixtureSanitizer(seller_id, sensitive).sanitize(payload)
    audit = _fixture_audit(sanitized, sensitive, seller_id)
    if not audit["passed"]:
        raise ValueError(f"fixture 脱敏检查失败: {audit['residual']}")
    output = Path(fixture_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    forbidden = {seller_id}
    for values in sensitive.values():
        forbidden.update(value for value in values if value)
    _atomic_write_json(output, sanitized, forbidden_values=forbidden)
    return {
        "source": str(source_path),
        "fixture": str(output),
        "audit": audit,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot Mercari profile response capture")
    parser.add_argument("--url")
    parser.add_argument("--sanitize-from")
    parser.add_argument("--seller-id")
    parser.add_argument(
        "--pagination-filter-capture",
        action="store_true",
        help="One navigation, one on-sale toggle click, and one next-page trigger",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(tempfile.gettempdir()) / "mercari_capture"),
        help="Repository-external directory for redacted raw diagnostics",
    )
    parser.add_argument(
        "--fixture-output",
        default=str(
            Path(__file__).resolve().parents[1]
            / "tests/fixtures/seller_monitor/mercari/items_page_1_sanitized.json"
        ),
    )
    parser.add_argument("--capture-wait-ms", type=int, default=12000)
    parser.add_argument("--navigation-timeout-ms", type=int, default=45000)
    parser.add_argument("--phase-timeout-ms", type=int, default=20000)
    parser.add_argument("--max-scroll-steps", type=int, default=12)
    parser.add_argument("--scroll-step-wait-ms", type=int, default=500)
    parser.add_argument(
        "--fixture-dir",
        default=str(
            Path(__file__).resolve().parents[1]
            / "tests/fixtures/seller_monitor/mercari"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sanitize_from:
        if not args.seller_id:
            raise ValueError("--sanitize-from 需要 --seller-id")
        result = sanitize_saved_candidate(
            args.sanitize_from,
            args.fixture_output,
            args.seller_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.url:
        raise ValueError("捕获模式需要 --url")
    if args.pagination_filter_capture:
        return capture_pagination_filter(args)
    return capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
