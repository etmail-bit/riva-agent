#!/usr/bin/env python3
"""整合月度總報告：把月盈虧、排班建議、實際 vs 建議人力比對、排班成本估算、
營運報告（通路/客單價/尖峰/回頭客/星期幾彙整）、平日假日杯數與排班眾數，
全部彙整進一份 md（2026-07-13 使用者要求：只想開一份檔案就看到全部，
不想每次都要分開找兩份報表檔＋三個網頁）。

取代原本 analyze_operations.py／estimate_staffing_cost.py 各自寫出的
operational_report_<日期>.md／staffing_cost_estimate_<日期>.md 兩份檔案——
這兩支腳本現在只回傳報告文字（build_report()），不再各自寫檔，統一由這支腳本
彙整、只寫出一份 reports/月度總報告_<日期>.md。

月盈虧／排班建議／實際vs建議比對這三段，網頁版本仍然保留「可調整參數即時試算」
的功能（月盈虧頁／排班建議頁），這份報告只收錄「用目前存檔的預設參數」跑出來的
快照版本，適合當這個月的存查/列印定版，想試不同參數組合還是要去網頁互動調整。

用法：
    source .venv/bin/activate
    python3 -m scripts.generate_full_report
"""
import sqlite3
from datetime import date
from pathlib import Path

from scripts.analyze_operations import _compute_all as _compute_operations
from scripts.analyze_operations import build_report as build_operations_report
from scripts.analyze_operations import persist_operational_insights
from scripts.analyze_staffing_daytype import cup_stats_by_daytype, roster_mode_by_weekday
from scripts.calculate_staffing import calculate_hourly_staffing, get_hourly_data
from scripts.calculate_staffing import get_periods as get_staffing_periods
from scripts.calculate_staffing import load_config as load_staffing_config
from scripts.compare_staffing import compare_aggregate
from scripts.estimate_staffing_cost import build_report as build_cost_report
from scripts.estimate_staffing_cost import compute_store_stats
from scripts.estimate_staffing_cost import get_periods as get_cost_periods
from scripts.calculate_pnl import load_config as load_cost_config
from scripts.estimate_staffing_cost import persist_staffing_insights

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
REPORTS_DIR = ROOT / "reports"


