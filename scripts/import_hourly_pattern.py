#!/usr/bin/env python3
"""時段占比匯入程式：把 data/raw 下的時段占比 xlsx 讀進 raw_hourly_pattern_monthly。

A、B 兩店欄位結構完全一致：時段/現場來客數/自取來客數/外送來客數/平台來客數/
銷售額/佔比/日均銷售額/日均杯數。但兩店的檔案都沒有門市名稱欄，店別判斷用
跟 import_revenue_monthly.py 一樣的慣例：檔名有「X店」標記就用標記，沒標記
一律視為 A 店（A 是最早、唯一店時期留下的命名慣例）。

用法：
    source .venv/bin/activate
    python3 scripts/import_hourly_pattern.py
"""
import glob
import re
import sqlite3
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
RAW_GLOB = str(ROOT / "data" / "raw" / "*時段占比.xlsx")

COLUMNS = [
    "時段", "現場來客數", "自取來客數", "外送來客數", "平台來客數",
    "銷售額", "佔比", "日均銷售額", "日均杯數",
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


def extract_year_month(filename):
    match = re.search(r"(20\d{2})(0[1-9]|1[0-2])", filename)
    if not match:
        raise ValueError(f"{filename}：檔名裡找不到 YYYYMM，請確認檔名格式")
    return f"{match.group(1)}-{match.group(2)}"


def resolve_store_for_file(filename, store_map):
    match = re.search(r"([A-Z])店", filename)
    if match and match.group(1) in store_map.values():
        return match.group(1)
    if "A" in store_map.values():
        print(f"  [慣例判斷] {filename}：無門市名稱欄、檔名也無標記，依慣例歸戶到 A 店")
        return "A"
    if len(store_map) == 1:
        return next(iter(store_map.values()))
    raise ValueError(f"{filename}：無法判斷店別，檔名沒有『X店』標記且 .env 有 2 間以上的店")


def import_file(path, store_id, conn):
    year_month = extract_year_month(path.name)
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    stripped_header = [str(h).strip() if h is not None else h for h in header]
    col = {name: stripped_header.index(name) for name in COLUMNS if name in stripped_header}

    missing = set(COLUMNS) - col.keys()
    if missing:
        raise ValueError(f"{path.name}：缺少必要欄位 {missing}，欄位結構可能變了")

    inserted = 0
    for row in data_rows:
        if row[col["時段"]] is None:
            continue
        conn.execute(
            """
            INSERT INTO raw_hourly_pattern_monthly
                (store_id, year_month, hour_slot, walkin_count, pickup_count,
                 delivery_count, platform_count, sales_amount, pct_of_total,
                 daily_avg_sales, daily_avg_cups, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                store_id,
                year_month,
                str(row[col["時段"]]),
                row[col["現場來客數"]],
                row[col["自取來客數"]],
                row[col["外送來客數"]],
                row[col["平台來客數"]],
                row[col["銷售額"]],
                row[col["佔比"]],
                row[col["日均銷售額"]],
                row[col["日均杯數"]],
                path.name,
            ),
        )
        inserted += 1
    return inserted, year_month


def main():
    store_map = load_store_map()
    if not store_map:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    files = sorted(Path(p) for p in glob.glob(RAW_GLOB))
    if not files:
        raise SystemExit(f"找不到符合 {RAW_GLOB} 的檔案")

    file_stores = {path: resolve_store_for_file(path.name, store_map) for path in files}

    # 重跑時先清掉「這次要匯入的店+月份」的舊資料，避免重複累加
    periods_to_refresh = {(store_id, extract_year_month(path.name)) for path, store_id in file_stores.items()}
    conn.executemany(
        "DELETE FROM raw_hourly_pattern_monthly WHERE store_id = ? AND year_month = ?",
        list(periods_to_refresh),
    )

    total = 0
    for path, store_id in file_stores.items():
        count, year_month = import_file(path, store_id, conn)
        print(f"{path.name} ({store_id}, {year_month}): 匯入 {count} 筆")
        total += count

    conn.commit()
    conn.close()
    print(f"完成，總計匯入 {total} 筆到 raw_hourly_pattern_monthly")


if __name__ == "__main__":
    main()
