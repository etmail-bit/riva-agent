#!/usr/bin/env python3
"""月盈虧計算程式：依 config/cost_rates.json 的費率與 monthly_cost_actuals 的實際數字，
把 Layer 2 資料算成 monthly_pnl。

計算順序（2026-07 與使用者確認，2026-07-10 新增原物料損耗一項）：
    營收 − 原物料 − 原物料損耗（cogs × material_waste_pct，還沒有實際盤點數字，先用概算率）
      − 平台抽成（Ubereats/Foodpanda）− 金流手續費（信用卡/其他電子支付）
      − 人事（底薪 × 1.196，底薪來源是 monthly_cost_actuals.labor_actual 或 config 概算值，
              不管來源是哪個都要乘 1.196 雇主保費負擔率）
      − 房租 − 水電 − 加盟金攤提 − 營業稅（只算「應稅」部分 × 5%，不是全部營收）
      = 稅前淨利
    稅前淨利 × 20% = 預估所得稅（虧損月份不倒扣，最低 0）
    稅前淨利 − 預估所得稅 = 稅後淨利

「實際數字優先，NULL 才 fallback 用 config 概算值」只適用於 monthly_cost_actuals 有提供
輸入欄位的三項：人事底薪、原物料、水電。房租、加盟金攤提沒有實際值輸入機制，一律用 config
（這兩項本來就是固定金額，不像人事/原物料/水電會逐月浮動）。

用法：
    source .venv/bin/activate
    python3 scripts/calculate_pnl.py
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
CONFIG_PATH = ROOT / "config" / "cost_rates.json"


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_fixed_cost(config, store_id, key):
    """回傳某店的固定成本設定值：`fixed_costs_monthly_overrides` 裡有該店該項目就用
    override（例如兩店房租不同），否則 fallback 用 `fixed_costs_monthly` 的共用預設值。"""
    overrides = config.get("fixed_costs_monthly_overrides", {}).get(store_id, {})
    if key in overrides:
        return overrides[key]
    return config["fixed_costs_monthly"][key]


def get_periods(conn):
    """回傳有資料的 (store_id, year_month) 清單，聯集 POS 稽核資料（daily_revenue_validated）
    跟手動輸入備援資料（monthly_revenue_manual），兩邊有任一邊資料就會出現在清單上。"""
    rows = conn.execute(
        """
        SELECT store_id, year_month FROM (
            SELECT store_id, substr(business_date, 1, 7) AS year_month
            FROM daily_revenue_validated
            UNION
            SELECT store_id, year_month FROM monthly_revenue_manual
        )
        GROUP BY store_id, year_month
        ORDER BY store_id, year_month
        """
    ).fetchall()
    return [(r["store_id"], r["year_month"]) for r in rows]


def get_revenue_breakdown(conn, store_id, year_month):
    """回傳 (revenue_dict, source)。POS 稽核過的 daily_revenue_validated 永遠優先；
    查無資料才 fallback 用 monthly_revenue_manual（使用者手動輸入的備援，全額視為應稅）。
    兩邊都沒有資料則回傳全 0、source='none'。"""
    row = conn.execute(
        """
        SELECT SUM(revenue_from_register) AS revenue,
               SUM(taxable_revenue) AS taxable_revenue,
               SUM(ubereats_amount) AS ubereats_amount,
               SUM(foodpanda_amount) AS foodpanda_amount,
               SUM(credit_card_amount) AS credit_card_amount,
               SUM(other_electronic_amount) AS other_electronic_amount
        FROM daily_revenue_validated
        WHERE store_id = ? AND substr(business_date, 1, 7) = ?
        """,
        (store_id, year_month),
    ).fetchone()
    if row["revenue"] is not None:
        return dict(row), "pos"

    manual_row = conn.execute(
        """
        SELECT revenue, ubereats_amount, foodpanda_amount,
               credit_card_amount, other_electronic_amount
        FROM monthly_revenue_manual
        WHERE store_id = ? AND year_month = ?
        """,
        (store_id, year_month),
    ).fetchone()
    if manual_row is not None:
        return {
            "revenue": manual_row["revenue"],
            "taxable_revenue": manual_row["revenue"],
            "ubereats_amount": manual_row["ubereats_amount"],
            "foodpanda_amount": manual_row["foodpanda_amount"],
            "credit_card_amount": manual_row["credit_card_amount"],
            "other_electronic_amount": manual_row["other_electronic_amount"],
        }, "manual"

    return {
        "revenue": 0,
        "taxable_revenue": 0,
        "ubereats_amount": 0,
        "foodpanda_amount": 0,
        "credit_card_amount": 0,
        "other_electronic_amount": 0,
    }, "none"


COST_ACTUAL_COLUMNS = [
    "labor_actual",
    "cogs_actual",
    "utilities_actual",
    "rent_actual",
    "franchise_amortization_actual",
    "ubereats_commission_pct_actual",
    "foodpanda_commission_pct_actual",
    "credit_card_fee_pct_actual",
    "other_electronic_fee_pct_actual",
    "business_tax_pct_actual",
    "corporate_income_tax_pct_actual",
]


def get_cost_actuals(conn, store_id, year_month):
    row = conn.execute(
        f"SELECT {', '.join(COST_ACTUAL_COLUMNS)} FROM monthly_cost_actuals "
        "WHERE store_id = ? AND year_month = ?",
        (store_id, year_month),
    ).fetchone()
    if row is None:
        return {col: None for col in COST_ACTUAL_COLUMNS}
    return dict(row)


def calculate_one(conn, config, store_id, year_month):
    revenue_row, revenue_source = get_revenue_breakdown(conn, store_id, year_month)
    revenue = revenue_row["revenue"] or 0
    taxable_revenue = revenue_row["taxable_revenue"] or 0
    ubereats_amount = revenue_row["ubereats_amount"] or 0
    foodpanda_amount = revenue_row["foodpanda_amount"] or 0
    credit_card_amount = revenue_row["credit_card_amount"] or 0
    other_electronic_amount = revenue_row["other_electronic_amount"] or 0

    actuals = get_cost_actuals(conn, store_id, year_month)
    rates = config["variable_cost_rates"]

    cogs = actuals["cogs_actual"] if actuals["cogs_actual"] is not None else round(revenue * rates["cogs_pct_of_revenue"])
    material_waste = round(cogs * rates.get("material_waste_pct", 0))

    labor_base = (
        actuals["labor_actual"]
        if actuals["labor_actual"] is not None
        else get_fixed_cost(config, store_id, "labor_base")
    )
    labor_cost = round(labor_base * (1 + rates["labor_insurance_overhead_pct"]))

    utilities = (
        actuals["utilities_actual"]
        if actuals["utilities_actual"] is not None
        else get_fixed_cost(config, store_id, "utilities_estimate")
    )

    rent = actuals["rent_actual"] if actuals["rent_actual"] is not None else get_fixed_cost(config, store_id, "rent")
    franchise_amortization = (
        actuals["franchise_amortization_actual"]
        if actuals["franchise_amortization_actual"] is not None
        else get_fixed_cost(config, store_id, "franchise_fee_amortization")
    )

    ubereats_pct = (
        actuals["ubereats_commission_pct_actual"]
        if actuals["ubereats_commission_pct_actual"] is not None
        else rates["platform_commission"]["ubereats"]
    )
    foodpanda_pct = (
        actuals["foodpanda_commission_pct_actual"]
        if actuals["foodpanda_commission_pct_actual"] is not None
        else rates["platform_commission"]["foodpanda"]
    )
    credit_card_pct = (
        actuals["credit_card_fee_pct_actual"]
        if actuals["credit_card_fee_pct_actual"] is not None
        else rates["payment_processing"]["credit_card"]
    )
    other_electronic_pct = (
        actuals["other_electronic_fee_pct_actual"]
        if actuals["other_electronic_fee_pct_actual"] is not None
        else rates["payment_processing"]["other_electronic"]
    )
    business_tax_pct = (
        actuals["business_tax_pct_actual"]
        if actuals["business_tax_pct_actual"] is not None
        else rates["business_tax_pct"]
    )
    corporate_income_tax_pct = (
        actuals["corporate_income_tax_pct_actual"]
        if actuals["corporate_income_tax_pct_actual"] is not None
        else rates["corporate_income_tax_pct"]
    )

    platform_commission = round(ubereats_amount * ubereats_pct + foodpanda_amount * foodpanda_pct)
    payment_processing_fee = round(
        credit_card_amount * credit_card_pct + other_electronic_amount * other_electronic_pct
    )
    business_tax = round(taxable_revenue * business_tax_pct)

    pretax_profit = (
        revenue
        - cogs
        - material_waste
        - platform_commission
        - payment_processing_fee
        - labor_cost
        - rent
        - utilities
        - franchise_amortization
        - business_tax
    )
    income_tax_estimate = max(0, round(pretax_profit * corporate_income_tax_pct))
    net_profit = pretax_profit - income_tax_estimate

    return {
        "revenue": revenue,
        "cogs": cogs,
        "material_waste": material_waste,
        "labor_cost": labor_cost,
        "rent": rent,
        "utilities": utilities,
        "franchise_amortization": franchise_amortization,
        "platform_commission": platform_commission,
        "payment_processing_fee": payment_processing_fee,
        "business_tax": business_tax,
        "pretax_profit": pretax_profit,
        "income_tax_estimate": income_tax_estimate,
        "net_profit": net_profit,
        "revenue_source": revenue_source,
    }


def save_pnl_result(conn, store_id, year_month, result):
    """把 calculate_one() 的結果 upsert 進 monthly_pnl。CLI 版 main() 跟網頁「儲存本月盈虧結果」
    按鈕共用這個函式，避免同一段 SQL 維護兩份。呼叫端自己負責 commit。"""
    conn.execute(
        """
        INSERT INTO monthly_pnl
            (store_id, year_month, revenue, cogs, material_waste, labor_cost, rent, utilities,
             franchise_amortization, platform_commission, payment_processing_fee,
             business_tax, pretax_profit, income_tax_estimate, net_profit, revenue_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(store_id, year_month) DO UPDATE SET
            revenue = excluded.revenue,
            cogs = excluded.cogs,
            material_waste = excluded.material_waste,
            labor_cost = excluded.labor_cost,
            rent = excluded.rent,
            utilities = excluded.utilities,
            franchise_amortization = excluded.franchise_amortization,
            platform_commission = excluded.platform_commission,
            payment_processing_fee = excluded.payment_processing_fee,
            business_tax = excluded.business_tax,
            pretax_profit = excluded.pretax_profit,
            income_tax_estimate = excluded.income_tax_estimate,
            net_profit = excluded.net_profit,
            revenue_source = excluded.revenue_source,
            calculated_at = datetime('now')
        """,
        (
            store_id,
            year_month,
            result["revenue"],
            result["cogs"],
            result["material_waste"],
            result["labor_cost"],
            result["rent"],
            result["utilities"],
            result["franchise_amortization"],
            result["platform_commission"],
            result["payment_processing_fee"],
            result["business_tax"],
            result["pretax_profit"],
            result["income_tax_estimate"],
            result["net_profit"],
            result["revenue_source"],
        ),
    )


def main():
    config = load_config()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    periods = get_periods(conn)
    if not periods:
        raise SystemExit("daily_revenue_validated 沒有資料，請先跑 import_cash_register.py 跟 validate_revenue.py")

    print(f"{'店':<3}{'月份':<9}{'營收':>9}{'稅前淨利':>10}{'稅後淨利':>10}")
    for store_id, year_month in periods:
        result = calculate_one(conn, config, store_id, year_month)
        save_pnl_result(conn, store_id, year_month, result)
        print(
            f"{store_id:<3}{year_month:<9}{result['revenue']:>9}{result['pretax_profit']:>10}"
            f"{result['net_profit']:>10}  ({result['revenue_source']})"
        )

    conn.commit()
    conn.close()
    print(f"\n完成，共計算 {len(periods)} 筆月份寫入 monthly_pnl")


if __name__ == "__main__":
    main()
