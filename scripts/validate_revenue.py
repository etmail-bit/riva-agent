#!/usr/bin/env python3
"""收銀機明細 vs 營收月報 跨來源比對。

設計邏輯：
1. 收銀機明細顆粒度到「日」，是月盈虧實際採用的營收數字來源
   -> 依 store_id + business_date 聚合，灌進 daily_revenue_validated（Layer 2）。
2. 營收月報只有「月」顆粒度，沒辦法拆回每天，只拿來做月加總的交叉稽核，
   金額對不上要明確印出警告，不能默默選一邊當正確答案。

用法：
    source .venv/bin/activate
    python3 scripts/validate_revenue.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"

# 月加總差異超過這個比例就印警告（金額很小的月份改用絕對值門檻，避免除以小分母誤報）
PCT_THRESHOLD = 0.01
ABS_THRESHOLD = 500


def populate_daily_revenue_validated(conn):
    rows = conn.execute(
        """
        SELECT store_id, business_date,
               SUM(gross_revenue) AS revenue_from_register,
               SUM(ubereats_amount) AS ubereats_amount,
               SUM(foodpanda_amount) AS foodpanda_amount,
               SUM(credit_card_amount) AS credit_card_amount,
               SUM(other_electronic_amount) AS other_electronic_amount,
               SUM(taxable_revenue) AS taxable_revenue
        FROM raw_cash_register_daily
        GROUP BY store_id, business_date
        """
    ).fetchall()

    for row in rows:
        conn.execute(
            """
            INSERT INTO daily_revenue_validated
                (store_id, business_date, revenue_from_register,
                 ubereats_amount, foodpanda_amount, credit_card_amount, other_electronic_amount,
                 taxable_revenue)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, business_date) DO UPDATE SET
                revenue_from_register = excluded.revenue_from_register,
                ubereats_amount = excluded.ubereats_amount,
                foodpanda_amount = excluded.foodpanda_amount,
                credit_card_amount = excluded.credit_card_amount,
                other_electronic_amount = excluded.other_electronic_amount,
                taxable_revenue = excluded.taxable_revenue
            """,
            row,
        )
    return len(rows)


def reconcile_monthly(conn):
    register_totals = {
        (r["store_id"], r["year_month"]): r["total"]
        for r in conn.execute(
            """
            SELECT store_id, substr(business_date, 1, 7) AS year_month,
                   SUM(revenue_from_register) AS total
            FROM daily_revenue_validated
            GROUP BY store_id, year_month
            """
        ).fetchall()
    }
    report_totals = {
        (r["store_id"], r["year_month"]): r["total"]
        for r in conn.execute(
            """
            SELECT store_id, year_month, SUM(amount) AS total
            FROM raw_revenue_monthly
            GROUP BY store_id, year_month
            """
        ).fetchall()
    }

    all_keys = sorted(set(register_totals) | set(report_totals))
    print(f"{'店':<3}{'月份':<9}{'收銀機加總':>10}{'營收月報':>10}{'差異':>10}{'差異%':>8}  結果")
    warnings = []
    for store_id, year_month in all_keys:
        register_total = register_totals.get((store_id, year_month))
        report_total = report_totals.get((store_id, year_month))
        if register_total is None or report_total is None:
            print(f"{store_id:<3}{year_month:<9}{'—':>10}{'—':>10}  兩邊資料不同時存在，暫時無法比對")
            continue
        diff = register_total - report_total
        if report_total:
            pct = abs(diff) / report_total
            flag = "WARNING" if (abs(diff) > ABS_THRESHOLD and pct > PCT_THRESHOLD) else "OK"
        else:
            # 營收月報那個月加總是 0：只要收銀機那邊有金額，不管門檻，一律示警（不能用除以零的假 0% 蓋過去）
            pct = 1.0 if diff else 0.0
            flag = "WARNING" if diff else "OK"
        if flag == "WARNING":
            warnings.append((store_id, year_month, diff, pct))
        print(
            f"{store_id:<3}{year_month:<9}{register_total:>10}{report_total:>10}{diff:>10}{pct*100:>7.2f}%  {flag}"
        )
    return warnings


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    count = populate_daily_revenue_validated(conn)
    conn.commit()
    print(f"daily_revenue_validated 已更新 {count} 筆（每日收銀機明細加總）\n")

    warnings = reconcile_monthly(conn)
    conn.close()

    if warnings:
        print(f"\n共 {len(warnings)} 個月份差異超過門檻（絕對值 > {ABS_THRESHOLD} 且比例 > {PCT_THRESHOLD*100:.0f}%），請人工檢查：")
        for store_id, year_month, diff, pct in warnings:
            print(f"  - {store_id} {year_month}：差 {diff}（{pct*100:.2f}%）")
    else:
        print("\n所有月份差異都在門檻內，沒有需要人工檢查的項目。")


if __name__ == "__main__":
    main()
