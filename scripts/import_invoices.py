#!/usr/bin/env python3
"""發票明細匯入程式：把 data/raw 下的發票明細 xlsx 讀進 raw_invoice_transactions。

刻意不依賴檔名判斷月份 —— 已發現 A 店有檔名為「2026發票.xlsx」但內容其實是 6 月資料，
檔名不可靠。一律從「時間」欄位本身取得實際交易時間。

搭配 idx_invoice_unique（store_id, invoice_no）唯一索引，用 INSERT OR IGNORE 讓同一張
發票不會因為被不同檔案（例如誤植/重複匯出的月份檔）重複灌入；如果某個檔案「新增 0 筆」，
代表這個檔案的發票號都已經存在過，很可能是重複檔案，要回頭跟店家確認。

用法：
    source .venv/bin/activate
    python3 scripts/import_invoices.py
"""
import glob
import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
ENV_PATH = ROOT / ".env"
RAW_GLOBS = [
    str(ROOT / "data" / "raw" / "*發票.xlsx"),
    str(ROOT / "data" / "raw" / "*發票明細.xlsx"),  # 帶「明細」二字的檔名慣例，保留相容（2026-07-08 發現，欄位結構經確認一致）
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


def to_iso_datetime(raw_time):
    if isinstance(raw_time, datetime):
        return raw_time.strftime("%Y-%m-%d %H:%M:%S")
    return datetime.strptime(str(raw_time).strip(), "%Y/%m/%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")


def import_file(path, store_map, conn):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    col = {name: idx for idx, name in enumerate(header)}

    read = 0
    inserted = 0
    for row in data_rows:
        if row[col["時間"]] is None:
            continue
        read += 1
        raw_store_name = row[col["門市名稱"]]
        store_id = store_map.get(raw_store_name)
        if store_id is None:
            raise ValueError(
                f"{path.name}：門市名稱在 .env 對照表裡找不到（不印出真實店名，避免洩漏），"
                "請確認 STORE_A_REAL_NAME / STORE_B_REAL_NAME"
            )

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO raw_invoice_transactions
                (store_id, register_no, invoice_serial, invoice_no, tx_status,
                 tx_time, amount, carrier_no, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                store_id,
                str(row[col["收銀機別"]]) if row[col["收銀機別"]] is not None else None,
                str(row[col["流水單號"]]) if row[col["流水單號"]] is not None else None,
                str(row[col["發票號"]]) if row[col["發票號"]] is not None else None,
                row[col["交易狀態"]],
                to_iso_datetime(row[col["時間"]]),
                row[col["發票金額"]],
                row[col["載具號碼"]],
                path.name,
            ),
        )
        if cursor.rowcount:
            inserted += 1
    return read, inserted


def main():
    store_map = load_store_map()
    if not store_map:
        raise SystemExit(".env 裡的 STORE_A_REAL_NAME / STORE_B_REAL_NAME 都是空的，請先填店名對照")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    files = sorted({Path(p) for pattern in RAW_GLOBS for p in glob.glob(pattern)})
    if not files:
        raise SystemExit(f"找不到符合 {RAW_GLOBS} 的檔案")

    total_read = 0
    total_inserted = 0
    for path in files:
        read, inserted = import_file(path, store_map, conn)
        flag = "  <== 新增 0 筆，很可能是重複檔案，請確認" if inserted == 0 else ""
        print(f"{path.name}: 讀到 {read} 筆，新增 {inserted} 筆{flag}")
        total_read += read
        total_inserted += inserted

    conn.commit()
    conn.close()
    print(f"完成，共讀到 {total_read} 筆，實際新增 {total_inserted} 筆到 raw_invoice_transactions")


if __name__ == "__main__":
    main()
