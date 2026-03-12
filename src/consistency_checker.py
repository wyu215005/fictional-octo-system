"""
Consistency Checker Module
--------------------------
Compares extracted metrics against a standard metric library (JSON) and
produces structured diff / alert messages.

Standard library format (config/standard_metrics.json):
  {
    "metrics": [
      {
        "name": "...",
        "alias": ["...", ...],
        "formula_type": "SUM|COUNT|AVG|...",
        "formula": "SUM(amount)",
        "filter_conditions": ["status = 'active'"],
        "dimensions": ["dt", "channel"],
        "description": "..."
      }
    ]
  }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .sql_parser import ParsedMetric

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_DEFAULT_LIBRARY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "standard_metrics.json"
)


@dataclass
class DiffItem:
    """A single difference between an extracted metric and its standard definition."""

    field: str
    expected: str
    actual: str
    severity: str  # "error" | "warning" | "info"


@dataclass
class CheckResult:
    """Result of comparing one extracted metric against the standard library."""

    metric_name: str
    matched_standard: Optional[str]   # name of the matched standard metric, or None
    match_score: float                 # 0.0 – 1.0
    diffs: List[DiffItem] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return len(self.diffs) > 0 or len(self.alerts) > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_standard_library(path: str = _DEFAULT_LIBRARY_PATH) -> List[Dict]:
    """Load and return the list of standard metric definitions."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("metrics", [])


def _normalise_formula(formula: str) -> str:
    """Strip whitespace and convert to upper-case for comparison."""
    return re.sub(r"\s+", "", formula).upper()


def _name_similarity(a: str, b: str) -> float:
    """Simple token overlap similarity between two strings (0–1)."""
    a_tokens = set(re.split(r"[\W_]+", a.lower()))
    b_tokens = set(re.split(r"[\W_]+", b.lower()))
    a_tokens.discard("")
    b_tokens.discard("")
    if not a_tokens and not b_tokens:
        return 1.0
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _match_standard(
    metric: ParsedMetric, library: List[Dict]
) -> Optional[tuple[Dict, float]]:
    """
    Find the best matching standard metric for *metric*.
    Returns (standard_dict, score) or None.
    """
    best: Optional[tuple[Dict, float]] = None
    best_score = 0.0

    for std in library:
        # Score based on name / alias similarity
        candidates = [std.get("name", "")] + list(std.get("alias", []))
        name_score = max(_name_similarity(metric.metric_name, c) for c in candidates)

        # Boost for matching formula_type
        type_score = (
            1.0
            if std.get("formula_type", "").upper() == metric.formula_type.upper()
            else 0.0
        )

        # Boost for matching formula body (normalised)
        formula_score = (
            1.0
            if _normalise_formula(std.get("formula", ""))
            == _normalise_formula(metric.formula)
            else 0.0
        )

        score = 0.5 * name_score + 0.3 * type_score + 0.2 * formula_score

        if score > best_score:
            best_score = score
            best = (std, score)

    if best and best_score > 0.35:
        return best
    return None


def _compare_filters(
    actual_filters: List[str], expected_filters: List[str]
) -> List[DiffItem]:
    """Return DiffItems for filter condition differences."""
    diffs: List[DiffItem] = []

    norm_actual = {re.sub(r"\s+", "", f).upper() for f in actual_filters}
    norm_expected = {re.sub(r"\s+", "", f).upper() for f in expected_filters}

    missing = norm_expected - norm_actual
    extra = norm_actual - norm_expected

    if missing:
        diffs.append(
            DiffItem(
                field="filter_conditions",
                expected=", ".join(sorted(missing)),
                actual="(缺失)",
                severity="error",
            )
        )
    if extra:
        diffs.append(
            DiffItem(
                field="filter_conditions",
                expected="(标准库中无此条件)",
                actual=", ".join(sorted(extra)),
                severity="warning",
            )
        )
    return diffs


