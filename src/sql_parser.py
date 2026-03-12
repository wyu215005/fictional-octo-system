"""
SQL Parser Module
-----------------
Extracts metrics information from Hive/Spark SQL using sqlparse and regex.

Extracted fields:
  - metric_name  : inferred from aliases or table context
  - formula_type : SUM / COUNT / AVG / MAX / MIN / OTHER
  - formula      : the full aggregate expression
  - filters      : list of WHERE conditions
  - dimensions   : list of GROUP BY columns
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

import sqlparse
from sqlparse.sql import IdentifierList, Identifier, Where
from sqlparse.tokens import Keyword, DML, Punctuation


@dataclass
class ParsedMetric:
    """Represents a single extracted metric from a SQL statement."""

    metric_name: str
    formula_type: str
    formula: str
    filters: List[str]
    dimensions: List[str]


@dataclass
class SQLParseResult:
    """Full parse result for one SQL statement."""

    raw_sql: str
    metrics: List[ParsedMetric] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0 and len(self.metrics) > 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_AGGREGATE_PATTERN = re.compile(
    r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\([^)]*\)",
    re.IGNORECASE,
)

_ALIAS_AFTER_AGG_PATTERN = re.compile(
    r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\([^)]*\)\s+(?:AS\s+)?(\w+)",
    re.IGNORECASE,
)


def _normalise(sql: str) -> str:
    """Strip comments and normalise whitespace."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return " ".join(sql.split())


def _extract_select_columns(sql_upper: str) -> List[str]:
    """Return the raw text between SELECT and FROM."""
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql_upper, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    columns_str = match.group(1).strip()
    # Split by commas that are not inside parentheses
    depth = 0
    current: List[str] = []
    parts: List[str] = []
    for ch in columns_str:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def _extract_where_conditions(sql: str) -> List[str]:
    """Extract individual conditions from the WHERE clause."""
    match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    where_str = match.group(1).strip()
    # Split on AND / OR at depth 0
    depth = 0
    current: List[str] = []
    conditions: List[str] = []
    tokens = re.split(r"(\bAND\b|\bOR\b|\(|\))", where_str, flags=re.IGNORECASE)
    for token in tokens:
        t = token.strip()
        if t == "(":
            depth += 1
            current.append(token)
        elif t == ")":
            depth -= 1
            current.append(token)
        elif t.upper() in ("AND", "OR") and depth == 0:
            cond = " ".join(current).strip()
            if cond:
                conditions.append(cond)
            current = []
        else:
            current.append(token)
    last = " ".join(current).strip()
    if last:
        conditions.append(last)
    return [c for c in conditions if c]


def _extract_group_by_columns(sql: str) -> List[str]:
    """Extract columns listed in GROUP BY."""
    match = re.search(
        r"\bGROUP\s+BY\b(.*?)(?:\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    group_str = match.group(1).strip()
    # Split by comma, strip whitespace
    cols = [c.strip() for c in group_str.split(",") if c.strip()]
    return cols


def _infer_metric_name(col_expr: str) -> str:
    """Try to pull an alias from a column expression, else build a name."""
    alias_match = re.search(r"\bAS\s+(\w+)\s*$", col_expr, re.IGNORECASE)
    if alias_match:
        return alias_match.group(1)
    # No explicit alias — try to extract from plain aggregate
    agg_match = re.search(
        r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(([^)]*)\)", col_expr, re.IGNORECASE
    )
    if agg_match:
        func = agg_match.group(1).upper()
        inner = agg_match.group(2).strip()
        inner = re.sub(r"\bDISTINCT\b\s*", "", inner, flags=re.IGNORECASE).strip()
        return f"{func.lower()}_{inner}".replace(".", "_").replace("*", "all")
    return col_expr.strip()


def _parse_column(col_expr: str) -> Optional[ParsedMetric]:
    """Parse a single SELECT column expression and return a ParsedMetric if it contains an aggregate."""
    agg_match = re.search(
        r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\([^)]*\)", col_expr, re.IGNORECASE
    )
    if not agg_match:
        return None

    formula_type = agg_match.group(1).upper()
    # Extract the full aggregate expression (may span nested parens for CASE WHEN)
    start = agg_match.start()
    # find the matching closing paren
    depth = 0
    end = start
    for i, ch in enumerate(col_expr[start:], start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    formula = col_expr[start:end].strip()
    metric_name = _infer_metric_name(col_expr)

    return ParsedMetric(
        metric_name=metric_name,
        formula_type=formula_type,
        formula=formula,
        filters=[],
        dimensions=[],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_sql(sql: str) -> SQLParseResult:
    """
    Parse a single SQL statement and extract metric information.

    Parameters
    ----------
    sql : str
        A Hive/Spark SQL SELECT statement.

    Returns
    -------
    SQLParseResult
    """
    result = SQLParseResult(raw_sql=sql)

    if not sql or not sql.strip():
        result.errors.append("SQL 语句为空")
        return result

    normalised = _normalise(sql)

    # Basic sanity check — must be a SELECT statement
    if not re.match(r"^\s*SELECT\b", normalised, re.IGNORECASE):
        result.errors.append("仅支持 SELECT 语句")
        return result

    try:
        # Extract components
        columns = _extract_select_columns(normalised)
        filters = _extract_where_conditions(normalised)
        dimensions = _extract_group_by_columns(normalised)

        if not columns:
            result.errors.append("无法解析 SELECT 子句中的列")
            return result

        for col in columns:
            metric = _parse_column(col)
            if metric:
                metric.filters = filters
                metric.dimensions = dimensions
                result.metrics.append(metric)

        if not result.metrics:
            result.errors.append(
                "SELECT 子句中未发现聚合函数（SUM/COUNT/AVG/MAX/MIN），无指标可提取"
            )

    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"解析异常: {exc}")

    return result
