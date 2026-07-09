#!/usr/bin/env python3
"""排班人力成本估算：把「實際排班 vs 需求驅動合理人力」的落差，換算成概估金額。

方法論（2026-07-09 使用者確認的薪資結構）：
    正職（店長/店員）領固定月薪，每天 daily_regular_hours 小時內排班不會增加變動成本；
    超過的部分算加班，跟兼職一樣以時薪計（wages.overtime_hourly）。
    「合理人力」= 每小時 max(capacity.min_floor_staff, ceil(當時杯量 / 單位產能))——
    純杯量算出來的建議人力離峰時段可能只有 1 人，但現場至少要有人顧收銀＋出杯，
    2 人是操作面下限，不是杯量算出來的，避免把不切實際的「1 人顧店」當基準。
    可省下的變動成本，取「總落差時數」跟「實際加班＋兼職時數」兩者的較小值——
    不會把正職的固定底薪時數也算進「可省」範圍，那筆錢不會因為調班表就消失。

只吃本機 Layer 1 原始排班明細（raw_staffing_actual）／raw_hourly_pattern_monthly，
輸出含真實金額，寫進 reports/（已被 .gitignore 排除），不進版控、不上雲端。

用法（注意用 -m 模組執行，這支腳本會 import 同目錄下的 calculate_staffing）：
    source .venv/bin/activate
    python3 -m scripts.estimate_staffing_cost
"""
import math
import sqlite3
from datetime import date
from pathlib import Path

from scripts.calculate_pnl import get_fixed_cost
from scripts.calculate_pnl import load_config as load_cost_config
from scripts.calculate_staffing import get_hourly_data, load_config

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
REPORTS_DIR = ROOT / "reports"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_periods(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id, substr(business_date, 1, 7) AS year_month "
        "FROM raw_staffing_actual ORDER BY store_id, year_month"
    ).fetchall()
    return [(r["store_id"], r["year_month"]) for r in rows]


def actual_hours_by_employee_day(conn, store_id, year_month):
    """回傳 {(employee_code, business_date): 當天總排班時數}——同一人同一天可能有多筆
    拆班紀錄，要先加總才能判斷有沒有超過每天的正職時數門檻。"""
    rows = conn.execute(
        "SELECT employee_code, business_date, SUM(scheduled_hours) AS hrs "
        "FROM raw_staffing_actual "
        "WHERE store_id = ? AND substr(business_date, 1, 7) = ? AND scheduled_hours IS NOT NULL "
        "GROUP BY employee_code, business_date",
        (store_id, year_month),
    ).fetchall()
    return {(r["employee_code"], r["business_date"]): r["hrs"] for r in rows}


def actual_cost(conn, store_id, year_month, config):
    wages = config["wages"]
    roles = config["employee_roles"]
    regular_hours = wages["daily_regular_hours"]

    by_day = actual_hours_by_employee_day(conn, store_id, year_month)
    sampled_days = sorted({d for _, d in by_day})
    n_days = len(sampled_days)

    fixed_employees = set()
    total_hours = 0.0
    variable_hours = 0.0
    unmapped = set()

    for (emp, _day), hrs in by_day.items():
        total_hours += hrs
        role = roles.get(emp)
        if role is None:
            unmapped.add(emp)
            continue
        if role in ("manager", "staff"):
            fixed_employees.add(emp)
            variable_hours += max(0.0, hrs - regular_hours)
        elif role == "part_time":
            variable_hours += hrs

    fixed_monthly_cost = sum(
        wages["manager_monthly"] if roles[e] == "manager" else wages["staff_monthly"]
        for e in fixed_employees
    )

    return {
        "sampled_days": n_days,
        "date_range": (sampled_days[0], sampled_days[-1]) if sampled_days else None,
        "total_hours": total_hours,
        "variable_hours": variable_hours,
        "fixed_employees": fixed_employees,
        "fixed_monthly_cost": fixed_monthly_cost,
        "unmapped_employees": unmapped,
    }


