"""Parse Kalshi weather contract language into measurable event conditions.

The parser turns a market's title/subtitle/rules into an :class:`EventCondition`
describing *exactly* what must happen for the YES side to settle true.

It handles both the simplified phrasing used in tests/mock data, e.g.::

    "Will the high temperature in NYC be above 75F on June 18?"

and the **real live Kalshi format**, where the cleanest signal is the
``yes_sub_title`` (the canonical YES definition)::

    title:    "Will the **high temp in NYC** be >89° on Jun 20, 2026?"
    subtitle: "90° or above"        -> high_temp >= 90
    subtitle: "81° or below"        -> high_temp <= 81
    subtitle: "88° to 89°"          -> high_temp in [88, 89]

Anything the parser cannot confidently resolve is surfaced via ``ambiguous`` and
``notes`` so downstream risk controls can avoid the market rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

# Variable keyword groups (checked in order; first match wins).
_VARIABLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("temp_range", re.compile(r"\btemp(?:erature)?\s+range\b|\bdiurnal\b", re.I)),
    ("high_temp", re.compile(r"\b(high(?:est)?|max(?:imum)?)\b[^.]*\btemp", re.I)),
    ("low_temp", re.compile(r"\b(low(?:est)?|min(?:imum)?)\b[^.]*\btemp", re.I)),
    ("snowfall", re.compile(r"\bsnow(?:fall)?\b", re.I)),
    ("rainfall", re.compile(r"\b(rain(?:fall)?|precip(?:itation)?)\b", re.I)),
    ("wind", re.compile(r"\bwind\b", re.I)),
    # Bare "temp"/"temperature" defaults to high temp (most Kalshi temp markets).
    ("high_temp", re.compile(r"\btemp(?:erature)?\b", re.I)),
]

_GTE = re.compile(
    r"(above|over|greater than|more than|at least|or (?:above|higher|greater|more)|"
    r"exceed(?:s|ing)?|≥|>=|>)",
    re.I,
)
_LTE = re.compile(
    r"(below|under|less than|fewer than|at most|or (?:below|lower|less)|"
    r"beneath|≤|<=|<)",
    re.I,
)
_BETWEEN = re.compile(r"\bbetween\b\s*(-?\d+\.?\d*)\s*(?:and|-|to)\s*(-?\d+\.?\d*)", re.I)

# A number possibly with a unit hint.
_NUM = re.compile(r"(-?\d+\.?\d*)\s*(°|degrees?|deg|f\b|fahrenheit|in(?:ch(?:es)?)?|mph)?", re.I)

# --- Subtitle grammar (the canonical YES definition on live markets) ---------
_SUB_GTE = re.compile(r"(-?\d+\.?\d*)\s*°?\s*(?:or (?:above|higher|more|greater)|\+)", re.I)
_SUB_LTE = re.compile(r"(-?\d+\.?\d*)\s*°?\s*(?:or (?:below|lower|less))", re.I)
_SUB_BETWEEN = re.compile(
    r"(-?\d+\.?\d*)\s*°?\s*(?:to|-|–|—|and)\s*(-?\d+\.?\d*)\s*°?", re.I
)

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) +
                       r")\b\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", re.I)

_MARKDOWN = re.compile(r"[*_`]+")


@dataclass
class EventCondition:
    """A measurable, settlement-relevant condition parsed from a contract."""

    variable: str                     # high_temp | low_temp | rainfall | snowfall | wind | temp_range
    operator: str                     # gt | gte | lt | lte | between
    threshold: Optional[float]
    threshold2: Optional[float] = None  # upper bound for "between"
    unit: str = ""                    # F | in | mph
    location_text: str = ""
    target_date: Optional[date] = None
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)
    raw_title: str = ""

    def describe(self) -> str:
        op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "between": "in"}.get(
            self.operator, self.operator)
        if self.operator == "between":
            return f"{self.variable} {op} [{self.threshold}, {self.threshold2}] {self.unit}"
        return f"{self.variable} {op} {self.threshold} {self.unit}".strip()


def _strip_markdown(text: str) -> str:
    return _MARKDOWN.sub("", text or "")


def _detect_variable(text: str) -> tuple[str, bool]:
    for name, pattern in _VARIABLE_PATTERNS:
        if pattern.search(text):
            return name, False
    return "high_temp", True  # default + ambiguity flag


def _default_unit(variable: str) -> str:
    return {
        "high_temp": "F", "low_temp": "F", "temp_range": "F",
        "rainfall": "in", "snowfall": "in", "wind": "mph",
    }.get(variable, "")


def _parse_date(text: str, reference: Optional[date] = None) -> Optional[date]:
    reference = reference or datetime.now(timezone.utc).date()
    low = text.lower()
    if "today" in low or "tonight" in low:
        return reference
    if "tomorrow" in low:
        return date.fromordinal(reference.toordinal() + 1)

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            pass

    match = _MONTH_RE.search(low)
    if match:
        month = _MONTHS[match.group(1).lower().rstrip(".")]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_subtitle(subtitle: str, variable: str) -> Optional[tuple[str, float, Optional[float]]]:
    """Parse a Kalshi YES subtitle into (operator, threshold, threshold2).

    Returns ``None`` if the subtitle does not match a known pattern.
    """
    if not subtitle:
        return None
    s = _strip_markdown(subtitle).strip()
    m = _SUB_GTE.search(s)
    if m:
        return "gte", float(m.group(1)), None
    m = _SUB_LTE.search(s)
    if m:
        return "lte", float(m.group(1)), None
    m = _SUB_BETWEEN.search(s)
    if m:
        a, b = sorted((float(m.group(1)), float(m.group(2))))
        return "between", a, b
    return None


def _parse_threshold(text: str, variable: str) -> tuple[Optional[float], str]:
    match = _NUM.search(text)
    if not match:
        return None, _default_unit(variable)
    value = float(match.group(1))
    unit_hint = (match.group(2) or "").lower()
    if unit_hint in {"°", "degrees", "degree", "deg", "f", "fahrenheit"}:
        unit = "F"
    elif unit_hint.startswith("in"):
        unit = "in"
    elif unit_hint == "mph":
        unit = "mph"
    else:
        unit = _default_unit(variable)
    return value, unit


def parse_contract(
    title: str,
    subtitle: str = "",
    rules: str = "",
    reference_date: Optional[date] = None,
) -> EventCondition:
    """Parse contract text into an :class:`EventCondition`.

    Args:
        title: Market title (primary source of structure).
        subtitle: The canonical YES definition (``yes_sub_title``) when known —
            preferred for the comparison + threshold because it is unambiguous.
        rules: Settlement rule text (used as a fallback / ambiguity check).
        reference_date: "Today" anchor for relative dates (defaults to UTC now).

    Returns:
        A populated :class:`EventCondition`; check ``.ambiguous`` and ``.notes``.
    """
    title_clean = _strip_markdown(title)
    full = " ".join(p for p in (title_clean, subtitle, rules) if p)
    cond = EventCondition(
        variable="high_temp", operator="gte", threshold=None, raw_title=title
    )

    variable, var_ambiguous = _detect_variable(title_clean or full)
    cond.variable = variable
    if var_ambiguous:
        cond.ambiguous = True
        cond.notes.append("Variable not explicit in title; defaulted to high_temp.")

    # 1) Prefer the canonical YES subtitle (unambiguous on live markets).
    sub = _parse_subtitle(subtitle, variable)
    if sub is not None:
        cond.operator, cond.threshold, cond.threshold2 = sub
        cond.unit = _default_unit(variable)
    else:
        # 2) Fall back to title phrasing.
        between = _BETWEEN.search(title_clean) or _BETWEEN.search(full)
        if between:
            cond.operator = "between"
            cond.threshold = float(between.group(1))
            cond.threshold2 = float(between.group(2))
            cond.unit = _default_unit(variable)
        else:
            # Direction: look at the operator that appears before the number.
            if _GTE.search(title_clean):
                cond.operator = "gte"
            elif _LTE.search(title_clean):
                cond.operator = "lte"
            else:
                cond.operator = "gte"
                cond.ambiguous = True
                cond.notes.append("Comparison direction not explicit; assumed >=.")
            value, unit = _parse_threshold(title_clean or full, variable)
            cond.threshold = value
            cond.unit = unit

    if cond.threshold is None:
        cond.ambiguous = True
        cond.notes.append("No numeric threshold found.")

    cond.location_text = title_clean
    cond.target_date = _parse_date(full, reference_date)
    if cond.target_date is None:
        cond.notes.append("Target date not found; using market close date downstream.")

    return cond
