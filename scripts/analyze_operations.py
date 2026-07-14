#!/usr/bin/env python3
"""營運報告：分析 Layer 1 原始明細（發票/營收月報/收銀機明細/時段占比），
找兩店的優缺點與提升營業額的操作面建議。

前提（使用者 2026-07-09 確認）：這是加盟店，**不能自行調價、原物料成本也是
固定的**，所以這裡的建議一律只談「操作面槓桿」（通路組合、尖峰時段人力配置、
客單價提升手法），不談調價或砍原物料成本。

只吃 Layer 1 原始明細（raw_invoice_transactions／raw_revenue_monthly／
raw_cash_register_daily／raw_hourly_pattern_monthly），這些表格依零洩漏原則
只存在本機 db/riva_agent.db，這支腳本跟輸出報告都刻意不會被任何雲端功能引用。

`build_report()` 回傳報告文字，由 `scripts/generate_full_report.py` 彙整進單一份
`reports/月度總報告_<日期>.md`（2026-07-13 起不再自己寫出獨立檔案）；`main()` 只在
CLI 除錯時印到終端機用。

用法：
    source .venv/bin/activate
    python3 -m scripts.analyze_operations
"""
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

from scripts.analyze_staffing_daytype import HOUR_SLOTS, WEEKDAY_NAMES

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"

# 通路分類：外送平台會被抽 35% 佣金（見 config/cost_rates.json），
# 自取/外帶沒有這筆隱形成本，這是通路組合分析的核心切點。
DELIVERY_ORDER_TYPES = {"UE外送", "FP外送", "街口外送", "你訂外送", "外送"}
PICKUP_ORDER_TYPES = {"自取", "外帶", "UE自取", "FP自取", "你訂自取"}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def channel_mix(conn, store_id):
    rows = conn.execute(
        "SELECT order_type, SUM(amount) AS amt FROM raw_revenue_monthly "
        "WHERE store_id = ? AND order_type != 'MONTHLY_TOTAL' GROUP BY order_type",
        (store_id,),
    ).fetchall()
    total = sum(r["amt"] for r in rows) or 1
    delivery = sum(r["amt"] for r in rows if r["order_type"] in DELIVERY_ORDER_TYPES)
    pickup = sum(r["amt"] for r in rows if r["order_type"] in PICKUP_ORDER_TYPES)
    other = total - delivery - pickup
    breakdown = sorted(
        [(r["order_type"], r["amt"], r["amt"] / total) for r in rows],
        key=lambda x: -x[1],
    )
    return {
        "total": total,
        "delivery_pct": delivery / total,
        "pickup_pct": pickup / total,
        "other_pct": other / total,
        "breakdown": breakdown,
    }


