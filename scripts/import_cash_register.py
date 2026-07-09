#!/usr/bin/env python3
"""收銀機明細匯入程式：把 data/raw 下的收銀機明細 xlsx 讀進 raw_cash_register_daily。

用法：
    source .venv/bin/activate
    python3 scripts/import_cash_register.py
"""
import glob
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
RAW_GLOBS = [
    str(ROOT / "data" / "raw" / "*收銀機明細.xlsx"),  # 正確用字
    str(ROOT / "data" / "raw" / "*收營機明細.xlsx"),  # 舊檔名慣例的錯字，保留相容（2026-07-08 發現）
    str(ROOT / "data" / "raw" / "*收銀機.xlsx"),  # 少打「明細」二字的檔名慣例，保留相容（2026-07-08 發現，欄位結構經確認一致）
    str(ROOT / "data" / "raw" / "*收銀機名細.xlsx"),  # 「明細」誤植為「名細」的檔名慣例，保留相容（2026-07-08 發現，欄位結構經確認一致）
]

# 分類規則（2026-07-07 與使用者確認）：
# 信用卡 = 刷卡機信用卡 + 信用卡線上；其餘電子支付方式一律歸「其他電子支付」
CREDIT_CARD_COLS = ["刷卡機信用卡", "信用卡線上"]
OTHER_ELECTRONIC_COLS = [
    "悠遊卡", "LinePay", "街口支付", "PxPay", "iPASSMONEY", "其他支付",
    "Linepay線上", "街口線上", "全盈線上", "ICashPay線上", "pxpay線上",
    "刷卡機悠遊卡", "刷卡機一卡通",
]


def load_store_map():
    """讀 .env 的 STORE_*_REAL_NAME，回傳 {真實店名: 代號}。"""
    store_map = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("STORE_") and key.endswith("_REAL_NAME") and value:
            store_id = key[len("STORE_"):-len("_REAL_NAME")]  # 'A' / 'B'
            store_map[value] = store_id
    return store_map


def to_iso_date(raw_date):
    if isinstance(raw_date, datetime):
        return raw_date.strftime("%Y-%m-%d")
    return datetime.strptime(str(raw_date).strip(), "%Y/%m/%d").strftime("%Y-%m-%d")


def as_int(value):
    return int(value) if value else 0


def import_file(path, store_map, conn):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    col = {name: idx for idx, name in enumerate(header)}

    inserted = 0
    for row in data_rows:
        if row[col["營業日期"]] is None:
            continue
        raw_store_name = row[col["門市名稱"]]
        store_id = store_map.get(raw_store_name)
        if store_id is None:
            raise ValueError(
                f"{path.name}：門市名稱在 .env 對照表裡找不到（不印出真實店名，避免洩漏），"
                "請確認 STORE_A_REAL_NAME / STORE_B_REAL_NAME 是否有填對"
            )

        record = {name: row[idx] for name, idx in col.items()}
        credit_card = sum(as_int(record.get(c)) for c in CREDIT_CARD_COLS)
        other_electronic = sum(as_int(record.get(c)) for c in OTHER_ELECTRONIC_COLS)

        conn.execute(
            """
            INSERT OR REPLACE INTO raw_cash_register_daily
                (store_id, business_date, register_no, gross_revenue,
                 ubereats_amount, foodpanda_amount, credit_card_amount,
                 other_electronic_amount, taxable_revenue, tax_exempt_revenue,
                 cash_outflow, payment_breakdown_json, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                store_id,
                to_iso_date(record["營業日期"]),
                str(record["收銀機別"]),
                as_int(record["營收金額"]),
                as_int(record.get("Ubereats")),
                as_int(record.get("Foodpanda")),
                credit_card,
                other_electronic,
                as_int(record.get("應稅")),
                as_int(record.get("免稅")),
                as_int(record.get("現金支出")),
                json.dumps(record, ensure_ascii=False, default=str),
                path.name,
            ),
        )
        inserted += 1
    return inserted


def main():
    store_map = load_store_map()
    if not store_map:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    for store_id in store_map.values():
        conn.execute("INSERT OR IGNORE INTO stores (store_id) VALUES (?)", (store_id,))

    files = sorted({Path(p) for pattern in RAW_GLOBS for p in glob.glob(pattern)})
    if not files:
        raise SystemExit(f"找不到符合 {RAW_GLOBS} 的檔案")

    total = 0
    for path in files:
        count = import_file(path, store_map, conn)
        print(f"{path.name}: 匯入 {count} 筆")
        total += count

    conn.commit()
    conn.close()
    print(f"完成，總計匯入 {total} 筆到 raw_cash_register_daily")


if __name__ == "__main__":
    main()
