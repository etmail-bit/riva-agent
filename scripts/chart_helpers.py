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


def build_bar_line_combo_chart(
    bar_df,
    x_field,
    bar_y_field,
    bar_y_title,
    line_df,
    line_y_field,
    line_y_title,
    x_title="時段",
    bar_category_field=None,
    bar_domain=None,
    bar_range=None,
    line_color="#d97706",
    height=300,
):
    """長條（左軸）+ 線（右軸，獨立刻度）的組合圖，用於「兩種不同單位的指標要在同一張圖
    比較」的場景（例如人力〈人〉vs 杯數〈杯〉，或發票張數〈張〉vs 營業額〈元〉）。

    bar_df／line_df 是兩份獨立的長格式資料，都需要 x_field 這欄。bar_category_field
    有給值時（例如「類別」欄存「建議人力」/「實際人力」），長條會依類別並排分組並上色
    （bar_domain/bar_range 要對齊）；不給則畫單一顏色的長條（例如只有一種指標）。"""
    x_enc = alt.X(f"{x_field}:N", title=x_title)
    bar_encode = {
        "x": x_enc,
        "y": alt.Y(f"{bar_y_field}:Q", title=bar_y_title),
        "tooltip": [x_field, bar_y_field] + ([bar_category_field] if bar_category_field else []),
    }
    if bar_category_field:
        bar_encode["xOffset"] = f"{bar_category_field}:N"
        bar_encode["color"] = alt.Color(
            f"{bar_category_field}:N",
            scale=alt.Scale(domain=bar_domain, range=bar_range),
            legend=alt.Legend(title=None),
        )
    bar = alt.Chart(bar_df).mark_bar().encode(**bar_encode)

    line = (
        alt.Chart(line_df)
        .mark_line(point=True, strokeWidth=2, color=line_color)
        .encode(
            x=x_enc,
            y=alt.Y(f"{line_y_field}:Q", title=line_y_title, axis=alt.Axis(titleColor=line_color)),
            tooltip=[x_field, line_y_field],
        )
    )
    # resolve_scale(y="independent")：長條跟線各自算自己的 y 軸範圍（人力用 0~10，
    # 杯數可能是 0~100），共用同一個 y 軸的話其中一個會被壓到看不出高低差異。
    return alt.layer(bar, line).resolve_scale(y="independent").properties(height=height)


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
        dy = _LABEL_DY_OFFSETS[i % len(_LABEL_DY_OFFSETS)]
        layers.append(
            alt.Chart(series_data)
            .mark_text(dy=dy, fontSize=10)
            .encode(
                x=x_scale,
                y=alt.Y(y_field_q),
                text=alt.Text(y_field_q, format=value_format),
                color=color_scale,
            )
        )
        # 圖例的顏色對照容易被忽略（尤其手機上圖例可能不顯眼），額外在每條線的
        # 最後一個點旁邊直接寫上系列名稱（例如「營收」），不用對照圖例就知道每條線是什麼。
        last_point = series_data.sort_values("year_month").tail(1)
        layers.append(
            alt.Chart(last_point)
            .mark_text(dy=dy, dx=32, fontSize=11, fontWeight="bold", align="left")
            .encode(
                x=x_scale,
                y=alt.Y(y_field_q),
                text=alt.Text("項目:N"),
                color=color_scale,
            )
        )

    # 系列名稱標籤畫在最後一個點的右邊（dx=32），如果不加右側留白，最後一個月份會
    # 剛好卡在圖表邊界，名稱標籤被裁切看不全（實測過，例如「稅後淨利」四個字只露出一半）。
    return (
        alt.layer(*layers)
        .properties(width=alt.Step(MONTH_STEP_PX), height=height, padding={"left": 5, "top": 5, "right": 70, "bottom": 5})
    )
