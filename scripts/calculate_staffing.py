#!/usr/bin/env python3
"""自動排班建議：依時段占比的「日均杯數」換算每小時建議前場人力，並對照班別時間窗。

核心公式：
    每小時建議前場人力 = ceil(日均杯數 ÷ 單位產能 ＋ 外送人力消耗小時數)
    外送人力消耗小時數 = 該時段日均外送單數 × 每單履約分鐘數 ÷ 60
        （2026-07-10 使用者提醒修正：外送單如果是店家自己送，會抽走一個人力出去外送
        fulfillment_minutes_per_order 分鐘，這段時間他沒辦法顧前場，不是「已經含在日均杯數裡
        不用另外算」——之前這裡的假設是錯的，只有平台叫車外送（外部騎士取貨）才不會佔用店內人力，
        店家自己的外送單（raw_hourly_pattern_monthly 的 delivery_count 欄位，跟 platform_count
        平台單分開存）才需要另外扣人力）。
    raw_hourly_pattern_monthly 的 delivery_count/platform_count 兩欄是「該月累計總數」，
    不是日均值，要除以當月天數才是跟 daily_avg_cups 同一個量級的「日均外送單數」——
    這是之前的既有 bug（這兩欄從來沒被拿來做過容量計算，只當參考欄印出來，所以沒被抓到）。
    煮茶班時段（預設 07:30 起 1 小時）該人力雖然在後場煮茶，但同時還能兼顧前場出杯，
    貢獻 tea_brewing.front_capacity_contribution_cups_per_hour（預設 8）杯/hr 的前場產能
    （2026-07-13 使用者澄清，取代先前「完全不計入前場產能」的假設）——這段時間的
    required_front_staff 公式改成 ceil(max(0, 杯量 - 8) / 產能 + 外送耗時)，另外用
    「+1 煮茶」標註，代表這個人本身仍然是額外配置（有人要顧茶湯），不是 0 產出。

這一版只印報表、不寫進資料庫（排班邏輯還在跟使用者對數字，等校準過一輪再考慮要不要落地成表）。
不做班次人數自動最佳化，只呈現「每個班次時間窗內、逐小時的人力需求」，怎麼配人由使用者自己判斷。

用法：
    source .venv/bin/activate
    python3 scripts/calculate_staffing.py
"""
import calendar
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
CONFIG_PATH = ROOT / "config" / "staffing_rules.json"


def _days_in_month(year_month):
    year, month = map(int, year_month.split("-"))
    return calendar.monthrange(year, month)[1]


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
    days = _days_in_month(year_month)
    result = {}
    for r in rows:
        d = dict(r)
        # delivery_count 存的是當月累計總數，除以當月天數才是跟 daily_avg_cups 同量級的日均值
        d["daily_avg_delivery_count"] = (d["delivery_count"] or 0) / days
        result[r["hour_slot"]] = d
    return result


def is_tea_brewing_hour(hour_slot, config):
    start_hour = int(config["tea_brewing"]["start_time"].split(":")[0])
    duration = config["tea_brewing"]["estimated_duration_hours"]
    hour = int(hour_slot)
    return start_hour <= hour < start_hour + duration


