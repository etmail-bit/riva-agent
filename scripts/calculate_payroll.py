#!/usr/bin/env python3
"""薪資計算：用真實排班資料（raw_staffing_actual）＋薪資結構（staffing_rules.json）＋
2026年官方勞健保/勞退級距費率（insurance_rates_2026.json），算出每位員工當月「公司總負擔成本」。

方法論（2026-07-13 使用者確認的範圍）：
    只算「公司要付多少」，不算「員工實際拿到手上多少」（員工自付額暫不計，之後有需要再加）。
    只算員工本人的保費，不含眷屬——但健保的公司負擔那 60% 依法要內含「全國平均眷屬人數」
    （目前 0.56 人），這是法定機制、全體公司通用，不是這個員工本人有沒有真的申報眷屬的問題，
    也不會轉嫁給員工，員工自付的 30% 一律只算本人。

月投保薪資的認定：
    正職（manager/staff）：直接用 staffing_rules.json 的固定月薪（不含加班費，加班費本身
    是變動的，不計入投保薪資基礎，這是常見的小型企業簡化做法，跟法定「應含經常性薪資」
    有些微出入，先用這個版本，之後有需要再校準）。
    兼職（part_time）：用當月實際工時 × 時薪算出當月實際收入，當作投保薪資基礎。

    勞保／職災保險：兼職月收入未達 12,540 元時，套用勞保局公告的「部分工時」特例
    （11,100/12,540 兩級，見 insurance_rates_2026.json 的 part_time_low_income_rule）；
    12,540~29,500（一般表最低級距）之間改查跟 pension 共用的細分級距表（源自勞保局同一份
    公告備註，2026-07-13 用真實資料測試時發現第一版直接跳到 29,500 會不合理墊高兼職成本，
    修正成這段平滑銜接，不是跳躍式的）；超過 29,500 就跟正職一樣查一般分級表。
    健保：沒有這條特例，一律直接查健保完整分級表（本身最低就是 29,500 元封底，任何低收入
    都會被墊到這個法定下限，這是法律本身的設計、不是我們的簡化）。
    勞退：也沒有特例，分級表本身細到 1,500 元起，直接查即可對應到兼職員工的真實低收入級距。

四項保險金額都是照公式（投保薪資 × 費率 × 負擔比例，四捨五入到元，勞保/職災保險比照官方做法把
兩個子費率分開算再相加，見 calc_labor_insurance/calc_occupational_injury）計算，不是照抄
勞保局/健保署逐級距已經算好的官方金額表——已用官方公告的三個資料點（11,100/29,500/45,800）
逐一核對過，勞保/健保/勞退三項金額都完全吻合；職災保險目前沒有可核對的官方逐點金額，用同一套
已驗證過的捨入邏輯類推。這份報表是給經營者做人事成本規劃用，不是拿去申報用的正式金額。

職業災害保險費率目前用政府公告「住宿及餐飲業」大類 0.19%（insurance_rates_2026.json 可調），
使用者尚未拿真實繳款單核對過，2026-07-13 先用這個當預設值。

只吃本機 Layer 1 原始排班明細（raw_staffing_actual），含真實薪資金額，只存在本機 db，
不上雲端、不寫進資料庫（比照 estimate_staffing_cost.py 的模式，只印報表）。

用法（注意用 -m 模組執行，這支腳本會 import 同目錄下的 estimate_staffing_cost）：
    source .venv/bin/activate
    python3 -m scripts.calculate_payroll
"""
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from scripts.calculate_staffing import load_config as load_staffing_config
from scripts.estimate_staffing_cost import actual_hours_by_employee_day

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
INSURANCE_CONFIG_PATH = ROOT / "config" / "insurance_rates_2026.json"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_insurance_config():
    return json.loads(INSURANCE_CONFIG_PATH.read_text(encoding="utf-8"))


def get_periods(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id, substr(business_date, 1, 7) AS year_month "
        "FROM raw_staffing_actual ORDER BY store_id, year_month"
    ).fetchall()
    return [(r["store_id"], r["year_month"]) for r in rows]


def lookup_bracket(wage: float, brackets: list) -> int:
    """一般分級表查表：wage 落在第一個 wage_ceiling >= wage 的級距就用那一級的投保金額；
    超過表上最高級距（wage_ceiling 為 null 那一格）固定封頂用那一級的投保金額，不會查表落空。"""
    for b in brackets:
        if b["wage_ceiling"] is None or wage <= b["wage_ceiling"]:
            return b["insured_amount"]
    return brackets[-1]["insured_amount"]


