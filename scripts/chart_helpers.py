"""共用的 Altair 折線圖建構工具，app.py／app_pnl.py 的月盈虧走勢圖都呼叫這裡，
避免每個頁面各自複製一份標籤邏輯（複製過的邏輯之後很容易漏改，見下面的踩雷記錄）。

踩雷記錄（2026-07-09 第一版）：原本每個資料點都標數值，手機窄螢幕上「用容器寬度硬擠」
（`use_container_width=True`）會讓 3 條線 x 10 個月 = 30 個標籤全部疊在一起看不清楚，
改成只標每條線最後一個點，其餘靠 tooltip 或表格查。

踩雷記錄（2026-07-09 第二版，使用者要求恢復每點標數值）：使用者要的其實是「每個月固定
間隔寬度，月份一多就整張圖變寬、用滑動看，不要硬壓縮擠在一起」——跟現有「兩店合計逐月
明細」表格的滑動查看模式是同一個邏輯，只是套用在圖表上。改法：x 軸改用 `alt.Step()`
給每個月固定像素寬度，圖表總寬度 = 月份數 × 固定寬度，呼叫端用
`st.altair_chart(chart, use_container_width=False)`（不要硬壓縮），月份一多圖表自然
變寬，Streamlit 外層容器有限寬時會出現水平捲軸。這樣每個點都能安全標數值，不會重疊。
"""
import altair as alt

# 依 domain 順序輪流分配的標籤垂直偏移量，避免同一個月份的多條線標籤疊在一起
_LABEL_DY_OFFSETS = [-12, 14, 30, 46]

# 每個月份在 X 軸上固定佔用的像素寬度——月份一多，圖表整體變寬，靠外層水平捲動查看，
# 不會被硬壓縮到看不清楚。這個數字要夠寬才放得下最長的標籤（例如 "-165,961"）。
MONTH_STEP_PX = 70


def build_trend_chart(chart_df, domain, color_range, height=300, y_field="金額", y_title="金額（TWD）", value_format=","):
    """chart_df 欄位需為 year_month／項目／<y_field>（預設「金額」，可傳其他欄位名稱
    給非金額的走勢圖用，例如百分比指標）。domain/color_range 是圖例類別跟顏色，順序
    要對齊。回傳一個 alt.LayerChart，每個資料點都標數值，X 軸固定間隔（月份一多圖表
    變寬，不會擠壓）。呼叫端記得搭配 `st.altair_chart(chart, use_container_width=False)`，
    不要用容器寬度硬壓縮。"""
    color_scale = alt.Color(
        "項目:N",
        scale=alt.Scale(domain=domain, range=color_range),
        legend=alt.Legend(title=None),
    )
    x_scale = alt.X("year_month:N", title="月份", scale=alt.Scale(paddingOuter=0.3))
    y_field_q = f"{y_field}:Q"
    line = (
        alt.Chart(chart_df)
        .mark_line(point=alt.OverlayMarkDef(filled=True, size=60), strokeWidth=2)
        .encode(
            x=x_scale,
            y=alt.Y(y_field_q, title=y_title),
            color=color_scale,
            tooltip=["year_month", "項目", y_field],
        )
    )

    layers = [line]
    for i, series_name in enumerate(domain):
        series_data = chart_df[chart_df["項目"] == series_name]
        if series_data.empty:
            continue
        layers.append(
            alt.Chart(series_data)
            .mark_text(dy=_LABEL_DY_OFFSETS[i % len(_LABEL_DY_OFFSETS)], fontSize=10)
            .encode(
                x=x_scale,
                y=alt.Y(y_field_q),
                text=alt.Text(y_field_q, format=value_format),
                color=color_scale,
            )
        )

    return alt.layer(*layers).properties(width=alt.Step(MONTH_STEP_PX), height=height)
