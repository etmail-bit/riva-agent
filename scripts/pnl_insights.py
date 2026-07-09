"""月盈虧彙整建議：從 monthly_pnl（Layer 3）即時算出觀察與建議文字。

刻意不把任何真實金額寫死在程式碼裡——全部在執行當下從資料庫查詢計算，
符合零洩漏原則（真實數字只活在資料庫，不進版控的原始碼）。app.py／app_pnl.py
共用這支模組，避免本機版跟雲端版各寫一份分析邏輯。
"""


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

        if s["fixed_pct_of_revenue"] is not None and s["fixed_pct_of_revenue"] > 0.5:
            line += f" 固定成本（人事＋房租＋水電＋加盟金攤提）平均占營收約 {s['fixed_pct_of_revenue']*100:.0f}%，偏高，是主要壓力來源。"

        if s["trend"] is not None:
            if s["trend"] > 0:
                line += " 近期表現較前期**改善**。"
            elif s["trend"] < 0:
                line += " 近期表現較前期**惡化**。"

        if s["manual_months"] > 0:
            line += f"（其中 {s['manual_months']} 個月營收是手動輸入、非 POS 稽核過，數字僅供參考）"

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
    lines.append(
        "- 先前「實際排班 vs 建議人力」比對發現多數時段有超編現象，"
        "若屬實，目前人事成本的概算值可能低估真實負擔，建議優先核對薪資單校準。"
    )
    if len(stats_by_store) > 1:
        revenues = {sid: s["avg_revenue"] for sid, s in stats_by_store.items()}
        best_store = max(revenues, key=revenues.get)
        worst_store = min(revenues, key=revenues.get)
        if best_store != worst_store:
            lines.append(
                f"- {best_store} 店平均營收高於 {worst_store} 店，兩店固定成本結構類似，"
                f"可比對兩店在座位/時段/品項組合上的差異，找出可複製到 {worst_store} 店的做法。"
            )

    return "\n".join(lines)
