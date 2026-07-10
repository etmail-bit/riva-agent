#!/usr/bin/env python3
"""一次性搬遷腳本：把本機真實資料（daily_revenue_validated／monthly_cost_actuals／
monthly_pnl／store_staffing_insights 的公開安全摘要／排班彙總結果）灌進 Turso 雲端
資料庫，讓 app_pnl.py 雲端版真正有資料可用（之前 Turso 上只有 stores 兩筆代號的空殼
資料庫）。

刻意不搬 Layer 1 原始報表（收銀機/發票/銷售明細）與 raw_staffing_actual（逐日逐員工
排班原始表）——這些永遠只留在本機，見 db/schema_cloud.sql 的說明。monthly_pnl
（Layer 3 正式紀錄）是 2026-07-09 跟使用者確認後加進來的，讓雲端「歷史走勢圖」／
「彙整建議」不用逐月手動按「儲存本月盈虧結果」才有資料。

store_staffing_insights 只同步 public_summary_text 這一欄（2026-07-09 使用者確認的
公開範圍：只放「預估可節省金額」），完整版（含真實人事成本／固定薪資數字）留在本機
summary_text 欄位，不會被這支腳本讀取或同步——見 migrate_staffing_insights()。
store_operational_insights（通路組合／客單價，含真實客單價金額）2026-07-09 使用者
決定不公開，這支腳本完全不處理那張表。2026-07-09 曾經誤把兩張表的完整版都同步
上去過，發現含真實金額後已經從 Turso 刪除，這裡改成只送安全版，別再犯同樣的錯。

2026-07-10 排班摘要上雲：raw_hourly_pattern_monthly／raw_hourly_pattern_daily 是
月/日彙總資料、本來就不含員工代碼，整表同步安全。但「實際 vs 建議人力比對」跟
「正職/兼職眾數」都需要查 raw_staffing_actual（逐日逐員工）才能算，且後者還需要
config 的 employee_roles（真實角色對照）——這兩個一律在本機把 compare()／
roster_mode_by_weekday() 跑完，只把彙總後、不含 employee_code／business_date 的
結果存進 staffing_hourly_comparison／staffing_roster_mode 兩張雲端專用表，員工代碼
跟 employee_roles/wages 本身永遠不會離開這台機器。見 migrate_staffing_comparison()／
migrate_roster_mode()。

冪等：全部用 INSERT ... ON CONFLICT DO UPDATE，可重複執行不會產生重複資料，
本機資料異動後（例如訂正某天營收、重跑 calculate_pnl.py、匯入新一批排班資料）重跑
這支腳本即可同步最新狀態到雲端。

用法：
    source .venv/bin/activate
    python3 -m scripts.migrate_layer2_to_turso
"""
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from scripts.analyze_staffing_daytype import WEEKDAY_NAMES, roster_mode_by_weekday
from scripts.calculate_staffing import load_config as load_staffing_config
from scripts.compare_staffing import compare as compare_staffing
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
                (store_id, year_month, revenue, cogs, material_waste, labor_cost, rent, utilities,
                 franchise_amortization, platform_commission, payment_processing_fee,
                 business_tax, pretax_profit, income_tax_estimate, net_profit,
                 revenue_source, calculated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                calculated_at = excluded.calculated_at
            """,
            (
                r["store_id"], r["year_month"], r["revenue"], r["cogs"], r["material_waste"],
                r["labor_cost"], r["rent"], r["utilities"], r["franchise_amortization"],
                r["platform_commission"], r["payment_processing_fee"], r["business_tax"],
                r["pretax_profit"], r["income_tax_estimate"], r["net_profit"],
                r["revenue_source"], r["calculated_at"],
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


def migrate_hourly_pattern_monthly(local, cloud):
    """月彙總的逐時段杯數/外送單數，不含員工資料，整表同步安全。"""
    rows = local.execute(
        "SELECT store_id, year_month, hour_slot, daily_avg_cups, delivery_count FROM raw_hourly_pattern_monthly"
    ).fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO raw_hourly_pattern_monthly (store_id, year_month, hour_slot, daily_avg_cups, delivery_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(store_id, year_month, hour_slot) DO UPDATE SET
                daily_avg_cups = excluded.daily_avg_cups,
                delivery_count = excluded.delivery_count
            """,
            (r["store_id"], r["year_month"], r["hour_slot"], r["daily_avg_cups"], r["delivery_count"]),
        )
    return len(rows)