def _compare_dimensions(
    actual_dims: List[str], expected_dims: List[str]
) -> List[DiffItem]:
    """Return DiffItems for GROUP BY dimension differences."""
    diffs: List[DiffItem] = []

    norm_actual = {d.strip().upper() for d in actual_dims}
    norm_expected = {d.strip().upper() for d in expected_dims}

    missing = norm_expected - norm_actual
    extra = norm_actual - norm_expected

    if missing:
        diffs.append(
            DiffItem(
                field="dimensions",
                expected=", ".join(sorted(missing)),
                actual="(缺失)",
                severity="warning",
            )
        )
    if extra:
        diffs.append(
            DiffItem(
                field="dimensions",
                expected="(标准库中无此维度)",
                actual=", ".join(sorted(extra)),
                severity="info",
            )
        )
    return diffs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_consistency(
    metrics: List[ParsedMetric],
    library_path: str = _DEFAULT_LIBRARY_PATH,
) -> List[CheckResult]:
    """
    Compare a list of extracted metrics against the standard metric library.

    Parameters
    ----------
    metrics : list of ParsedMetric
    library_path : str, optional
        Path to the standard_metrics.json file.

    Returns
    -------
    list of CheckResult
    """
    library = _load_standard_library(library_path)
    results: List[CheckResult] = []

    for metric in metrics:
        match_info = _match_standard(metric, library)

        if match_info is None:
            # No standard definition found
            result = CheckResult(
                metric_name=metric.metric_name,
                matched_standard=None,
                match_score=0.0,
                alerts=["⚠️  未在标准指标库中找到匹配的指标定义，请确认指标口径是否已登记"],
            )
            results.append(result)
            continue

        std, score = match_info
        diffs: List[DiffItem] = []
        alerts: List[str] = []

        # Compare formula type
        if std.get("formula_type", "").upper() != metric.formula_type.upper():
            diffs.append(
                DiffItem(
                    field="formula_type",
                    expected=std.get("formula_type", ""),
                    actual=metric.formula_type,
                    severity="error",
                )
            )
            alerts.append(
                f"❌  聚合函数类型不匹配：标准为 {std.get('formula_type')}，实际为 {metric.formula_type}"
            )

        # Compare formula body
        if _normalise_formula(std.get("formula", "")) != _normalise_formula(
            metric.formula
        ):
            diffs.append(
                DiffItem(
                    field="formula",
                    expected=std.get("formula", ""),
                    actual=metric.formula,
                    severity="error",
                )
            )
            alerts.append(
                f"❌  计算公式不匹配：标准为 [{std.get('formula')}]，实际为 [{metric.formula}]"
            )

        # Compare filter conditions
        filter_diffs = _compare_filters(
            metric.filters, std.get("filter_conditions", [])
        )
        diffs.extend(filter_diffs)
        for d in filter_diffs:
            if d.severity == "error":
                alerts.append(f"❌  过滤条件缺失（标准要求）: {d.expected}")
            elif d.severity == "warning":
                alerts.append(f"⚠️  过滤条件多余（标准库无此条件）: {d.actual}")

        # Compare dimensions
        dim_diffs = _compare_dimensions(
            metric.dimensions, std.get("dimensions", [])
        )
        diffs.extend(dim_diffs)
        for d in dim_diffs:
            if d.severity == "warning":
                alerts.append(f"⚠️  GROUP BY 维度缺失（标准要求）: {d.expected}")
            elif d.severity == "info":
                alerts.append(f"ℹ️  GROUP BY 维度多余（标准库无此维度）: {d.actual}")

        if not alerts:
            alerts.append("✅  指标口径与标准库一致，无差异")

        results.append(
            CheckResult(
                metric_name=metric.metric_name,
                matched_standard=std.get("name", ""),
                match_score=round(score, 3),
                diffs=diffs,
                alerts=alerts,
            )
        )

    return results
