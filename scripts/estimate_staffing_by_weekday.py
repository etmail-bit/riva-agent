#!/usr/bin/env python3
"""星期幾（一~日）逐時段建議人力估計：給 B 店用的，因為 B 店沒有 raw_hourly_pattern_daily
（假日單日樣本）也沒有 raw_staffing_actual（排班原始資料），analyze_staffing_daytype.py
那套「平日/假日反推法」跟「星期幾排班眾數」在 B 店都會回傳空結果（見該檔案 docstring）。

2026-07-14 使用者提議的替代做法：用真實發票交易時間戳記回推的「星期幾 x 時段」發票張數
（analyze_operations.hourly_channel_by_weekday，這是目前系統裡唯一「逐日、涵蓋一~日七天」
都有真實時間戳記的資料），乘上一個「每張發票約幾杯」的轉換比例，換算成估計杯數，再套用
calculate_staffing.py 同一套建議人力公式。

轉換比例本身是真實資料算出來的（有杯數月份的「月總杯數 ÷ 月發票張數」），不是憑空假設，
但套用到其他月份/星期幾時，最終的「估計杯數」跟「建議人力」都是推算值，不是實際量到的
杯數——這點要老實跟使用者講，不能包裝成跟 A 店平日/假日反推法一樣的「真實資料」等級。

外送耗時修正：raw_hourly_pattern_monthly 的 delivery_count 只有「月累計」顆粒度，沒有星期幾
拆分，這裡用「該時段所有可用月份的日均外送單數平均值」套用到每個星期幾（七天一致），當作
近似值，跟杯數估計不同等級（杯數至少有星期幾差異，外送沒有）。

用法：
    source .venv/bin/activate
    python3 -m scripts.estimate_staffing_by_weekday
"""
import sqlite3
import statistics
from pathlib import Path

from scripts.analyze_operations import hourly_channel_by_weekday
from scripts.analyze_staffing_daytype import HOUR_SLOTS, WEEKDAY_NAMES
from scripts.calculate_staffing import (
    _days_in_month,
    calculate_delivery_hours,
    is_tea_brewing_hour,
    load_config,
    required_front_staff_for_hour,
)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
WEEKDAY_TO_DAYTYPE = {**{wd: "平日" for wd in WEEKDAY_NAMES[:5]}, **{wd: "假日" for wd in WEEKDAY_NAMES[5:]}}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def cups_per_invoice_ratio(conn, store_id) -> dict:
    """回傳 {"by_month": {year_month: ratio}, "avg": float}，用有真實杯數的月份
    （月總杯數 ÷ 該月正常發票張數）算出「每張發票約幾杯」。月總杯數 = 該月每個
    時段 daily_avg_cups 加總（= 當日全時段平均杯數）再乘上當月天數。"""
    months = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id = ? ORDER BY 1",
            (store_id,),
        ).fetchall()
    ]
    by_month = {}
    for year_month in months:
        cups_rows = conn.execute(
            "SELECT daily_avg_cups FROM raw_hourly_pattern_monthly WHERE store_id = ? AND year_month = ?",
            (store_id, year_month),
        ).fetchall()
        daily_total_cups = sum(r[0] or 0 for r in cups_rows)
        month_total_cups = daily_total_cups * _days_in_month(year_month)

        invoice_count = conn.execute(
            "SELECT COUNT(*) FROM raw_invoice_transactions "
            "WHERE store_id = ? AND tx_status = '正常' AND substr(tx_time, 1, 7) = ?",
            (store_id, year_month),
        ).fetchone()[0]
        if invoice_count == 0 or month_total_cups == 0:
            continue
        by_month[year_month] = month_total_cups / invoice_count

    avg = round(statistics.mean(by_month.values()), 3) if by_month else None
    return {"by_month": by_month, "avg": avg}


def avg_delivery_by_hour(conn, store_id) -> dict:
    """{hour_slot: 日均外送單數}，跨所有可用月份取平均（沒有星期幾拆分，七天套同一個值）。"""
    rows = conn.execute(
        "SELECT year_month, hour_slot, delivery_count FROM raw_hourly_pattern_monthly WHERE store_id = ?",
        (store_id,),
    ).fetchall()
    per_hour = {h: [] for h in HOUR_SLOTS}
    for r in rows:
        if r["hour_slot"] not in per_hour:
            continue
        daily_avg = (r["delivery_count"] or 0) / _days_in_month(r["year_month"])
        per_hour[r["hour_slot"]].append(daily_avg)
    return {h: (statistics.mean(v) if v else 0.0) for h, v in per_hour.items()}