SCENARIOS = ("conservative", "aggressive")
SCENARIO_LABELS = {"conservative": "保守版", "aggressive": "積極版"}


def justified_hours_per_day(conn, store_id, year_month, config, scenario: str):
    """需求驅動的「合理人力」：開店時段每小時取 max(該小時操作下限, 杯量/產能無條件進位)，
    另外加煮茶時段的後場人力（固定算 1 人）。

    兩種情境差在尖峰時段（config.scenario.peak_hours）的操作下限：
    - conservative（保守版）：尖峰用 scenario.peak_floor_staff（例如 3 人）當緩衝，
      因為尖峰杯量常常已經接近全部套用 min_floor_staff 時的產能上限，直接砍到
      離峰同一個下限風險較高（假日/爆單時容易塞車）。
    - aggressive（積極版）：全天一律套用 capacity.min_floor_staff，是純公式算出來的
      理論上限，只當參考基準，不是直接可以照做的排班建議。
    """
    capacity_cfg = config["capacity"]
    hourly_data = get_hourly_data(conn, store_id, year_month)
    if not hourly_data:
        return None

    base_floor = capacity_cfg.get("min_floor_staff", 1)
    capacity = capacity_cfg["cups_per_staff_per_hour"]
    scenario_cfg = config.get("scenario", {})
    peak_hours = set(scenario_cfg.get("peak_hours", []))
    peak_floor = scenario_cfg.get("peak_floor_staff", base_floor)

    open_hours_total = 0.0
    for hour_slot, data in hourly_data.items():
        floor = peak_floor if (scenario == "conservative" and hour_slot in peak_hours) else base_floor
        cups = data["daily_avg_cups"] or 0
        demand_driven = math.ceil(cups / capacity) if cups else 0
        open_hours_total += max(floor, demand_driven)

    prep_hours = config["tea_brewing"]["estimated_duration_hours"]
    return open_hours_total + prep_hours


def compute_store_stats(conn, store_id, year_month, config, cost_config):
    """把逐店的排班成本推算彙整成一個 dict，report 文字跟 pnl_insights 交叉引用的
    結論句共用同一份數字，避免同樣的東西算兩次。scenarios 裡放保守/積極兩版的
    合理人力／落差／可省成本，其餘（實際排班、跟 P&L 預設值的比較）兩版共用。"""
    actual = actual_cost(conn, store_id, year_month, config)
    if actual["sampled_days"] == 0:
        return None

    n = actual["sampled_days"]
    actual_per_day = actual["total_hours"] / n
    variable_per_day = actual["variable_hours"] / n
    rate = config["wages"]["overtime_hourly"]

    scenarios = {}
    for scenario in SCENARIOS:
        justified_per_day = justified_hours_per_day(conn, store_id, year_month, config, scenario)
        if justified_per_day is None:
            return None
        gap_per_day = max(0.0, actual_per_day - justified_per_day)
        avoidable_per_day = min(variable_per_day, gap_per_day)
        scenarios[scenario] = {
            "justified_per_day": justified_per_day,
            "gap_per_day": gap_per_day,
            "avoidable_per_day": avoidable_per_day,
            "avoidable_cost_per_day": avoidable_per_day * rate,
        }

    insurance_pct = cost_config["variable_cost_rates"]["labor_insurance_overhead_pct"]
    schedule_derived_labor_base = actual["fixed_monthly_cost"] + variable_per_day * 30 * rate
    schedule_derived_monthly = schedule_derived_labor_base * (1 + insurance_pct)
    default_labor_base = get_fixed_cost(cost_config, store_id, "labor_base")
    default_monthly = default_labor_base * (1 + insurance_pct)

    return {
        "store_id": store_id,
        "year_month": year_month,
        "actual": actual,
        "n": n,
        "actual_per_day": actual_per_day,
        "variable_per_day": variable_per_day,
        "rate": rate,
        "scenarios": scenarios,
        "schedule_derived_monthly": schedule_derived_monthly,
        "default_monthly": default_monthly,
    }


