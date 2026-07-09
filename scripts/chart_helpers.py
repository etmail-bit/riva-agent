"""共用的 Altair 折線圖建構工具，app.py／app_pnl.py 的月盈虧走勢圖都呼叫這裡，
避免每個頁面各自複製一份標籤邏輯（複製過的邏輯之後很容易漏改，見下面的踩雷記錄）。

踩雷記錄（2026-07-09）：原本每個資料點都標數值，手機窄螢幕上 3 條線 x 10 個月
= 30 個標籤會疊在一起完全看不清楚。改成只標每條線「最後一個點」（最新月份），
其餘數值靠 tooltip（滑鼠/點擊）或 Streamlit 圖表工具列內建的「Show data」表格
檢視取得——這是 dataviz 設計準則的作法：標籤只挑重點標，不是每個點都標。
"""
import altair as alt

# 依 domain 順序輪流分配的標籤垂直偏移量，避免最後一個月份的多條線標籤疊在一起
_LABEL_DY_OFFSETS = [-12, 14, 30, 46]


def build_trend_chart(chart_df, domain, color_range, height=300):
    """chart_df 欄位需為 year_month／項目／金額。domain/color_range 是圖例類別跟
    顏色，順序要對齊。回傳一個 alt.LayerChart，只在每條線的最新月份標數值。"""
    color_scale = alt.Color(
        "項目:N",
        scale=alt.Scale(domain=domain, range=color_range),
        legend=alt.Legend(title=None),
    )
    line = (
        alt.Chart(chart_df)
        .mark_line(point=alt.OverlayMarkDef(filled=True, size=60), strokeWidth=2)
        .encode(
            x=alt.X("year_month:N", title="月份"),
            y=alt.Y("金額:Q", title="金額（TWD）"),
            color=color_scale,
            tooltip=["year_month", "項目", "金額"],
        )
    )

    last_month = chart_df["year_month"].max()
    layers = [line]
    for i, series_name in enumerate(domain):
        series_last = chart_df[
            (chart_df["項目"] == series_name) & (chart_df["year_month"] == last_month)
        ]
        if series_last.empty:
            continue
        layers.append(
            alt.Chart(series_last)
            .mark_text(dy=_LABEL_DY_OFFSETS[i % len(_LABEL_DY_OFFSETS)], fontSize=11)
            .encode(
                x=alt.X("year_month:N"),
                y=alt.Y("金額:Q"),
                text=alt.Text("金額:Q", format=","),
                color=color_scale,
            )
        )

    return alt.layer(*layers).properties(height=height)
