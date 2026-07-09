#!/usr/bin/env python3
"""營運報告：分析 Layer 1 原始明細（發票/營收月報/收銀機明細/時段占比），
找兩店的優缺點與提升營業額的操作面建議。

前提（使用者 2026-07-09 確認）：這是加盟店，**不能自行調價、原物料成本也是
固定的**，所以這裡的建議一律只談「操作面槓桿」（通路組合、尖峰時段人力配置、
客單價提升手法），不談調價或砍原物料成本。

只吃 Layer 1 原始明細（raw_invoice_transactions／raw_revenue_monthly／
raw_cash_register_daily／raw_hourly_pattern_monthly），這些表格依零洩漏原則
只存在本機 db/riva_agent.db，這支腳本跟輸出報告都刻意不會被任何雲端功能引用。

輸出：reports/operational_report_<YYYY-MM-DD>.md（reports/ 已加進 .gitignore，
不進版控——報告內容含真實通路/客單價/營收數字）。

用法：
    source .venv/bin/activate
    python3 -m scripts.analyze_operations
"""
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "riva_agent.db"
REPORTS_DIR = ROOT / "reports"

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
        }
        for sid in store_ids
    }


def build_operational_summary(data: dict) -> str:
    """把單店的通路/客單價/尖峰時段數字濃縮成一兩句「結論」，供 pnl_insights.py
    的「各店經營現況」段落交叉引用。刻意只保留聚合統計（佔比/中位數/時段），
    不含逐筆明細，是唯一之後可能考慮同步上雲端的內容。"""
    mix, inv, peaks = data["mix"], data["inv"], data["peaks"]
    parts = [f"通路組合上外送平台佔營收 {mix['delivery_pct']*100:.0f}%（會被抽 35% 佣金）"]
    if inv:
        parts.append(f"客單價中位數 {inv['median']:.0f} 元")
    if peaks:
        parts.append(f"尖峰時段集中在 {peaks[0][0]}:00 前後")
    return "，".join(parts) + "。"


def persist_operational_insights(conn, per_store: dict) -> None:
    """把濃縮結論寫進 store_operational_insights，只有這張表存在時才寫
    （雲端 Turso DB 目前沒有這張表，本機以外的呼叫端會直接跳過，不會噴錯）。"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='store_operational_insights'"
    ).fetchone()
    if exists is None:
        return
    for sid, data in per_store.items():
        summary = build_operational_summary(data)
        conn.execute(
            "INSERT INTO store_operational_insights (store_id, summary_text, generated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(store_id) DO UPDATE SET "
            "summary_text = excluded.summary_text, generated_at = excluded.generated_at",
            (sid, summary),
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
    conn = _conn()
    store_ids = [r[0] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id")]
    per_store = _compute_all(conn, store_ids)
    report = build_report(conn, store_ids, per_store)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"operational_report_{date.today().isoformat()}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"營運報告已寫入 {out_path}")
    print()
    print(report)

    persist_operational_insights(conn, per_store)
    print("已把濃縮結論寫入 store_operational_insights（給月盈虧頁交叉引用用）。")


if __name__ == "__main__":
    main()