def _time_to_minutes(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def is_shift_active(hour_slot, shift, config):
    """用分鐘級的區間重疊判斷，而不是只看小時整數——班別現在有 17:30/18:30 這種
    半點結束的時間，只截小時的話會把最後半小時所在的那個 hour_slot 漏掉。"""
    hour = int(hour_slot)
    window_start, window_end = hour * 60, (hour + 1) * 60
    start_minutes = _time_to_minutes(shift["start"])
    end_minutes = _time_to_minutes(shift["end"])
    return start_minutes < window_end and end_minutes > window_start


def calculate_delivery_hours(daily_avg_delivery_count, config):
    """該時段外送單消耗掉的人力小時數（店家自己送的單，不含平台叫車外送）。"""
    minutes_per_order = config["delivery"]["fulfillment_minutes_per_order"]
    return daily_avg_delivery_count * minutes_per_order / 60


def required_front_staff_for_hour(cups, delivery_hours, is_tea, config):
    """算這個時段總共要排幾個前場人力（回傳的是總人數，不是「另外還要配幾個」）。

    2026-07-14 使用者澄清修正：「開早」班（見 tea_brewing 設定的時間窗）那個人整段
    班次的前場產能都被壓在 front_capacity_contribution_cups_per_hour（預設8杯/hr），
    不是完整的 capacity.cups_per_staff_per_hour（25杯/hr）——**但只有這一個人被打折，
    這段時間如果還缺人，補進來的人一律用完整產能算**。這個人本身這整段時間都算一個
    班（+1），不是「除了他還要配幾個」這種需要另外心算加總的數字；之前的版本沒加這個
    +1，且舊版的煮茶窗口只設定 1 小時，這次一起修正為完整的開早班時長。

    公式：
        煮茶（開早班）時段：ceil(max(0, 杯量-8) / 25 + 外送耗時) + 1
        一般時段：          ceil(杯量 / 25 + 外送耗時)

    2026-07-14 使用者要求：不要只回傳取整後的整數，取整前的公式值也要能看到（例如
    「ceil(2.01)」而不是直接看到 3，不然不知道差多少就會多/少一個人）——回傳
    {"raw": 取整前的數字, "required": 取整後的建議人力, "formula": 帶入實際數字的算式文字}。
    """
    capacity = config["capacity"]["cups_per_staff_per_hour"]
    if is_tea:
        tea_contribution = config["tea_brewing"].get("front_capacity_contribution_cups_per_hour", 0)
        remaining_cups = max(0, cups - tea_contribution)
        raw = remaining_cups / capacity + delivery_hours
        required = math.ceil(raw) + 1
        formula = f"ceil({remaining_cups:g}/{capacity}+{delivery_hours:.2f})+1"
    elif cups or delivery_hours:
        raw = cups / capacity + delivery_hours
        required = math.ceil(raw)
        formula = f"ceil({cups:g}/{capacity}+{delivery_hours:.2f})"
    else:
        raw, required, formula = 0.0, 0, "0"
    return {"raw": round(raw, 2), "required": required, "formula": formula}


def calculate_hourly_staffing(hourly_data, config):
    result = {}
    for hour_slot, data in hourly_data.items():
        cups = data["daily_avg_cups"] or 0
        is_tea = is_tea_brewing_hour(hour_slot, config)
        delivery_hours = calculate_delivery_hours(data["daily_avg_delivery_count"], config)
        calc = required_front_staff_for_hour(cups, delivery_hours, is_tea, config)
        result[hour_slot] = {
            "cups": cups,
            "delivery_hours": round(delivery_hours, 2),
            "required_front_staff": calc["required"],
            "required_front_staff_raw": calc["raw"],
            "required_front_staff_formula": calc["formula"],
            "tea_brewing": is_tea,
            "delivery_count": round(data["daily_avg_delivery_count"], 2),
        }
    return result


def print_report(store_id, year_month, staffing, config):
    print(f"\n=== {store_id} 店 {year_month} ===")
    print(f"{'時段':<6}{'日均杯數':>8}{'建議前場人力':>12}{'公式':>26}{'煮茶':>6}{'日均外送單':>10}{'外送耗時(hr)':>12}")
    for hour_slot in sorted(staffing.keys()):
        s = staffing[hour_slot]
        tea_flag = "煮茶" if s["tea_brewing"] else ""
        print(
            f"{hour_slot:<6}{s['cups']:>8}{s['required_front_staff']:>12}{s['required_front_staff_formula']:>26}{tea_flag:>6}"
            f"{s['delivery_count']:>10}{s['delivery_hours']:>12}"
        )
    print("（「公式」欄是取整前的完整算式，例如 ceil(2.01) 才會知道差多少就多/少一個人，不是只看最後的整數）")
    print("（「煮茶」欄有標記的時段＝開早班時間窗，這個人整段班次前場產能只算8杯/hr（其他人一律25杯/hr），"
          "「建議前場人力」已經是總人數，含這個人在內，不用再另外+1）")
    print("（「建議前場人力」已經把外送耗時併進需求裡：ceil(杯數/產能 + 外送耗時小時數)，煮茶時段另外+1固定人頭）")

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

    # 2026-07-10 更新：外送單（店家自己送）會抽走一個人力出去外送，已經併進上面的
    # required_front_staff 公式（calculate_delivery_hours），不再是只當參考的欄位。


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
