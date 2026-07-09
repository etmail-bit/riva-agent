"""Streamlit 網頁入口（雲端部署版）：只有月盈虧功能。

跟本機用的 app.py 是分開的獨立入口：
  - app.py    本機跑，含月盈虧＋排班建議（排班較機密，不上雲）
  - app_pnl.py 雲端部署用，只 import 月盈虧相關程式，排班程式碼完全沒被引用

資料庫改用 Turso（雲端，透過 scripts/turso_client.py 的 HTTP 翻譯層），
不是本機的 db/riva_agent.db。

帳號設定檔是 config/auth_config.yaml（不進版控），一律用
scripts/manage_accounts.py 管理帳號，不要手動編輯。
"""
import copy
import json
import os
import re
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from dotenv import load_dotenv
from yaml.loader import SafeLoader

from scripts.calculate_pnl import COST_ACTUAL_COLUMNS, calculate_one, get_revenue_breakdown, save_pnl_result
from scripts.calculate_pnl import load_config as load_pnl_config
from scripts.turso_client import TursoConnection

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "auth_config.yaml"
COST_CONFIG_PATH = ROOT / "config" / "cost_rates.json"

load_dotenv(ROOT / ".env")  # 本機測試用；部署到 Streamlit Cloud 後改吃 st.secrets

CHART_COLOR_REVENUE = "#2a78d6"
CHART_COLOR_NET_PROFIT = "#1baf7a"

st.set_page_config(page_title="飲料店月盈虧", page_icon="🧋")


def _get_secret(key: str) -> str:
    """Streamlit Cloud 部署後優先讀 st.secrets；本機測試時 fallback 讀 .env（os.environ）。"""
    try:
        return st.secrets[key]
    except Exception:
        return os.environ[key]


@st.cache_resource
def get_db_connection() -> TursoConnection:
    return TursoConnection(
        url=_get_secret("TURSO_DATABASE_URL"),
        token=_get_secret("TURSO_AUTH_TOKEN"),
    )


def render_manual_revenue_section(conn: TursoConnection, store_id: str) -> None:
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


def render_pnl_page() -> None:
    conn = get_db_connection()
    config = load_pnl_config()

    stores = [
        r["store_id"]
        for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()
    ]
    store_id = st.selectbox("店別", stores, format_func=lambda s: f"{s} 店")

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
    fixed = config["fixed_costs_monthly"]
    rates = config["variable_cost_rates"]

    with st.expander("固定成本", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            labor_base = st.number_input(
                "人事底薪（元/月）", min_value=0, step=1000,
                value=int(actuals["labor_actual"] if actuals["labor_actual"] is not None else fixed["labor_base"]),
                key=k("labor_base"),
            )
            rent = st.number_input(
                "房租（元/月）", min_value=0, step=1000,
                value=int(actuals["rent_actual"] if actuals["rent_actual"] is not None else fixed["rent"]),
                key=k("rent"),
            )
        with col2:
            utilities = st.number_input(
                "水電概算（元/月）", min_value=0, step=500,
                value=int(
                    actuals["utilities_actual"] if actuals["utilities_actual"] is not None else fixed["utilities_estimate"]
                ),
                key=k("utilities"),
            )
            franchise_amortization = st.number_input(
                "加盟金攤提（元/月）", min_value=0, step=1000,
                value=int(
                    actuals["franchise_amortization_actual"]
                    if actuals["franchise_amortization_actual"] is not None
                    else fixed["franchise_fee_amortization"]
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
    working_config["fixed_costs_monthly"]["labor_base"] = labor_base
    working_config["fixed_costs_monthly"]["rent"] = rent
    working_config["fixed_costs_monthly"]["utilities_estimate"] = utilities
    working_config["fixed_costs_monthly"]["franchise_fee_amortization"] = franchise_amortization
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
            config["fixed_costs_monthly"]["labor_base"] = labor_base
            config["fixed_costs_monthly"]["rent"] = rent
            config["fixed_costs_monthly"]["utilities_estimate"] = utilities
            config["fixed_costs_monthly"]["franchise_fee_amortization"] = franchise_amortization
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

    history_df = pd.DataFrame(
        [(r["year_month"], r["revenue"], r["net_profit"]) for r in history],
        columns=["year_month", "營收", "稅後淨利"],
    )
    chart_df = history_df.melt("year_month", var_name="項目", value_name="金額")
    color_scale = alt.Color(
        "項目:N",
        scale=alt.Scale(
            domain=["營收", "稅後淨利"],
            range=[CHART_COLOR_REVENUE, CHART_COLOR_NET_PROFIT],
        ),
        legend=alt.Legend(title=None),
    )
    line = (
        alt.Chart(chart_df)
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=alt.X("year_month:N", title="月份"),
            y=alt.Y("金額:Q", title="金額（TWD）"),
            color=color_scale,
            tooltip=["year_month", "項目", "金額"],
        )
    )
    revenue_labels = (
        alt.Chart(chart_df[chart_df["項目"] == "營收"])
        .mark_text(dy=-12, fontSize=11)
        .encode(
            x=alt.X("year_month:N"),
            y=alt.Y("金額:Q"),
            text=alt.Text("金額:Q", format=","),
            color=color_scale,
        )
    )
    net_profit_labels = (
        alt.Chart(chart_df[chart_df["項目"] == "稅後淨利"])
        .mark_text(dy=12, fontSize=11)
        .encode(
            x=alt.X("year_month:N"),
            y=alt.Y("金額:Q"),
            text=alt.Text("金額:Q", format=","),
            color=color_scale,
        )
    )
    chart = alt.layer(line, revenue_labels, net_profit_labels).properties(height=300)
    st.altair_chart(chart, use_container_width=True)


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
        st.error("此帳號沒有查看月盈虧的權限，請聯絡管理者。")
        st.stop()

    st.title("月盈虧")
    render_pnl_page()
