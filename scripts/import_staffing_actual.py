#!/usr/bin/env python3
"""實際排班匯入程式：把 data/staffing_actual_raw.csv 讀進 raw_staffing_actual。

這張表存的是「實際排班表」的原始謄打內容（原始班表是照片，人工謄打成 CSV），
還沒跟 calculate_staffing.py 算出的建議人力比對過，比對邏輯之後另外做。

員工代碼（取真實姓名中最有辨識度的一個字）對照真實姓名只存在 .env，
本程式跟本資料表都只認代碼，不會出現真實姓名。

用法：
    1. 把謄打好的排班資料放進 data/staffing_actual_raw.csv
       （範本：data/staffing_actual_raw.example.csv）
    2. source .venv/bin/activate && python3 scripts/import_staffing_actual.py
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
CSV_PATH = ROOT / "data" / "staffing_actual_raw.csv"


def load_valid_store_ids():
    store_ids = set()
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("STORE_") and key.endswith("_REAL_NAME") and value:
            store_ids.add(key[len("STORE_"):-len("_REAL_NAME")])
    return store_ids


def load_valid_employee_codes():
    codes = set()
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("EMPLOYEE_") and key.endswith("_NAME") and value:
            codes.add(key[len("EMPLOYEE_"):-len("_NAME")])
    return codes


def as_float_or_none(value):
    value = (value or "").strip()
    return float(value) if value else None


def as_str_or_none(value):
    value = (value or "").strip()
    return value or None


def main():
    if not CSV_PATH.exists():
        raise SystemExit(
            f"找不到 {CSV_PATH}，請先把謄打好的排班資料放進這個檔案"
            "（範本：data/staffing_actual_raw.example.csv）"
        )

    valid_store_ids = load_valid_store_ids()
    if not valid_store_ids:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    valid_employee_codes = load_valid_employee_codes()
    if not valid_employee_codes:
        raise SystemExit(".env 裡沒有任何 EMPLOYEE_*_NAME，請先填員工代碼對照")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    for code in valid_employee_codes:
        conn.execute("INSERT OR IGNORE INTO employees (employee_code) VALUES (?)", (code,))

    count = 0
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            store_id = (row.get("store_id") or "").strip()
            business_date = (row.get("business_date") or "").strip()
            employee_code = (row.get("employee_code") or "").strip()
            if not store_id and not business_date:
                continue  # 跳過空白列

            if store_id not in valid_store_ids:
                raise ValueError(f"店代號『{store_id}』不在 .env 對照表裡，請確認 CSV 內容")
            if employee_code not in valid_employee_codes:
                raise ValueError(f"員工代碼『{employee_code}』不在 .env 對照表裡，請確認 CSV 內容")

            conn.execute(
                """
                INSERT INTO raw_staffing_actual
                    (store_id, business_date, employee_code, shift_label,
                     start_time, end_time, scheduled_hours, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id, business_date, employee_code) DO UPDATE SET
                    shift_label = excluded.shift_label,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    scheduled_hours = excluded.scheduled_hours,
                    source_file = excluded.source_file
                """,
                (
                    store_id,
                    business_date,
                    employee_code,
                    as_str_or_none(row.get("shift_label")),
                    as_str_or_none(row.get("start_time")),
                    as_str_or_none(row.get("end_time")),
                    as_float_or_none(row.get("scheduled_hours")),
                    CSV_PATH.name,
                ),
            )
            count += 1

    conn.commit()
    conn.close()
    print(f"完成，寫入/更新 {count} 筆到 raw_staffing_actual")


if __name__ == "__main__":
    main()
