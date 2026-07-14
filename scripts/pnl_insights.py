"""月盈虧彙整建議：從 monthly_pnl（Layer 3）即時算出觀察與建議文字。

刻意不把任何真實金額寫死在程式碼裡——全部在執行當下從資料庫查詢計算，
符合零洩漏原則（真實數字只活在資料庫，不進版控的原始碼）。app.py／app_pnl.py
共用這支模組，避免本機版跟雲端版各寫一份分析邏輯。
"""
import sqlite3


def add_total_row(records: list, label_col: str, sum_cols: list, label_value: str = "累計") -> list:
    """在逐月明細表格尾端加一列加總（使用者要求：一眼看到累計虧損/獲利金額，
    不用自己逐月加）。只加總 sum_cols 指定的金額欄位——百分比欄位加總沒有意義
    （例如「原物料% 加總」不是任何有意義的數字），一律留 None 不顯示。"""
    if not records:
        return records
    total_row = {k: None for k in records[0].keys()}
    total_row[label_col] = label_value
    for col in sum_cols:
        total_row[col] = sum(r[col] for r in records if r.get(col) is not None)
    return records + [total_row]


def _store_stats(conn, store_id):
    rows = conn.execute(
        "SELECT year_month, revenue, cogs, labor_cost, rent, utilities, "
        "franchise_amortization, pretax_profit, net_profit, revenue_source "
        "FROM monthly_pnl WHERE store_id = ? ORDER BY year_month",
        (store_id,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    n = len(rows)
    if n == 0:
        return None

    avg_revenue = sum(r["revenue"] for r in rows) / n
    avg_net_profit = sum(r["net_profit"] for r in rows) / n
    loss_months = sum(1 for r in rows if r["net_profit"] < 0)
    avg_fixed = sum(
        r["labor_cost"] + r["rent"] + r["utilities"] + r["franchise_amortization"] for r in rows
    ) / n
    manual_months = sum(1 for r in rows if r["revenue_source"] == "manual")

    trend = None
    if n >= 4:
        half = max(1, n // 3)
        first_avg = sum(r["net_profit"] for r in rows[:half]) / half
        last_avg = sum(r["net_profit"] for r in rows[-half:]) / half
        trend = last_avg - first_avg

    return {
        "store_id": store_id,
        "n": n,
        "avg_revenue": avg_revenue,
        "avg_net_profit": avg_net_profit,
        "loss_months": loss_months,
        "avg_fixed": avg_fixed,
        "fixed_pct_of_revenue": (avg_fixed / avg_revenue) if avg_revenue else None,
        "manual_months": manual_months,
        "trend": trend,
        "first_month": rows[0]["year_month"],
        "last_month": rows[-1]["year_month"],
    }


def _combined_stats(conn, store_count):
    rows = conn.execute(
        "SELECT year_month, SUM(net_profit) AS combined_net_profit, "
        "COUNT(DISTINCT store_id) AS store_count "
        "FROM monthly_pnl GROUP BY year_month HAVING store_count = ? ORDER BY year_month",
        (store_count,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    n = len(rows)
    if n == 0:
        return None
    avg_combined = sum(r["combined_net_profit"] for r in rows) / n
    loss_months = sum(1 for r in rows if r["combined_net_profit"] < 0)
    return {"n": n, "avg_combined": avg_combined, "loss_months": loss_months}


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _pinpoint_worst_month(records: list) -> dict | None:
    """從 generate_monthly_breakdown() 的逐月成本結構裡找出淨利最差的月份，
    以及當月哪個成本科目 % 明顯高於這家店自己的歷史平均——不重新查資料庫，
    直接拿現成的逐月資料算，跟該店自己比較（不同店固定成本結構本來就不同，
    跟別店比沒有意義）。"""
    valid = [r for r in records if r["稅後淨利"] is not None]
    if len(valid) < 2:
        return None

    cost_cols = ["原物料%", "人事%", "房租%", "水電%", "平台抽成%"]
    worst = min(valid, key=lambda r: r["稅後淨利"])
    avgs = {
        col: sum(v) / len(v)
        for col in cost_cols
        if (v := [r[col] for r in valid if r[col] is not None])
    }
    deviations = {
        col: worst[col] - avgs[col]
        for col in cost_cols
        if worst.get(col) is not None and col in avgs
    }
    if not deviations:
        return None

    driver_col = max(deviations, key=deviations.get)
    return {
        "month": worst["月份"],
        "net_profit": worst["稅後淨利"],
        "driver_label": driver_col.rstrip("%"),
        "driver_pct": worst[driver_col],
        "driver_avg_pct": avgs[driver_col],
        "deviation": deviations[driver_col],
    }


def _has_any_cost_actuals(conn) -> bool:
    columns = [
        "labor_actual", "cogs_actual", "utilities_actual", "rent_actual",
        "franchise_amortization_actual",
    ]
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM monthly_cost_actuals "
        f"WHERE {' OR '.join(f'{c} IS NOT NULL' for c in columns)}"
    ).fetchone()
    return dict(row)["c"] > 0


def generate_monthly_breakdown(conn, store_id) -> list:
    """回傳這店逐月的成本結構（占營收 %），一個月一筆 dict，方便逐月比較找盈虧原因。
    百分比欄位而非金額欄位，才能跨月份（營收規模不同）直接比較。"""
    rows = conn.execute(
        "SELECT year_month, revenue, cogs, material_waste, labor_cost, rent, utilities, "
        "platform_commission, pretax_profit, net_profit "
        "FROM monthly_pnl WHERE store_id = ? ORDER BY year_month",
        (store_id,),
    ).fetchall()

    records = []
    for r in rows:
        row = dict(r)
        revenue = row["revenue"] or 0
        pct = (lambda v: round(v / revenue * 100, 1)) if revenue else (lambda v: None)
        records.append({
            "月份": row["year_month"],
            "營收": revenue,
            "原物料%": pct(row["cogs"]),
            "原物料損耗%": pct(row["material_waste"]),
            "人事%": pct(row["labor_cost"]),
            "房租%": pct(row["rent"]),
            "水電%": pct(row["utilities"]),
            "平台抽成%": pct(row["platform_commission"]),
            "稅前淨利": row["pretax_profit"],
            "稅後淨利": row["net_profit"],
            "損益": "虧損" if row["net_profit"] < 0 else "獲利",
        })
    return records


def generate_pnl_insights(conn) -> str:
    """回傳一段 Markdown 文字，彙整兩店歷史月盈虧的觀察與建議。"""
    store_ids = [r["store_id"] for r in conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()]
    stats_by_store = {sid: _store_stats(conn, sid) for sid in store_ids}
    stats_by_store = {sid: s for sid, s in stats_by_store.items() if s is not None}
    if not stats_by_store:
        return "尚未有足夠的月盈虧歷史紀錄可供彙整分析。"

    lines = ["### 各店經營現況"]
    for sid, s in stats_by_store.items():
        if s["loss_months"] == s["n"]:
            status = f"近 {s['n']} 個月（{s['first_month']}～{s['last_month']}）**每個月都是虧損**"
        elif s["loss_months"] == 0:
            status = f"近 {s['n']} 個月（{s['first_month']}～{s['last_month']}）**每個月都是獲利**"
        else:
            status = (
                f"近 {s['n']} 個月（{s['first_month']}～{s['last_month']}）中有 "
                f"{s['loss_months']} 個月虧損、{s['n'] - s['loss_months']} 個月獲利，"
                "損益在盈虧線附近擺盪"
            )
        line = f"- **{sid} 店**：{status}。"

        pin = _pinpoint_worst_month(generate_monthly_breakdown(conn, sid))
        if pin is not None and pin["deviation"] >= 2:
            line += (
                f" 淨利最差的月份是 {pin['month']}（稅後淨利 {pin['net_profit']:,} 元），"
                f"當月{pin['driver_label']}占營收 {pin['driver_pct']:.1f}%，"
                f"比{sid} 店自己的平均（{pin['driver_avg_pct']:.1f}%）高出 {pin['deviation']:.1f} 個百分點，"
                "是該月主要的獲利壓力來源。"
            )
        elif s["fixed_pct_of_revenue"] is not None and s["fixed_pct_of_revenue"] > 0.5:
            line += f" 固定成本（人事＋房租＋水電＋加盟金攤提）平均占營收約 {s['fixed_pct_of_revenue']*100:.0f}%，偏高，是主要壓力來源。"

        if s["trend"] is not None:
            if s["trend"] > 0:
                line += " 近期表現較前期**改善**。"
            elif s["trend"] < 0:
                line += " 近期表現較前期**惡化**。"

        if s["manual_months"] > 0:
            line += f"（其中 {s['manual_months']} 個月營收是手動輸入、非 POS 稽核過，數字僅供參考）"

        # 2026-07-14：雲端安全版的 store_operational_insights 沒有 summary_text 欄位
        # （那欄含真實客單價金額，只留本機），只有 public_summary_text。整段包進
        # try/except，不只包 SELECT——_table_exists() 本身也是查同一個 conn，任何
        # 跟這張表有關的環節出錯都不該讓整頁掛掉，雲端就直接跳過這句，安全版的
        # 通路/回頭客/客單價指數改由 app_pnl.render_operational_insights() 另一個
        # 獨立區塊顯示，不在這句話裡重複塞一次。
        try:
            if _table_exists(conn, "store_operational_insights"):
                op_row = conn.execute(
                    "SELECT summary_text FROM store_operational_insights WHERE store_id = ?", (sid,)
                ).fetchone()
                if op_row is not None:
                    line += f" {dict(op_row)['summary_text']}"
        except sqlite3.OperationalError:
            pass

        lines.append(line)

    combined = _combined_stats(conn, len(store_ids))
    if combined and len(store_ids) > 1:
        if combined["loss_months"] == combined["n"]:
            combined_status = "**兩店合計每個月都是虧損**"
        elif combined["loss_months"] == 0:
            combined_status = "**兩店合計每個月都是獲利**"
        else:
            combined_status = f"兩店合計 {combined['loss_months']}／{combined['n']} 個月虧損"
        lines.append(f"- **兩店合計**：{combined_status}，平均每月合計稅後淨利以資料庫最新試算為準（見上方走勢圖）。")

    lines.append("")
    lines.append("### 建議")
    if not _has_any_cost_actuals(conn):
        lines.append(
            "- ⚠️ 以上數字目前**完全靠 `config/cost_rates.json` 的概算值**試算，"
            "還沒有任何一個月填入真實成本數字（`monthly_cost_actuals`）。"
            "概算跟實際的落差是目前分析最大的不確定性來源，建議優先補真實數字。"
        )
    has_staffing = _table_exists(conn, "store_staffing_insights")
    for sid in stats_by_store:
        staffing_row = None
        if has_staffing:
            staffing_row = conn.execute(
                "SELECT summary_text FROM store_staffing_insights WHERE store_id = ?", (sid,)
            ).fetchone()
        if staffing_row is not None:
            lines.append(f"- **{sid} 店**：{dict(staffing_row)['summary_text']}")
        else:
            lines.append(
                f"- **{sid} 店**：先前「實際排班 vs 建議人力」比對發現多數時段有超編現象，"
                "若屬實，目前人事成本的概算值可能低估真實負擔，建議優先核對薪資單校準。"
            )
    if len(stats_by_store) > 1:
        revenues = {sid: s["avg_revenue"] for sid, s in stats_by_store.items()}
        best_store = max(revenues, key=revenues.get)
        worst_store = min(revenues, key=revenues.get)
        if best_store != worst_store:
            gap = revenues[best_store] - revenues[worst_store]
            gap_pct = gap / revenues[worst_store] * 100 if revenues[worst_store] else None
            gap_desc = f"約 {gap:,.0f} 元" + (f"（高 {gap_pct:.0f}%）" if gap_pct is not None else "")
            has_ops = _table_exists(conn, "store_operational_insights")
            lines.append(
                f"- {best_store} 店平均月營收比 {worst_store} 店高{gap_desc}，兩店固定成本結構類似，"
                f"可比對兩店在通路組合／客單價／尖峰時段人力配置上的差異"
                + ("（見上方各店經營現況段落）" if has_ops else "")
                + f"，找出可複製到 {worst_store} 店的做法。"
            )

    return "\n".join(lines)