def _part_time_floor(wage: float, insurance_config: dict) -> int | None:
    """部分工時勞工的勞保/職災保險投保薪資判定，比照勞保局「勞工保險投保薪資分級表」
    備註三：月收入未達 12,540 元時固定用 11,100 或 12,540 元；超過 12,540 元則依備註三
    轉介的備註二（原本是給職業訓練機構受訓者用的細分級距表，11,101~28,590 那段跟
    pension 分級表共用同一組官方數字）查表，直到超過 28,590 元才改查一般分級表
    （一般表最低就是 29,500）。2026-07-13 修正：第一版直接讓超過 12,540 元的兼職員工
    跳去查一般表（等於直接墊高到 29,500），漏看了備註三其實是轉介備註二這段細分級距，
    用真實資料測試時發現一位兼職月收入 18,816 元的員工被不合理地拉到 29,500 元計算才抓到。
    回傳 None 代表月收入已經超過這整段特例（>= 一般表最低級距），改用一般分級表查。"""
    rule = insurance_config["part_time_low_income_rule"]
    if wage <= rule["floor_1_ceiling"]:
        return rule["floor_1"]
    if wage <= rule["floor_2_ceiling"]:
        return rule["floor_2"]
    general_floor = insurance_config["labor_insurance"]["brackets"][0]["wage_ceiling"]
    if wage < general_floor:
        return lookup_bracket(wage, insurance_config["pension"]["brackets"])
    return None


def determine_labor_insured_wage(wage: float, role: str, insurance_config: dict) -> int:
    if role == "part_time":
        floor = _part_time_floor(wage, insurance_config)
        if floor is not None:
            return floor
    return lookup_bracket(wage, insurance_config["labor_insurance"]["brackets"])


def determine_occupational_injury_insured_wage(wage: float, role: str, insurance_config: dict) -> int:
    if role == "part_time":
        floor = _part_time_floor(wage, insurance_config)
        if floor is not None:
            return floor
    return lookup_bracket(wage, insurance_config["occupational_injury_insurance"]["brackets"])


def _round(x) -> int:
    """比照勞保局/健保署官方金額表的捨入慣例：四捨五入到元（ROUND_HALF_UP），
    不是 Python 內建 round() 的銀行家捨入——用官方三個資料點反推確認過，
    差在 .5 這個臨界值兩者捨入方向可能不同，見 calculate_payroll 模組說明。"""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_labor_insurance(insured_wage: int, insurance_config: dict) -> dict:
    """普通事故(11.5%)、就業保險(1%)分開算、各自四捨五入後再相加，不是直接用
    合計12.5%算一次——這是用官方三個資料點逐一核對後才發現的，兩種算法在
    某些級距會差1元，只有分開算才會跟官方金額完全對上。"""
    c = insurance_config["labor_insurance"]
    totals = {party: 0 for party in c["split"]}
    for rate in (c["ordinary_rate"], c["employment_insurance_rate"]):
        sub_total = insured_wage * rate
        for party, ratio in c["split"].items():
            totals[party] += _round(sub_total * ratio)
    return totals


def calc_occupational_injury(insured_wage: int, insurance_config: dict) -> dict:
    """行業別費率、上下班災害費率分開算、各自四捨五入後再相加，比照 calc_labor_insurance
    同一套邏輯（沒有官方金額表可以逐點核對職災保險，但兩者同屬勞保局同一份規費，
    用已驗證過的勞保捨入方式比較穩妥，見模組說明）。"""
    c = insurance_config["occupational_injury_insurance"]
    totals = {party: 0 for party in c["split"]}
    for rate in (c["industry_rate"], c["commute_rate"]):
        sub_total = insured_wage * rate
        for party, ratio in c["split"].items():
            totals[party] += _round(sub_total * ratio)
    return totals


def calc_health_insurance(insured_wage: int, insurance_config: dict) -> dict:
    """員工自付只算本人；公司負擔依法要內含全國平均眷屬人數（見模組說明），
    不是員工本人有沒有真的申報眷屬的問題，也不會轉嫁給員工。"""
    c = insurance_config["health_insurance"]
    base = insured_wage * c["rate"]
    employee = _round(base * c["split"]["employee"])
    employer = _round(base * c["split"]["employer"] * (1 + c["employer_avg_dependents_multiplier"]))
    return {"employee": employee, "employer": employer}


def calc_pension(insured_wage: int, insurance_config: dict) -> dict:
    c = insurance_config["pension"]
    return {"employer": _round(insured_wage * c["rate"])}


