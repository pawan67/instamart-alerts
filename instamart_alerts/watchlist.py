"""Watch definitions and the rule that decides what counts as a hit."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .instamart import Product


@dataclass(frozen=True)
class Watch:
    name: str
    query: str
    min_discount_pct: float
    # Instamart pads search results with sponsored/related items from other
    # aisles, so constrain to the categories the query is really about.
    categories: tuple[str, ...] = ()
    # Optional substring filters on the product name (case-insensitive).
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_price: float | None = None
    in_stock_only: bool = True
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "query": self.query,
            "min_discount_pct": self.min_discount_pct,
            "categories": list(self.categories),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_price": self.max_price,
            "in_stock_only": self.in_stock_only,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, e: dict[str, Any]) -> Watch:
        return cls(
            name=(e.get("name") or e["query"]).strip(),
            query=e["query"].strip(),
            min_discount_pct=float(e["min_discount_pct"]),
            categories=tuple(e.get("categories") or ()),
            include=tuple(e.get("include") or ()),
            exclude=tuple(e.get("exclude") or ()),
            max_price=(float(e["max_price"]) if e.get("max_price") is not None else None),
            in_stock_only=bool(e.get("in_stock_only", True)),
            enabled=bool(e.get("enabled", True)),
        )

    def matches(self, p: Product) -> bool:
        if self.in_stock_only and not p.in_stock:
            return False
        if self.categories:
            haystack = f"{p.category} {p.sub_category}".lower()
            if not any(c.lower() in haystack for c in self.categories):
                return False
        name = p.name.lower()
        if self.include and not any(s.lower() in name for s in self.include):
            return False
        if any(s.lower() in name for s in self.exclude):
            return False
        return True

    def is_hit(self, p: Product) -> bool:
        if not self.matches(p):
            return False
        if self.max_price is not None and p.price > self.max_price:
            return False
        return p.discount_pct >= self.min_discount_pct


@dataclass
class Watchlist:
    watches: list[Watch] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Watchlist:
        if not path.exists():
            return cls([])
        try:
            raw: Any = json.loads(path.read_text())
            entries = raw["watches"] if isinstance(raw, dict) else raw
            return cls([Watch.from_dict(e) for e in entries])
        except (json.JSONDecodeError, OSError):
            return cls([])

    def save(self, path: Path) -> None:
        """Write atomically — the web UI and a running poller can both read this."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"watches": [w.to_dict() for w in self.watches]}, indent=2)
        )
        tmp.replace(path)

    @property
    def active(self) -> list[Watch]:
        return [w for w in self.watches if w.enabled]
