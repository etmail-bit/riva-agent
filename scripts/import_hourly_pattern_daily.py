#!/usr/bin/env python3
"""單日時段占比匯入程式：把 data/raw/週末時段占比/ 底下逐一星期六/日的時段占比 xlsx
讀進 raw_hourly_pattern_daily。跟 import_hourly_pattern.py（月彙總版）是姊妹腳本，
差別是這裡處理的是「單一天」的樣本，不是整月平均。

檔名慣例：「...202606_A店＿第1個星期六時段占比.xlsx」——從檔名解析出年月、店別、
「星期六」或「星期天」、第幾個。business_date 用「該月第 N 個星期六/日」配合行事曆
算出來（假設檔名序號是照日期由小到大排的，這是唯一能還原真實日期的方式，因為
檔案本身沒有日期欄）。

去重複邏輯：用「內容」而不是檔名判斷重複——同一批檔案裡曾經抓到「檔名序號不同、
但逐時段數字完全一樣」的狀況（研判是同一天存了兩次或標錯序號），一律只留一筆。

用法：
    source .venv/bin/activate
    python3 scripts/import_hourly_pattern_daily.py
"""
import calendar
import glob
import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
RAW_GLOB = str(ROOT / "data" / "raw" / "週末時段占比" / "*.xlsx")

COLUMNS = [
    "時段", "現場來客數", "自取來客數", "外送來客數", "平台來客數",
    "銷售額", "佔比", "日均杯數",
]


def load_store_map():
    store_map = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("STORE_") and key.endswith("_REAL_NAME") and value:
            store_id = key[len("STORE_"):-len("_REAL_NAME")]
            store_map[value] = store_id
    return store_map


def parse_filename(name, store_map):
    ym_match = re.search(r"(20\d{2})(0[1-9]|1[0-2])", name)
    if not ym_match:
        raise ValueError(f"{name}：檔名裡找不到 YYYYMM")
    year_month = f"{ym_match.group(1)}-{ym_match.group(2)}"

    store_match = re.search(r"([A-Z])店", name)
    if store_match and store_match.group(1) in store_map.values():
        store_id = store_match.group(1)
    elif "A" in store_map.values():
        store_id = "A"
    else:
        raise ValueError(f"{name}：無法判斷店別")

    daytype_match = re.search(r"第(\d+)['’]?個星期(六|天)", name)
    if not daytype_match:
        raise ValueError(f"{name}：檔名裡找不到『第N個星期六/天』")
    ordinal = int(daytype_match.group(1))
    weekday_target = 5 if daytype_match.group(2) == "六" else 6  # 5=Sat, 6=Sun (date.weekday())
    return store_id, year_month, weekday_target, ordinal


def nth_weekday_date(year_month, weekday_target, ordinal):
    """回傳該月第 ordinal 個 weekday_target（5=六,6=日）的日期。"""
    year, month = map(int, year_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    matches = [
        date(year, month, d) for d in range(1, days_in_month + 1)
        if date(year, month, d).weekday() == weekday_target
    ]
    if ordinal > len(matches):
        raise ValueError(f"{year_month} 只有 {len(matches)} 個目標星期，檔名卻標第 {ordinal} 個")
    return matches[ordinal - 1]


def load_file_cups(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    stripped_header = [str(h).strip() if h is not None else h for h in header]
    col = {name: stripped_header.index(name) for name in COLUMNS if name in stripped_header}
    missing = set(COLUMNS) - col.keys()
    if missing:
        raise ValueError(f"{path.name}：缺少必要欄位 {missing}")

    parsed = {}
    for row in data_rows:
        hour = row[col["時段"]]
        if hour is None:
            continue
        parsed[str(hour)] = {
            "walkin_count": row[col["現場來客數"]],
            "pickup_count": row[col["自取來客數"]],
            "delivery_count": row[col["外送來客數"]],
            "platform_count": row[col["平台來客數"]],
            "sales_amount": row[col["銷售額"]],
            "pct_of_total": row[col["佔比"]],
            "cups": row[col["日均杯數"]],
        }
    return parsed


def main():
    store_map = load_store_map()
    if not store_map:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    files = sorted(Path(p) for p in glob.glob(RAW_GLOB))
    if not files:
        raise SystemExit(f"找不到符合 {RAW_GLOB} 的檔案")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    seen_content = defaultdict(set)  # (store_id, business_date) -> {content_hash}
    inserted, skipped_dup = 0, 0

    for path in files:
        store_id, year_month, weekday_target, ordinal = parse_filename(path.name, store_map)
        business_date = nth_weekday_date(year_month, weekday_target, ordinal).isoformat()
        parsed = load_file_cups(path)
        content_hash = tuple(sorted((h, v["cups"]) for h, v in parsed.items()))

        group_key = (store_id, year_month, weekday_target)
        if content_hash in seen_content[group_key]:
            print(f"  [跳過內容重複] {path.name}（跟同組另一個檔案逐時段杯數完全一樣）")
            skipped_dup += 1
            continue
        seen_content[group_key].add(content_hash)

        conn.execute("DELETE FROM raw_hourly_pattern_daily WHERE store_id = ? AND business_date = ?",
                     (store_id, business_date))
        for hour_slot, v in parsed.items():
            conn.execute(
                """
                INSERT INTO raw_hourly_pattern_daily
                    (store_id, business_date, hour_slot, walkin_count, pickup_count,
                     delivery_count, platform_count, sales_amount, pct_of_total, cups, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (store_id, business_date, hour_slot, v["walkin_count"], v["pickup_count"],
                 v["delivery_count"], v["platform_count"], v["sales_amount"], v["pct_of_total"],
                 v["cups"], path.name),
            )
        inserted += 1
        print(f"{path.name} -> {store_id} 店 {business_date}：匯入 {len(parsed)} 個時段")

    conn.commit()
    conn.close()
    print(f"\n完成，匯入 {inserted} 天的樣本，跳過 {skipped_dup} 個內容重複的檔案")


if __name__ == "__main__":
    main()
