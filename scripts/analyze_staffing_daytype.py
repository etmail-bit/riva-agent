#!/usr/bin/env python3
"""排班平日/假日分析：2026-07-10 完成的分析（見 PROGRESS.md 對應章節），這支腳本把當時
臨時寫的分析整理成正式、可重複執行的模組，供 CLI 與 app.py 排班頁共用。

兩個角度：
1. cup_stats_by_daytype()：平日/假日逐時段杯數（最大/最小/平均）。真正的假日杯數來自
   raw_hourly_pattern_daily（使用者額外提供的星期六/日單日樣本，見
   scripts/import_hourly_pattern_daily.py），平日杯數則用代數關係從
   raw_hourly_pattern_monthly（月彙總，真實 POS 資料）反推：
       月彙總 × 當月天數 = 平日杯數 × 平日天數 + 假日杯數 × 假日天數
   兩邊都是真實資料撐出來的，不是估計值（跟同一天稍早用發票交易筆數比例反推的版本不同，
   那版嚴重低估了假日需求，已經被這個版本取代）。

2. roster_mode_by_weekday()：星期幾 x 時段的實際排班人力（正職/兼職），用「眾數」代表
   （70 天樣本中最常出現的組合），比整月單一平均更貼近排班決策的顆粒度。

只吃 A 店的資料——B 店目前沒有 raw_hourly_pattern_daily 樣本，也沒有 raw_staffing_actual
排班原始資料，兩個函式在資料不足時會回傳空結果，呼叫端（CLI 或 app.py）自行處理提示訊息。

用法：
    source .venv/bin/activate
    python3 -m scripts.analyze_staffing_daytype
"""
import calendar
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
CONFIG_PATH = ROOT / "config" / "staffing_rules.json"
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
HOUR_SLOTS = [f"{h:02d}" for h in range(7, 22)]


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _to_minutes(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def cup_stats_by_daytype(conn, store_id, months) -> dict:
    """回傳 {"平日": [...], "假日": [...]}，每筆是 {時段, 月數, 平均杯數, 最大值, 最小值}。
    只會用 raw_hourly_pattern_daily 裡有真實假日樣本、且 raw_hourly_pattern_monthly 也有
    對應月份彙總資料的月份才能反推，兩邊都沒有資料時該月直接跳過（不會用估計值頂替）。"""
    daily_rows = conn.execute(
        "SELECT business_date, hour_slot, cups FROM raw_hourly_pattern_daily WHERE store_id = ?",
        (store_id,),
    ).fetchall()
    if not daily_rows:
        return {"平日": [], "假日": []}

    by_month_hour = defaultdict(list)  # (year_month, hour_slot) -> [(business_date, cups), ...]
    for r in daily_rows:
        year_month = r["business_date"][:7]
        if year_month not in months:
            continue
        by_month_hour[(year_month, r["hour_slot"])].append((r["business_date"], r["cups"] or 0))

    per_hour = {"平日": defaultdict(list), "假日": defaultdict(list)}
    months_with_samples = sorted({ym for ym, _ in by_month_hour})

    for year_month in months_with_samples:
        blended_rows = conn.execute(
            "SELECT hour_slot, daily_avg_cups FROM raw_hourly_pattern_monthly "
            "WHERE store_id = ? AND year_month = ?",
            (store_id, year_month),
        ).fetchall()
        blended = {r["hour_slot"]: r["daily_avg_cups"] or 0 for r in blended_rows}
        if not blended:
            continue

        y, m = map(int, year_month.split("-"))
        n_days = calendar.monthrange(y, m)[1]
        n_weekend_cal = sum(
            1 for d in range(1, n_days + 1) if date(y, m, d).weekday() >= 5
        )
        n_weekday_cal = n_days - n_weekend_cal
        if n_weekday_cal == 0:
            continue

        for hour_slot in HOUR_SLOTS:
            samples = by_month_hour.get((year_month, hour_slot))
            if not samples or hour_slot not in blended:
                continue
            weekend_avg = sum(c for _, c in samples) / len(samples)
            weekday_avg = (n_days * blended[hour_slot] - n_weekend_cal * weekend_avg) / n_weekday_cal
            per_hour["假日"][hour_slot].append(weekend_avg)
            per_hour["平日"][hour_slot].append(weekday_avg)

    result = {"平日": [], "假日": []}
    for daytype in ("平日", "假日"):
        for hour_slot in HOUR_SLOTS:
            vals = per_hour[daytype][hour_slot]
            if not vals:
                continue
            result[daytype].append({
                "時段": f"{hour_slot}:00",
                "月數": len(vals),
                "平均杯數": round(sum(vals) / len(vals), 1),
                "最大值": round(max(vals), 1),
                "最小值": round(min(vals), 1),
            })
    return result


def roster_mode_by_weekday(conn, store_id, start, end, config) -> list:
    """回傳每個時段一筆 dict：{時段, 星期一_正職, 星期一_兼職, 星期一_一致比例, ...}。
    「眾數」= 該星期幾在取樣範圍內最常出現的（正職人數, 兼職人數）組合。"""
    roles = config["employee_roles"]
    rows = conn.execute(
        "SELECT business_date, employee_code, start_time, end_time FROM raw_staffing_actual "
        "WHERE store_id = ? AND business_date BETWEEN ? AND ? "
        "AND start_time IS NOT NULL AND end_time IS NOT NULL",
        (store_id, start, end),
    ).fetchall()
    if not rows:
        return []

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["business_date"]].append(r)

    cell_counts = {wd: {h: Counter() for h in HOUR_SLOTS} for wd in WEEKDAY_NAMES}
    d, d_end = date.fromisoformat(start), date.fromisoformat(end)
    while d <= d_end:
        wd_name = WEEKDAY_NAMES[d.weekday()]
        day_rows = by_date.get(d.isoformat(), [])
        for hour_slot in HOUR_SLOTS:
            hour = int(hour_slot)
            window_start, window_end = hour * 60, (hour + 1) * 60
            ft, pt = 0, 0
            for r in day_rows:
                s, e = _to_minutes(r["start_time"]), _to_minutes(r["end_time"])
                if s < window_end and e > window_start:
                    role = roles.get(r["employee_code"])
                    if role in ("manager", "staff"):
                        ft += 1
                    elif role == "part_time":
                        pt += 1
            cell_counts[wd_name][hour_slot][(ft, pt)] += 1
        d += timedelta(days=1)

    result = []
    for hour_slot in HOUR_SLOTS:
        row = {"時段": f"{hour_slot}:00"}
        for wd in WEEKDAY_NAMES:
            counter = cell_counts[wd][hour_slot]
            total = sum(counter.values())
            if total == 0:
                row[f"星期{wd}_正職"] = None
                row[f"星期{wd}_兼職"] = None
                row[f"星期{wd}_一致比例"] = None
                continue
            (ft, pt), n = counter.most_common(1)[0]
            row[f"星期{wd}_正職"] = ft
            row[f"星期{wd}_兼職"] = pt
            row[f"星期{wd}_一致比例"] = f"{n}/{total}"
        result.append(row)
    return result