def estimate_staffing_by_weekday(conn, store_id, config) -> dict:
    """回傳 {"ratio": {...}, "rows": [...], "daytype_avg": {"平日": [...], "假日": [...]}}。
    rows 每筆：{時段, 星期一_估計杯數, 星期一_建議人力, ..., 星期日_估計杯數, 星期日_建議人力}。
    daytype_avg 每筆（平日/假日各一組列表）：{時段, 平均估計杯數, 平均建議人力}，
    七天各自算完後再依平日(一~五)/假日(六日)取平均彙整。"""
    ratio_info = cups_per_invoice_ratio(conn, store_id)
    if ratio_info["avg"] is None:
        return {"ratio": ratio_info, "rows": [], "daytype_avg": {"平日": [], "假日": []}}
    ratio = ratio_info["avg"]

    weekday_invoice_rows = hourly_channel_by_weekday(conn, store_id)
    invoice_by_hour = {r["時段"]: r for r in weekday_invoice_rows}

    delivery_by_hour = avg_delivery_by_hour(conn, store_id)

    per_weekday_hour = {wd: {} for wd in WEEKDAY_NAMES}
    for hour_slot in HOUR_SLOTS:
        inv_row = invoice_by_hour.get(hour_slot)
        delivery_hours = calculate_delivery_hours(delivery_by_hour.get(hour_slot, 0.0), config)
        is_tea = is_tea_brewing_hour(hour_slot, config)
        for wd in WEEKDAY_NAMES:
            invoice_count = inv_row.get(f"星期{wd}_發票張數") if inv_row else None
            if invoice_count is None:
                per_weekday_hour[wd][hour_slot] = None
                continue
            est_cups = round(invoice_count * ratio, 1)
            calc = required_front_staff_for_hour(est_cups, delivery_hours, is_tea, config)
            per_weekday_hour[wd][hour_slot] = {
                "est_cups": est_cups,
                "required_front_staff": calc["required"],
                "required_front_staff_formula": calc["formula"],
                "tea_brewing": is_tea,
            }

    rows = []
    for hour_slot in HOUR_SLOTS:
        row = {"時段": f"{hour_slot}:00"}
        for wd in WEEKDAY_NAMES:
            cell = per_weekday_hour[wd][hour_slot]
            row[f"星期{wd}_估計杯數"] = cell["est_cups"] if cell else None
            row[f"星期{wd}_建議人力"] = cell["required_front_staff"] if cell else None
            row[f"星期{wd}_公式"] = cell["required_front_staff_formula"] if cell else None
        rows.append(row)

    daytype_avg = {"平日": [], "假日": []}
    for daytype in ("平日", "假日"):
        wds = [wd for wd, dt in WEEKDAY_TO_DAYTYPE.items() if dt == daytype]
        for hour_slot in HOUR_SLOTS:
            cells = [per_weekday_hour[wd][hour_slot] for wd in wds if per_weekday_hour[wd][hour_slot]]
            if not cells:
                continue
            daytype_avg[daytype].append({
                "時段": f"{hour_slot}:00",
                "平均估計杯數": round(sum(c["est_cups"] for c in cells) / len(cells), 1),
                "平均建議人力": round(sum(c["required_front_staff"] for c in cells) / len(cells), 1),
            })

    return {"ratio": ratio_info, "rows": rows, "daytype_avg": daytype_avg}


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
    config = load_config()
    store_id = "B"

    result = estimate_staffing_by_weekday(conn, store_id, config)
    ratio_info = result["ratio"]

    print(f"########## {store_id} 店：每張發票約幾杯（轉換比例，來源真實月杯數 ÷ 真實月發票張數）##########")
    for ym, r in ratio_info["by_month"].items():
        print(f"  {ym}: {r:.3f} 杯/張")
    if ratio_info["avg"] is None:
        raise SystemExit(f"{store_id} 店沒有任何月份同時有杯數與發票資料，無法估算。")
    print(f"  平均值（套用到全部星期幾）: {ratio_info['avg']:.3f} 杯/張")

    print(f"\n########## {store_id} 店：星期幾 x 時段 估計杯數與建議人力（七天各自）##########")
    cols = ["時段"] + [f"星期{wd}_{k}" for wd in WEEKDAY_NAMES for k in ("估計杯數", "建議人力")]
    _print_table(result["rows"], cols)

    for daytype in ("平日", "假日"):
        print(f"\n########## {store_id} 店：{daytype} 彙整（七天平均）##########")
        _print_table(result["daytype_avg"][daytype], ["時段", "平均估計杯數", "平均建議人力"])

    print(
        "\n（提醒：估計杯數＝發票張數 × 轉換比例，是推算值，不是實際量到的杯數；"
        "外送耗時修正用跨月日均值套用到七天，沒有星期幾拆分，是近似值，不是真實星期幾外送單數；"
        "「建議人力」已經是總人數，開早班時段含那個只能貢獻8杯/hr的人在內，不用再另外+1。）"
    )
    conn.close()


if __name__ == "__main__":
    main()