def _demote_headers(text: str) -> str:
    """把子報告裡的 `## ` 二級標題降成 `### ` 三級，讓子報告內容正確巢狀在
    這裡的 `## N. ...` 章節底下，不會變成同一層級的標題（純排版調整，不影響內容）。"""
    return "\n".join(
        ("#" + line if line.startswith("## ") else line) for line in text.splitlines()
    )


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _stores(conn):
    return [r["store_id"] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id")]


def build_pnl_section(conn, store_ids) -> str:
    lines = [
        "## 1. 月盈虧（歷史走勢，讀 `monthly_pnl` 既有紀錄）",
        "",
        "完整互動版（可調整費率參數即時試算、含彙整走勢圖）請到網頁「月盈虧」頁，這裡只列出目前資料庫裡的計算結果。",
        "",
    ]
    any_data = False
    for sid in store_ids:
        rows = conn.execute(
            "SELECT year_month, revenue, pretax_profit, net_profit, revenue_source "
            "FROM monthly_pnl WHERE store_id = ? ORDER BY year_month",
            (sid,),
        ).fetchall()
        if not rows:
            continue
        any_data = True
        lines.append(f"### {sid} 店")
        lines.append("")
        lines.append("| 月份 | 營收 | 稅前淨利 | 稅後淨利 | 資料來源 |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['year_month']} | {r['revenue']:,} | {r['pretax_profit']:,} | "
                f"{r['net_profit']:,} | {r['revenue_source']} |"
            )
        lines.append("")
    if not any_data:
        lines.append("（`monthly_pnl` 目前沒有資料，先跑 `calculate_pnl.py`。）")
        lines.append("")
    return "\n".join(lines)


def build_staffing_recommendation_section(conn, store_ids, config) -> str:
    lines = [
        "## 2. 排班建議（各店最新月份快照，目前存檔的預設參數）",
        "",
        "可調整參數即時試算請到網頁「排班建議」頁；下面是用 `config/staffing_rules.json` "
        "目前的預設值，對每店最新一個有時段占比資料的月份，算出的逐時段建議前場人力。",
        "",
    ]
    any_data = False
    for sid in store_ids:
        periods = [ym for s, ym in get_staffing_periods(conn) if s == sid]
        if not periods:
            continue
        any_data = True
        year_month = periods[-1]
        hourly_data = get_hourly_data(conn, sid, year_month)
        staffing = calculate_hourly_staffing(hourly_data, config)
        lines.append(f"### {sid} 店 {year_month}")
        lines.append("")
        lines.append("| 時段 | 日均杯數 | 建議前場人力 | 煮茶 | 日均外送單 | 外送耗時(hr) |")
        lines.append("|---|---|---|---|---|---|")
        for hour_slot in sorted(staffing.keys()):
            s = staffing[hour_slot]
            tea_flag = "煮茶" if s["tea_brewing"] else ""
            lines.append(
                f"| {hour_slot}:00 | {s['cups']} | {s['required_front_staff']} | "
                f"{tea_flag} | {s['delivery_count']} | {s['delivery_hours']} |"
            )
        lines.append("")
    if not any_data:
        lines.append("（目前沒有任何店有 `raw_hourly_pattern_monthly` 資料。）")
        lines.append("")
    return "\n".join(lines)


def build_comparison_section(conn, store_ids, config) -> str:
    lines = [
        "## 3. 實際排班 vs 建議人力比對（跨月彙整平均）",
        "",
        "每個月分別算出當月的建議人力／實際人力，再對這些月份取平均（不是把原始杯數先加總"
        "再算一次），比較貼近「每個月本來就各自排過一次班」的實況。",
        "",
    ]
    any_data = False
    for sid in store_ids:
        hourly_months = {ym for s, ym in get_staffing_periods(conn) if s == sid}
        actual_months = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT substr(business_date, 1, 7) FROM raw_staffing_actual WHERE store_id = ?",
                (sid,),
            ).fetchall()
        }
        common_months = sorted(hourly_months & actual_months)
        if not common_months:
            continue
        any_data = True
        rows = compare_aggregate(conn, config, sid, common_months)
        lines.append(f"### {sid} 店（取樣月份：{'、'.join(common_months)}）")
        lines.append("")
        lines.append("| 時段 | 日均杯數 | 建議人力 | 實際平均人力 | 差異 | 有實際資料的月數 |")
        lines.append("|---|---|---|---|---|---|")
        for row in rows:
            actual_disp = "—" if row["actual"] is None else row["actual"]
            diff_disp = "—" if row["diff"] is None else row["diff"]
            lines.append(
                f"| {row['hour_slot']}:00 | {row['cups']} | {row['recommended']} | "
                f"{actual_disp} | {diff_disp} | {row['months_with_actual']}/{row['months_total']} |"
            )
        lines.append("")
    if not any_data:
        lines.append("（目前沒有任何店同時有時段占比資料跟實際排班資料可以比對。）")
        lines.append("")
    return "\n".join(lines)


