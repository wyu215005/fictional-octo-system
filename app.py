"""
智能指标助手 — 主应用
=====================
Streamlit web UI for the Risk Data Warehouse "Intelligent Indicator Assistant".

Layout
------
  Left column  : SQL input + action buttons
  Right column : Parsed metrics + consistency check results
"""

import json
import os

import streamlit as st

# ---------------------------------------------------------------------------
# Page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="智能指标助手",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Imports from src package
# ---------------------------------------------------------------------------
import sys

sys.path.insert(0, os.path.dirname(__file__))

from src.metric_extractor import extract_metrics  # noqa: E402
from src.consistency_checker import check_consistency  # noqa: E402

# ---------------------------------------------------------------------------
# Sample SQL snippets
# ---------------------------------------------------------------------------
SAMPLE_SQLS = {
    "示例 1 — 日活跃用户数（标准）": """\
SELECT
    dt,
    channel,
    COUNT(DISTINCT user_id) AS dau
FROM dwd_user_active_log
WHERE status = 'active'
  AND dt >= current_date
GROUP BY dt, channel
""",
    "示例 2 — 交易总金额（公式有偏差）": """\
SELECT
    dt,
    merchant_id,
    SUM(amount) AS gmv
FROM dwd_transaction_detail
WHERE status = 'success'
GROUP BY dt, merchant_id
""",
    "示例 3 — 风险用户数（过滤条件缺失）": """\
SELECT
    dt,
    COUNT(DISTINCT user_id) AS risk_users
FROM dwd_risk_user
WHERE risk_level = 'high'
GROUP BY dt
""",
    "示例 4 — 多指标 SQL": """\
SELECT
    dt,
    merchant_id,
    COUNT(transaction_id)           AS transaction_count,
    SUM(transaction_amount)         AS gmv,
    AVG(transaction_amount)         AS avg_amount
FROM dwd_transaction_detail
WHERE status = 'success'
  AND is_deleted = 0
GROUP BY dt, merchant_id
""",
}

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ 配置")
    st.markdown("---")
    st.subheader("LLM 配置（可选）")
    openai_key = st.text_input(
        "OpenAI API Key",
        type="password",
        help="填入后将使用 AI 模式进行指标提取，否则使用正则解析",
    )
    if openai_key:
        os.environ["OPENAI_API_KEY"] = openai_key

    openai_model = st.selectbox(
        "模型",
        ["gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4o-mini"],
        index=0,
    )
    os.environ["OPENAI_MODEL"] = openai_model

    base_url = st.text_input(
        "API Base URL（可选）",
        placeholder="https://api.openai.com/v1",
        help="如使用代理或 Azure，请填写自定义 Base URL",
    )
    if base_url:
        os.environ["OPENAI_BASE_URL"] = base_url

    st.markdown("---")
    st.subheader("标准指标库")
    std_lib_path = os.path.join(os.path.dirname(__file__), "config", "standard_metrics.json")
    if os.path.exists(std_lib_path):
        with open(std_lib_path, encoding="utf-8") as fh:
            std_lib_data = json.load(fh)
        metrics_in_lib = std_lib_data.get("metrics", [])
        st.success(f"已加载 {len(metrics_in_lib)} 个标准指标")
        with st.expander("查看标准指标列表"):
            for m in metrics_in_lib:
                st.markdown(
                    f"- **{m['name']}** (`{m['formula_type']}`) — {m.get('description', '')}"
                )
    else:
        st.warning("未找到标准指标库文件")

    st.markdown("---")
    st.caption("© 2024 风控数仓 · 智能指标助手")

# ---------------------------------------------------------------------------
# Main page title
# ---------------------------------------------------------------------------
st.title("🔍 智能指标助手")
st.markdown(
    "输入 **Hive / Spark SQL**，自动提取指标信息并与标准指标库进行口径一致性检查。"
)
st.markdown("---")

# ---------------------------------------------------------------------------
# Two-column layout: left = input, right = results
# ---------------------------------------------------------------------------
left_col, right_col = st.columns([1, 1], gap="large")