def migrate_hourly_pattern_daily(local, cloud):
    """單日的逐時段杯數樣本，不含員工資料，整表同步安全。"""
    rows = local.execute(
        "SELECT store_id, business_date, hour_slot, cups FROM raw_hourly_pattern_daily"
    ).fetchall()
    for r in rows:
        cloud.execute(
            """
            INSERT INTO raw_hourly_pattern_daily (store_id, business_date, hour_slot, cups)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(store_id, business_date, hour_slot) DO UPDATE SET cups = excluded.cups
            """,
            (r["store_id"], r["business_date"], r["hour_slot"], r["cups"]),
        )
    return len(rows)


def migrate_staffing_comparison(local, cloud, staffing_config):
    """對每個有 raw_staffing_actual 資料的 store_id/year_month，在本機把
    compare_staffing.compare() 跑完，只把彙總後的 {hour_slot, recommended, actual, diff}
    同步上雲，raw_staffing_actual 本身（逐日逐員工明細）不會被查出來的欄位以外的內容碰到。"""
    periods = local.execute(
        "SELECT DISTINCT store_id, substr(business_date, 1, 7) AS year_month "
        "FROM raw_staffing_actual ORDER BY store_id, year_month"
    ).fetchall()
    n = 0
    for p in periods:
        rows = compare_staffing(local, staffing_config, p["store_id"], p["year_month"])
        for row in rows:
            if row["actual"] is None:
                continue
            cloud.execute(
                """
                INSERT INTO staffing_hourly_comparison (store_id, year_month, hour_slot, recommended, actual, diff)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id, year_month, hour_slot) DO UPDATE SET
                    recommended = excluded.recommended,
                    actual = excluded.actual,
                    diff = excluded.diff
                """,
                (p["store_id"], p["year_month"], row["hour_slot"], row["recommended"], row["actual"], row["diff"]),
            )
            n += 1
    return n


def migrate_roster_mode(local, cloud, staffing_config):
    """對每個店，在本機把 analyze_staffing_daytype.roster_mode_by_weekday() 跑完
    （這一步需要 config["employee_roles"]，全程只在本機記憶體處理），只把彙總後、
    不含 employee_code、不含 business_date 的「星期幾 x 時段」正職/兼職人數同步上雲。"""
    store_ids = [r["store_id"] for r in local.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()]
    n = 0
    for store_id in store_ids:
        date_range = local.execute(
            "SELECT MIN(business_date), MAX(business_date) FROM raw_staffing_actual WHERE store_id = ?",
            (store_id,),
        ).fetchone()
        if date_range[0] is None:
            continue
        roster = roster_mode_by_weekday(local, store_id, date_range[0], date_range[1], staffing_config)
        for row in roster:
            hour_slot = row["時段"].split(":")[0]  # "07:00" -> "07"，跟 HOUR_SLOTS 的裸小時格式一致
            for wd in WEEKDAY_NAMES:
                ft, pt = row.get(f"星期{wd}_正職"), row.get(f"星期{wd}_兼職")
                if ft is None:
                    continue
                cloud.execute(
                    """
                    INSERT INTO staffing_roster_mode
                        (store_id, hour_slot, weekday, full_time_count, part_time_count, consistency_ratio)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_id, hour_slot, weekday) DO UPDATE SET
                        full_time_count = excluded.full_time_count,
                        part_time_count = excluded.part_time_count,
                        consistency_ratio = excluded.consistency_ratio,
                        generated_at = datetime('now')
                    """,
                    (store_id, hour_slot, wd, ft, pt, row.get(f"星期{wd}_一致比例")),
                )
                n += 1
    return n


def main():
    local = _local_conn()
    cloud = TursoConnection()
    ensure_schema(cloud)
    staffing_config = load_staffing_config()

    n1 = migrate_daily_revenue_validated(local, cloud)
    print(f"daily_revenue_validated: {n1} 筆已同步到 Turso")
    n2 = migrate_monthly_cost_actuals(local, cloud)
    print(f"monthly_cost_actuals: {n2} 筆已同步到 Turso")
    n3 = migrate_monthly_pnl(local, cloud)
    print(f"monthly_pnl: {n3} 筆已同步到 Turso")
    n4 = migrate_staffing_insights(local, cloud)
    print(f"store_staffing_insights（僅公開安全版摘要）: {n4} 筆已同步到 Turso")
    n5 = migrate_hourly_pattern_monthly(local, cloud)
    print(f"raw_hourly_pattern_monthly: {n5} 筆已同步到 Turso")
    n6 = migrate_hourly_pattern_daily(local, cloud)
    print(f"raw_hourly_pattern_daily: {n6} 筆已同步到 Turso")
    n7 = migrate_staffing_comparison(local, cloud, staffing_config)
    print(f"staffing_hourly_comparison（彙總快照）: {n7} 筆已同步到 Turso")
    n8 = migrate_roster_mode(local, cloud, staffing_config)
    print(f"staffing_roster_mode（彙總快照）: {n8} 筆已同步到 Turso")


if __name__ == "__main__":
    main()