def build_daytype_section(conn, store_ids, config) -> str:
    lines = ["## 6. 平日/假日逐時段杯數＋星期幾 x 時段排班眾數", ""]
    any_data = False
    for sid in store_ids:
        all_months = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT year_month FROM raw_hourly_pattern_monthly WHERE store_id = ? ORDER BY 1",
                (sid,),
            ).fetchall()
        ]
        daytype_stats = cup_stats_by_daytype(conn, sid, all_months)
        date_range_row = conn.execute(
            "SELECT MIN(business_date), MAX(business_date) FROM raw_staffing_actual WHERE store_id = ?",
            (sid,),
        ).fetchone()

        has_daytype = daytype_stats["平日"] or daytype_stats["假日"]
        has_roster = date_range_row[0] is not None
        if not has_daytype and not has_roster:
            continue
        any_data = True
        lines.append(f"### {sid} 店")
        lines.append("")

        if has_daytype:
            lines.append("**平日/假日逐時段杯數**（「假日」是真實星期六/日單日樣本平均，「平日」是月彙總代數反推，不是估計值）")
            lines.append("")
            for label in ("平日", "假日"):
                rows = daytype_stats[label]
                if not rows:
                    continue
                lines.append(f"_{label}_")
                lines.append("")
                lines.append("| 時段 | 平均杯數 | 最大值 | 最小值 | 月數 |")
                lines.append("|---|---|---|---|---|")
                for r in rows:
                    lines.append(
                        f"| {r['時段']}:00 | {r['平均杯數']} | {r['最大值']} | {r['最小值']} | {r['月數']} |"
                    )
                lines.append("")

        if has_roster:
            roster_rows = roster_mode_by_weekday(conn, sid, date_range_row[0], date_range_row[1], config)
            if roster_rows:
                lines.append(f"**星期幾 x 時段實際排班人力眾數**（取樣範圍 {date_range_row[0]} ~ {date_range_row[1]}）")
                lines.append("")
                cols = list(roster_rows[0].keys())
                lines.append("| " + " | ".join(cols) + " |")
                lines.append("|" + "---|" * len(cols))
                for row in roster_rows:
                    lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
                lines.append("")
    if not any_data:
        lines.append("（目前沒有任何店有假日單日樣本或實際排班原始資料。）")
        lines.append("")
    return "\n".join(lines)


def main():
    conn = _conn()
    store_ids = _stores(conn)
    staffing_config = load_staffing_config()
    cost_config = load_cost_config()

    sections = [
        f"# 月度總報告（{date.today().isoformat()} 產出）",
        "",
        "這是把系統目前所有分析彙整成的單一報告，跑完一次 `monthly-ops-refresh` 之後看這份就好，"
        "不用再分開找檔案。月盈虧／排班建議／實際vs建議比對這三段有網頁互動版可以調參數即時試算，"
        "這份報告只收錄目前存檔預設值的快照。完整導覽說明見 `reports/00_報表總覽.md`。",
        "",
        "---",
        "",
        build_pnl_section(conn, store_ids),
        "---",
        "",
        build_staffing_recommendation_section(conn, store_ids, staffing_config),
        "---",
        "",
        build_comparison_section(conn, store_ids, staffing_config),
        "---",
        "",
    ]

    lines_str = "\n".join(sections)

    lines_str += "\n## 4. 排班人力成本估算\n\n"
    cost_stats_by_store = {}
    for sid, year_month in get_cost_periods(conn):
        s = compute_store_stats(conn, sid, year_month, staffing_config, cost_config)
        if s is not None:
            cost_stats_by_store[sid] = s
    if cost_stats_by_store:
        cost_report = build_cost_report(conn, staffing_config, cost_stats_by_store)
        # 去掉 build_cost_report 自帶的一級標題，改用這裡統一的章節編號
        cost_report = "\n".join(cost_report.splitlines()[2:])
        lines_str += _demote_headers(cost_report)
        persist_staffing_insights(conn, cost_stats_by_store)
    else:
        lines_str += "（目前沒有任何店有實際排班資料可以估算成本。）\n"
    lines_str += "\n\n---\n\n"

    lines_str += "## 5. 營運報告（通路組合／客單價／尖峰時段／回頭客／星期幾彙整）\n\n"
    ops_stats_by_store = _compute_operations(conn, store_ids)
    ops_report = build_operations_report(conn, store_ids, ops_stats_by_store)
    ops_report = "\n".join(ops_report.splitlines()[2:])
    lines_str += _demote_headers(ops_report)
    persist_operational_insights(conn, ops_stats_by_store)
    lines_str += "\n\n---\n\n"

    lines_str += build_daytype_section(conn, store_ids, staffing_config)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"月度總報告_{date.today().isoformat()}.md"
    out_path.write_text(lines_str, encoding="utf-8")
    print(f"月度總報告已寫入 {out_path}")
    conn.close()


if __name__ == "__main__":
    main()
