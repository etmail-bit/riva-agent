"""Streamlit 網頁入口（雲端部署版）：月盈虧＋排班摘要（彙總後的安全版）。

跟本機用的 app.py 是分開的獨立入口：
  - app.py    本機跑，含月盈虧＋完整版排班（逐日明細、員工代碼對照，較機密，不上雲）
  - app_pnl.py 雲端部署用，排班部分只 import／顯示彙總後的安全結果，
    raw_staffing_actual（逐日逐員工明細）與 config 的 wages／employee_roles
    永遠不會被這支程式碰到，見 render_staffing_summary_page() 的說明。

資料庫讀的是雲端安全版快照（見 get_db_connection() 說明），不是本機的
db/riva_agent.db；快照由 scripts/build_cloud_snapshot.py 產生。

帳號設定檔是 config/auth_config.yaml（不進版控），一律用
scripts/manage_accounts.py 管理帳號，不要手動編輯。
"""
import base64
import copy
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from dotenv import load_dotenv
from yaml.loader import SafeLoader

from scripts.analyze_staffing_daytype import HOUR_SLOTS, WEEKDAY_NAMES, cup_stats_by_daytype
from scripts.calculate_pnl import COST_ACTUAL_COLUMNS, calculate_one, get_fixed_cost, get_revenue_breakdown, save_pnl_result
from scripts.calculate_pnl import load_config as load_pnl_config
from scripts.calculate_staffing import (
    calculate_delivery_hours,
    calculate_hourly_staffing,
    get_hourly_data,
    is_tea_brewing_hour,
    required_front_staff_for_hour,
)
from scripts.calculate_staffing import load_config as load_staffing_config_local
from scripts.chart_helpers import build_bar_line_combo_chart, build_trend_chart
from scripts.estimate_staffing_by_weekday import avg_delivery_by_hour
from scripts.pnl_insights import add_total_row, generate_monthly_breakdown, generate_pnl_insights

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "auth_config.yaml"
COST_CONFIG_PATH = ROOT / "config" / "cost_rates.json"
STAFFING_CONFIG_PATH = ROOT / "config" / "staffing_rules.json"

load_dotenv(ROOT / ".env")  # 本機測試用；部署到 Streamlit Cloud 後改吃 st.secrets

CHART_COLOR_REVENUE = "#2a78d6"
CHART_COLOR_NET_PROFIT = "#1baf7a"
CHART_COLOR_COMBINED_NET_PROFIT = "#d97706"
CHART_COLOR_STORE_B_NET_PROFIT = "#7c3aed"

st.set_page_config(page_title="飲料店月盈虧", page_icon="🧋")

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


def _get_secret(key: str) -> str:
    """Streamlit Cloud 部署後優先讀 st.secrets；本機測試時 fallback 讀 .env（os.environ）。"""
    try:
        return st.secrets[key]
    except Exception:
        return os.environ[key]


def load_cost_rates_config() -> dict:
    """雲端部署時，真實成本數字存在 Secrets 的 COST_RATES_JSON（內容是整份
    cost_rates.json 的文字）；本機測試時沒有這把 key，fallback 讀本機檔案
    （config/cost_rates.json 本來就被 .gitignore 排除，雲端主機讀不到）。"""
    try:
        return json.loads(st.secrets["COST_RATES_JSON"])
    except Exception:
        return load_pnl_config()


def load_staffing_rules_config() -> dict:
    """雲端部署時讀 Secrets 的 STAFFING_RULES_JSON——這把 key 裡只會有
    scripts/print_safe_staffing_config.py 印出的安全子集（capacity/delivery/
    tea_brewing/shifts/part_time/scenario），不含 wages／employee_roles，
    是使用者自己在本機產生、自己貼上雲端後台的，這支程式從頭到尾不會經手
    真實薪資或員工代碼對照表。本機測試時 fallback 讀本機完整版
    config/staffing_rules.json 沒關係，因為這個頁面只會用到上面那幾個安全 key。"""
    try:
        return json.loads(st.secrets["STAFFING_RULES_JSON"])
    except Exception:
        return load_staffing_config_local()


@st.cache_resource
def get_db_connection() -> sqlite3.Connection:
    """2026-07-13 汰換 Turso（見 scripts/build_cloud_snapshot.py 檔頭說明）：雲端部署時，
    資料庫快照整份存在 Secrets 的 DB_SNAPSHOT_B64（Base64 編碼），解碼寫成暫存檔後用
    sqlite3 開啟，不再依賴任何外部資料庫服務的即時連線。本機測試時沒有這把 Secrets key，
    fallback 直接讀 build_cloud_snapshot.py 產生的本機檔案 db/cloud_snapshot.db，
    跟雲端走同一套 SQL 查詢程式碼。"""
    try:
        raw = base64.b64decode(st.secrets["DB_SNAPSHOT_B64"])
        db_path = Path(tempfile.gettempdir()) / "riva_agent_cloud_snapshot.db"
        db_path.write_bytes(raw)
    except Exception:
        db_path = ROOT / "db" / "cloud_snapshot.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
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
        existing
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


