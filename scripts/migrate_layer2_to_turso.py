#!/usr/bin/env python3
"""一次性搬遷腳本：把本機真實資料（daily_revenue_validated／monthly_cost_actuals／
monthly_pnl／store_staffing_insights 的公開安全摘要）灌進 Turso 雲端資料庫，讓
app_pnl.py 雲端版真正有資料可用（之前 Turso 上只有 stores 兩筆代號的空殼資料庫）。

刻意不搬 Layer 1 原始報表（收銀機/發票/銷售明細）與排班相關表格——永遠只留在本機，
見 db/schema_cloud.sql 的說明。monthly_pnl（Layer 3 正式紀錄）是 2026-07-09 跟使用者
確認後加進來的，讓雲端「歷史走勢圖」／「彙整建議」不用逐月手動按「儲存本月盈虧結果」
才有資料。

store_staffing_insights 只同步 public_summary_text 這一欄（2026-07-09 使用者確認的
公開範圍：只放「預估可節省金額」），完整版（含真實人事成本／固定薪資數字）留在本機
summary_text 欄位，不會被這支腳本讀取或同步——見 migrate_staffing_insights()。
store_operational_insights（通路組合／客單價，含真實客單價金額）2026-07-09 使用者
決定不公開，這支腳本完全不處理那張表。2026-07-09 曾經誤把兩張表的完整版都同步
上去過，發現含真實金額後已經從 Turso 刪除，這裡改成只送安全版，別再犯同樣的錯。

冪等：全部用 INSERT ... ON CONFLICT DO UPDATE，可重複執行不會產生重複資料，
本機資料異動後（例如訂正某天營收、重跑 calculate_pnl.py）重跑這支腳本即可同步最新
狀態到雲端。

用法：
    source .venv/bin/activate
    python3 -m scripts.migrate_layer2_to_turso
"""
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from scripts.turso_client import TursoConnection

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
SCHEMA_CLOUD_PATH = ROOT / "db" / "schema_cloud.sql"

load_dotenv(ROOT / ".env")


def _local_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(cloud):
    cloud.executescript(SCHEMA_CLOUD_PATH.read_text(encoding="utf-8"))


def migrate_daily_revenue_validated(local, cloud):
    rows = local.execute("SELECT * FROM daily_revenue_validated").fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO daily_revenue_validated
                (store_id, business_date, revenue_from_register, revenue_from_monthly_report,
                 discrepancy, ubereats_amount, foodpanda_amount, credit_card_amount,
                 other_electronic_amount, taxable_revenue)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, business_date) DO UPDATE SET
                revenue_from_register = excluded.revenue_from_register,
                revenue_from_monthly_report = excluded.revenue_from_monthly_report,
                discrepancy = excluded.discrepancy,
                ubereats_amount = excluded.ubereats_amount,
                foodpanda_amount = excluded.foodpanda_amount,
                credit_card_amount = excluded.credit_card_amount,
                other_electronic_amount = excluded.other_electronic_amount,
                taxable_revenue = excluded.taxable_revenue
            """,
            (
                r["store_id"], r["business_date"], r["revenue_from_register"],
                r["revenue_from_monthly_report"], r["discrepancy"], r["ubereats_amount"],
                r["foodpanda_amount"], r["credit_card_amount"], r["other_electronic_amount"],
                r["taxable_revenue"],
            ),
        )
    return len(rows)


def migrate_monthly_cost_actuals(local, cloud):
    rows = local.execute("SELECT * FROM monthly_cost_actuals").fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO monthly_cost_actuals
                (store_id, year_month, labor_actual, cogs_actual, utilities_actual, rent_actual,
                 franchise_amortization_actual, ubereats_commission_pct_actual,
                 foodpanda_commission_pct_actual, credit_card_fee_pct_actual,
                 other_electronic_fee_pct_actual, business_tax_pct_actual,
                 corporate_income_tax_pct_actual, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                corporate_income_tax_pct_actual = excluded.corporate_income_tax_pct_actual,
                notes = excluded.notes
            """,
            (
                r["store_id"], r["year_month"], r["labor_actual"], r["cogs_actual"],
                r["utilities_actual"], r["rent_actual"], r["franchise_amortization_actual"],
                r["ubereats_commission_pct_actual"], r["foodpanda_commission_pct_actual"],
                r["credit_card_fee_pct_actual"], r["other_electronic_fee_pct_actual"],
                r["business_tax_pct_actual"], r["corporate_income_tax_pct_actual"], r["notes"],
            ),
        )
    return len(rows)


def migrate_monthly_pnl(local, cloud):
    rows = local.execute("SELECT * FROM monthly_pnl").fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO monthly_pnl
                (store_id, year_month, revenue, cogs, labor_cost, rent, utilities,
                 franchise_amortization, platform_commission, payment_processing_fee,
                 business_tax, pretax_profit, income_tax_estimate, net_profit,
                 revenue_source, calculated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, year_month) DO UPDATE SET
                revenue = excluded.revenue,
                cogs = excluded.cogs,
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
                calculated_at = excluded.calculated_at
            """,
            (
                r["store_id"], r["year_month"], r["revenue"], r["cogs"], r["labor_cost"],
                r["rent"], r["utilities"], r["franchise_amortization"], r["platform_commission"],
                r["payment_processing_fee"], r["business_tax"], r["pretax_profit"],
                r["income_tax_estimate"], r["net_profit"], r["revenue_source"], r["calculated_at"],
            ),
        )
    return len(rows)


def migrate_staffing_insights(local, cloud):
    """只同步 store_staffing_insights 的 public_summary_text 欄位（2026-07-09 使用者
    確認的公開範圍：只放「預估可節省金額」）。summary_text 那個含真實人事成本／固定
    薪資的完整版**刻意不查、不送**，本機以外的地方看不到那些數字。

    store_operational_insights（通路組合／客單價，含真實客單價金額與營收佔比）
    2026-07-09 使用者決定不公開，這支腳本完全不處理那張表，只留在本機給 app.py 用。
    """
    exists = local.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='store_staffing_insights'"
    ).fetchone()
    if exists is None:
        return 0
    rows = local.execute(
        "SELECT store_id, public_summary_text, generated_at FROM store_staffing_insights"
    ).fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO store_staffing_insights (store_id, summary_text, generated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(store_id) DO UPDATE SET
                summary_text = excluded.summary_text,
                generated_at = excluded.generated_at
            """,
            (r["store_id"], r["public_summary_text"], r["generated_at"]),
        )
    return len(rows)


def main():
    local = _local_conn()
    cloud = TursoConnection()
    ensure_schema(cloud)

    n1 = migrate_daily_revenue_validated(local, cloud)
    print(f"daily_revenue_validated: {n1} 筆已同步到 Turso")
    n2 = migrate_monthly_cost_actuals(local, cloud)
    print(f"monthly_cost_actuals: {n2} 筆已同步到 Turso")
    n3 = migrate_monthly_pnl(local, cloud)
    print(f"monthly_pnl: {n3} 筆已同步到 Turso")
    n4 = migrate_staffing_insights(local, cloud)
    print(f"store_staffing_insights（僅公開安全版摘要）: {n4} 筆已同步到 Turso")


if __name__ == "__main__":
    main()