def _print_table(rows, cols):
    if not rows:
        print("（無資料）")
        return
    widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("-" * (sum(widths.values()) + 2 * len(cols)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main():
    conn = _conn()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    store_ids = [r[0] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id")]

    for store_id in store_ids:
        print(f"\n########## {store_id} 店：平日/假日逐時段杯數 ##########")
        months = [r[0] for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id=? ORDER BY 1",
            (store_id,)
        ).fetchall()]
        stats = cup_stats_by_daytype(conn, store_id, months)
        for daytype in ("平日", "假日"):
            print(f"\n=== {daytype} ===")
            _print_table(stats[daytype], ["時段", "月數", "平均杯數", "最大值", "最小值"])

        print(f"\n########## {store_id} 店：星期幾 x 時段實際排班眾數 ##########")
        date_rows = conn.execute(
            "SELECT MIN(business_date), MAX(business_date) FROM raw_staffing_actual WHERE store_id=?",
            (store_id,),
        ).fetchone()
        if date_rows[0] is None:
            print("（無排班原始資料）")
            continue
        roster = roster_mode_by_weekday(conn, store_id, date_rows[0], date_rows[1], config)
        cols = ["時段"] + [f"星期{wd}_{k}" for wd in WEEKDAY_NAMES for k in ("正職", "兼職")]
        _print_table(roster, cols)


if __name__ == "__main__":
    main()