def render_operational_insights(conn: sqlite3.Connection) -> None:
    """通路組合／客單價相對指數／回頭客（2026-07-14 新增，取代 2026-07-09「完全不公開」
    的舊決定）。只讀 store_operational_insights.public_summary_text 這個安全版 JSON
    （見 scripts/analyze_operations.public_operational_summary()）——通路組合/回頭客
    是百分比，客單價是「相對指數」（平均值=100，不是真實金額），不含任何真實金額或
    真實客數，真實客單價的完整版只留在本機 app.py。"""
    rows = conn.execute(
        "SELECT store_id, public_summary_text FROM store_operational_insights "
        "WHERE public_summary_text IS NOT NULL ORDER BY store_id"
    ).fetchall()
    if not rows:
        return

    st.subheader("營運概況（通路組合／客單價相對指數／回頭客）")
    st.caption("客單價用「相對指數」呈現（平均值＝100），不是真實金額；通路組合／回頭客都是百分比，不含真實客數。")
    for r in rows:
        data = json.loads(r["public_summary_text"])
        st.markdown(f"**{r['store_id']} 店**")
        cols = st.columns(3)
        if data.get("delivery_pct") is not None:
            cols[0].metric("外送平台佔營收", f"{data['delivery_pct']}%")
        if data.get("repeat_customer_pct") is not None:
            cols[1].metric("回頭客佔客數", f"{data['repeat_customer_pct']}%")
        if data.get("repeat_revenue_pct") is not None:
            cols[2].metric("回頭客佔營收", f"{data['repeat_revenue_pct']}%")
        ticket = data.get("ticket_price_index")
        if ticket:
            st.caption(
                f"客單價相對指數（平均＝100）：中位數 {ticket['median_index']}、"
                f"25分位 {ticket['p25_index']}、75分位 {ticket['p75_index']}"
            )
        if data.get("peak_hours"):
            st.caption(f"尖峰時段：{', '.join(f'{h}:00' for h in data['peak_hours'])}")


def render_combined_pnl_page(conn: sqlite3.Connection, stores: list) -> None:
    """兩店合計盈虧趨勢＋彙整建議＋營運概況（2026-07-14 起營運概況的安全版摘要也會
    顯示，見 render_operational_insights()；本機版 app.py 才有的逐筆通路明細/客單價
    分布圖表這種更細的版本，依零洩漏原則只留在本機）。"""
    st.subheader("兩店合計盈虧趨勢")
    rows = conn.execute(
        "SELECT year_month, store_id, net_profit FROM monthly_pnl ORDER BY year_month"
    ).fetchall()
    if not rows:
        st.caption("monthly_pnl 還沒有歷史紀錄。")
        return

    by_month = {}
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

    render_operational_insights(conn)