def build_staffing_summary(s: dict) -> str:
    """濃縮成一段「結論」，供 pnl_insights.py 交叉引用；只放聚合後的推算數字，
    邏輯跟 analyze_operations.py 的 build_operational_summary() 一致。"""
    default_monthly, schedule_monthly = s["default_monthly"], s["schedule_derived_monthly"]
    direction = "低" if schedule_monthly < default_monthly else "高"
    diff = abs(schedule_monthly - default_monthly)
    cons, aggr = s["scenarios"]["conservative"], s["scenarios"]["aggressive"]
    return (
        f"依真實排班反推（樣本 {s['actual']['date_range'][0]}～{s['actual']['date_range'][1]}，"
        f"{s['n']} 天）：平均每天排班 {s['actual_per_day']:.1f} 人-小時。"
        f"估計可省下的變動成本，保守版（尖峰維持緩衝人力）約 "
        f"{cons['avoidable_cost_per_day'] * 30:,.0f} 元/月，積極版（尖峰也砍到操作下限，"
        f"風險較高，僅供參考）約 {aggr['avoidable_cost_per_day'] * 30:,.0f} 元/月。"
        f"換算含勞健保的人事成本約 {schedule_monthly:,.0f} 元/月，"
        f"比系統目前預設概算值（{default_monthly:,.0f} 元/月）{direction}約 {diff:,.0f} 元，"
        "建議之後改用真實排班／薪資單校準，不要只套預設值。"
    )


def build_public_staffing_summary(s: dict) -> str:
    """2026-07-09 使用者確認的公開範圍：只放「預估可節省金額」，不含真實人事成本／
    固定薪資／實際排班時數這些底片數字。這個版本（不是 build_staffing_summary()
    那個完整版）才是 migrate_layer2_to_turso.py 會同步上雲端的內容。"""
    cons, aggr = s["scenarios"]["conservative"], s["scenarios"]["aggressive"]
    return (
        "排班分析：尖峰時段人力配置有調整空間，估計每月可節省變動成本，"
        f"保守版（尖峰維持緩衝人力）約 {cons['avoidable_cost_per_day'] * 30:,.0f} 元，"
        f"積極版（尖峰也砍到操作下限，風險較高，僅供參考）約 "
        f"{aggr['avoidable_cost_per_day'] * 30:,.0f} 元。"
    )


