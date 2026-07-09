#!/usr/bin/env python3
"""月成本實際數字匯入程式：把 data/monthly_cost_actuals.csv 讀進 monthly_cost_actuals。

這張表存的是「人事/原物料/水電」的實際數字（不是報表匯出的，是你自己月底登記的）。
欄位留空 = 還沒有實際數字，月盈虧計算時會 fallback 用 config/cost_rates.json 的概算值。

用法：
    1. 複製 data/monthly_cost_actuals.example.csv 為 data/monthly_cost_actuals.csv
       （這份真實檔案已被 .gitignore 排除，不會進版控）
    2. 每個月填一行實際數字，留空的欄位就是「先不管，用概算值」
    3. source .venv/bin/activate && python3 scripts/import_cost_actuals.py
"""
import csv
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
CSV_PATH = ROOT / "data" / "monthly_cost_actuals.csv"


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


def as_int_or_none(value):
    value = (value or "").strip()
    return int(value) if value else None


def main():
    if not CSV_PATH.exists():
        raise SystemExit(
            f"找不到 {CSV_PATH}，請先複製 data/monthly_cost_actuals.example.csv 為 "
            "data/monthly_cost_actuals.csv 再填入實際數字"
        )

    valid_store_ids = load_valid_store_ids()
    if not valid_store_ids:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    count = 0
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            store_id = (row.get("store_id") or "").strip()
            year_month = (row.get("year_month") or "").strip()
            if not store_id and not year_month:
                continue  # 跳過空白列

            if store_id not in valid_store_ids:
                raise ValueError(f"店代號『{store_id}』不在 .env 對照表裡，請確認 CSV 內容")
            if not re.match(r"^\d{4}-\d{2}$", year_month):
                raise ValueError(f"year_month『{year_month}』格式不對，要是 YYYY-MM")

            conn.execute(
                """
                INSERT INTO monthly_cost_actuals
                    (store_id, year_month, labor_actual, cogs_actual, utilities_actual, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id, year_month) DO UPDATE SET
                    labor_actual = excluded.labor_actual,
                    cogs_actual = excluded.cogs_actual,
                    utilities_actual = excluded.utilities_actual,
                    notes = excluded.notes
                """,
                (
                    store_id,
                    year_month,
                    as_int_or_none(row.get("labor_actual")),
                    as_int_or_none(row.get("cogs_actual")),
                    as_int_or_none(row.get("utilities_actual")),
                    (row.get("notes") or "").strip() or None,
                ),
            )
            count += 1

    conn.commit()
    conn.close()
    print(f"完成，寫入/更新 {count} 筆到 monthly_cost_actuals")


if __name__ == "__main__":
    main()