def render_pnl_page() -> None:
    conn = get_db_connection()
    config = load_cost_rates_config()

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
        r["year_month"]
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
        return f"{name}__{store_id}__{year_month}"

    actuals_row = conn.execute(
        f"SELECT {', '.join(COST_ACTUAL_COLUMNS)} FROM monthly_cost_actuals "
        "WHERE store_id = ? AND year_month = ?",
        (store_id, year_month),
    ).fetchone()
    actuals = actuals_row if actuals_row else {col: None for col in COST_ACTUAL_COLUMNS}

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
        if is_cloud:
            st.caption("雲端版本不支援「儲存為新的預設值」（雲端硬碟重啟後會還原），請用「儲存為本月實際值」，或聯絡管理者更新 Secrets。")
        elif st.button("儲存為新的預設值", key="save_cost_rates"):
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

    saved_pnl = conn.execute(
        "SELECT revenue, cogs, material_waste, platform_commission, payment_processing_fee, "
        "labor_cost, labor_cost_source, rent, utilities, franchise_amortization, business_tax, "
        "pretax_profit, income_tax_estimate, net_profit, calculated_at "
        "FROM monthly_pnl WHERE store_id = ? AND year_month = ?",
        (store_id, year_month),
    ).fetchone()

    # 2026-07-14 修正：最上面三個大數字之前永遠顯示「即時試算」，下面明細表改抓已儲存
    # 版本之後，同一頁會出現兩個不同的「稅前淨利」，非常容易誤會。現在兩處統一用同一個
    # 來源（`display`）決定——有已儲存紀錄就都用那份（比較穩定，可能含即時試算算不出來
    # 的真實薪資），沒有才 fallback 用即時試算，不會再各自抓不同來源。
    display = saved_pnl if saved_pnl is not None else result
    saved_tag = "（已儲存版本）" if saved_pnl is not None else ""
    if display["labor_cost_source"] == "real_payroll":
        st.caption(f"✅ 人事成本是真實薪資計算結果{saved_tag}。")
    elif display["labor_cost_source"] == "manual_actual":
        st.caption(f"📝 人事成本＝手動輸入的底薪 × 概算保費負擔率{saved_tag}。")
    elif display["labor_cost_source"] == "estimate":
        st.caption(f"📐 人事成本目前是概算值，僅供參考{saved_tag}。")

    col1, col2, col3 = st.columns(3)
    col1.metric("營收", f"{display['revenue']:,}")
    col2.metric("稅前淨利", f"{display['pretax_profit']:,}")
    col3.metric("稅後淨利", f"{display['net_profit']:,}")

    if saved_pnl is None:
        st.caption("尚未儲存正式紀錄，以上是即時試算結果，按下面按鈕可寫入 monthly_pnl。")
    elif saved_pnl["net_profit"] != result["net_profit"]:
        st.caption(
            f"⚠️ 以上是已儲存的正式紀錄（{saved_pnl['calculated_at']}）。"
            f"目前試算結果不同（稅後淨利 {result['net_profit']:,}），按下面按鈕可更新成目前這個版本。"
        )
    else:
        st.caption(f"✅ 以上跟目前試算結果一致（最後計算時間 {saved_pnl['calculated_at']}）")

    if st.button("儲存本月盈虧結果", key="save_pnl_result"):
        save_pnl_result(conn, store_id, year_month, result)
        conn.commit()
        st.success(f"已將 {store_id} 店 {year_month} 的盈虧結果寫入 monthly_pnl")
        st.rerun()

    def _breakdown_rows(d):
        return [
            ("營收", d["revenue"]),
            ("原物料（含包材）", -d["cogs"]),
            ("原物料損耗", -d["material_waste"]),
            ("平台抽成", -d["platform_commission"]),
            ("金流手續費", -d["payment_processing_fee"]),
            ("人事（含勞健保）", -d["labor_cost"]),
            ("房租", -d["rent"]),
            ("水電", -d["utilities"]),
            ("加盟金攤提", -d["franchise_amortization"]),
            ("營業稅", -d["business_tax"]),
            ("稅前淨利", d["pretax_profit"]),
            ("預估所得稅", -d["income_tax_estimate"]),
            ("稅後淨利", d["net_profit"]),
        ]

    st.dataframe(pd.DataFrame(_breakdown_rows(display), columns=["項目", "金額"]), hide_index=True, use_container_width=True)
    if saved_pnl is not None and saved_pnl["net_profit"] != result["net_profit"]:
        with st.expander("目前試算結果明細（參數或資料來源跟已儲存版本不同才會出現差異）"):
            st.dataframe(pd.DataFrame(_breakdown_rows(result), columns=["項目", "金額"]), hide_index=True, use_container_width=True)

    st.subheader(f"{store_id} 店歷史走勢")
    history = conn.execute(
        "SELECT year_month, revenue, net_profit FROM monthly_pnl "
        "WHERE store_id = ? ORDER BY year_month",
        (store_id,),
    ).fetchall()
    if not history:
        st.caption("monthly_pnl 還沒有歷史紀錄，請先執行 scripts/calculate_pnl.py。")
        return

    history_df = pd.DataFrame(
        [(r["year_month"], r["revenue"], r["net_profit"]) for r in history],
        columns=["year_month", "營收", "稅後淨利"],
    )
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