def invoice_stats(conn, store_id):
    amounts = sorted(
        r[0]
        for r in conn.execute(
            "SELECT amount FROM raw_invoice_transactions WHERE store_id = ? AND tx_status = '正常'",
            (store_id,),
        )
    )
    n = len(amounts)
    if n == 0:
        return None
    return {
        "n": n,
        "avg": sum(amounts) / n,
        "median": amounts[n // 2],
        "p25": amounts[n // 4],
        "p75": amounts[3 * n // 4],
    }


def hourly_channel_by_weekday(conn, store_id):
    """星期幾（一~日）x 時段：日均發票張數與日均營業額（2026-07-13 新增，供排班人力
    圖表交叉比對用）。跟 analyze_staffing_daytype.roster_mode_by_weekday() 同一套
    「用每筆交易的真實日期回推日曆星期幾」方法，來源是 raw_invoice_transactions
    （只算 tx_status='正常'，排除作廢發票）——這是目前系統裡唯一「逐日、涵蓋一~日
    七天」都有真實時間戳記的資料。杯數本身沒有這個顆粒度（cup_stats_by_daytype()
    只能拆到平日/假日兩組，見該函式說明），所以這裡用發票張數當杯量的替代指標；
    營業額則是直接量到的真實金額，不是替代值。

    回傳每個時段一筆 dict：{時段, 星期一_發票張數, 星期一_營業額, 星期一_樣本天數, ...}，
    「發票張數」「營業額」都是「同一星期幾、同一時段」在取樣範圍內的日均值（先把每天
    彙總成當天這個時段的總張數/總金額，再依星期幾分組取平均），不是取樣期間的總和。
    """
    rows = conn.execute(
        "SELECT substr(tx_time, 1, 10) AS biz_date, substr(tx_time, 12, 2) AS hour_slot, amount "
        "FROM raw_invoice_transactions WHERE store_id = ? AND tx_status = '正常'",
        (store_id,),
    ).fetchall()
    if not rows:
        return []

    daily_cell = defaultdict(lambda: {"count": 0, "amount": 0})
    for r in rows:
        cell = daily_cell[(r["biz_date"], r["hour_slot"])]
        cell["count"] += 1
        cell["amount"] += r["amount"]

    cell_samples = {wd: {h: {"count": [], "amount": []} for h in HOUR_SLOTS} for wd in WEEKDAY_NAMES}
    for (biz_date, hour_slot), agg in daily_cell.items():
        if hour_slot not in HOUR_SLOTS:
            continue
        wd_name = WEEKDAY_NAMES[date.fromisoformat(biz_date).weekday()]
        cell_samples[wd_name][hour_slot]["count"].append(agg["count"])
        cell_samples[wd_name][hour_slot]["amount"].append(agg["amount"])

    result = []
    for hour_slot in HOUR_SLOTS:
        row = {"時段": hour_slot}
        for wd in WEEKDAY_NAMES:
            counts = cell_samples[wd][hour_slot]["count"]
            amounts = cell_samples[wd][hour_slot]["amount"]
            row[f"星期{wd}_發票張數"] = round(sum(counts) / len(counts), 1) if counts else None
            row[f"星期{wd}_營業額"] = round(sum(amounts) / len(amounts)) if amounts else None
            row[f"星期{wd}_樣本天數"] = len(counts)
        result.append(row)
    return result


def weekday_daily_summary(conn, store_id):
    """星期幾（一~日）彙整：每日總營業額／總發票張數的中位數、最大值、最小值
    （2026-07-13 新增，回應使用者「哪個星期幾表現特別好/特別差、異常日是哪天」的問題）。
    跟 hourly_channel_by_weekday() 不同顆粒度：那支是「星期幾 x 時段」的日均值，
    這支是先把每天彙總成當天一個總數，再依星期幾分組取統計值，並保留最大/最小值
    發生的實際日期，方便回頭查當天是否有連假、天氣、設備故障等特殊事件。

    回傳每個星期幾一筆 dict：{星期, 天數, 營業額中位數, 營業額最小, 營業額最小日期,
    營業額最大, 營業額最大日期, 發票中位數, 發票最小, 發票最小日期, 發票最大, 發票最大日期}。
    """
    rows = conn.execute(
        "SELECT substr(tx_time, 1, 10) AS biz_date, amount "
        "FROM raw_invoice_transactions WHERE store_id = ? AND tx_status = '正常'",
        (store_id,),
    ).fetchall()
    if not rows:
        return []

    daily = defaultdict(lambda: [0, 0])  # biz_date -> [revenue, count]
    for r in rows:
        cell = daily[r["biz_date"]]
        cell[0] += r["amount"]
        cell[1] += 1

    by_weekday = {wd: [] for wd in WEEKDAY_NAMES}
    for biz_date, (revenue, count) in daily.items():
        wd_name = WEEKDAY_NAMES[date.fromisoformat(biz_date).weekday()]
        by_weekday[wd_name].append((biz_date, revenue, count))

    result = []
    for wd in WEEKDAY_NAMES:
        items = by_weekday[wd]
        if not items:
            continue
        revs = sorted(items, key=lambda x: x[1])
        cnts = sorted(items, key=lambda x: x[2])
        n = len(items)
        result.append({
            "星期": wd,
            "天數": n,
            "營業額中位數": round(statistics.median(x[1] for x in items)),
            "營業額最小": revs[0][1], "營業額最小日期": revs[0][0],
            "營業額最大": revs[-1][1], "營業額最大日期": revs[-1][0],
            "發票中位數": statistics.median(x[2] for x in items),
            "發票最小": cnts[0][2], "發票最小日期": cnts[0][0],
            "發票最大": cnts[-1][2], "發票最大日期": cnts[-1][0],
        })
    return result


def repeat_customer_stats(conn, store_id):
    """回頭客分析（2026-07-10 新增）。carrier_no（手機載具號碼）視同客戶個資等級的識別碼，
    只在這裡當 GROUP BY 的內部 key 用，從頭到尾不把原始 carrier_no 存進回傳值或報告——
    只回傳聚合後的統計數字（總客數、回訪分布、回頭客營收佔比），不留任何可以反查回
    單一顧客的中間產物，符合零洩漏原則。"""
    rows = conn.execute(
        "SELECT carrier_no, substr(tx_time,1,10) AS biz_date, substr(tx_time,1,7) AS ym, amount "
        "FROM raw_invoice_transactions WHERE store_id = ? AND tx_status = '正常' AND carrier_no IS NOT NULL",
        (store_id,),
    ).fetchall()
    if not rows:
        return None

    visits, spend = {}, {}
    by_month = {}
    for r in rows:
        visits.setdefault(r["carrier_no"], set()).add(r["biz_date"])
        spend[r["carrier_no"]] = spend.get(r["carrier_no"], 0) + r["amount"]
        by_month.setdefault(r["ym"], set()).add(r["carrier_no"])

    total_customers = len(visits)
    repeat_ids = {cid for cid, dates in visits.items() if len(dates) >= 2}
    repeat_revenue = sum(spend[cid] for cid in repeat_ids)
    total_revenue = sum(spend.values())

    months_sorted = sorted(by_month)
    seen = set()
    monthly_trend = []  # [{year_month, returning_pct}, ...]，第一個月沒有歷史可比，returning_pct=None
    for ym in months_sorted:
        this_month = by_month[ym]
        returning_pct = (len(this_month & seen) / len(this_month) * 100) if seen else None
        monthly_trend.append({"year_month": ym, "returning_pct": returning_pct})
        seen |= this_month
    latest_returning_pct = monthly_trend[-1]["returning_pct"] if monthly_trend else None

    bucket_labels = ["1次", "2次", "3~5次", "6~10次", "11次以上"]
    visit_buckets = {label: 0 for label in bucket_labels}
    for cid, dates in visits.items():
        n = len(dates)
        if n == 1:
            visit_buckets["1次"] += 1
        elif n == 2:
            visit_buckets["2次"] += 1
        elif n <= 5:
            visit_buckets["3~5次"] += 1
        elif n <= 10:
            visit_buckets["6~10次"] += 1
        else:
            visit_buckets["11次以上"] += 1

    return {
        "total_customers": total_customers,
        "repeat_pct": len(repeat_ids) / total_customers * 100,
        "repeat_revenue_pct": (repeat_revenue / total_revenue * 100) if total_revenue else 0,
        "latest_month": months_sorted[-1] if months_sorted else None,
        "latest_returning_pct": latest_returning_pct,
        "first_month": months_sorted[0] if months_sorted else None,
        "visit_buckets": visit_buckets,
        "monthly_trend": monthly_trend,
    }


def peak_hours(conn, store_id, top_n=5):
    rows = conn.execute(
        "SELECT hour_slot, AVG(daily_avg_sales) AS avg_sales, AVG(daily_avg_cups) AS avg_cups "
        "FROM raw_hourly_pattern_monthly WHERE store_id = ? "
        "GROUP BY hour_slot ORDER BY avg_sales DESC LIMIT ?",
        (store_id, top_n),
    ).fetchall()
    return [(r["hour_slot"], r["avg_sales"], r["avg_cups"]) for r in rows]


def _compute_all(conn, store_ids) -> dict:
    return {
        sid: {
            "mix": channel_mix(conn, sid),
            "inv": invoice_stats(conn, sid),
            "peaks": peak_hours(conn, sid),
            "repeat": repeat_customer_stats(conn, sid),
            "weekday_summary": weekday_daily_summary(conn, sid),
        }
        for sid in store_ids
    }


def build_operational_summary(data: dict) -> str:
    """把單店的通路/客單價/尖峰時段數字濃縮成一兩句「結論」，供 pnl_insights.py
    的「各店經營現況」段落交叉引用。刻意只保留聚合統計（佔比/中位數/時段），
    不含逐筆明細，是唯一之後可能考慮同步上雲端的內容。"""
    mix, inv, peaks, repeat = data["mix"], data["inv"], data["peaks"], data.get("repeat")
    parts = [f"通路組合上外送平台佔營收 {mix['delivery_pct']*100:.0f}%（會被抽 35% 佣金）"]
    if inv:
        parts.append(f"客單價中位數 {inv['median']:.0f} 元")
    if peaks:
        parts.append(f"尖峰時段集中在 {peaks[0][0]}:00 前後")
    if repeat:
        parts.append(f"回頭客佔客數 {repeat['repeat_pct']:.0f}%、貢獻營收 {repeat['repeat_revenue_pct']:.0f}%")
    return "，".join(parts) + "。"


def public_operational_summary(data: dict) -> dict | None:
    """雲端安全版營運摘要（2026-07-14 新增，使用者這天決定開放，取代 2026-07-09
    「不公開」的舊決定）：通路組合／回頭客用百分比，客單價改成「相對指數」
    （以平均值＝100 為基準，中位數/25分位/75分位換算成平均值的百分比），
    不會出現任何真實金額或真實客數，只看得出「分布形狀」（例如 p75_index
    明顯偏高代表存在幾筆偏高的訂單，不是金額本身）。回傳 None 代表這個店
    目前沒有發票/回頭客資料可以算。"""
    mix, inv, peaks, repeat = data["mix"], data["inv"], data["peaks"], data.get("repeat")
    if not mix and not inv:
        return None

    ticket_index = None
    if inv and inv["avg"]:
        avg = inv["avg"]
        ticket_index = {
            "avg_index": 100,
            "median_index": round(inv["median"] / avg * 100),
            "p25_index": round(inv["p25"] / avg * 100),
            "p75_index": round(inv["p75"] / avg * 100),
        }

    visit_buckets_pct = None
    if repeat and repeat["total_customers"]:
        total = repeat["total_customers"]
        visit_buckets_pct = {
            label: round(count / total * 100, 1) for label, count in repeat["visit_buckets"].items()
        }

    return {
        "delivery_pct": round(mix["delivery_pct"] * 100, 1) if mix else None,
        "pickup_pct": round(mix["pickup_pct"] * 100, 1) if mix else None,
        "other_pct": round(mix["other_pct"] * 100, 1) if mix else None,
        "ticket_price_index": ticket_index,
        "peak_hours": [p[0] for p in peaks] if peaks else None,
        "repeat_customer_pct": round(repeat["repeat_pct"], 1) if repeat else None,
        "repeat_revenue_pct": round(repeat["repeat_revenue_pct"], 1) if repeat else None,
        "visit_buckets_pct": visit_buckets_pct,
        "monthly_returning_trend": repeat["monthly_trend"] if repeat else None,
    }


def persist_operational_insights(conn, per_store: dict) -> None:
    """把濃縮結論寫進 store_operational_insights，只有這張表存在時才寫
    （雲端 Turso DB 目前沒有這張表，本機以外的呼叫端會直接跳過，不會噴錯）。
    `public_summary_text` 存的是 `public_operational_summary()` 的 JSON 字串，
    2026-07-14 新增，`build_cloud_snapshot.py` 只會同步這個欄位上雲端，
    `summary_text`（含真實客單價金額）繼續留在本機。"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='store_operational_insights'"
    ).fetchone()
    if exists is None:
        return
    for sid, data in per_store.items():
        summary = build_operational_summary(data)
        public_summary = public_operational_summary(data)
        conn.execute(
            "INSERT INTO store_operational_insights (store_id, summary_text, public_summary_text, generated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(store_id) DO UPDATE SET "
            "summary_text = excluded.summary_text, public_summary_text = excluded.public_summary_text, "
            "generated_at = excluded.generated_at",
            (sid, summary, json.dumps(public_summary, ensure_ascii=False) if public_summary else None),
        )
    conn.commit()


def build_report(conn, store_ids, per_store: dict | None = None) -> str:
    lines = [
        f"# 營運報告（{date.today().isoformat()} 產出）",
        "",
        "資料來源：發票明細／營收月報／收銀機明細／時段占比（Layer 1 原始明細，本機資料庫）。",
        "**前提：加盟店不能自行調價、原物料成本固定，以下建議只談通路組合／人力配置／客單價提升等操作面槓桿。**",
        "",
    ]

    if per_store is None:
        per_store = _compute_all(conn, store_ids)

    for sid in store_ids:
        mix, inv, peaks = per_store[sid]["mix"], per_store[sid]["inv"], per_store[sid]["peaks"]
        repeat = per_store[sid].get("repeat")

        lines.append(f"## {sid} 店")
        lines.append("")
        lines.append(
            f"- **通路組合**：外送平台佔營收 {mix['delivery_pct']*100:.1f}%"
            f"（會被抽 35% 佣金），自取/外帶佔 {mix['pickup_pct']*100:.1f}%"
            + (f"，其他 {mix['other_pct']*100:.1f}%" if mix["other_pct"] > 0.005 else "")
        )
        top3 = mix["breakdown"][:3]
        lines.append(
            "  最大宗通路：" + "、".join(f"{name}（{pct*100:.1f}%）" for name, _, pct in top3)
        )
        if inv:
            lines.append(
                f"- **客單價**：平均 {inv['avg']:.0f} 元／中位數 {inv['median']} 元"
                f"（25 分位 {inv['p25']} 元、75 分位 {inv['p75']} 元），共 {inv['n']:,} 筆有效發票"
            )
        if peaks:
            peak_desc = "、".join(f"{h}:00（平均 {s:.0f} 元／{c:.0f} 杯）" for h, s, c in peaks[:3])
            lines.append(f"- **尖峰時段**：{peak_desc}")
        if repeat:
            lines.append(
                f"- **回頭客（2026-07-10 新增，用手機載具號碼識別，只算聚合統計，不留個人層級資料）**："
                f"回頭客（2 次以上消費）佔客數 {repeat['repeat_pct']:.1f}%，"
                f"但貢獻了 {repeat['repeat_revenue_pct']:.1f}% 的營收"
                + (
                    f"；{repeat['latest_month']} 這個月的客人裡，有 {repeat['latest_returning_pct']:.1f}% "
                    f"是之前月份（最早從 {repeat['first_month']} 開始）就出現過的老客，這個比例逐月成長中"
                    if repeat["latest_returning_pct"] is not None else ""
                )
            )
        weekday_summary = per_store[sid].get("weekday_summary")
        if weekday_summary:
            lines.append(
                "- **星期幾營業額/發票張數彙整**（每日彙總後依星期幾分組，2026-07-13 新增）："
            )
            for row in weekday_summary:
                lines.append(
                    f"  - 星期{row['星期']}（{row['天數']} 天）：營業額中位數 {row['營業額中位數']:,} 元"
                    f"（最低 {row['營業額最小']:,} 元／{row['營業額最小日期']}，"
                    f"最高 {row['營業額最大']:,} 元／{row['營業額最大日期']}）；"
                    f"發票中位數 {row['發票中位數']:.0f} 張"
                    f"（最低 {row['發票最小']} 張／{row['發票最小日期']}，"
                    f"最高 {row['發票最大']} 張／{row['發票最大日期']}）"
                )
        lines.append("")

    if len(store_ids) > 1:
        lines.append("## 兩店比較")
        lines.append("")
        a, b = store_ids[0], store_ids[1]
        mix_a, mix_b = per_store[a]["mix"], per_store[b]["mix"]
        delivery_diff = (mix_a["delivery_pct"] - mix_b["delivery_pct"]) * 100
        if abs(delivery_diff) > 3:
            higher, lower = (a, b) if delivery_diff > 0 else (b, a)
            lines.append(
                f"- **{higher} 店外送平台佔比比 {lower} 店高約 {abs(delivery_diff):.1f} 個百分點**"
                f"，代表 {higher} 店有較高比例的營收要被抽 35% 平台佣金，"
                f"是可以優先檢視的獲利缺口（不是營收不夠，是營收的組成被抽走比較多）。"
            )
        inv_a, inv_b = per_store[a]["inv"], per_store[b]["inv"]
        if inv_a and inv_b:
            ticket_diff = inv_a["avg"] - inv_b["avg"]
            if abs(ticket_diff) > 5:
                higher, lower = (a, b) if ticket_diff > 0 else (b, a)
                lines.append(
                    f"- **{higher} 店平均客單價比 {lower} 店高約 {abs(ticket_diff):.0f} 元**，"
                    f"可以了解 {higher} 店的加購/組合搭配方式，看能不能複製到 {lower} 店。"
                )
        peaks_a = {h for h, _, _ in per_store[a]["peaks"][:3]}
        peaks_b = {h for h, _, _ in per_store[b]["peaks"][:3]}
        common_peaks = peaks_a & peaks_b
        if common_peaks:
            lines.append(
                f"- 兩店尖峰時段高度重疊（{', '.join(sorted(common_peaks))} 點），"
                "代表這幾個時段的產能瓶頸是兩店共通問題，不是單一店的個別狀況。"
            )
        repeat_a, repeat_b = per_store[a].get("repeat"), per_store[b].get("repeat")
        if repeat_a and repeat_b:
            lines.append(
                f"- 兩店都是「少數回頭客貢獻多數營收」的結構：{a} 店回頭客佔客數 "
                f"{repeat_a['repeat_pct']:.0f}%、貢獻營收 {repeat_a['repeat_revenue_pct']:.0f}%；"
                f"{b} 店回頭客佔客數 {repeat_b['repeat_pct']:.0f}%、貢獻營收 "
                f"{repeat_b['repeat_revenue_pct']:.0f}%。"
            )
        lines.append("")

    lines.append("## 建議（操作面槓桿，不涉及調價／原物料成本）")
    lines.append("")
    lines.append(
        "- **通路組合**：外送平台抽成 35% 是固定成本結構裡最大的「隱形折扣」。"
        "外送佔比較高的店可以優先評估：outbound 訊息／App 是否有效引導客人改用自取"
        "（例如自取限定加購優惠、取貨時間預估更準），把佣金留在自己手上，不需要調整售價。"
    )
    lines.append(
        "- **尖峰時段人力配置**：先前「實際排班 vs 建議人力」比對發現多數時段整體超編，"
        "但這裡看到的尖峰時段（中午 11-14 點附近）很集中——超編可能不是「人太多」，"
        "而是「人排在不對的時段」，建議把總班表時數往尖峰時段集中，離峰時段酌減，"
        "不增加總人事成本也可能改善出餐速度與外送平台準時率。"
    )
    lines.append(
        "- **客單價**：兩間店客單價中位數都偏低（約 85~90 元），在不能調價的前提下，"
        "可以靠「加購話術」（例如加大、加點心）拉高客單價，不是靠單價本身。"
        "客單價較高的店的做法值得整理成 SOP 複製到另一店。"
    )
    lines.append("")

    return "\n".join(lines)


def main():
    """CLI 除錯用進入點：只印到終端機，不寫檔（2026-07-13 起，標準管線改成
    `scripts/generate_full_report.py` 統一彙整成單一報告，這支腳本不再自己
    寫出 `operational_report_<日期>.md`，避免產出使用者不想要的分散檔案）。"""
    conn = _conn()
    store_ids = [r[0] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id")]
    per_store = _compute_all(conn, store_ids)
    report = build_report(conn, store_ids, per_store)
    print(report)

    persist_operational_insights(conn, per_store)
    print("已把濃縮結論寫入 store_operational_insights（給月盈虧頁交叉引用用）。")


if __name__ == "__main__":
    main()
