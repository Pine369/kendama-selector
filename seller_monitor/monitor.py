"""Seller-monitor orchestration and event detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from seller_monitor.config import MonitorConfig
from seller_monitor.models import ListingSnapshot, NotificationResult
from seller_monitor.repository import SellerMonitorRepository
from seller_monitor.utils import atomic_write_json, event_key, item_identity, utc_now


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    mode: str
    status: str
    seller_total: int
    seller_succeeded: int
    seller_failed: int
    event_count: int
    accepted_count: int
    started_at: str
    finished_at: str


class SellerMonitorService:
    def __init__(self, repository: SellerMonitorRepository, adapters: dict, notifier=None):
        self.repository = repository
        self.adapters = adapters
        self.notifier = notifier

    @staticmethod
    def _payload(snapshot: ListingSnapshot, event_type: str, old_price, new_price, term_type=None) -> dict:
        return {
            "event_type": event_type,
            "term_type": term_type,
            "platform": snapshot.platform,
            "seller_key": snapshot.seller_key,
            "seller_name": snapshot.seller_name,
            "seller_url": snapshot.seller_url,
            "item_id": snapshot.item_id,
            "item_url": snapshot.item_url,
            "title": snapshot.title,
            "image_url": snapshot.image_url,
            "listing_type": snapshot.listing_type,
            "current_price": snapshot.current_price,
            "old_price": old_price,
            "new_price": new_price,
            "observed_at": snapshot.observed_at or utc_now(),
        }

    def _create_event(self, run_id, snapshot, change, event_type, old_price, new_price, term_type=None) -> bool:
        key = event_key(
            snapshot.platform,
            change.identity_key,
            event_type,
            new_price=new_price,
            term_type=term_type,
        )
        return self.repository.create_event(
            event_key=key,
            run_id=run_id,
            item_row_id=change.item_row_id,
            event_type=event_type,
            term_type=term_type,
            old_price=old_price,
            new_price=new_price,
            payload=self._payload(snapshot, event_type, old_price, new_price, term_type),
        )

    def _process_snapshot(
        self,
        run_id: str,
        snapshot: ListingSnapshot,
        baseline: bool,
        notify_price_increase: bool = False,
    ) -> tuple[str, int]:
        identity_key, _ = item_identity(snapshot)
        change = self.repository.upsert_snapshot(run_id, snapshot)
        if baseline:
            return identity_key, 0
        created = 0
        if change.is_new:
            created += int(
                self._create_event(
                    run_id, snapshot, change, "new_listing", None, snapshot.current_price
                )
            )
            return identity_key, created

        if (
            snapshot.listing_type == "fixed"
            and change.previous_price is not None
            and snapshot.current_price is not None
            and snapshot.current_price < change.previous_price
        ):
            created += int(
                self._create_event(
                    run_id,
                    snapshot,
                    change,
                    "fixed_price_drop",
                    change.previous_price,
                    snapshot.current_price,
                )
            )
        elif (
            notify_price_increase
            and snapshot.listing_type == "fixed"
            and change.previous_price is not None
            and snapshot.current_price is not None
            and snapshot.current_price > change.previous_price
        ):
            created += int(
                self._create_event(
                    run_id,
                    snapshot,
                    change,
                    "fixed_price_increase",
                    change.previous_price,
                    snapshot.current_price,
                )
            )
        if snapshot.listing_type == "auction":
            terms = (
                ("start_price", change.previous_auction_start_price, snapshot.auction_start_price),
                ("buyout_price", change.previous_auction_buyout_price, snapshot.auction_buyout_price),
            )
            for term_type, old_price, new_price in terms:
                if old_price is not None and new_price is not None and old_price != new_price:
                    created += int(
                        self._create_event(
                            run_id,
                            snapshot,
                            change,
                            "auction_terms_change",
                            old_price,
                            new_price,
                            term_type,
                        )
                    )
        return identity_key, created

    def _dispatch_pending(self) -> int:
        if self.notifier is None:
            return 0
        accepted = 0
        for event in self.repository.pending_events():
            attempt = self.repository.claim_event(event["event_key"])
            if attempt is None:
                continue
            try:
                result = self.notifier.send(event["payload"])
            except Exception as exc:  # notifier implementations are an external boundary
                result = NotificationResult(status="delivery_unknown", error=str(exc))
            self.repository.finish_attempt(event["event_key"], attempt, result)
            accepted += int(result.status == "accepted")
        return accepted

    def run(self, config: MonitorConfig, *, mode: str = "once") -> RunSummary:
        started_at = utc_now()
        self.repository.initialize()
        self.repository.sync_sellers(config.sellers)
        sellers = self.repository.active_sellers(seller.seller_key for seller in config.sellers)
        run_id = self.repository.start_run(mode, len(sellers))
        succeeded = failed = events = 0
        fatal_error = None
        try:
            for seller in sellers:
                if mode == "bootstrap" and seller.baseline_completed_at:
                    continue
                check_id = self.repository.start_check(run_id, seller.seller_key)
                adapter = self.adapters.get(seller.platform)
                if adapter is None:
                    error = f"未注册平台适配器: {seller.platform}"
                    self.repository.mark_seller_error(seller.seller_key, error)
                    self.repository.finish_check(check_id, "failed", error=error)
                    failed += 1
                    continue
                try:
                    result = adapter.fetch_seller(seller)
                    baseline = seller.baseline_completed_at is None
                    seen = set()
                    seller_events = 0
                    for snapshot in result.snapshots:
                        if snapshot.seller_key != seller.seller_key or snapshot.platform != seller.platform:
                            raise ValueError("适配器返回了不属于当前卖家的商品")
                        identity, count = self._process_snapshot(
                            run_id,
                            snapshot,
                            baseline,
                            config.notify_price_increase,
                        )
                        seen.add(identity)
                        seller_events += count
                    if result.complete:
                        self.repository.mark_missing(seller.seller_key, seen, utc_now())
                    self.repository.mark_seller_success(
                        seller.seller_key,
                        complete_baseline=baseline and result.complete,
                    )
                    check_status = "success" if result.complete else "partial_failure"
                    self.repository.finish_check(check_id, check_status, result, seller_events)
                    events += seller_events
                    if result.complete:
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    self.repository.mark_seller_error(seller.seller_key, error)
                    self.repository.finish_check(check_id, "failed", error=error)
                    failed += 1
            # Bootstrap is deliberately notification-silent, including old
            # pending events. A later normal --once run may dispatch them.
            accepted = 0 if mode == "bootstrap" else self._dispatch_pending()
            status = "success" if failed == 0 else "partial_failure"
        except Exception as exc:
            accepted = 0
            status = "failed"
            fatal_error = f"{type(exc).__name__}: {exc}"
        self.repository.finish_run(run_id, status, succeeded, failed, events, accepted, fatal_error)
        summary = RunSummary(
            run_id=run_id,
            mode=mode,
            status=status,
            seller_total=len(sellers),
            seller_succeeded=succeeded,
            seller_failed=failed,
            event_count=events,
            accepted_count=accepted,
            started_at=started_at,
            finished_at=utc_now(),
        )
        atomic_write_json(config.state_path, asdict(summary))
        return summary