# ---- LEFT COLUMN -----------------------------------------------------------
with left_col:
    st.subheader("📝 SQL 输入")

    selected_sample = st.selectbox(
        "快速填入示例 SQL",
        ["（请选择示例或自行输入）"] + list(SAMPLE_SQLS.keys()),
        index=0,
    )
    if selected_sample != "（请选择示例或自行输入）":
        default_sql = SAMPLE_SQLS[selected_sample]
    else:
        default_sql = st.session_state.get("last_sql", "")

    sql_input = st.text_area(
        "请输入 SQL 语句",
        value=default_sql,
        height=320,
        placeholder="SELECT ... FROM ... WHERE ... GROUP BY ...",
        label_visibility="collapsed",
    )

    analyse_btn = st.button("🚀 开始解析 & 检查", type="primary", use_container_width=True)
    clear_btn = st.button("🗑️ 清空", use_container_width=True)

    if clear_btn:
        st.session_state["last_sql"] = ""
        st.rerun()

    if sql_input:
        st.session_state["last_sql"] = sql_input

    st.markdown("---")
    st.subheader("🗂️ 解析说明")
    st.markdown(
        """
| 提取项 | 来源 |
|--------|------|
| 指标名称 | `AS` 别名或聚合函数+字段推断 |
| 计算公式 | `SUM` / `COUNT` / `AVG` / `MAX` / `MIN` |
| 过滤条件 | `WHERE` 子句 |
| 维度 | `GROUP BY` 子句 |
        """
    )

# ---- RIGHT COLUMN ----------------------------------------------------------
with right_col:
    st.subheader("📊 解析结果 & 一致性检查")

    if not analyse_btn:
        st.info("👈 在左侧输入 SQL 后点击「开始解析 & 检查」按钮")
    else:
        if not sql_input or not sql_input.strip():
            st.error("SQL 语句不能为空，请先输入内容。")
        else:
            with st.spinner("正在解析 SQL …"):
                parse_result, extraction_mode = extract_metrics(sql_input)

            # -- Mode badge
            mode_color = "🟢" if "AI" in extraction_mode else "🔵"
            st.caption(f"{mode_color} 提取模式：**{extraction_mode}**")

            if parse_result.errors:
                for err in parse_result.errors:
                    st.error(f"解析错误：{err}")
            elif not parse_result.metrics:
                st.warning("未提取到任何聚合指标，请确认 SQL 包含 SUM/COUNT/AVG/MAX/MIN。")
            else:
                st.success(f"共提取到 **{len(parse_result.metrics)}** 个指标")

                # Consistency check
                with st.spinner("正在进行口径一致性检查 …"):
                    check_results = check_consistency(parse_result.metrics)

                # Render each metric
                for idx, (metric, check) in enumerate(
                    zip(parse_result.metrics, check_results), start=1
                ):
                    has_errors = any(
                        d.severity == "error" for d in check.diffs
                    )
                    has_warnings = any(
                        d.severity in ("warning",) for d in check.diffs
                    )
                    if has_errors:
                        header_icon = "❌"
                    elif has_warnings:
                        header_icon = "⚠️"
                    else:
                        header_icon = "✅"

                    with st.expander(
                        f"{header_icon} 指标 {idx}：{metric.metric_name}",
                        expanded=True,
                    ):
                        # Metric detail table
                        col_a, col_b = st.columns(2)
                        with col_a:
                            st.markdown("**指标基本信息**")
                            st.table(
                                {
                                    "字段": [
                                        "指标名称",
                                        "聚合类型",
                                        "计算公式",
                                    ],
                                    "值": [
                                        metric.metric_name,
                                        metric.formula_type,
                                        metric.formula,
                                    ],
                                }
                            )
                        with col_b:
                            st.markdown("**过滤 & 维度**")
                            st.markdown(
                                "**过滤条件 (WHERE)**\n"
                                + (
                                    "\n".join(f"- `{f}`" for f in metric.filters)
                                    if metric.filters
                                    else "_（无）_"
                                )
                            )
                            st.markdown(
                                "**分组维度 (GROUP BY)**\n"
                                + (
                                    "\n".join(f"- `{d}`" for d in metric.dimensions)
                                    if metric.dimensions
                                    else "_（无）_"
                                )
                            )

                        st.markdown("---")
                        st.markdown("**口径一致性检查**")

                        if check.matched_standard:
                            st.caption(
                                f"匹配标准指标：**{check.matched_standard}**"
                                f"（匹配得分 {check.match_score:.0%}）"
                            )
                        else:
                            st.caption("未匹配到标准指标")

                        for alert in check.alerts:
                            if alert.startswith("✅"):
                                st.success(alert)
                            elif alert.startswith("❌"):
                                st.error(alert)
                            elif alert.startswith("⚠️"):
                                st.warning(alert)
                            else:
                                st.info(alert)

                        if check.diffs:
                            with st.expander("🔎 详细差异列表"):
                                diff_data = {
                                    "字段": [d.field for d in check.diffs],
                                    "标准值": [d.expected for d in check.diffs],
                                    "实际值": [d.actual for d in check.diffs],
                                    "级别": [d.severity for d in check.diffs],
                                }
                                st.dataframe(diff_data, use_container_width=True)
