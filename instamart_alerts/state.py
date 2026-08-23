"""Alert de-duplication.

A price monitor that re-sends on every poll is noise. We remember the price each
SKU was last alerted at and stay quiet until something genuinely new happens:
the price falls further, the deal lapsed and came back, or the cooldown expires.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .instamart import Product

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_HOURS = 24.0


@dataclass
class AlertState:
    path: Path
    seen: dict[str, dict[str, Any]]
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS

    @classmethod
    def load(cls, path: Path, cooldown_hours: float = DEFAULT_COOLDOWN_HOURS) -> AlertState:
        seen: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                seen = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("resetting unreadable alert state: %s", e)
        return cls(path=path, seen=seen, cooldown_hours=cooldown_hours)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.seen, indent=2, sort_keys=True))

    def _key(self, watch_name: str, p: Product) -> str:
        return f"{watch_name}::{p.sku_id}"

    def should_alert(self, watch_name: str, p: Product) -> bool:
        prev = self.seen.get(self._key(watch_name, p))
        if not prev:
            return True
        if not prev.get("hit"):
            # Was below threshold last run and has now crossed back over.
            return True
        if p.price < float(prev.get("price", p.price)):
            return True  # a deeper cut is worth re-flagging
        age_hours = (time.time() - float(prev.get("ts", 0))) / 3600
        return age_hours >= self.cooldown_hours

    def record(self, watch_name: str, p: Product, *, hit: bool) -> None:
        self.seen[self._key(watch_name, p)] = {
            "name": p.name,
            "price": p.price,
            "mrp": p.mrp,
            "discount_pct": p.discount_pct,
            "hit": hit,
            "ts": time.time(),
        }

    def prune(self, live_keys: set[str], max_age_days: float = 30.0) -> None:
        """Drop entries for SKUs that have not been seen in a long time."""
        cutoff = time.time() - max_age_days * 86400
        for k in list(self.seen):
            if k not in live_keys and float(self.seen[k].get("ts", 0)) < cutoff:
                del self.seen[k]