def render_staffing_summary_page() -> None:
    """排班摘要（雲端安全版）。2026-07-10 新增，只顯示彙總後的結果：

    - 「逐時段建議人力」「平日/假日杯數配置」：雲端即時運算，用的是
      raw_hourly_pattern_monthly／raw_hourly_pattern_daily（月/日彙總，本來就
      不含員工資料）＋ load_staffing_rules_config() 的安全子集設定，公式跟本機
      app.py 完全共用（scripts/calculate_staffing.py／
      scripts/analyze_staffing_daytype.py），不是另外寫一份。

    - 「實際 vs 建議人力比對」「正職/兼職眾數」：這兩項需要 raw_staffing_actual
      （逐日逐員工）才能算，這張表本身不上雲，所以雲端這裡查的是
      staffing_hourly_comparison／staffing_roster_mode 這兩張快照表——本機執行
      `python3 -m scripts.migrate_layer2_to_turso` 時才會更新，不是雲端即時重算，
      頁面上會標示資料同步時間，跟目前即時互動的「建議人力表」不同，第一版先不
      提供調參數功能（比照月盈虧頁之後再視需要加）。
    """
    conn = get_db_connection()
    working_config = load_staffing_rules_config()

    stores = [r["store_id"] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()]
    store_id = st.selectbox("店別", stores, format_func=lambda s: f"{s} 店", key="staffing_store")

    periods = [
        r["year_month"]
        for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id = ? ORDER BY 1 DESC",
            (store_id,),
        ).fetchall()
    ]
    if not periods:
        st.info(f"{store_id} 店目前雲端還沒有時段占比資料，請先在本機執行同步腳本。")
        return
    year_month = st.selectbox("月份", periods, key="staffing_month")

    st.subheader(f"{store_id} 店　{year_month}　逐時段建議人力")
    hourly_data = get_hourly_data(conn, store_id, year_month)
    if not hourly_data:
        st.info("這個月份沒有時段占比資料。")
    else:
        staffing = calculate_hourly_staffing(hourly_data, working_config)
        hourly_rows = [
            {
                "時段": f"{hour_slot}:00",
                "日均杯數": staffing[hour_slot]["cups"],
                "建議前場人力": staffing[hour_slot]["required_front_staff"],
                "公式（取整前）": staffing[hour_slot]["required_front_staff_formula"],
                "開早班": "Y" if staffing[hour_slot]["tea_brewing"] else "",
                "日均外送單": staffing[hour_slot]["delivery_count"],
                "外送耗時(hr)": staffing[hour_slot]["delivery_hours"],
            }
            for hour_slot in sorted(staffing.keys())
        ]
        st.dataframe(pd.DataFrame(hourly_rows), hide_index=True, use_container_width=True)
        st.caption(
            "「開早班」欄有標記的時段，那個人整段班次前場產能只算8杯/hr（其他人一律全產能），"
            "「建議前場人力」已經是總人數（含這個人），不用再另外+1。"
            "已經把外送耗時併進需求：開早班時段 ceil(max(0,杯數-8)/產能 + 外送耗時) + 1，其餘時段 ceil(杯數/產能 + 外送耗時)"
        )

    st.subheader(f"{store_id} 店　平日/假日逐時段杯數")
    all_months = [
        r["year_month"]
        for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id = ? ORDER BY 1",
            (store_id,),
        ).fetchall()
    ]
    daytype_stats = cup_stats_by_daytype(conn, store_id, all_months)
    if not daytype_stats["平日"] and not daytype_stats["假日"]:
        st.info(f"{store_id} 店目前雲端沒有可拆分平日/假日的樣本資料。")
    else:
        wd_by_hour = {row["時段"]: row for row in daytype_stats["平日"]}
        we_by_hour = {row["時段"]: row for row in daytype_stats["假日"]}
        all_hours = sorted(set(wd_by_hour) | set(we_by_hour))
        merged_rows = [
            {
                "時段": h,
                "平日_月數": wd_by_hour.get(h, {}).get("月數"),
                "假日_月數": we_by_hour.get(h, {}).get("月數"),
                "平日_平均杯數": wd_by_hour.get(h, {}).get("平均杯數"),
                "假日_平均杯數": we_by_hour.get(h, {}).get("平均杯數"),
                "平日_最大值": wd_by_hour.get(h, {}).get("最大值"),
                "假日_最大值": we_by_hour.get(h, {}).get("最大值"),
                "平日_最小值": wd_by_hour.get(h, {}).get("最小值"),
                "假日_最小值": we_by_hour.get(h, {}).get("最小值"),
            }
            for h in all_hours
        ]
        st.dataframe(pd.DataFrame(merged_rows), hide_index=True, use_container_width=True)
        chart_df = pd.DataFrame(
            [{"時段": h, "類型": "平日", "平均杯數": wd_by_hour[h]["平均杯數"]} for h in all_hours if h in wd_by_hour]
            + [{"時段": h, "類型": "假日", "平均杯數": we_by_hour[h]["平均杯數"]} for h in all_hours if h in we_by_hour]
        )
        daytype_cups_chart = (
            alt.Chart(chart_df)
            .mark_line(point=True, strokeWidth=2)
            .encode(
                x=alt.X("時段:N", title="時段"),
                y=alt.Y("平均杯數:Q", title="平均杯數（杯）"),
                color=alt.Color("類型:N", legend=alt.Legend(title=None)),
                tooltip=["時段", "類型", "平均杯數"],
            )
            .properties(height=300)
        )
        st.altair_chart(daytype_cups_chart, use_container_width=True)

    st.subheader(f"{store_id} 店　實際 vs 建議人力比對")
    compare_mode = st.radio(
        "比對範圍", ["單月", "全年彙總（排除2月）", "平日/假日拆分（真實資料）"],
        horizontal=True, key="staffing_compare_mode",
    )

    months_note = ""
    if compare_mode == "平日/假日拆分（真實資料）":
        daytype_rows = conn.execute(
            "SELECT daytype, hour_slot, cups, recommended, formula, actual, diff "
            "FROM staffing_hourly_comparison_daytype WHERE store_id = ? ORDER BY daytype, hour_slot",
            (store_id,),
        ).fetchall()
        if not daytype_rows:
            st.info(f"{store_id} 店雲端還沒有平日/假日拆分快照，請先在本機執行同步腳本。")
        else:
            st.caption(
                "杯量用真實星期六/日單日樣本反推，只涵蓋有這種樣本的月份，範圍比「全年彙總」窄，"
                "但每個數字都是真實資料撐出來的。這是本機同步時算好的快照，不是即時重算。"
            )
            for daytype in ("平日", "假日"):
                rows = [r for r in daytype_rows if r["daytype"] == daytype]
                st.markdown(f"**{daytype}**")
                if not rows:
                    st.info(f"{store_id} 店目前沒有{daytype}的真實樣本可以比對。")
                    continue
                daytype_df = pd.DataFrame(
                    [
                        {
                            "時段": f"{r['hour_slot']}:00",
                            "建議人力": r["recommended"],
                            "公式（取整前）": r["formula"],
                            "實際平均人力": r["actual"],
                            "差異": r["diff"],
                        }
                        for r in rows
                    ]
                )
                st.dataframe(daytype_df, hide_index=True, use_container_width=True)
                bar_df = pd.DataFrame(
                    [{"時段": r["hour_slot"], "類別": "建議人力", "人力": r["recommended"]} for r in rows]
                    + [{"時段": r["hour_slot"], "類別": "實際平均人力", "人力": r["actual"]} for r in rows]
                )
                line_df = pd.DataFrame([{"時段": r["hour_slot"], "日均杯數": r["cups"]} for r in rows])
                daytype_chart = build_bar_line_combo_chart(
                    bar_df, "時段", "人力", "人力（人）",
                    line_df, "日均杯數", "日均杯數（杯）",
                    bar_category_field="類別",
                    bar_domain=["建議人力", "實際平均人力"],
                    bar_range=[CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT],
                    line_color=CHART_COLOR_COMBINED_NET_PROFIT,
                )
                st.altair_chart(daytype_chart, use_container_width=True)
    elif compare_mode == "單月":
        comparison_rows = conn.execute(
            "SELECT hour_slot, recommended, actual, diff FROM staffing_hourly_comparison "
            "WHERE store_id = ? AND year_month = ? ORDER BY hour_slot",
            (store_id, year_month),
        ).fetchall()
        cups_lookup = {h: staffing[h]["cups"] for h in staffing} if hourly_data else {}
        empty_message = "這個月份雲端還沒有比對快照，請先在本機執行同步腳本。"
    else:
        year_row = conn.execute(
            "SELECT MAX(year) AS year FROM staffing_hourly_comparison_yearly WHERE store_id = ?", (store_id,)
        ).fetchone()
        year = year_row["year"] if year_row else None
        if year:
            comparison_rows = conn.execute(
                "SELECT hour_slot, recommended, actual, diff, cups, months_included "
                "FROM staffing_hourly_comparison_yearly WHERE store_id = ? AND year = ? ORDER BY hour_slot",
                (store_id, year),
            ).fetchall()
            cups_lookup = {r["hour_slot"]: r["cups"] for r in comparison_rows}
            if comparison_rows:
                months_note = f"（納入月份：{comparison_rows[0]['months_included']}）"
        else:
            comparison_rows = []
            cups_lookup = {}
        empty_message = "雲端還沒有全年彙總快照，請先在本機執行同步腳本。"

    if compare_mode != "平日/假日拆分（真實資料）":
        if not comparison_rows:
            st.info(empty_message)
        else:
            comparison_df = pd.DataFrame(
                [
                    {
                        "時段": f"{r['hour_slot']}:00",
                        "建議人力": r["recommended"],
                        "實際平均人力": r["actual"],
                        "差異": r["diff"],
                    }
                    for r in comparison_rows
                ]
            )
            st.dataframe(comparison_df, hide_index=True, use_container_width=True)
            st.caption(f"這是本機同步時算好的快照，不是即時重算——本機排班資料更新後要重新執行同步腳本，這裡才會跟著更新。{months_note}")

            # 「建議人力」每個時段都有，不需要有實際資料才顯示；「實際平均人力」缺資料的
            # 時段留 None，Altair 對 None 的數值長條不會畫（空白），不會擋住建議人力那根長條。
            bar_df = pd.DataFrame(
                [{"時段": r["hour_slot"], "類別": "建議人力", "人力": r["recommended"]} for r in comparison_rows]
                + [{"時段": r["hour_slot"], "類別": "實際平均人力", "人力": r["actual"]} for r in comparison_rows]
            )
            line_df = pd.DataFrame(
                [
                    {"時段": r["hour_slot"], "日均杯數": cups_lookup[r["hour_slot"]]}
                    for r in comparison_rows
                    if r["hour_slot"] in cups_lookup
                ]
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
            st.caption("橘線是該時段日均杯數（右軸）")

    st.subheader(f"{store_id} 店　星期幾 x 時段實際排班人力（正職/兼職眾數）")
    roster_rows = conn.execute(
        "SELECT hour_slot, weekday, full_time_count, part_time_count, consistency_ratio, generated_at "
        "FROM staffing_roster_mode WHERE store_id = ?",
        (store_id,),
    ).fetchall()
    if not roster_rows:
        st.info(f"{store_id} 店目前雲端還沒有排班眾數快照，請先在本機執行同步腳本。")
    else:
        by_hour = {}
        generated_at = roster_rows[0]["generated_at"]
        for r in roster_rows:
            row = by_hour.setdefault(r["hour_slot"], {"時段": f"{r['hour_slot']}:00"})
            row[f"星期{r['weekday']}_正職"] = r["full_time_count"]
            row[f"星期{r['weekday']}_兼職"] = r["part_time_count"]
            row[f"星期{r['weekday']}_一致比例"] = r["consistency_ratio"]
        roster_df = pd.DataFrame(
            [by_hour[h] for h in HOUR_SLOTS if h in by_hour]
        )
        st.dataframe(roster_df, hide_index=True, use_container_width=True)
        st.caption(
            f"這是本機同步時算好的快照（{generated_at}），不是即時重算。"
            "「眾數」＝這個時段、這個星期幾，取樣範圍內最常出現的（正職人數, 兼職人數）組合；"
            "「一致比例」越接近「分母/分母」代表這個時段的排法越固定。"
        )

    st.subheader(f"{store_id} 店　星期幾 x 時段：發票張數與營業額")
    weekday_rows = conn.execute(
        "SELECT weekday, hour_slot, invoice_count, revenue, sample_days FROM staffing_channel_by_weekday "
        "WHERE store_id = ?",
        (store_id,),
    ).fetchall()
    if not weekday_rows:
        st.info(f"{store_id} 店目前雲端還沒有星期幾發票張數/營業額快照，請先在本機執行同步腳本。")
    else:
        weekday_choice = st.selectbox("星期幾", WEEKDAY_NAMES, format_func=lambda w: f"星期{w}", key="cloud_channel_weekday")
        selected_rows = [r for r in weekday_rows if r["weekday"] == weekday_choice]
        if not selected_rows:
            st.info(f"{store_id} 店星期{weekday_choice}目前沒有足夠樣本。")
        else:
            bar_df = pd.DataFrame([{"時段": r["hour_slot"], "發票張數": r["invoice_count"]} for r in selected_rows])
            line_df = pd.DataFrame([{"時段": r["hour_slot"], "營業額": r["revenue"]} for r in selected_rows])
            sample_days = max((r["sample_days"] or 0) for r in selected_rows)
            channel_chart = build_bar_line_combo_chart(
                bar_df, "時段", "發票張數", "發票張數（張）",
                line_df, "營業額", "營業額（元）",
                line_color=CHART_COLOR_COMBINED_NET_PROFIT,
            )
            st.altair_chart(channel_chart, use_container_width=True)
            st.caption(
                f"用每筆交易的真實日期回推星期{weekday_choice}（取樣約 {sample_days} 個星期{weekday_choice}），"
                "「發票張數」是杯量的替代指標（目前沒有逐日、涵蓋一~日七天的真實杯數資料），"
                "「營業額」是直接量到的真實金額，兩者都是日均值。這是本機同步時算好的快照，不是即時重算。"
            )

    st.subheader(f"{store_id} 店　星期幾 x 時段：建議人力 vs 實際排班人力")
    ratio_row = conn.execute(
        "SELECT ratio FROM staffing_cup_invoice_ratio WHERE store_id = ?", (store_id,)
    ).fetchone()
    if not ratio_row or not weekday_rows:
        st.info(f"{store_id} 店目前雲端沒有足夠的杯數/發票資料，無法估算星期幾建議人力。")
    else:
        ratio = ratio_row["ratio"]
        delivery_by_hour = avg_delivery_by_hour(conn, store_id)
        invoice_by_hour = {}
        for r in weekday_rows:
            invoice_by_hour.setdefault(r["hour_slot"], {})[r["weekday"]] = r["invoice_count"]
        roster_actual_by_hour = {}
        for r in roster_rows:
            actual = None
            if r["full_time_count"] is not None and r["part_time_count"] is not None:
                actual = r["full_time_count"] + r["part_time_count"]
            roster_actual_by_hour.setdefault(r["hour_slot"], {})[r["weekday"]] = actual

        def _weekday_hour_rows(weekday: str) -> list:
            rows = []
            for hour_slot in HOUR_SLOTS:
                invoice_count = invoice_by_hour.get(hour_slot, {}).get(weekday)
                if invoice_count is None:
                    continue
                est_cups = round(invoice_count * ratio, 1)
                delivery_hours = calculate_delivery_hours(delivery_by_hour.get(hour_slot, 0.0), working_config)
                is_tea = is_tea_brewing_hour(hour_slot, working_config)
                calc = required_front_staff_for_hour(est_cups, delivery_hours, is_tea, working_config)
                rows.append({
                    "hour_slot": hour_slot,
                    "estimated_cups": est_cups,
                    "recommended": calc["required"],
                    "formula": calc["formula"],
                    "actual": roster_actual_by_hour.get(hour_slot, {}).get(weekday),
                })
            return rows

        est_weekday_choice = st.selectbox(
            "星期幾", WEEKDAY_NAMES, format_func=lambda w: f"星期{w}", key="cloud_staffing_weekday"
        )
        weekday_rows_calc = _weekday_hour_rows(est_weekday_choice)
        if not weekday_rows_calc:
            st.info(f"{store_id} 店星期{est_weekday_choice}目前沒有足夠樣本。")
        else:
            detail_df = pd.DataFrame(
                [
                    {
                        "時段": f"{r['hour_slot']}:00",
                        "估計杯數": r["estimated_cups"],
                        "公式（取整前）": r["formula"],
                        "建議人力": r["recommended"],
                        "實際人力": r["actual"],
                    }
                    for r in weekday_rows_calc
                ]
            )
            st.dataframe(detail_df, hide_index=True, use_container_width=True)
            bar_df = pd.DataFrame(
                [{"時段": r["hour_slot"], "類別": "建議人力", "人力": r["recommended"]} for r in weekday_rows_calc]
                + [{"時段": r["hour_slot"], "類別": "實際人力", "人力": r["actual"]} for r in weekday_rows_calc]
            )
            line_df = pd.DataFrame([{"時段": r["hour_slot"], "估計杯數": r["estimated_cups"]} for r in weekday_rows_calc])
            weekday_staffing_chart = build_bar_line_combo_chart(
                bar_df, "時段", "人力", "人力（人）",
                line_df, "估計杯數", "估計杯數（杯，推算值）",
                bar_category_field="類別",
                bar_domain=["建議人力", "實際人力"],
                bar_range=[CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT],
                line_color=CHART_COLOR_COMBINED_NET_PROFIT,
            )
            st.altair_chart(weekday_staffing_chart, use_container_width=True)
            st.caption(
                "估計杯數＝發票張數 × 「每張發票約幾杯」轉換比例，是推算值，不是實際量到的杯數；"
                "外送耗時修正用跨月日均值套用到七天，也是近似值。「實際人力」沒有排班原始資料的"
                "時段留空，不是 0 人。"
            )

        st.markdown("**平日／假日彙整**")
        daytype_weekdays = {**{wd: "平日" for wd in WEEKDAY_NAMES[:5]}, **{wd: "假日" for wd in WEEKDAY_NAMES[5:]}}
        for daytype in ("平日", "假日"):
            wds_in_group = [wd for wd, dt in daytype_weekdays.items() if dt == daytype]
            per_hour = {}
            for wd in wds_in_group:
                for r in _weekday_hour_rows(wd):
                    cell = per_hour.setdefault(r["hour_slot"], {"cups": [], "recommended": [], "actual": []})
                    cell["cups"].append(r["estimated_cups"])
                    cell["recommended"].append(r["recommended"])
                    if r["actual"] is not None:
                        cell["actual"].append(r["actual"])
            if not per_hour:
                st.info(f"{store_id} 店目前沒有{daytype}的估計資料可以彙整。")
                continue
            daytype_merged = [
                {
                    "時段": hour_slot,
                    "建議人力": round(sum(v["recommended"]) / len(v["recommended"]), 1),
                    "實際人力": round(sum(v["actual"]) / len(v["actual"]), 2) if v["actual"] else None,
                    "估計杯數": round(sum(v["cups"]) / len(v["cups"]), 1),
                }
                for hour_slot, v in sorted(per_hour.items())
            ]
            st.caption(f"{daytype}（{'、'.join(f'星期{wd}' for wd in wds_in_group)} 平均）")
            daytype_bar_df = pd.DataFrame(
                [{"時段": r["時段"], "類別": "建議人力", "人力": r["建議人力"]} for r in daytype_merged]
                + [{"時段": r["時段"], "類別": "實際人力", "人力": r["實際人力"]} for r in daytype_merged]
            )
            daytype_line_df = pd.DataFrame([{"時段": r["時段"], "估計杯數": r["估計杯數"]} for r in daytype_merged])
            daytype_weekday_chart = build_bar_line_combo_chart(
                daytype_bar_df, "時段", "人力", "人力（人）",
                daytype_line_df, "估計杯數", "估計杯數（杯，推算值）",
                bar_category_field="類別",
                bar_domain=["建議人力", "實際人力"],
                bar_range=[CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT],
                line_color=CHART_COLOR_COMBINED_NET_PROFIT,
            )
            st.altair_chart(daytype_weekday_chart, use_container_width=True)

    st.subheader(f"{store_id} 店　星期幾彙整（相對星期五的差異）")
    summary_rows = conn.execute(
        "SELECT weekday, days, revenue_median_vs_friday, revenue_min_vs_friday, revenue_min_date, "
        "revenue_max_vs_friday, revenue_max_date, invoice_median_vs_friday, invoice_min_vs_friday, "
        "invoice_min_date, invoice_max_vs_friday, invoice_max_date "
        "FROM staffing_weekday_summary_public WHERE store_id = ?",
        (store_id,),
    ).fetchall()
    if not summary_rows:
        st.info(f"{store_id} 店目前雲端還沒有星期幾彙整快照，請先在本機執行同步腳本。")
    else:
        order = {wd: i for i, wd in enumerate(WEEKDAY_NAMES)}
        summary_rows = sorted(summary_rows, key=lambda r: order.get(r["weekday"], 99))

        def _fmt_pct(v):
            if v is None:
                return None
            return f"{'+' if v > 0 else ''}{v}%"

        summary_df = pd.DataFrame(
            [
                {
                    "星期": f"星期{r['weekday']}",
                    "天數": r["days"],
                    "營業額中位數(vs星期五)": _fmt_pct(r["revenue_median_vs_friday"]),
                    "營業額最小(vs星期五)": _fmt_pct(r["revenue_min_vs_friday"]),
                    "營業額最小發生日": r["revenue_min_date"],
                    "營業額最大(vs星期五)": _fmt_pct(r["revenue_max_vs_friday"]),
                    "營業額最大發生日": r["revenue_max_date"],
                    "發票張數中位數(vs星期五)": _fmt_pct(r["invoice_median_vs_friday"]),
                }
                for r in summary_rows
            ]
        )
        st.dataframe(summary_df, hide_index=True, use_container_width=True)
        st.caption(
            "所有數字都是「跟星期五中位數比差多少 %」，不是真實金額——星期五本身固定顯示 0%，"
            "當基準。正數代表比星期五多、負數代表比星期五少。最大/最小發生日期是真實日期，"
            "方便回頭查當天是否有連假、天氣、設備故障等特殊事件，但看不出當天實際金額。"
        )


def _cloud_secrets_available() -> bool:
    """雲端部署（Streamlit Cloud）時，帳號設定存在 Secrets 的 AUTH_CONFIG_YAML 這把 key 底下
    （內容就是整份 auth_config.yaml 的文字）；本機測試時沒有這把 key，fallback 讀本機檔案。"""
    try:
        return "AUTH_CONFIG_YAML" in st.secrets
    except Exception:
        return False


def load_auth_config() -> dict:
    if _cloud_secrets_available():
        return yaml.load(st.secrets["AUTH_CONFIG_YAML"], Loader=SafeLoader)
    if not CONFIG_PATH.exists():
        st.error(
            "找不到 config/auth_config.yaml，請先在終端機執行：\n\n"
            "`python scripts/manage_accounts.py add --username ... "
            "--name ... --email ... --role admin`\n\n建立至少一個管理者帳號。"
        )
        st.stop()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=SafeLoader)


