#!/usr/bin/env python3
"""實際排班 vs 建議人力比對：把 raw_staffing_actual 的實際人力，跟
calculate_staffing.calculate_hourly_staffing() 算出的建議人力做逐時段比較。

兩邊顆粒度刻意都做成「整月平均」才對得起來：
    建議人力 = 該時段日均杯數 ÷ 單位產能（無條件進位），本來就是整月平均
    實際人力 = 該時段「有排班資料的天數」中，平均每天有幾個人的班有涵蓋這個時段
              （分母是「有實際排班紀錄的天數」，不是整月天數——資料還沒補齊時
              分母只會算已謄打的天數，不會被沒資料的天數拉低）

用法（注意是 -m 模組執行，不是 python3 scripts/compare_staffing.py：
    這支腳本會 import 同目錄下的 calculate_staffing，直接執行檔案的話
    Python 找不到 scripts 這個套件，會噴 ModuleNotFoundError）：
    source .venv/bin/activate
    python3 -m scripts.compare_staffing
"""
import sqlite3
from pathlib import Path

from scripts.calculate_staffing import calculate_hourly_staffing, get_hourly_data
from scripts.calculate_staffing import load_config as load_staffing_config

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"


def _time_to_minutes(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def get_actual_days_and_intervals(conn, store_id, year_month):
    """回傳（有排班資料的天數清單, [(start_分鐘, end_分鐘), ...]）。"""
    days = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT business_date FROM raw_staffing_actual "
            "WHERE store_id = ? AND substr(business_date, 1, 7) = ?",
            (store_id, year_month),
        ).fetchall()
    ]
    shifts = conn.execute(
        "SELECT start_time, end_time FROM raw_staffing_actual "
        "WHERE store_id = ? AND substr(business_date, 1, 7) = ? "
        "AND start_time IS NOT NULL AND end_time IS NOT NULL",
        (store_id, year_month),
    ).fetchall()
    intervals = [(_time_to_minutes(r["start_time"]), _time_to_minutes(r["end_time"])) for r in shifts]
    return days, intervals


def calculate_actual_hourly_average(conn, store_id, year_month, hour_slots):
    """回傳 {hour_slot: 平均實際人力}；沒有任何排班資料時回傳空 dict。"""
    days, intervals = get_actual_days_and_intervals(conn, store_id, year_month)
    if not days:
        return {}

    result = {}
    for hour_slot in hour_slots:
        hour = int(hour_slot)
        window_start, window_end = hour * 60, (hour + 1) * 60
        count = sum(1 for start, end in intervals if start < window_end and end > window_start)
        result[hour_slot] = round(count / len(days), 2)
    return result


def compare(conn, config, store_id, year_month):
    """回傳逐時段比較結果的 list，每筆含 hour_slot/recommended/actual/diff。
    actual 為 None 代表這個時段完全沒有實際排班資料可以比。
    """
    hourly_data = get_hourly_data(conn, store_id, year_month)
    staffing = calculate_hourly_staffing(hourly_data, config)
    actual_avg = calculate_actual_hourly_average(conn, store_id, year_month, staffing.keys())

    rows = []
    for hour_slot in sorted(staffing.keys()):
        recommended = staffing[hour_slot]["required_front_staff"]
        actual = actual_avg.get(hour_slot)
        diff = None if actual is None else round(actual - recommended, 2)
        rows.append(
            {
                "hour_slot": hour_slot,
                "recommended": recommended,
                "actual": actual,
                "diff": diff,
            }
        )
    return rows


def print_report(store_id, year_month, rows):
    print(f"\n=== {store_id} 店 {year_month} 實際排班 vs 建議人力 ===")
    print(f"{'時段':<6}{'建議人力':>8}{'實際平均人力':>12}{'差異':>8}")
    for row in rows:
        actual_display = "—" if row["actual"] is None else row["actual"]
        diff_display = "—" if row["diff"] is None else row["diff"]
        print(f"{row['hour_slot']:<6}{row['recommended']:>8}{actual_display:>12}{diff_display:>8}")


def main():
    config = load_staffing_config()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    periods = conn.execute(
        "SELECT DISTINCT store_id, substr(business_date, 1, 7) AS year_month "
        "FROM raw_staffing_actual ORDER BY store_id, year_month"
    ).fetchall()
    if not periods:
        raise SystemExit("raw_staffing_actual 沒有資料，請先跑 import_staffing_actual.py")

    for store_id, year_month in periods:
        rows = compare(conn, config, store_id, year_month)
        print_report(store_id, year_month, rows)

    conn.close()


if __name__ == "__main__":
    main()