def persist_staffing_insights(conn, stats_by_store: dict) -> None:
    """把濃縮結論寫進 store_staffing_insights，只有這張表存在時才寫
    （雲端 Turso DB 目前沒有這張表，本機以外的呼叫端會直接跳過，不會噴錯）。
    summary_text 是完整版（本機 app.py 專用，含真實人事成本等數字，不可同步上雲端）；
    public_summary_text 是只放預估可節省金額的公開安全版（migrate_layer2_to_turso.py
    只會同步這一欄）。"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='store_staffing_insights'"
    ).fetchone()
    if exists is None:
        return
    for sid, s in stats_by_store.items():
        summary = build_staffing_summary(s)
        public_summary = build_public_staffing_summary(s)
        conn.execute(
            "INSERT INTO store_staffing_insights "
            "(store_id, summary_text, public_summary_text, generated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(store_id) DO UPDATE SET "
            "summary_text = excluded.summary_text, "
            "public_summary_text = excluded.public_summary_text, "
            "generated_at = excluded.generated_at",
            (sid, summary, public_summary),
        )
    conn.commit()


def build_report(conn, config, stats_by_store: dict) -> str:
    scenario_cfg = config.get("scenario", {})
    lines = [f"# 排班人力成本估算（{date.today().isoformat()} 產出）", ""]
    lines.append(
        "方法論：正職（店長/店員）固定月薪不隨班表變動，只有超過每天 "
        f"{config['wages']['daily_regular_hours']} 小時的部分算加班；兼職全額以時薪計。"
        f"「合理人力」= 每小時 max(操作下限, 杯量÷產能無條件進位)——離峰下限是 "
        f"{config['capacity']['min_floor_staff']} 人；尖峰時段（{'、'.join(scenario_cfg.get('peak_hours', []))} 點）"
        f"保守版另外抓 {scenario_cfg.get('peak_floor_staff')} 人當緩衝（尖峰杯量常接近產能上限，"
        "全部砍到離峰下限風險較高），積極版尖峰也套用離峰下限，是純公式理論上限，僅供參考。"
        "可省下的金額，取「總落差時數」跟「實際加班＋兼職時數」兩者較小值——"
        "不會把正職的固定底薪時數也算進可省範圍，那筆錢不會因為調班表就消失。"
    )
    lines.append("")

    for store_id, s in stats_by_store.items():
        actual = s["actual"]
        lines.append(f"## {store_id} 店 {s['year_month']}")
        lines.append("")
        lines.append(
            f"- 取樣範圍：{actual['date_range'][0]} ～ {actual['date_range'][1]}，"
            f"共 {s['n']} 天（原始排班資料謄打進度，不代表整月都有資料）"
        )
        if actual["unmapped_employees"]:
            lines.append(
                f"- ⚠️ 員工代碼 {'、'.join(sorted(actual['unmapped_employees']))} "
                "沒有在 config/staffing_rules.json 的 employee_roles 設定角色，"
                "這些人的時數**沒有**計入下面的金額估算，先補上角色設定才會準。"
            )
        lines.append(f"- 實際總排班：平均每天 {s['actual_per_day']:.1f} 人-小時")
        lines.append(
            f"- 固定月薪成本：{actual['fixed_monthly_cost']:,} 元/月"
            f"（{len(actual['fixed_employees'])} 位正職，不隨班表調整而變）"
        )
        lines.append(
            f"- 實際變動成本（加班＋兼職）：取樣期間平均每天 {s['variable_per_day']:.1f} 小時，"
            f"換算月成本約 {s['variable_per_day'] * 30 * s['rate']:,.0f} 元"
            "（用取樣期間的每日平均外推整月，實際會隨營業天數與排班變動）"
        )
        lines.append("")
        lines.append("  | 情境 | 合理人力(人-小時/天) | 落差(小時/天) | 估計可省(元/月) |")
        lines.append("  |---|---|---|---|")
        for scenario in SCENARIOS:
            sc = s["scenarios"][scenario]
            lines.append(
                f"  | {SCENARIO_LABELS[scenario]} | {sc['justified_per_day']:.1f} | "
                f"{sc['gap_per_day']:.1f} | {sc['avoidable_cost_per_day'] * 30:,.0f} |"
            )
        lines.append("")
        lines.append(
            "  保守版尖峰時段維持緩衝人力，積極版是純公式理論上限——實際能省多少，"
            "還要看怎麼調整班表（例如尖峰三班疊在一起、晚班沒有隨客流量收斂，見 operational_report）。"
        )
        lines.append(
            f"- 換算含勞健保的人事成本約 {s['schedule_derived_monthly']:,.0f} 元/月，"
            f"對照系統目前預設概算值 {s['default_monthly']:,.0f} 元/月"
            f"（{'低' if s['schedule_derived_monthly'] < s['default_monthly'] else '高'}約 "
            f"{abs(s['schedule_derived_monthly'] - s['default_monthly']):,.0f} 元）。"
        )
        lines.append("")

    return "\n".join(lines)


def main():
    config = load_config()
    cost_config = load_cost_config()
    conn = _conn()

    stats_by_store = {}
    for store_id, year_month in get_periods(conn):
        s = compute_store_stats(conn, store_id, year_month, config, cost_config)
        if s is not None:
            stats_by_store[store_id] = s

    report = build_report(conn, config, stats_by_store)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"staffing_cost_estimate_{date.today().isoformat()}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"排班人力成本估算已寫入 {out_path}")
    print()
    print(report)

    persist_staffing_insights(conn, stats_by_store)
    print("已把濃縮結論寫入 store_staffing_insights（給月盈虧頁交叉引用用）。")


if __name__ == "__main__":
    main()