auth_config = load_auth_config()
cookie = auth_config["cookie"]
is_cloud = _cloud_secrets_available()

# 本機測試傳檔案路徑字串，套件會自動讀取/寫回整份設定檔（密碼變更等）；
# 雲端部署傳 dict（從 Secrets 解析出來），因為雲端硬碟每次重啟都會恢復原狀，
# 沒辦法真的「寫回檔案」，所以雲端模式不提供自助改密碼，見下面 sidebar 那段。
authenticator = stauth.Authenticate(
    auth_config["credentials"] if is_cloud else str(CONFIG_PATH),
    cookie["name"],
    cookie["key"],
    cookie["expiry_days"],
    auto_hash=False,
)

authenticator.login(location="main")

auth_status = st.session_state.get("authentication_status")

if auth_status is False:
    st.error("帳號或密碼錯誤")
elif auth_status is None:
    st.info("請輸入帳號密碼登入")
elif auth_status:
    name = st.session_state.get("name")
    roles = st.session_state.get("roles") or []

    with st.sidebar:
        st.write(f"歡迎，{name}")
        authenticator.logout(location="sidebar")
        st.divider()
        if is_cloud:
            st.caption("雲端版本不支援自助修改密碼（雲端硬碟重啟後會還原），請聯絡管理者更新帳號設定。")
        else:
            st.caption("修改密碼")
            try:
                username = st.session_state.get("username")
                if authenticator.reset_password(username, location="sidebar"):
                    st.success("密碼已更新，下次登入請用新密碼")
            except Exception as e:
                st.error(str(e))

    if "admin" not in roles:
        st.error("此帳號沒有查看這個雲端網站的權限，請聯絡管理者。")
        st.stop()

    with st.sidebar:
        st.divider()
        page_choice = st.radio("功能選單", ["月盈虧", "排班摘要"])

    if page_choice == "月盈虧":
        st.title("月盈虧")
        render_pnl_page()
    else:
        st.title("排班摘要")
        st.caption("只顯示彙總後的安全版結果，逐日排班明細與員工代碼對照永遠留在本機，不會出現在這裡。")
        render_staffing_summary_page()