def resolve_role(employee_code: str, year_month: str, staffing_config: dict) -> str | None:
    """員工角色查詢：先查 employee_role_overrides_by_month（處理角色隨月份變動的情況，
    例如某人某幾個月是正職、後來轉兼職），查無才 fallback 用 employee_roles 的「現在」角色。
    2026-07-14 新增，起因是謄打 3、4 月班表時發現「方」那兩個月其實是正職，跟現在的
    part_time 設定不一樣，若不處理會讓那兩個月的薪資算成兼職時薪制，金額差很多。"""
    overrides = staffing_config.get("employee_role_overrides_by_month", {})
    override = overrides.get(employee_code, {}).get(year_month)
    if override is not None:
        return override
    return staffing_config["employee_roles"].get(employee_code)


def monthly_pay_basis(conn, store_id, year_month, staffing_config) -> dict:
    """回傳 {employee_code: {"total_hours":.., "overtime_hours":.., "days_worked":..}}——
    跟 estimate_staffing_cost.py 的 actual_cost() 共用同一份「當天總排班時數」查詢，
    只是這裡要拆到「逐員工」顆粒度，不是整店彙總，所以重新聚合一次。"""
    by_day = actual_hours_by_employee_day(conn, store_id, year_month)
    regular_hours = staffing_config["wages"]["daily_regular_hours"]

    result = {}
    for (emp, _day), hrs in by_day.items():
        row = result.setdefault(emp, {"total_hours": 0.0, "overtime_hours": 0.0, "days_worked": 0})
        row["total_hours"] += hrs
        row["days_worked"] += 1
        if resolve_role(emp, year_month, staffing_config) in ("manager", "staff"):
            row["overtime_hours"] += max(0.0, hrs - regular_hours)
    return result


def calculate_employee_payroll(employee_code: str, role: str, monthly_basis: dict,
                                staffing_config: dict, insurance_config: dict) -> dict:
    wages = staffing_config["wages"]
    if role in ("manager", "staff"):
        base_pay = wages["manager_monthly"] if role == "manager" else wages["staff_monthly"]
        overtime_pay = monthly_basis["overtime_hours"] * wages["overtime_hourly"]
    elif role == "part_time":
        base_pay = monthly_basis["total_hours"] * wages["part_time_hourly"]
        overtime_pay = 0.0
    else:
        raise ValueError(f"未知角色：{role}")

    insured_wage_basis = base_pay  # 投保薪資基礎不含加班費，見模組說明

    labor_wage = determine_labor_insured_wage(insured_wage_basis, role, insurance_config)
    occ_wage = determine_occupational_injury_insured_wage(insured_wage_basis, role, insurance_config)
    health_wage = lookup_bracket(insured_wage_basis, insurance_config["health_insurance"]["brackets"])
    pension_wage = lookup_bracket(insured_wage_basis, insurance_config["pension"]["brackets"])

    labor = calc_labor_insurance(labor_wage, insurance_config)
    occ = calc_occupational_injury(occ_wage, insurance_config)
    health = calc_health_insurance(health_wage, insurance_config)
    pension = calc_pension(pension_wage, insurance_config)

    employer_insurance_total = labor["employer"] + occ["employer"] + health["employer"] + pension["employer"]
    company_total_cost = base_pay + overtime_pay + employer_insurance_total

    return {
        "employee_code": employee_code,
        "role": role,
        "days_worked": monthly_basis["days_worked"],
        "total_hours": round(monthly_basis["total_hours"], 1),
        "base_pay": round(base_pay),
        "overtime_pay": round(overtime_pay),
        "labor_insurance_employer": labor["employer"],
        "occupational_injury_employer": occ["employer"],
        "health_insurance_employer": health["employer"],
        "pension_employer": pension["employer"],
        "employer_insurance_total": employer_insurance_total,
        "company_total_cost": round(company_total_cost),
    }


def build_payroll_report(conn, store_id, year_month, staffing_config, insurance_config):
    basis = monthly_pay_basis(conn, store_id, year_month, staffing_config)

    rows, unmapped = [], []
    for emp, mb in sorted(basis.items()):
        role = resolve_role(emp, year_month, staffing_config)
        if role is None:
            unmapped.append(emp)
            continue
        rows.append(calculate_employee_payroll(emp, role, mb, staffing_config, insurance_config))
    return rows, unmapped


