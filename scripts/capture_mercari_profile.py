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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Request, Response, sync_playwright


PROFILE_RE = re.compile(r"^https://jp\.mercari\.com/user/profile/(?P<seller_id>[0-9]+)$")
ITEM_ID_RE = re.compile(r"\bm[0-9]{5,}\b", re.IGNORECASE)
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


def _safe_url(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    query_names = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    redacted_query = "&".join(f"{key}=<redacted>" for key in query_names)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, "")), query_names


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
    value = re.sub(r"(?i)(token|secret|signature|authorization)=([^&\s]+)", r"\1=<redacted>", value)
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
            raw_path.write_text(
                json.dumps(_redact_raw_storage(candidate.parsed_json), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif index <= 2 and candidate.text is not None:
            raw_path.write_text(_scrub_secret_string(candidate.text), encoding="utf-8")
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
        "target_url": state.target_url,
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
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
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
            fixture_path.write_text(
                json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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
    output.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
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
    return capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
