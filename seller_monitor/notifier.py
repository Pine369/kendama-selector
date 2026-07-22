"""Independent PushPlus HTML notifier.

PushPlus code=200 means only that the provider accepted the request. It is not
proof that the message was delivered to a WeChat client.
"""

from __future__ import annotations

import html
from pathlib import Path

import requests

from seller_monitor.models import NotificationResult


PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"


def _money(value: int | None) -> str:
    return "价格未知" if value is None else f"¥{value:,}"


def render_notification_html(payload: dict) -> str:
    event_labels = {
        "new_listing": "新上架",
        "fixed_price_drop": "降价",
        "fixed_price_increase": "涨价",
        "auction_terms_change": "拍卖条件变化",
    }
    platform_labels = {
        "mercari": "Mercari",
        "yahoo_auctions": "Yahoo! Auctions",
        "rakuten": "Rakuten",
    }
    event_label = event_labels.get(payload.get("event_type"), payload.get("event_type", "商品变化"))
    platform_label = platform_labels.get(payload.get("platform"), payload.get("platform", "未知平台"))
    listing_label = "拍卖" if payload.get("listing_type") == "auction" else "普通商品"
    old_price = payload.get("old_price")
    new_price = payload.get("new_price")
    price_line = _money(new_price)
    drop_line = ""
    if payload.get("event_type") == "fixed_price_drop" and old_price is not None and new_price is not None:
        amount = old_price - new_price
        percentage = (amount / old_price * 100) if old_price else 0
        price_line = f"{_money(old_price)} → {_money(new_price)}"
        drop_line = f"<p><strong>降价：</strong>{_money(amount)}（{percentage:.2f}%）</p>"
    elif payload.get("event_type") == "auction_terms_change":
        price_line = f"{_money(old_price)} → {_money(new_price)}"
        term_labels = {"start_price": "起拍价", "buyout_price": "即决价"}
        drop_line = f"<p><strong>变更项：</strong>{html.escape(term_labels.get(payload.get('term_type'), payload.get('term_type') or '拍卖条款'))}</p>"
    elif payload.get("event_type") == "fixed_price_increase":
        price_line = f"{_money(old_price)} → {_money(new_price)}"

    image_url = html.escape(payload.get("image_url") or "", quote=True)
    image = (
        f'<p><img src="{image_url}" alt="商品主图" '
        'style="max-width:100%;height:auto;border-radius:8px"></p>'
        if image_url else ""
    )
    item_url = html.escape(payload.get("item_url") or "", quote=True)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(event_label)}｜{html.escape(platform_label)}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;color:#222">
<div style="max-width:640px;margin:auto;padding:16px">
<h2>【{html.escape(event_label)}｜{html.escape(platform_label)}】</h2>
<p><strong>卖家：</strong>{html.escape(payload.get('seller_name') or '')}</p>
<p><strong>类型：</strong>{listing_label}</p>
<p><strong>商品：</strong>{html.escape(payload.get('title') or '')}</p>
<p><strong>价格：</strong>{price_line}</p>
{drop_line}<p><strong>检测时间：</strong>{html.escape(payload.get('observed_at') or '')}</p>
{image}<p><a href="{item_url}">查看商品</a></p>
</div></body></html>"""


def notification_title(payload: dict) -> str:
    label = {"new_listing": "新上架", "fixed_price_drop": "降价", "fixed_price_increase": "涨价", "auction_terms_change": "拍卖条件变化"}.get(
        payload.get("event_type"), "商品变化"
    )
    return f"【{label}｜{payload.get('platform', '')}】{payload.get('title', '')}"


class PushPlusNotifier:
    def __init__(self, token: str, *, timeout: tuple[float, float] = (5.0, 15.0), session=None):
        if not token:
            raise ValueError("PUSHPLUS_TOKEN 不能为空")
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()

    def send(self, payload: dict) -> NotificationResult:
        body = {
            "token": self.token,
            "title": notification_title(payload),
            "content": render_notification_html(payload),
            "template": "html",
            "channel": "wechat",
        }
        try:
            response = self.session.post(PUSHPLUS_ENDPOINT, json=body, timeout=self.timeout)
        except requests.ConnectTimeout as exc:
            return NotificationResult(status="retryable_failure", error=str(exc))
        except requests.ReadTimeout as exc:
            return NotificationResult(status="delivery_unknown", error=str(exc))
        except requests.ConnectionError as exc:
            return NotificationResult(status="retryable_failure", error=str(exc))
        except requests.RequestException as exc:
            return NotificationResult(status="delivery_unknown", error=str(exc))

        try:
            data = response.json()
        except ValueError:
            data = {}
        code = str(data.get("code", ""))
        message = str(data.get("msg") or data.get("message") or "")
        provider_id = data.get("data")
        provider_id = str(provider_id) if provider_id not in (None, "") else None
        if response.status_code == 200 and code == "200":
            return NotificationResult(
                status="accepted",
                provider_message_id=provider_id,
                provider_code=code,
                provider_message=message,
                http_status=response.status_code,
            )
        return NotificationResult(
            status="rejected",
            provider_code=code or None,
            provider_message=message or None,
            http_status=response.status_code,
            error=f"PushPlus rejected request: HTTP {response.status_code}, code={code or 'unknown'}",
        )


def write_preview(path: str | Path, payload: dict | None = None) -> Path:
    preview = payload or {
        "event_type": "fixed_price_drop",
        "platform": "yahoo_auctions",
        "seller_name": "测试卖家（不会发送）",
        "listing_type": "auction",
        "title": "剑玉商品通知预览",
        "image_url": "https://placehold.co/800x600?text=Seller+Monitor+Preview",
        "item_url": "https://example.invalid/item/preview",
        "old_price": 8000,
        "new_price": 6500,
        "term_type": None,
        "observed_at": "2026-07-22 14:30:00 +08:00",
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_notification_html(preview), encoding="utf-8")
    return output
