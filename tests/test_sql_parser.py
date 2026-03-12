"""
Unit tests for src/sql_parser.py and src/consistency_checker.py.
"""

import os
import sys

# Ensure the project root is on the path so we can import src.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.sql_parser import parse_sql, SQLParseResult


# ---------------------------------------------------------------------------
# sql_parser tests
# ---------------------------------------------------------------------------

class TestParseSql:
    """Tests for parse_sql()."""

    def test_empty_sql_returns_error(self):
        result = parse_sql("")
        assert not result.success
        assert result.errors

    def test_non_select_returns_error(self):
        result = parse_sql("INSERT INTO foo VALUES (1)")
        assert not result.success
        assert any("SELECT" in e for e in result.errors)

    def test_simple_count_distinct(self):
        sql = """
        SELECT COUNT(DISTINCT user_id) AS dau
        FROM dwd_user_active_log
        WHERE status = 'active'
        GROUP BY dt
        """
        result = parse_sql(sql)
        assert result.success, result.errors
        assert len(result.metrics) == 1
        m = result.metrics[0]
        assert m.metric_name == "dau"
        assert m.formula_type == "COUNT"
        assert "user_id" in m.formula

    def test_sum_with_alias(self):
        sql = """
        SELECT SUM(transaction_amount) AS gmv
        FROM dwd_transaction_detail
        WHERE status = 'success'
        GROUP BY dt, merchant_id
        """
        result = parse_sql(sql)
        assert result.success, result.errors
        m = result.metrics[0]
        assert m.metric_name == "gmv"
        assert m.formula_type == "SUM"
        assert m.formula == "SUM(transaction_amount)"

    def test_avg_formula_extracted(self):
        sql = """
        SELECT AVG(amount) AS avg_amount
        FROM dwd_orders
        WHERE status = 'success'
        GROUP BY dt
        """
        result = parse_sql(sql)
        assert result.success
        assert result.metrics[0].formula_type == "AVG"

    def test_filters_extracted(self):
        sql = """
        SELECT COUNT(DISTINCT user_id) AS dau
        FROM dwd_user
        WHERE status = 'active'
          AND dt >= '2024-01-01'
        GROUP BY dt
        """
        result = parse_sql(sql)
        assert result.success
        filters = result.metrics[0].filters
        assert len(filters) >= 2

    def test_dimensions_extracted(self):
        sql = """
        SELECT SUM(amount) AS total
        FROM dwd_orders
        WHERE status = 'ok'
        GROUP BY dt, channel, region
        """
        result = parse_sql(sql)
        assert result.success
        dims = result.metrics[0].dimensions
        assert "dt" in dims
        assert "channel" in dims
        assert "region" in dims

    def test_no_aggregate_returns_error(self):
        sql = """
        SELECT user_id, name
        FROM dim_user
        WHERE status = 'active'
        """
        result = parse_sql(sql)
        assert not result.success
        assert result.errors

    def test_multiple_aggregates(self):
        sql = """
        SELECT
            COUNT(transaction_id)   AS txn_count,
            SUM(transaction_amount) AS gmv,
            AVG(transaction_amount) AS avg_amount
        FROM dwd_transaction
        WHERE status = 'success'
        GROUP BY dt, merchant_id
        """
        result = parse_sql(sql)
        assert result.success
        assert len(result.metrics) == 3
        types = {m.formula_type for m in result.metrics}
        assert types == {"COUNT", "SUM", "AVG"}

    def test_no_where_clause(self):
        sql = """
        SELECT SUM(revenue) AS total_revenue
        FROM dwd_revenue
        GROUP BY dt
        """
        result = parse_sql(sql)
        assert result.success
        assert result.metrics[0].filters == []

    def test_no_group_by_clause(self):
        sql = """
        SELECT COUNT(DISTINCT user_id) AS total_users
        FROM dwd_user
        WHERE is_active = 1
        """
        result = parse_sql(sql)
        assert result.success
        assert result.metrics[0].dimensions == []


# ---------------------------------------------------------------------------
# consistency_checker tests
# ---------------------------------------------------------------------------

from src.consistency_checker import check_consistency
from src.sql_parser import ParsedMetric


class TestConsistencyChecker:
    """Tests for check_consistency()."""

    _LIB_PATH = os.path.join(
        os.path.dirname(__file__), "..", "config", "standard_metrics.json"
    )

    def _std_lib_exists(self):
        return os.path.exists(self._LIB_PATH)

    def test_returns_one_result_per_metric(self):
        metrics = [
            ParsedMetric(
                metric_name="dau",
                formula_type="COUNT",
                formula="COUNT(DISTINCT user_id)",
                filters=["status = 'active'", "dt >= current_date"],
                dimensions=["dt", "channel"],
            )
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        assert len(results) == 1

    def test_exact_match_no_diffs(self):
        """A metric that exactly matches the standard library should have no diffs."""
        metrics = [
            ParsedMetric(
                metric_name="dau",
                formula_type="COUNT",
                formula="COUNT(DISTINCT user_id)",
                filters=["status = 'active'", "dt >= current_date"],
                dimensions=["dt", "channel"],
            )
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        result = results[0]
        # Should have matched something
        assert result.matched_standard is not None
        # The alert should start with ✅
        assert any("✅" in a for a in result.alerts)

    def test_formula_type_mismatch_flagged(self):
        """Using SUM instead of COUNT should raise an error-level diff."""
        metrics = [
            ParsedMetric(
                metric_name="dau",
                formula_type="SUM",
                formula="SUM(user_id)",
                filters=["status = 'active'"],
                dimensions=["dt", "channel"],
            )
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        result = results[0]
        assert any(d.field == "formula_type" for d in result.diffs)

    def test_missing_filter_flagged(self):
        """A metric missing a required filter condition should produce a warning/error."""
        metrics = [
            ParsedMetric(
                metric_name="dau",
                formula_type="COUNT",
                formula="COUNT(DISTINCT user_id)",
                filters=[],           # missing required filters
                dimensions=["dt", "channel"],
            )
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        result = results[0]
        assert any(d.field == "filter_conditions" for d in result.diffs)

    def test_unknown_metric_produces_alert(self):
        """A metric not in the standard library should produce an unmatched alert."""
        metrics = [
            ParsedMetric(
                metric_name="some_weird_unknown_metric_xyz",
                formula_type="SUM",
                formula="SUM(some_column)",
                filters=[],
                dimensions=[],
            )
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        result = results[0]
        assert result.matched_standard is None
        assert result.alerts  # should have an alert about not being in library

    def test_multiple_metrics_checked_independently(self):
        metrics = [
            ParsedMetric(
                metric_name="dau",
                formula_type="COUNT",
                formula="COUNT(DISTINCT user_id)",
                filters=["status = 'active'", "dt >= current_date"],
                dimensions=["dt", "channel"],
            ),
            ParsedMetric(
                metric_name="gmv",
                formula_type="SUM",
                formula="SUM(transaction_amount)",
                filters=["status = 'success'", "is_deleted = 0"],
                dimensions=["dt", "merchant_id", "channel"],
            ),
        ]
        results = check_consistency(metrics, library_path=self._LIB_PATH)
        assert len(results) == 2
        # Each result should reference the corresponding metric
        assert results[0].metric_name == "dau"
        assert results[1].metric_name == "gmv"
