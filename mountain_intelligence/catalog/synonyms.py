"""Controlled bilingual (es/en) vocabulary used to expand free-text search terms.

Synonyms only widen *text* matching. They never satisfy a structured requirement: filters and
mandatory requirements are always evaluated on normalized specification values.
"""

from __future__ import annotations

from .normalization import fold

_GROUPS: list[tuple[str, ...]] = [
    ("bota", "botas", "boot", "boots"),
    ("zapatilla", "zapatillas", "shoe", "shoes"),
    ("crampon", "crampones", "crampons"),
    ("mochila", "mochilas", "backpack", "pack", "rucksack"),
    ("chaqueta", "chaquetas", "jacket", "jackets", "chamarra"),
    ("impermeable", "impermeables", "waterproof", "hardshell", "shell"),
    ("plumas", "plumifero", "down", "puffer"),
    ("polar", "fleece", "forro"),
    ("aislante", "insulated", "insulation", "aislamiento"),
    ("casco", "helmet"),
    ("piolet", "piolets", "axe"),
    ("alpinismo", "mountaineering", "alpine", "alpinista"),
    ("invierno", "invernal", "winter"),
    ("trekking", "senderismo", "hiking", "excursionismo"),
    ("ligero", "ligera", "light", "lightweight", "ultralight"),
    ("capucha", "hood"),
    ("ventilacion", "ventilaciones", "vents", "pit"),
    ("semiautomatico", "semiautomaticos", "semi"),
    ("automatico", "automaticos", "automatic", "step"),
    ("correas", "strap", "universal"),
    ("membrana", "membrane"),
    ("cuero", "leather", "piel"),
    ("hielo", "ice"),
    ("nieve", "snow"),
]

STOPWORDS = {
    fold(w)
    for w in [
        "de",
        "del",
        "la",
        "las",
        "el",
        "los",
        "un",
        "una",
        "unos",
        "unas",
        "y",
        "o",
        "a",
        "en",
        "con",
        "sin",
        "para",
        "por",
        "que",
        "mas",
        "menos",
        "muy",
        "mi",
        "me",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "with",
        "without",
        "to",
        "in",
        "on",
        "my",
        "me",
        "is",
        "are",
        "under",
        "over",
        "less",
        "than",
        "more",
        "busca",
        "buscar",
        "quiero",
        "necesito",
        "recomiendame",
        "dame",
        "algo",
    ]
}

_INDEX: dict[str, tuple[str, ...]] = {}
for group in _GROUPS:
    folded = tuple(fold(term) for term in group)
    for term in folded:
        _INDEX[term] = folded


def expand(token: str) -> tuple[str, ...]:
    return _INDEX.get(token, (token,))
