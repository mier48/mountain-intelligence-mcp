"""Category classification shared by website adapters.

Classification never limits *discovery*: every product is extracted, then classified.

Source config keys:

- ``category_rules``: ``[{"pattern": regex, "category": slug, "field": "any"}]``, first match wins.
  ``field`` selects what the pattern is matched against: ``url``, ``name``, ``source_category``,
  ``product_type``, ``tags``, ``url_and_category`` (URL + manufacturer category: the historical
  JSON-LD behaviour) or ``any`` (all of them joined). The default is ``category_rule_field`` from the
  source config, else the adapter's own default.
- ``default_category``: legacy single-category sources. Used when no rule matches.

When nothing matches and there is no default, the product is ``uncategorized`` (basis
``fallback``): it is kept with its original metadata instead of being dropped or forced into a
technical category it may not belong to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mountain_intelligence.catalog.taxonomy import UNCATEGORIZED_SLUG

SIGNALS = ("url", "name", "source_category", "product_type", "tags")
RULE_FIELDS = (*SIGNALS, "url_and_category", "any")


@dataclass(frozen=True)
class Classification:
    category: str
    basis: str  # rule | default | fallback
    rule: str = ""


def classify(cfg: dict[str, Any], *, default_field: str = "any", **signals: Any) -> Classification:
    """Classify from signals (url, name, source_category, product_type, tags)."""
    texts: dict[str, str] = {}
    for key in SIGNALS:
        value = signals.get(key) or ""
        texts[key] = " ".join(value) if isinstance(value, (list, tuple)) else str(value)
    texts["url_and_category"] = f"{texts['url']} {texts['source_category']}"
    texts["any"] = " ".join(texts[k] for k in SIGNALS)
    fallback_field = cfg.get("category_rule_field", default_field)
    for rule in cfg.get("category_rules", []):
        haystack = texts.get(rule.get("field", fallback_field), texts["any"])
        if re.search(rule["pattern"], haystack, re.IGNORECASE):
            return Classification(str(rule["category"]), "rule", str(rule["pattern"])[:200])
    if cfg.get("default_category"):
        return Classification(str(cfg["default_category"]), "default")
    return Classification(UNCATEGORIZED_SLUG, "fallback")