def _stores(conn):
    return [r[0] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id")]


def build_payroll_section(conn, store_ids, staffing_config, insurance_config) -> str:
    """跟 estimate_staffing_cost.py 的 build_report() 同一個模式：回傳 markdown 文字，
    CLI（main()）跟 generate_full_report.py 共用同一份，不重複算兩次也不會兩邊產出對不起來。"""
    overtime_rate = staffing_config["wages"]["overtime_hourly"]
    lines = [
        "## 7. 薪資計算（正職/兼職，公司總負擔成本）",
        "",
        f"公司總負擔成本 = 底薪/工資 ＋ 加班費（正職超過每日 {staffing_config['wages']['daily_regular_hours']} "
        f"小時的部分，{overtime_rate} 元/小時，依真實排班表逐日核算，不是估計值）＋ 勞保／職災保險／健保／"
        "勞退雇主負擔（依 2026 年官方級距費率計算，見 `config/insurance_rates_2026.json`）。"
        "不含員工自付額，也不是拿去申報用的正式金額，數字可能跟官方金額表有 ±1 元等級的捨入落差，"
        "詳見 `scripts/calculate_payroll.py` 模組說明。",
        "",
    ]
    for sid in store_ids:
        periods = sorted(ym for s, ym in get_periods(conn) if s == sid)
        if not periods:
            lines.append(f"### {sid} 店")
            lines.append("")
            lines.append("（目前沒有排班原始資料，無法計算真實薪資。）")
            lines.append("")
            continue
        for year_month in periods:
            rows, unmapped = build_payroll_report(conn, sid, year_month, staffing_config, insurance_config)
            lines.append(f"### {sid} 店 {year_month}")
            lines.append("")
            if unmapped:
                lines.append(f"⚠️ 員工代碼 {'、'.join(sorted(unmapped))} 沒有設定角色，未列入下表。")
                lines.append("")
            if not rows:
                lines.append("（沒有已對應角色的員工資料。）")
                lines.append("")
                continue
            lines.append(
                "| 代碼 | 角色 | 天數 | 時數 | 底薪/工資 | 加班費 | 勞保(公司) | "
                "職災(公司) | 健保(公司) | 勞退(公司) | 公司總成本 |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            total_cost = 0
            for r in rows:
                lines.append(
                    f"| {r['employee_code']} | {r['role']} | {r['days_worked']} | {r['total_hours']} | "
                    f"{r['base_pay']:,} | {r['overtime_pay']:,} | {r['labor_insurance_employer']:,} | "
                    f"{r['occupational_injury_employer']:,} | {r['health_insurance_employer']:,} | "
                    f"{r['pension_employer']:,} | {r['company_total_cost']:,} |"
                )
                total_cost += r["company_total_cost"]
            lines.append(f"| **合計** |  |  |  |  |  |  |  |  |  | **{total_cost:,}** |")
            lines.append("")
    return "\n".join(lines)


def print_report(store_id, year_month, rows, unmapped):
    print(f"\n=== {store_id} 店 {year_month} 薪資計算（公司總負擔成本，未扣員工自付額） ===\n")
    if unmapped:
        print(f"⚠️ 員工代碼 {'、'.join(sorted(unmapped))} 沒有設定角色，未列入下表。\n")

    header = (f"{'代碼':<4}{'角色':<8}{'天數':>4}{'時數':>7}{'底薪/工資':>10}{'加班費':>8}"
              f"{'勞保':>7}{'職災':>6}{'健保':>7}{'勞退':>7}{'公司總成本':>10}")
    print(header)
    print("-" * len(header))

    total_cost = 0
    for r in rows:
        print(
            f"{r['employee_code']:<4}{r['role']:<8}{r['days_worked']:>4}{r['total_hours']:>7}"
            f"{r['base_pay']:>10,}{r['overtime_pay']:>8,}"
            f"{r['labor_insurance_employer']:>7,}{r['occupational_injury_employer']:>6,}"
            f"{r['health_insurance_employer']:>7,}{r['pension_employer']:>7,}"
            f"{r['company_total_cost']:>10,}"
        )
        total_cost += r["company_total_cost"]
    print("-" * len(header))
    print(f"{'合計':<4}{'':<8}{'':>4}{'':>7}{'':>10}{'':>8}{'':>7}{'':>6}{'':>7}{'':>7}{total_cost:>10,}")


def main():
    """CLI 除錯用進入點：只印到終端機，不寫檔、不寫資料庫（比照 estimate_staffing_cost.py）。"""
    staffing_config = load_staffing_config()
    insurance_config = load_insurance_config()
    conn = _conn()

    for store_id, year_month in get_periods(conn):
        rows, unmapped = build_payroll_report(conn, store_id, year_month, staffing_config, insurance_config)
        if rows or unmapped:
            print_report(store_id, year_month, rows, unmapped)


if __name__ == "__main__":
    main()
