"""Streamlit 網頁入口：登入 + 依角色顯示選單。

角色權限：
  admin -> 月盈虧 + 排班建議
  staff -> 只有排班建議

帳號設定檔是 config/auth_config.yaml（不進版控），一律用
scripts/manage_accounts.py 管理帳號，不要手動編輯。
"""
import copy
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader

from scripts.analyze_operations import channel_mix, hourly_channel_by_weekday, repeat_customer_stats, weekday_daily_summary
from scripts.analyze_staffing_daytype import WEEKDAY_NAMES, cup_stats_by_daytype, roster_mode_by_weekday
from scripts.calculate_pnl import COST_ACTUAL_COLUMNS, calculate_one, get_fixed_cost, get_revenue_breakdown, save_pnl_result
from scripts.calculate_pnl import load_config as load_pnl_config
from scripts.calculate_staffing import calculate_hourly_staffing, get_hourly_data, is_shift_active
from scripts.calculate_staffing import load_config as load_staffing_config
from scripts.chart_helpers import build_bar_line_combo_chart, build_trend_chart
from scripts.compare_staffing import compare as compare_actual_vs_recommended
from scripts.compare_staffing import compare_aggregate as compare_staffing_aggregate
from scripts.pnl_insights import add_total_row, generate_monthly_breakdown, generate_pnl_insights

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "auth_config.yaml"
DB_PATH = ROOT / "db" / "riva_agent.db"
STAFFING_CONFIG_PATH = ROOT / "config" / "staffing_rules.json"
COST_CONFIG_PATH = ROOT / "config" / "cost_rates.json"

# 折線圖顏色固定用這兩色（驗證過的色盲友善色票，順序固定不循環）：
# 營收 = 藍，稅後淨利 = 青綠
CHART_COLOR_REVENUE = "#2a78d6"
CHART_COLOR_NET_PROFIT = "#1baf7a"
CHART_COLOR_COMBINED_NET_PROFIT = "#d97706"
CHART_COLOR_STORE_B_NET_PROFIT = "#7c3aed"

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

st.set_page_config(page_title="飲料店營運效能優化系統", page_icon="🧋")

