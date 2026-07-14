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

序號標錯的處理（2026-07-14 使用者提醒）：B 店這批樣本檔混用「禮拜6/7」「禮拜六/天」
兩種寫法，而且出現「同一個『第N個星期六/日』被兩份內容不同的檔案指到」「檔名標的
序號超過當月實際天數（例如六月只有4個星期日，卻有檔案標『第5個』）」這兩種狀況——
真正的日期已經無法從檔名還原。使用者確認：這個分析（cup_stats_by_daytype()）只在乎
「哪個月、哪個時段」的杯量分布，不會用到 business_date 的精確星期幾或日期，所以這裡
不逼近真實日期，改成在該月份內找一個沒被佔用的日期當「佔位日期」存進去，把真實杯數
資料留下來一起算平均，不因為序號標錯就整份丟掉。佔位日期不代表真的是那一天發生的，
之後如果要精確對到日期，要靠使用者從原始 POS 系統重新核對。

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

    # 「星期/禮拜」＋「六/天」或口語簡寫「6/7」（使用者打字習慣不一致，2026-07-14
    # B 店新補的樣本檔混用了這幾種寫法，同一批裡「禮拜6」跟「禮拜六」都指星期六）。
    daytype_match = re.search(r"第(\d+)['’]?個(?:星期|禮拜)(六|天|6|7)", name)
    if not daytype_match:
        raise ValueError(f"{name}：檔名裡找不到『第N個星期六/天』（或『禮拜六/禮拜天/禮拜6/禮拜7』）")
    ordinal = int(daytype_match.group(1))
    weekday_target = 5 if daytype_match.group(2) in ("六", "6") else 6  # 5=Sat, 6=Sun (date.weekday())
    return store_id, year_month, weekday_target, ordinal


def nth_weekday_date(year_month, weekday_target, ordinal):
    """回傳該月第 ordinal 個 weekday_target（5=六,6=日）的日期；序號超過當月實際天數時
    回傳 None（呼叫端會改用佔位日期，見上方 docstring 的「序號標錯的處理」）。"""
    year, month = map(int, year_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    matches = [
        date(year, month, d) for d in range(1, days_in_month + 1)
        if date(year, month, d).weekday() == weekday_target
    ]
    if ordinal > len(matches):
        return None
    return matches[ordinal - 1]


def placeholder_date(year_month, already_used):
    """在 year_month 這個月裡找一個沒被 already_used 佔用的日期當佔位日期（由小到大找
    第一個空位），已佔用的集合會就地更新。只在「檔名序號無法還原出真實日期」時使用。"""
    year, month = map(int, year_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    for d in range(1, days_in_month + 1):
        candidate = date(year, month, d)
        if candidate not in already_used:
            already_used.add(candidate)
            return candidate
    raise ValueError(f"{year_month} 整個月的日期都被佔用了，沒有空位可以當佔位日期")


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

    # 第一輪：算出每個檔案「自然」對應的日期（第N個星期六/日），先不管衝突。
    parsed_files = []
    for path in files:
        store_id, year_month, weekday_target, ordinal = parse_filename(path.name, store_map)
        natural_date = nth_weekday_date(year_month, weekday_target, ordinal)
        parsed = load_file_cups(path)
        content_hash = tuple(sorted((h, v["cups"]) for h, v in parsed.items()))
        parsed_files.append((path, store_id, year_month, natural_date, parsed, content_hash))

    # 第二輪：內容完全相同（同一天存兩次）的檔案先合併成一組，一組只留一個代表，照
    # files 的排序（已經按檔名排過）決定處理順序，確保每次重跑結果一致。
    groups = {}  # (store_id, year_month, natural_date, content_hash) -> (代表 path, 代表 parsed)
    order = []
    for path, store_id, year_month, natural_date, parsed, content_hash in parsed_files:
        key = (store_id, year_month, natural_date, content_hash)
        if key not in groups:
            groups[key] = (path, parsed)
            order.append(key)
        else:
            print(f"  [跳過內容重複] {path.name}（跟 {groups[key][0].name} 逐時段杯數完全一樣）")

    # 第三輪：這個月已經有真的算得出來的 natural_date，一律優先保留給第一個拿到它的
    # 內容組；同一個 natural_date 之後如果還有內容不同的組別（序號標錯指向不同天），
    # 或 natural_date 本身是 None（序號超過當月實際天數），都改分配佔位日期，不覆蓋、
    # 不丟資料——已用掉的日期（含佔位日期本身）會即時登記，避免佔位日期互相撞期。
    used_by_month = defaultdict(set)  # (store_id, year_month) -> {已使用的 date}
    claimed_natural = set()  # (store_id, natural_date) 已經被某個內容組領走了
    to_insert = []  # (path, store_id, business_date_str, parsed)
    placeholder_notes = []
    for key in order:
        store_id, year_month, natural_date, content_hash = key
        rep_path, rep_parsed = groups[key]
        if natural_date is not None and (store_id, natural_date) not in claimed_natural:
            business_date = natural_date
            claimed_natural.add((store_id, natural_date))
            used_by_month[(store_id, year_month)].add(natural_date)
        else:
            business_date = placeholder_date(year_month, used_by_month[(store_id, year_month)])
            placeholder_notes.append((rep_path.name, store_id, business_date.isoformat()))
        to_insert.append((rep_path, store_id, business_date.isoformat(), rep_parsed))

    if placeholder_notes:
        print(f"以下 {len(placeholder_notes)} 筆檔名的序號無法還原出唯一的真實日期，改用同一個月內的佔位日期"
              "（真實杯數資料照樣算進去，只是 business_date 不代表真的是那一天）：")
        for name, store_id, business_date in placeholder_notes:
            print(f"  {name} -> {store_id} 店 {business_date}（佔位）")
        print()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    inserted, skipped_dup = 0, len(parsed_files) - len(to_insert)

    for path, store_id, business_date, parsed in to_insert:
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
