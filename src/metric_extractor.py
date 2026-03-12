"""
Metric Extractor Module
-----------------------
Uses LangChain + an LLM to extract richer metric metadata from SQL.
Falls back to the pure regex-based sql_parser when no LLM is configured.

Environment variables (optional):
  OPENAI_API_KEY   — OpenAI API key
  OPENAI_BASE_URL  — Custom base URL (e.g. for Azure or local proxy)
  OPENAI_MODEL     — Model name, default "gpt-3.5-turbo"

When the LLM is unavailable the module silently uses the regex parser and
annotates the result so the UI can display the mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from .sql_parser import ParsedMetric, SQLParseResult, parse_sql

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LangChain integration (optional)
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT_TEMPLATE = """你是一位数据仓库专家。请从下面的 Hive/Spark SQL 中提取所有指标信息。

SQL:
{sql}

请以 JSON 数组格式返回，每个指标包含以下字段：
- metric_name: 指标名称（使用 AS 别名，若无别名则根据函数和字段推断中文或英文名称）
- formula_type: 聚合函数类型（SUM / COUNT / AVG / MAX / MIN / OTHER）
- formula: 完整的聚合表达式，例如 SUM(amount) 或 COUNT(DISTINCT user_id)
- filters: WHERE 条件列表（字符串数组）
- dimensions: GROUP BY 的维度列表（字符串数组）

只返回 JSON，不要有任何额外的文字说明。格式示例：
[
  {{
    "metric_name": "日活跃用户数",
    "formula_type": "COUNT",
    "formula": "COUNT(DISTINCT user_id)",
    "filters": ["status = 'active'", "dt = '2024-01-01'"],
    "dimensions": ["dt", "channel"]
  }}
]
"""


def _build_llm():
    """Attempt to construct a LangChain LLM. Returns None if not configured."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("sk-placeholder"):
        return None

    try:
        from langchain_openai import ChatOpenAI  # type: ignore

        model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
        base_url = os.getenv("OPENAI_BASE_URL")
        kwargs: Dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "openai_api_key": api_key,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    except ImportError:
        logger.warning("langchain_openai 未安装，回退到正则解析模式")
        return None
    except Exception as exc:
        logger.warning("LLM 初始化失败: %s，回退到正则解析模式", exc)
        return None


def _parse_llm_response(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Try to extract a JSON array from the LLM response text."""
    # Sometimes the model wraps the JSON in code fences
    code_fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_fence:
        raw = code_fence.group(1)

    # Try to find the first '[' ... ']' block
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _llm_extract(sql: str, llm) -> Optional[SQLParseResult]:
    """Call the LLM and convert its response to a SQLParseResult."""
    try:
        from langchain_core.messages import HumanMessage  # type: ignore

        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(sql=sql)
        response = llm.invoke([HumanMessage(content=prompt)])
        raw_content = response.content if hasattr(response, "content") else str(response)

        parsed = _parse_llm_response(raw_content)
        if not parsed:
            logger.warning("LLM 返回内容无法解析为 JSON，回退到正则解析")
            return None

        result = SQLParseResult(raw_sql=sql)
        for item in parsed:
            result.metrics.append(
                ParsedMetric(
                    metric_name=item.get("metric_name", "未命名指标"),
                    formula_type=(item.get("formula_type") or "OTHER").upper(),
                    formula=item.get("formula", ""),
                    filters=item.get("filters") or [],
                    dimensions=item.get("dimensions") or [],
                )
            )
        return result
    except Exception as exc:
        logger.warning("LLM 提取失败: %s，回退到正则解析", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metrics(sql: str) -> tuple[SQLParseResult, str]:
    """
    Extract metrics from SQL using LLM when available, otherwise regex.

    Parameters
    ----------
    sql : str
        Hive/Spark SQL SELECT statement.

    Returns
    -------
    (SQLParseResult, mode)
        mode is either "AI (LangChain)" or "正则解析 (Regex)"
    """
    llm = _build_llm()
    if llm is not None:
        llm_result = _llm_extract(sql, llm)
        if llm_result is not None and llm_result.success:
            return llm_result, "AI (LangChain)"

    regex_result = parse_sql(sql)
    return regex_result, "正则解析 (Regex)"
