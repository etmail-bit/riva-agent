#!/usr/bin/env python3
"""自動排班建議：依時段占比的「日均杯數」換算每小時建議前場人力，並對照班別時間窗。

核心公式：
    每小時建議前場人力 = ceil(日均杯數 ÷ 單位產能)
    外送單已經算在日均杯數裡（一杯就是一杯，不分內用外送），不會重複疊加人力。
    煮茶班時段（預設 07:30 起 1 小時）該人力在後場煮茶，不計入前場產能，
    另外用「+1 煮茶」標註，煮完後併入前場支援。

這一版只印報表、不寫進資料庫（排班邏輯還在跟使用者對數字，等校準過一輪再考慮要不要落地成表）。
不做班次人數自動最佳化，只呈現「每個班次時間窗內、逐小時的人力需求」，怎麼配人由使用者自己判斷。

用法：
    source .venv/bin/activate
    python3 scripts/calculate_staffing.py
"""
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
CONFIG_PATH = ROOT / "config" / "staffing_rules.json"


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_periods(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id, year_month FROM raw_hourly_pattern_monthly ORDER BY store_id, year_month"
    ).fetchall()
    return [(r["store_id"], r["year_month"]) for r in rows]


def get_hourly_data(conn, store_id, year_month):
    rows = conn.execute(
        """
        SELECT hour_slot, daily_avg_cups, delivery_count
        FROM raw_hourly_pattern_monthly
        WHERE store_id = ? AND year_month = ?
        ORDER BY hour_slot
        """,
        (store_id, year_month),
    ).fetchall()
    return {r["hour_slot"]: dict(r) for r in rows}


def is_tea_brewing_hour(hour_slot, config):
    start_hour = int(config["tea_brewing"]["start_time"].split(":")[0])
    duration = config["tea_brewing"]["estimated_duration_hours"]
    hour = int(hour_slot)
    return start_hour <= hour < start_hour + duration


def is_shift_active(hour_slot, shift, config):
    hour = int(hour_slot)
    start_hour = int(shift["start"].split(":")[0])
    end_hour = int(shift["end"].split(":")[0])
    return start_hour <= hour < end_hour


def calculate_hourly_staffing(hourly_data, config):
    capacity = config["capacity"]["cups_per_staff_per_hour"]
    result = {}
    for hour_slot, data in hourly_data.items():
        cups = data["daily_avg_cups"] or 0
        required = math.ceil(cups / capacity) if cups else 0
        result[hour_slot] = {
            "cups": cups,
            "required_front_staff": required,
            "tea_brewing": is_tea_brewing_hour(hour_slot, config),
            "delivery_count": data["delivery_count"] or 0,
        }
    return result


def print_report(store_id, year_month, staffing, config):
    print(f"\n=== {store_id} 店 {year_month} ===")
    print(f"{'時段':<6}{'日均杯數':>8}{'建議前場人力':>12}{'煮茶':>6}{'外送單':>6}")
    for hour_slot in sorted(staffing.keys()):
        s = staffing[hour_slot]
        tea_flag = "煮茶" if s["tea_brewing"] else ""
        print(
            f"{hour_slot:<6}{s['cups']:>8}{s['required_front_staff']:>12}{tea_flag:>6}{s['delivery_count']:>6}"
        )
    print("（「煮茶」欄有標記的時段，除了前場人力，後場還要另外 +1 人煮茶，此人力不計入前場產能）")

    print(f"\n班別對照（單位產能 {config['capacity']['cups_per_staff_per_hour']} 杯/人/hr）：")
    for shift in config["shifts"]:
        active_hours = [h for h in staffing if is_shift_active(h, shift, config)]
        if not active_hours:
            continue
        peak = max(staffing[h]["required_front_staff"] for h in active_hours)
        avg = sum(staffing[h]["required_front_staff"] for h in active_hours) / len(active_hours)
        print(
            f"  {shift['name']}（{shift['start']}~{shift['end']}）："
            f"時段內尖峰需求 {peak} 人，平均需求 {avg:.1f} 人"
        )

    # 外送單量已經包含在「日均杯數」裡（一杯就是一杯，不分內用外送），只要前場人力配到位，
    # 出杯速度自然在 SLA 內，不需要另外疊加一個獨立的外送壓力公式——之前版本試過會對幾乎
    # 每個時段誤報，是校準錯誤，拿掉了。外送單量欄位保留在表格裡純供參考。


def main():
    config = load_config()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    periods = get_periods(conn)
    if not periods:
        raise SystemExit("raw_hourly_pattern_monthly 沒有資料，請先跑 import_hourly_pattern.py")

    for store_id, year_month in periods:
        hourly_data = get_hourly_data(conn, store_id, year_month)
        staffing = calculate_hourly_staffing(hourly_data, config)
        print_report(store_id, year_month, staffing, config)

    conn.close()


if __name__ == "__main__":
    main()