# 走勢圖改成固定月距寬度後（見 chart_helpers.py），圖表本身會比手機螢幕寬。
# 沒有這段 CSS 的話，瀏覽器預設把「整個網頁」都撐寬變成橫向捲動，標題/文字跟著一起
# 滑走；限定只有圖表所在的 stFullScreenFrame 容器可以橫向捲動，其餘版面維持不動。
st.markdown(
    """
    <style>
    div[data-testid="stFullScreenFrame"] {
        overflow-x: auto;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_latest_operational_report() -> str | None:
    """讀 reports/ 底下最新一份 scripts/analyze_operations.py 產出的營運報告
    （Layer 1 原始明細分析結果，只放本機，這支函式只在 app.py 用，app_pnl.py 不會 import）。"""
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("operational_report_*.md"), reverse=True)
    if not files:
        return None
    return files[0].read_text(encoding="utf-8")


@st.cache_resource
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def render_manual_revenue_section(conn: sqlite3.Connection, store_id: str) -> None:
    st.caption(
        "只有在該月「完全沒有」POS 匯入資料時，這裡填的數字才會被拿去算月盈虧；"
        "POS 資料（daily_revenue_validated）永遠優先，不會被這裡的輸入覆蓋。"
    )

    year_month_input = st.text_input(
        "月份（格式 YYYY-MM）", key="manual_revenue_year_month", placeholder="2026-07"
    )
    if not year_month_input:
        st.caption("請輸入月份以繼續。")
        return
    if not re.fullmatch(r"\d{4}-\d{2}", year_month_input):
        st.error("月份格式錯誤，請用 YYYY-MM，例如 2026-07")
        return

    existing = conn.execute(
        "SELECT revenue, ubereats_amount, foodpanda_amount, credit_card_amount, other_electronic_amount "
        "FROM monthly_revenue_manual WHERE store_id = ? AND year_month = ?",
        (store_id, year_month_input),
    ).fetchone()
    defaults = (
        dict(existing)
        if existing
        else {
            "revenue": 0,
            "ubereats_amount": 0,
            "foodpanda_amount": 0,
            "credit_card_amount": 0,
            "other_electronic_amount": 0,
        }
    )
    if existing:
        st.caption(f"{store_id} 店 {year_month_input} 已有手動輸入資料，以下已預填現有值。")

    col1, col2 = st.columns(2)
    with col1:
        revenue = st.number_input(
            "月營收（元）", min_value=0, step=1000, value=int(defaults["revenue"]), key="mr_revenue"
        )
        ubereats = st.number_input(
            "Ubereats 金額（元）", min_value=0, step=500,
            value=int(defaults["ubereats_amount"]), key="mr_ubereats",
        )
        foodpanda = st.number_input(
            "Foodpanda 金額（元）", min_value=0, step=500,
            value=int(defaults["foodpanda_amount"]), key="mr_foodpanda",
        )
    with col2:
        credit_card = st.number_input(
            "信用卡金額（元）", min_value=0, step=500,
            value=int(defaults["credit_card_amount"]), key="mr_credit_card",
        )
        other_electronic = st.number_input(
            "其他電子支付金額（元）", min_value=0, step=500,
            value=int(defaults["other_electronic_amount"]), key="mr_other_electronic",
        )

    platform_and_payment_total = ubereats + foodpanda + credit_card + other_electronic
    can_save = True
    if platform_and_payment_total > revenue:
        st.error(
            f"Ubereats+Foodpanda+信用卡+其他電子支付加總（{platform_and_payment_total:,}）"
            f"超過月營收（{revenue:,}），請檢查是否打錯數字。"
        )
        can_save = False

    col_save, col_clear = st.columns(2)
    with col_save:
        if st.button("儲存本月手動輸入", disabled=not can_save, key="mr_save"):
            conn.execute(
                """
                INSERT INTO monthly_revenue_manual
                    (store_id, year_month, revenue, ubereats_amount, foodpanda_amount,
                     credit_card_amount, other_electronic_amount, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(store_id, year_month) DO UPDATE SET
                    revenue = excluded.revenue,
                    ubereats_amount = excluded.ubereats_amount,
                    foodpanda_amount = excluded.foodpanda_amount,
                    credit_card_amount = excluded.credit_card_amount,
                    other_electronic_amount = excluded.other_electronic_amount,
                    updated_at = datetime('now')
                """,
                (store_id, year_month_input, revenue, ubereats, foodpanda, credit_card, other_electronic),
            )
            conn.commit()
            st.success(f"已儲存 {store_id} 店 {year_month_input} 的手動輸入營收")
            st.rerun()
    with col_clear:
        if existing and st.button("清除本月手動輸入", key="mr_clear"):
            conn.execute(
                "DELETE FROM monthly_revenue_manual WHERE store_id = ? AND year_month = ?",
                (store_id, year_month_input),
            )
            conn.commit()
            st.success(f"已清除 {store_id} 店 {year_month_input} 的手動輸入營收")
            st.rerun()


def render_combined_pnl_page(conn: sqlite3.Connection, stores: list[str]) -> None:
    st.subheader("兩店合計盈虧趨勢")
    rows = conn.execute(
        "SELECT year_month, store_id, net_profit FROM monthly_pnl ORDER BY year_month"
    ).fetchall()
    if not rows:
        st.caption("monthly_pnl 還沒有歷史紀錄，請先執行 scripts/calculate_pnl.py。")
        return

    by_month: dict[str, dict[str, int]] = {}
    for r in rows:
        by_month.setdefault(r["year_month"], {})[r["store_id"]] = r["net_profit"]

    records = []
    for year_month, per_store in sorted(by_month.items()):
        for sid in stores:
            if sid in per_store:
                records.append((year_month, f"{sid} 店稅後淨利", per_store[sid]))
        if all(sid in per_store for sid in stores):
            records.append((year_month, "兩店合計淨利", sum(per_store[sid] for sid in stores)))

    chart_df = pd.DataFrame(records, columns=["year_month", "項目", "金額"])
    store_colors = [CHART_COLOR_NET_PROFIT, CHART_COLOR_STORE_B_NET_PROFIT]
    domain = [f"{sid} 店稅後淨利" for sid in stores] + ["兩店合計淨利"]
    color_range = store_colors[: len(stores)] + [CHART_COLOR_COMBINED_NET_PROFIT]
    chart = build_trend_chart(chart_df, domain, color_range, height=320)
    st.altair_chart(chart, use_container_width=False)

    st.markdown(generate_pnl_insights(conn))

    st.subheader("兩店合計逐月明細")
    st.caption("手機看圖表標籤有限時，這張表可以左右滑動查看每個月的完整數字，最後一列是累計金額。")
    table_records = []
    for year_month, per_store in sorted(by_month.items()):
        row = {"月份": year_month}
        for sid in stores:
            row[f"{sid} 店稅後淨利"] = per_store.get(sid)
        if all(sid in per_store for sid in stores):
            row["兩店合計淨利"] = sum(per_store[sid] for sid in stores)
        table_records.append(row)
    sum_cols = [f"{sid} 店稅後淨利" for sid in stores] + ["兩店合計淨利"]
    table_records = add_total_row(table_records, "月份", sum_cols)
    st.dataframe(pd.DataFrame(table_records), hide_index=True, use_container_width=True)

    st.subheader("通路組合／回頭客分析（2026-07-10 新增圖表）")
    store_color_range = [CHART_COLOR_NET_PROFIT, CHART_COLOR_STORE_B_NET_PROFIT][: len(stores)]
    store_color_scale = alt.Scale(domain=stores, range=store_color_range)

    mix_rows = []
    repeat_data = {}
    for sid in stores:
        mix = channel_mix(conn, sid)
        mix_rows.append({"店別": sid, "通路": "外送平台", "佔比": round(mix["delivery_pct"] * 100, 1)})
        mix_rows.append({"店別": sid, "通路": "自取/外帶", "佔比": round(mix["pickup_pct"] * 100, 1)})
        if mix["other_pct"] > 0.005:
            mix_rows.append({"店別": sid, "通路": "其他", "佔比": round(mix["other_pct"] * 100, 1)})
        repeat = repeat_customer_stats(conn, sid)
        if repeat:
            repeat_data[sid] = repeat

    col_mix, col_visit = st.columns(2)
    with col_mix:
        st.caption("通路組合（佔營收 %）")
        mix_chart = (
            alt.Chart(pd.DataFrame(mix_rows))
            .mark_bar()
            .encode(
                x=alt.X("通路:N", title=None),
                y=alt.Y("佔比:Q", title="佔營收 %"),
                color=alt.Color("店別:N", scale=store_color_scale, legend=alt.Legend(title=None)),
                xOffset="店別:N",
                tooltip=["店別", "通路", "佔比"],
            )
            .properties(height=280)
        )
        st.altair_chart(mix_chart, use_container_width=True)

    with col_visit:
        st.caption("回訪次數分布（佔客數 %）")
        if repeat_data:
            bucket_labels = ["1次", "2次", "3~5次", "6~10次", "11次以上"]
            visit_rows = [
                {
                    "店別": sid,
                    "回訪次數": label,
                    "佔客數": round(r["visit_buckets"][label] / r["total_customers"] * 100, 1),
                }
                for sid, r in repeat_data.items()
                for label in bucket_labels
            ]
            visit_chart = (
                alt.Chart(pd.DataFrame(visit_rows))
                .mark_bar()
                .encode(
                    x=alt.X("回訪次數:N", title=None, sort=bucket_labels),
                    y=alt.Y("佔客數:Q", title="佔客數 %"),
                    color=alt.Color("店別:N", scale=store_color_scale, legend=alt.Legend(title=None)),
                    xOffset="店別:N",
                    tooltip=["店別", "回訪次數", "佔客數"],
                )
                .properties(height=280)
            )
            st.altair_chart(visit_chart, use_container_width=True)
        else:
            st.caption("目前沒有回頭客資料（需要發票明細有留手機載具號碼）。")

    if repeat_data:
        st.caption("回頭客佔比逐月成長趨勢（這個月的客人裡，有多少比例是之前月份就出現過的老客）")
        trend_rows = [
            {"year_month": row["year_month"], "項目": sid, "回訪比例": round(row["returning_pct"], 1)}
            for sid, r in repeat_data.items()
            for row in r["monthly_trend"]
            if row["returning_pct"] is not None
        ]
        if trend_rows:
            trend_chart = build_trend_chart(
                pd.DataFrame(trend_rows), stores, store_color_range,
                height=260, y_field="回訪比例", y_title="回訪比例（%）", value_format=".1f",
            )
            st.altair_chart(trend_chart, use_container_width=False)
        st.caption("只有「這個月之前已經有至少一個月的歷史資料」才算得出回訪比例，第一個月沒有歷史可比、不會出現在圖上。")

    st.subheader("營運報告（發票／營收／收銀機明細分析）")
    report = load_latest_operational_report()
    if report:
        st.markdown(report)
    else:
        st.caption(
            "尚未產生營運報告，請在終端機執行：`python3 -m scripts.analyze_operations`"
        )


def render_pnl_page() -> None:
    conn = get_db_connection()
    config = load_pnl_config()

    stores = [
        r["store_id"]
        for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()
    ]
    store_choice = st.selectbox(
        "店別", stores + ["彙整"],
        format_func=lambda s: "彙整（兩店合計）" if s == "彙整" else f"{s} 店",
    )
    if store_choice == "彙整":
        render_combined_pnl_page(conn, stores)
        return
    store_id = store_choice

    with st.expander("手動輸入月營收（POS 沒資料時的備援）"):
        render_manual_revenue_section(conn, store_id)

    periods = [
        r[0]
        for r in conn.execute(
            """
            SELECT year_month FROM (
                SELECT substr(business_date, 1, 7) AS year_month
                FROM daily_revenue_validated WHERE store_id = ?
                UNION
                SELECT year_month FROM monthly_revenue_manual WHERE store_id = ?
            )
            ORDER BY 1 DESC
            """,
            (store_id, store_id),
        ).fetchall()
    ]
    if not periods:
        st.warning(f"{store_id} 店目前沒有已驗證的營收資料，請先完成資料匯入與跨來源比對，或在上面手動輸入。")
        return

    year_month = st.selectbox("月份", periods)

    def k(name: str) -> str:
        """widget key 要包含 store_id/year_month，切換店別或月份時才會正確重置預設值。"""
        return f"{name}__{store_id}__{year_month}"

    actuals_row = conn.execute(
        f"SELECT {', '.join(COST_ACTUAL_COLUMNS)} FROM monthly_cost_actuals "
        "WHERE store_id = ? AND year_month = ?",
        (store_id, year_month),
    ).fetchone()
    actuals = dict(actuals_row) if actuals_row else {col: None for col in COST_ACTUAL_COLUMNS}

    revenue_row, _ = get_revenue_breakdown(conn, store_id, year_month)
    current_revenue = revenue_row["revenue"] or 0

    ACTUAL_FIELD_LABELS = {
        "labor_actual": "人事底薪",
        "cogs_actual": "原物料成本",
        "utilities_actual": "水電",
        "rent_actual": "房租",
        "franchise_amortization_actual": "加盟金攤提",
        "ubereats_commission_pct_actual": "Ubereats 抽成%",
        "foodpanda_commission_pct_actual": "Foodpanda 抽成%",
        "credit_card_fee_pct_actual": "信用卡手續費%",
        "other_electronic_fee_pct_actual": "其他電子支付手續費%",
        "business_tax_pct_actual": "營業稅%",
        "corporate_income_tax_pct_actual": "營所稅%",
    }

    st.subheader("試算參數（可調整，預設值來自本月實際值，沒有實際值才用 config/cost_rates.json 概算）")
    overridden = [label for key, label in ACTUAL_FIELD_LABELS.items() if actuals.get(key) is not None]
    if overridden:
        st.caption(f"📌 {store_id} 店 {year_month} 已有本月實際值覆蓋：{'、'.join(overridden)}")
    rates = config["variable_cost_rates"]

    with st.expander("固定成本", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            labor_base = st.number_input(
                "人事底薪（元/月）", min_value=0, step=1000,
                value=int(
                    actuals["labor_actual"] if actuals["labor_actual"] is not None
                    else get_fixed_cost(config, store_id, "labor_base")
                ),
                key=k("labor_base"),
            )
            rent = st.number_input(
                "房租（元/月）", min_value=0, step=1000,
                value=int(
                    actuals["rent_actual"] if actuals["rent_actual"] is not None
                    else get_fixed_cost(config, store_id, "rent")
                ),
                key=k("rent"),
            )
        with col2:
            utilities = st.number_input(
                "水電概算（元/月）", min_value=0, step=500,
                value=int(
                    actuals["utilities_actual"] if actuals["utilities_actual"] is not None
                    else get_fixed_cost(config, store_id, "utilities_estimate")
                ),
                key=k("utilities"),
            )
            franchise_amortization = st.number_input(
                "加盟金攤提（元/月）", min_value=0, step=1000,
                value=int(
                    actuals["franchise_amortization_actual"]
                    if actuals["franchise_amortization_actual"] is not None
                    else get_fixed_cost(config, store_id, "franchise_fee_amortization")
                ),
                key=k("franchise_amortization"),
            )

    with st.expander("變動成本比例", expanded=True):
        if actuals["cogs_actual"] is not None and current_revenue > 0:
            cogs_pct_default = round(actuals["cogs_actual"] / current_revenue * 100, 1)
        else:
            cogs_pct_default = round(rates["cogs_pct_of_revenue"] * 100, 1)
        cogs_pct = st.number_input(
            "原物料成本佔營收 %", min_value=0.0, max_value=100.0, step=0.5,
            value=cogs_pct_default, key=k("cogs_pct"),
        )

    with st.expander("平台與金流費率"):
        col1, col2 = st.columns(2)
        with col1:
            ubereats_pct = st.number_input(
                "Ubereats 抽成 %", min_value=0.0, max_value=100.0, step=0.5,
                value=round(
                    (
                        actuals["ubereats_commission_pct_actual"]
                        if actuals["ubereats_commission_pct_actual"] is not None
                        else rates["platform_commission"]["ubereats"]
                    ) * 100, 1,
                ),
                key=k("ubereats_pct"),
            )
            foodpanda_pct = st.number_input(
                "Foodpanda 抽成 %", min_value=0.0, max_value=100.0, step=0.5,
                value=round(
                    (
                        actuals["foodpanda_commission_pct_actual"]
                        if actuals["foodpanda_commission_pct_actual"] is not None
                        else rates["platform_commission"]["foodpanda"]
                    ) * 100, 1,
                ),
                key=k("foodpanda_pct"),
            )
        with col2:
            credit_card_pct = st.number_input(
                "信用卡手續費 %", min_value=0.0, max_value=100.0, step=0.1,
                value=round(
                    (
                        actuals["credit_card_fee_pct_actual"]
                        if actuals["credit_card_fee_pct_actual"] is not None
                        else rates["payment_processing"]["credit_card"]
                    ) * 100, 1,
                ),
                key=k("credit_card_pct"),
            )
            other_electronic_pct = st.number_input(
                "其他電子支付手續費 %", min_value=0.0, max_value=100.0, step=0.1,
                value=round(
                    (
                        actuals["other_electronic_fee_pct_actual"]
                        if actuals["other_electronic_fee_pct_actual"] is not None
                        else rates["payment_processing"]["other_electronic"]
                    ) * 100, 1,
                ),
                key=k("other_electronic_pct"),
            )

    with st.expander("稅率"):
        st.caption("⚠️ 法定稅率，調整僅供試算，「儲存為新的預設值」前請確認實際稅制沒有變動")
        col1, col2 = st.columns(2)
        with col1:
            business_tax_pct = st.number_input(
                "營業稅 %", min_value=0.0, max_value=100.0, step=0.5,
                value=round(
                    (
                        actuals["business_tax_pct_actual"]
                        if actuals["business_tax_pct_actual"] is not None
                        else rates["business_tax_pct"]
                    ) * 100, 1,
                ),
                key=k("business_tax_pct"),
            )
        with col2:
            corporate_income_tax_pct = st.number_input(
                "營所稅 %", min_value=0.0, max_value=100.0, step=0.5,
                value=round(
                    (
                        actuals["corporate_income_tax_pct_actual"]
                        if actuals["corporate_income_tax_pct_actual"] is not None
                        else rates["corporate_income_tax_pct"]
                    ) * 100, 1,
                ),
                key=k("corporate_income_tax_pct"),
            )

    working_config = copy.deepcopy(config)
    # 寫進「該店的 override」而不是共用預設值，這樣不管這個欄位原本是共用值還是
    # 單店 override，頁面上調整的數字都能正確蓋掉、拿去算 calculate_one()。
    working_store_override = working_config.setdefault("fixed_costs_monthly_overrides", {}).setdefault(store_id, {})
    working_store_override["labor_base"] = labor_base
    working_store_override["rent"] = rent
    working_store_override["utilities_estimate"] = utilities
    working_store_override["franchise_fee_amortization"] = franchise_amortization
    working_config["variable_cost_rates"]["cogs_pct_of_revenue"] = cogs_pct / 100
    working_config["variable_cost_rates"]["platform_commission"]["ubereats"] = ubereats_pct / 100
    working_config["variable_cost_rates"]["platform_commission"]["foodpanda"] = foodpanda_pct / 100
    working_config["variable_cost_rates"]["payment_processing"]["credit_card"] = credit_card_pct / 100
    working_config["variable_cost_rates"]["payment_processing"]["other_electronic"] = (
        other_electronic_pct / 100
    )
    working_config["variable_cost_rates"]["business_tax_pct"] = business_tax_pct / 100
    working_config["variable_cost_rates"]["corporate_income_tax_pct"] = (
        corporate_income_tax_pct / 100
    )

    col_save_default, col_save_actual = st.columns(2)
    with col_save_default:
        if st.button("儲存為新的預設值", key="save_cost_rates"):
            # 這個店已經有 override 的項目，繼續存回 override（不動共用值，避免動到另一店）；
            # 沒有 override 的項目，存回共用預設值（跟現有兩店共用的行為一致）。
            store_overrides = config.setdefault("fixed_costs_monthly_overrides", {}).setdefault(store_id, {})

            def save_fixed(key, value):
                if key in store_overrides:
                    store_overrides[key] = value
                else:
                    config["fixed_costs_monthly"][key] = value

            save_fixed("labor_base", labor_base)
            save_fixed("rent", rent)
            save_fixed("utilities_estimate", utilities)
            save_fixed("franchise_fee_amortization", franchise_amortization)
            config["variable_cost_rates"]["cogs_pct_of_revenue"] = cogs_pct / 100
            config["variable_cost_rates"]["platform_commission"]["ubereats"] = ubereats_pct / 100
            config["variable_cost_rates"]["platform_commission"]["foodpanda"] = foodpanda_pct / 100
            config["variable_cost_rates"]["payment_processing"]["credit_card"] = credit_card_pct / 100
            config["variable_cost_rates"]["payment_processing"]["other_electronic"] = (
                other_electronic_pct / 100
            )
            config["variable_cost_rates"]["business_tax_pct"] = business_tax_pct / 100
            config["variable_cost_rates"]["corporate_income_tax_pct"] = corporate_income_tax_pct / 100
            COST_CONFIG_PATH.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            st.success("已更新 config/cost_rates.json，之後執行 calculate_pnl.py 也會套用新數字")
    with col_save_actual:
        st.caption(f"↓ 只套用到 {store_id} 店 {year_month}，不影響其他月份／config 預設值")
        if st.button("儲存為本月實際值", key="save_month_actuals"):
            cogs_actual_to_save = round(current_revenue * cogs_pct / 100)
            conn.execute(
                """
                INSERT INTO monthly_cost_actuals
                    (store_id, year_month, labor_actual, cogs_actual, utilities_actual,
                     rent_actual, franchise_amortization_actual,
                     ubereats_commission_pct_actual, foodpanda_commission_pct_actual,
                     credit_card_fee_pct_actual, other_electronic_fee_pct_actual,
                     business_tax_pct_actual, corporate_income_tax_pct_actual)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id, year_month) DO UPDATE SET
                    labor_actual = excluded.labor_actual,
                    cogs_actual = excluded.cogs_actual,
                    utilities_actual = excluded.utilities_actual,
                    rent_actual = excluded.rent_actual,
                    franchise_amortization_actual = excluded.franchise_amortization_actual,
                    ubereats_commission_pct_actual = excluded.ubereats_commission_pct_actual,
                    foodpanda_commission_pct_actual = excluded.foodpanda_commission_pct_actual,
                    credit_card_fee_pct_actual = excluded.credit_card_fee_pct_actual,
                    other_electronic_fee_pct_actual = excluded.other_electronic_fee_pct_actual,
                    business_tax_pct_actual = excluded.business_tax_pct_actual,
                    corporate_income_tax_pct_actual = excluded.corporate_income_tax_pct_actual
                """,
                (
                    store_id, year_month, labor_base, cogs_actual_to_save, utilities,
                    rent, franchise_amortization,
                    ubereats_pct / 100, foodpanda_pct / 100,
                    credit_card_pct / 100, other_electronic_pct / 100,
                    business_tax_pct / 100, corporate_income_tax_pct / 100,
                ),
            )
            conn.commit()
            st.success(f"已儲存 {store_id} 店 {year_month} 的本月實際參數值")
            st.rerun()

    result = calculate_one(conn, working_config, store_id, year_month)

    st.subheader(f"{store_id} 店　{year_month}")
    if result["revenue_source"] == "manual":
        st.caption("📝 本月營收數字來自手動輸入（沒有 POS 稽核過的資料），僅供參考。")
    col1, col2, col3 = st.columns(3)
    col1.metric("營收", f"{result['revenue']:,}")
    col2.metric("稅前淨利", f"{result['pretax_profit']:,}")
    col3.metric("稅後淨利", f"{result['net_profit']:,}")

    saved_pnl = conn.execute(
        "SELECT net_profit, calculated_at FROM monthly_pnl WHERE store_id = ? AND year_month = ?",
        (store_id, year_month),
    ).fetchone()
    if saved_pnl is None:
        st.caption("尚未儲存正式紀錄，以上是即時試算結果，按下面按鈕可寫入 monthly_pnl。")
    elif saved_pnl["net_profit"] != result["net_profit"]:
        st.caption(
            f"⚠️ 已儲存的正式紀錄（稅後淨利 {saved_pnl['net_profit']:,}，"
            f"{saved_pnl['calculated_at']}）跟目前試算結果不同，按下面按鈕可更新成目前這個版本。"
        )
    else:
        st.caption(f"✅ 目前試算結果跟已儲存的正式紀錄一致（最後計算時間 {saved_pnl['calculated_at']}）")

    if st.button("儲存本月盈虧結果", key="save_pnl_result"):
        save_pnl_result(conn, store_id, year_month, result)
        conn.commit()
        st.success(f"已將 {store_id} 店 {year_month} 的盈虧結果寫入 monthly_pnl")
        st.rerun()

    breakdown_df = pd.DataFrame(
        [
            ("營收", result["revenue"]),
            ("原物料（含包材）", -result["cogs"]),
            ("原物料損耗", -result["material_waste"]),
            ("平台抽成", -result["platform_commission"]),
            ("金流手續費", -result["payment_processing_fee"]),
            ("人事（含勞健保）", -result["labor_cost"]),
            ("房租", -result["rent"]),
            ("水電", -result["utilities"]),
            ("加盟金攤提", -result["franchise_amortization"]),
            ("營業稅", -result["business_tax"]),
            ("稅前淨利", result["pretax_profit"]),
            ("預估所得稅", -result["income_tax_estimate"]),
            ("稅後淨利", result["net_profit"]),
        ],
        columns=["項目", "金額"],
    )
    st.dataframe(breakdown_df, hide_index=True, use_container_width=True)

    st.subheader(f"{store_id} 店歷史走勢")
    history = conn.execute(
        "SELECT year_month, revenue, net_profit FROM monthly_pnl "
        "WHERE store_id = ? ORDER BY year_month",
        (store_id,),
    ).fetchall()
    if not history:
        st.caption("monthly_pnl 還沒有歷史紀錄，請先執行 scripts/calculate_pnl.py。")
        return

    history_df = pd.DataFrame(history, columns=["year_month", "營收", "稅後淨利"])
    chart_df = history_df.melt("year_month", var_name="項目", value_name="金額")

    # 兩店合計淨利：只算「所有店都有資料」的月份，避免只有單店資料的月份被誤算成合計數字
    combined_rows = conn.execute(
        "SELECT year_month, SUM(net_profit) AS combined_net_profit, "
        "COUNT(DISTINCT store_id) AS store_count "
        "FROM monthly_pnl GROUP BY year_month HAVING store_count = ? ORDER BY year_month",
        (len(stores),),
    ).fetchall()
    chart_domain = ["營收", "稅後淨利"]
    chart_range = [CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT]
    if len(stores) > 1 and combined_rows:
        combined_df = pd.DataFrame(
            [(r["year_month"], "兩店合計淨利", r["combined_net_profit"]) for r in combined_rows],
            columns=["year_month", "項目", "金額"],
        )
        chart_df = pd.concat([chart_df, combined_df], ignore_index=True)
        chart_domain.append("兩店合計淨利")
        chart_range.append(CHART_COLOR_COMBINED_NET_PROFIT)

    chart = build_trend_chart(chart_df, chart_domain, chart_range, height=300)
    st.altair_chart(chart, use_container_width=False)

    st.markdown(generate_pnl_insights(conn))

    st.subheader(f"{store_id} 店逐月成本結構（找盈虧原因用）")
    st.caption("成本欄位都是「占當月營收 %」，不是金額，這樣營收規模不同的月份才能直接比較；最後一列是累計金額（百分比欄位加總沒有意義，留空）。")
    monthly_table = generate_monthly_breakdown(conn, store_id)
    if monthly_table:
        monthly_table = add_total_row(monthly_table, "月份", ["營收", "稅前淨利", "稅後淨利"])
        st.dataframe(pd.DataFrame(monthly_table), hide_index=True, use_container_width=True)


def render_staffing_page() -> None:
    conn = get_db_connection()
    saved_config = load_staffing_config()

    stores = [
        r["store_id"]
        for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()
    ]
    store_id = st.selectbox("店別", stores, format_func=lambda s: f"{s} 店")

    periods = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly "
            "WHERE store_id = ? ORDER BY 1 DESC",
            (store_id,),
        ).fetchall()
    ]
    if not periods:
        st.warning(f"{store_id} 店目前沒有時段占比資料，請先完成 import_hourly_pattern.py 匯入。")
        return
    year_month = st.selectbox("月份", periods)

    st.subheader("排班參數（可調整，預設值來自 config/staffing_rules.json）")
    col1, col2, col3 = st.columns(3)
    with col1:
        capacity = st.number_input(
            "單位產能（杯/人/hr）",
            min_value=1,
            value=int(saved_config["capacity"]["cups_per_staff_per_hour"]),
        )
    with col2:
        tea_start = st.text_input(
            "煮茶開始時間（HH:MM）", value=saved_config["tea_brewing"]["start_time"]
        )
        tea_duration = st.number_input(
            "煮茶時數",
            min_value=0.5,
            step=0.5,
            value=float(saved_config["tea_brewing"]["estimated_duration_hours"]),
        )
    with col3:
        part_time_min = st.number_input(
            "兼職最短時數", min_value=1, value=int(saved_config["part_time"]["min_hours"])
        )
        part_time_max = st.number_input(
            "兼職最長時數", min_value=1, value=int(saved_config["part_time"]["max_hours"])
        )

    st.caption("班別時間窗（可直接編輯儲存格，或用最下面一列新增班別）")
    shifts_df = pd.DataFrame(saved_config["shifts"])
    edited_shifts_df = st.data_editor(
        shifts_df, num_rows="dynamic", hide_index=True, use_container_width=True, key="shifts_editor"
    )

    working_config = copy.deepcopy(saved_config)
    working_config["capacity"]["cups_per_staff_per_hour"] = capacity
    working_config["tea_brewing"]["start_time"] = tea_start
    working_config["tea_brewing"]["estimated_duration_hours"] = tea_duration
    working_config["shifts"] = edited_shifts_df.to_dict("records")

    if st.button("儲存為新的預設值"):
        saved_config["capacity"]["cups_per_staff_per_hour"] = capacity
        saved_config["tea_brewing"]["start_time"] = tea_start
        saved_config["tea_brewing"]["estimated_duration_hours"] = tea_duration
        saved_config["part_time"]["min_hours"] = part_time_min
        saved_config["part_time"]["max_hours"] = part_time_max
        saved_config["shifts"] = edited_shifts_df.to_dict("records")
        STAFFING_CONFIG_PATH.write_text(
            json.dumps(saved_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        st.success("已更新 config/staffing_rules.json，之後執行 calculate_staffing.py 也會套用新數字")

    hourly_data = get_hourly_data(conn, store_id, year_month)
    if not hourly_data:
        st.warning("這個月份沒有時段占比資料。")
        return
    staffing = calculate_hourly_staffing(hourly_data, working_config)

    st.subheader(f"{store_id} 店　{year_month}　逐時段建議人力")
    hourly_rows = [
        {
            "時段": f"{hour_slot}:00",
            "日均杯數": staffing[hour_slot]["cups"],
            "建議前場人力": staffing[hour_slot]["required_front_staff"],
            "煮茶": "煮茶 +1" if staffing[hour_slot]["tea_brewing"] else "",
            "日均外送單": staffing[hour_slot]["delivery_count"],
            "外送耗時(hr)": staffing[hour_slot]["delivery_hours"],
        }
        for hour_slot in sorted(staffing.keys())
    ]
    st.dataframe(pd.DataFrame(hourly_rows), hide_index=True, use_container_width=True)
    st.caption(
        "「煮茶」欄有標記的時段，除了前場人力，後場還要另外 +1 人煮茶，此人力不計入前場產能。"
        "「建議前場人力」已經把外送耗時併進需求：ceil(杯數/產能 + 外送耗時小時數)"
    )

    hourly_df = pd.DataFrame(hourly_rows)
    staffing_chart = build_bar_line_combo_chart(
        hourly_df, "時段", "日均杯數", "日均杯數（杯）",
        hourly_df, "建議前場人力", "建議前場人力（人）",
        line_color=CHART_COLOR_COMBINED_NET_PROFIT,
    )
    st.altair_chart(staffing_chart, use_container_width=True)
    st.caption("長條（左軸）＝日均杯數，橘線（右軸）＝建議前場人力，兩者刻度各自獨立，方便看杯量跟人力需求的曲線是否同步升降。")

    st.subheader("班別彙總")
    summary_rows = []
    for shift in working_config["shifts"]:
        active_hours = [h for h in staffing if is_shift_active(h, shift, working_config)]
        if not active_hours:
            continue
        peak = max(staffing[h]["required_front_staff"] for h in active_hours)
        avg = sum(staffing[h]["required_front_staff"] for h in active_hours) / len(active_hours)
        summary_rows.append(
            {
                "班別": shift["name"],
                "時間": f"{shift['start']}~{shift['end']}",
                "尖峰需求": peak,
                "平均需求": round(avg, 1),
            }
        )
    st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
    st.caption(f"兼職班長度參考：{part_time_min}~{part_time_max} 小時")

    st.subheader("實際排班 vs 建議人力比對")
    compare_mode = st.radio("比對範圍", ["單月", "全年彙總（排除2月）"], horizontal=True, key="compare_mode")

    if compare_mode == "單月":
        comparison_rows = compare_actual_vs_recommended(conn, working_config, store_id, year_month)
        for row in comparison_rows:
            row["cups"] = staffing.get(row["hour_slot"], {}).get("cups")
        scope_caption = f"{store_id} 店 {year_month}"
        months_note = ""
    else:
        today_str = date.today().isoformat()
        current_year, current_year_month = today_str[:4], today_str[:7]
        agg_months = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly "
                "WHERE store_id = ? AND year_month LIKE ? AND year_month != ? AND year_month < ? "
                "ORDER BY 1",
                (store_id, f"{current_year}-%", f"{current_year}-02", current_year_month),
            ).fetchall()
        ]
        if not agg_months:
            st.info(f"{store_id} 店 {current_year} 年目前沒有可彙總的完整月份資料。")
            return
        comparison_rows = compare_staffing_aggregate(conn, working_config, store_id, agg_months)
        scope_caption = f"{store_id} 店 {current_year} 年彙總"
        months_note = f"（納入月份：{'、'.join(agg_months)}，已排除 {current_year}-02 過年月份與未過完的當月）"

    if all(row["actual"] is None for row in comparison_rows):
        st.info(f"{scope_caption} 還沒有實際排班資料可比對，請先用 scripts/import_staffing_actual.py 匯入。")
        return

    def status_label(diff):
        if diff is None:
            return "無資料"
        if diff > 0:
            return f"超編 (+{diff})"
        if diff < 0:
            return f"人力不足 ({diff})"
        return "剛好"

    comparison_df = pd.DataFrame(
        [
            {
                "時段": f"{row['hour_slot']}:00",
                "建議人力": row["recommended"],
                "實際平均人力": row["actual"],
                "差異": row["diff"],
                "狀態": status_label(row["diff"]),
            }
            for row in comparison_rows
        ]
    )
    st.dataframe(comparison_df, hide_index=True, use_container_width=True)

    understaffed = sum(1 for r in comparison_rows if r["diff"] is not None and r["diff"] < 0)
    overstaffed = sum(1 for r in comparison_rows if r["diff"] is not None and r["diff"] > 0)
    st.caption(f"有實際資料的時段中：{understaffed} 個時段人力不足、{overstaffed} 個時段超編{months_note}")

    chart_rows = [r for r in comparison_rows if r["actual"] is not None]
    bar_df = pd.DataFrame(
        [{"時段": r["hour_slot"], "類別": "建議人力", "人力": r["recommended"]} for r in chart_rows]
        + [{"時段": r["hour_slot"], "類別": "實際平均人力", "人力": r["actual"]} for r in chart_rows]
    )
    line_df = pd.DataFrame(
        [{"時段": r["hour_slot"], "日均杯數": r["cups"]} for r in chart_rows]
    )
    compare_chart = build_bar_line_combo_chart(
        bar_df, "時段", "人力", "人力（人）",
        line_df, "日均杯數", "日均杯數（杯）",
        bar_category_field="類別",
        bar_domain=["建議人力", "實際平均人力"],
        bar_range=[CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT],
        line_color=CHART_COLOR_COMBINED_NET_PROFIT,
    )
    st.altair_chart(compare_chart, use_container_width=True)
    st.caption("「實際平均人力」的分母只算「已經有謄打排班資料的天數」，資料補齊前不代表整月狀況；橘線是該時段日均杯數（右軸）")


def render_hourly_pattern_page() -> None:
    """專門看『各時段人力和杯數』的頁面（2026-07-10 新增），從排班建議頁搬出來獨立
    成一頁，避免排班建議頁（參數調整＋建議人力＋比對）跟這裡的純觀察資料混在一起。"""
    conn = get_db_connection()
    saved_config = load_staffing_config()

    stores = [
        r["store_id"]
        for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()
    ]
    store_id = st.selectbox("店別", stores, format_func=lambda s: f"{s} 店")

    st.subheader("平日/假日逐時段杯數")
    all_months = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id = ? ORDER BY 1",
            (store_id,),
        ).fetchall()
    ]
    daytype_stats = cup_stats_by_daytype(conn, store_id, all_months)
    if not daytype_stats["平日"] and not daytype_stats["假日"]:
        st.info(
            f"{store_id} 店目前沒有 raw_hourly_pattern_daily 樣本（需要使用者額外提供逐一星期六/日的"
            "時段占比報表，見 scripts/import_hourly_pattern_daily.py），無法拆分平日/假日。"
        )
    else:
        col_wd, col_we = st.columns(2)
        with col_wd:
            st.caption("平日（反推值，見下方說明）")
            st.dataframe(pd.DataFrame(daytype_stats["平日"]), hide_index=True, use_container_width=True)
        with col_we:
            st.caption("假日（真實樣本平均，非估計值）")
            st.dataframe(pd.DataFrame(daytype_stats["假日"]), hide_index=True, use_container_width=True)
        st.caption(
            "「假日」欄是直接把使用者額外提供的星期六/日單日時段占比報表拿來平均，是真的量出來的數字。"
            "「平日」欄目前沒有對應的單日原始報表可以直接量，只能用代數反推：POS 系統本來就有的"
            "「月彙總」杯數（真實資料，但整月平日+假日混在一起，沒有分開）減掉「假日」欄真實量到的"
            "杯數，剩下的部分才是平日的杯數——這一步是計算出來的，不是實際量到的，但因為公式兩邊都是"
            "真實資料，準確度比之前用發票交易筆數比例去猜的估計版本高很多。"
            "「月數」是有真實假日樣本可以反推的月份數，不是所有已匯入月份都會出現在這裡。"
        )

    st.subheader("星期幾 x 時段實際排班人力（正職/兼職眾數）")
    date_range_row = conn.execute(
        "SELECT MIN(business_date), MAX(business_date) FROM raw_staffing_actual WHERE store_id = ?",
        (store_id,),
    ).fetchone()
    if date_range_row[0] is None:
        st.info(f"{store_id} 店目前沒有排班原始資料，無法算星期幾眾數。")
    else:
        roster_rows = roster_mode_by_weekday(
            conn, store_id, date_range_row[0], date_range_row[1], saved_config
        )
        roster_df = pd.DataFrame(roster_rows)
        st.dataframe(roster_df, hide_index=True, use_container_width=True)
        st.caption(
            f"取樣範圍 {date_range_row[0]} ~ {date_range_row[1]}。"
            "「眾數」＝這個時段、這個星期幾，在取樣範圍內最常出現的（正職人數, 兼職人數）組合。"
            "「一致比例」＝這個眾數組合的可信度，例如某格顯示「9/10」，代表取樣範圍內這個時段"
            "共有 10 個該星期幾可以比對，其中 9 天的班表都排出跟眾數一樣的（正職, 兼職）人數，"
            "只有 1 天不一樣——比例越接近「分母/分母」（例如 10/10）代表這個時段的排法越固定，"
            "比例偏低（例如 5/10 或更低）代表這個時段的實際排法變動很大，眾數只是「最常見」的"
            "配置，不是「幾乎每次都這樣」，看到比例偏低的格子時，這個數字的參考價值要打折扣。"
        )

    st.subheader("星期幾 x 時段：發票張數與營業額")
    weekday_rows = hourly_channel_by_weekday(conn, store_id)
    if not weekday_rows:
        st.info(f"{store_id} 店目前沒有發票明細資料，無法拆分星期幾。")
    else:
        weekday_choice = st.selectbox("星期幾", WEEKDAY_NAMES, format_func=lambda w: f"星期{w}", key="channel_weekday")
        bar_df = pd.DataFrame(
            [
                {"時段": r["時段"], "發票張數": r[f"星期{weekday_choice}_發票張數"]}
                for r in weekday_rows
                if r[f"星期{weekday_choice}_發票張數"] is not None
            ]
        )
        line_df = pd.DataFrame(
            [
                {"時段": r["時段"], "營業額": r[f"星期{weekday_choice}_營業額"]}
                for r in weekday_rows
                if r[f"星期{weekday_choice}_營業額"] is not None
            ]
        )
        if bar_df.empty:
            st.info(f"{store_id} 店星期{weekday_choice}目前沒有足夠樣本。")
        else:
            # 用「樣本天數最多的時段」當代表值，不是隨便抓第一個有資料的時段——冷門時段
            # （例如剛開店的 07/08 點）常常整天掛零單，樣本天數會比尖峰時段少很多，
            # 拿冷門時段的天數當「取樣約 N 天」的說明文字會嚴重低估實際取樣範圍。
            sample_days = max(
                (r[f"星期{weekday_choice}_樣本天數"] for r in weekday_rows), default=0
            )
            channel_chart = build_bar_line_combo_chart(
                bar_df, "時段", "發票張數", "發票張數（張）",
                line_df, "營業額", "營業額（元）",
                line_color=CHART_COLOR_COMBINED_NET_PROFIT,
            )
            st.altair_chart(channel_chart, use_container_width=True)
            st.caption(
                f"用每筆交易的真實日期回推星期{weekday_choice}（取樣約 {sample_days} 個星期{weekday_choice}），"
                "「發票張數」是杯量的替代指標（目前沒有逐日、涵蓋一~日七天的真實杯數資料），"
                "「營業額」是直接量到的真實金額，兩者都是日均值，不是取樣期間的總和。"
            )

    st.subheader("星期幾彙整：每日營業額/發票張數的中位數、最大值、最小值")
    weekday_summary_rows = weekday_daily_summary(conn, store_id)
    if not weekday_summary_rows:
        st.info(f"{store_id} 店目前沒有發票明細資料，無法彙整星期幾統計。")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "星期": f"星期{r['星期']}",
                        "天數": r["天數"],
                        "營業額中位數": r["營業額中位數"],
                        "營業額最小": r["營業額最小"],
                        "最小發生日": r["營業額最小日期"],
                        "營業額最大": r["營業額最大"],
                        "最大發生日": r["營業額最大日期"],
                        "發票中位數": r["發票中位數"],
                        "發票最小": r["發票最小"],
                        "發票最小發生日": r["發票最小日期"],
                        "發票最大": r["發票最大"],
                        "發票最大發生日": r["發票最大日期"],
                    }
                    for r in weekday_summary_rows
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "先把每天彙總成當天的總營業額/總發票張數，再依星期幾分組取中位數/最大/最小值"
            "（並附上最大/最小值實際發生的日期，方便回頭查是不是連假、天氣、設備故障等特殊事件）。"
            "跟上面「星期幾 x 時段」圖表的差別：那張圖是時段層級的日均值，這張表是先看「當天全天」表現。"
        )


def load_cookie_settings() -> dict:
    if not CONFIG_PATH.exists():
        st.error(
            "找不到 config/auth_config.yaml，請先在終端機執行：\n\n"
            "`python scripts/manage_accounts.py add --username ... "
            "--name ... --email ... --role admin`\n\n建立至少一個管理者帳號。"
        )
        st.stop()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=SafeLoader)["cookie"]


cookie = load_cookie_settings()

# credentials 直接傳「設定檔路徑」（字串），套件會自動讀取/寫回整份設定檔
# （包含密碼變更、登入紀錄），不用我們自己再手動存檔。
authenticator = stauth.Authenticate(
    str(CONFIG_PATH),
    cookie["name"],
    cookie["key"],
    cookie["expiry_days"],
    auto_hash=False,  # 密碼一律由 manage_accounts.py 預先雜湊，避免誤判明文密碼
)

authenticator.login(location="main")

auth_status = st.session_state.get("authentication_status")

if auth_status is False:
    st.error("帳號或密碼錯誤")
elif auth_status is None:
    st.info("請輸入帳號密碼登入")
elif auth_status:
    name = st.session_state.get("name")
    username = st.session_state.get("username")
    roles = st.session_state.get("roles") or []

    with st.sidebar:
        st.write(f"歡迎，{name}")
        authenticator.logout(location="sidebar")

        pages = []
        if "admin" in roles:
            pages.append("月盈虧")
        pages.append("排班建議")
        pages.append("時段人力與杯數")
        choice = st.radio("功能選單", pages)

        st.divider()
        st.caption("修改密碼")
        try:
            if authenticator.reset_password(username, location="sidebar"):
                st.success("密碼已更新，下次登入請用新密碼")
        except Exception as e:
            st.error(str(e))

    if choice == "月盈虧":
        st.title("月盈虧")
        render_pnl_page()
    elif choice == "排班建議":
        st.title("排班建議")
        render_staffing_page()
    elif choice == "時段人力與杯數":
        st.title("時段人力與杯數")
        render_hourly_pattern_page()
