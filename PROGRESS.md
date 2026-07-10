# 專案進度總覽

飲料店營運效能優化系統。2 間飲料店加盟（代號 A、B，真實店名只存在 `.env`）。目標：把 POS 報表自動整理成月盈虧、排班建議、熱銷品項分析，最終包成網頁介面。

**零洩漏原則：任何真實店名、地址、真實財務數字都不可以出現在會進版控的檔案裡。** `.gitignore` 已經排除 `data/raw/`、`data/**/*.csv`、`config/**/*.json`（皆有 `.example` 版本可進版控）、`.env`、`db/**/*.db`。

## 資料流程架構

```
Layer 1（原始報表，貼近 POS 匯出格式）
  收銀機明細 / 營收月報 / 發票明細 / 銷售統計明細 / 時段占比 / 月成本 CSV（手動）
        ↓
Layer 2（跨來源稽核允收）
  daily_revenue_validated（收銀機明細 vs 營收月報交叉比對）
  monthly_cost_actuals（人事/原物料/水電實際數字，NULL 則 fallback 用 config 概算值）
  monthly_revenue_manual（月營收人工輸入備援，2026-07-08 新增，只有該月完全沒有
    daily_revenue_validated 資料時才會被 calculate_pnl.py 拿去用，POS 資料永遠優先）
        ↓
Layer 3（分析產出）
  monthly_pnl（月盈虧，已完成）
  排班建議（已完成，尚未落地成資料表，還在校準參數）
  熱銷品項分析（部分完成，缺單品成本）
  網頁介面（進行中，月盈虧+排班兩頁已完成並驗證過，見下方「網頁介面」一節）
```

資料庫檔案：`db/riva_agent.db`（SQLite，不進版控）。結構定義：`db/schema.sql`（可進版控，唯一事實來源，**改結構時記得同步 ALTER 實際資料庫，不能只改這個檔案**，之前踩過這個雷）。

## 已完成功能與對應腳本

| 功能 | 腳本 | 說明 |
|---|---|---|
| 收銀機明細匯入 | `scripts/import_cash_register.py` | Layer 1，含應稅/免稅、信用卡/其他電子支付歸戶；檔名比對同時接受「收銀機明細」「收營機明細」（錯字）「收銀機」（少打明細）「收銀機名細」（明/名打反）四種慣例 |
| 營收月報匯入 | `scripts/import_revenue_monthly.py` | Layer 1，A/B 兩店報表結構不同，用欄位對照表統一 |
| 跨來源比對 | `scripts/validate_revenue.py` | 產出 Layer 2 的 daily_revenue_validated |
| 發票明細匯入 | `scripts/import_invoices.py` | 用發票號唯一索引防重複匯入 |
| 月成本輸入 | `scripts/import_cost_actuals.py` | 讀 `data/monthly_cost_actuals.csv`（範本：`.example.csv`） |
| **月盈虧計算** | `scripts/calculate_pnl.py` | 讀 `config/cost_rates.json` + Layer 2 資料，寫入 `monthly_pnl` |
| 銷售統計/明細匯入 | `scripts/import_product_sales.py` | Layer 1，B 店多了金額/時段欄位 |
| 時段占比匯入 | `scripts/import_hourly_pattern.py` | Layer 1，兩店都沒有門市名稱欄，用檔名慣例判斷店別 |
| **排班建議** | `scripts/calculate_staffing.py` | 讀 `config/staffing_rules.json`，目前只印報表沒落地成表 |
| 實際排班匯入 | `scripts/import_staffing_actual.py` | 手動謄打照片成 CSV 後匯入 `raw_staffing_actual` |
| **實際排班 vs 建議人力比對** | `scripts/compare_staffing.py` | 整月平均顆粒度比對，已接進網頁排班頁面 |
| **帳號管理** | `scripts/manage_accounts.py` | 新增/移除/列出帳號、手動重設密碼，寫入 `config/auth_config.yaml`（不進版控） |
| **網頁介面** | `app.py` | Streamlit，登入 + 依角色分頁（見下方「網頁介面」一節） |
| 網頁啟動腳本 | `scripts/run_app.sh` | 固定帶好 localhost/headless 等參數，避免手動漏打 |

## 月盈虧計算邏輯（重要，網頁要重用這個公式）

```
營收 − 原物料 − 平台抽成(Ubereats/Foodpanda) − 金流手續費(信用卡/其他電子支付)
  − 人事(底薪 × 1.196，不管底薪來源是實際值或概算值都要乘)
  − 房租 − 水電 − 加盟金攤提 − 營業稅(只算「應稅」部分 × 5%)
  = 稅前淨利
稅前淨利 × 20% = 預估所得稅（虧損月份不倒扣，最低 0）
稅前淨利 − 預估所得稅 = 稅後淨利
```

實作在 `scripts/calculate_pnl.py` 的 `calculate_one()` 函式，輸入是 store_id + year_month，回傳一個 dict。**這個函式已經是乾淨可重用的形式，網頁可以直接 import 呼叫，不用重寫邏輯。**

`monthly_pnl` 表用 `UNIQUE(store_id, year_month)` 當唯一鍵，同月重算會更新、不同月份會累積——**歷史紀錄本來就有在存，只是還沒有介面可以看。**

**營收來源 fallback（2026-07-08 新增）**：`get_revenue_breakdown()` 優先查 `daily_revenue_validated`（POS 稽核過），查無資料才 fallback 查 `monthly_revenue_manual`（網頁手動輸入的備援，全額視為應稅，沒有拆免稅欄位）。兩邊都沒資料則營收算 0。`monthly_pnl` 多了一個 `revenue_source` 欄位（`'pos'`/`'manual'`/`'none'`），記錄這個月的數字究竟是稽核過的還是手動輸入的，網頁上會用 📝 標示手動輸入的月份。

## 排班建議邏輯

```
每小時建議前場人力 = ceil(日均杯數 ÷ 單位產能)
```

外送單量已經包含在日均杯數裡，不重複疊加。煮茶時段（預設 07:30 起 1 小時）該人力不計入前場產能，另外標註 +1。參數在 `config/staffing_rules.json`：單位產能 **30 杯/hr**（2026-07-09 使用者依現場觀察從 40 校正下修）、5 個班別時間窗（開早/早一/早二/尖峰班/晚班，2026-07-09 已改成跟真實排班一致，見下方「排班瓶頸分析」一節）、兼職 3~4 小時。**這些參數都是使用者給的估計值，還在校準，之後要能在網頁上調整。**

`scripts/calculate_staffing.py` 目前只印報表，還沒寫進資料庫（設計還在跟使用者對數字）。網頁的排班頁面已經可以現場調整參數試算，但「儲存為新的預設值」只會寫回 `config/staffing_rules.json`，不會寫進資料庫。

## 實際排班資料（`raw_staffing_actual`，2026-07-08 新增）

使用者提供真實排班表照片（原本放在專案根目錄的 `排班/` 資料夾，**已加進 `.gitignore`，不會進版控**）。

- **員工代碼**：取真實姓名中最有辨識度的一個字（例如「王小美」→「美」，僅為說明用的虛構範例），對照表存在 `.env` 的 `EMPLOYEE_<代碼>_NAME`，跟店名代號（`STORE_A_REAL_NAME`）同一套做法。資料庫 `employees`/`raw_staffing_actual` 兩張表只存代碼，真實姓名只有 `.env` 看得到。
- **資料來源**：手動謄打照片內容成 `data/staffing_actual_raw.csv`（範本 `.example.csv`），不做 OCR（手寫/彩色表格辨識不可靠，這規模人工謄打更快更準）。目前已匯入 A 店 2026-06-16~30 這一批（9 位員工、110 筆，含「假/公/補」等未上班的標記日）。
- **匯入程式**：`scripts/import_staffing_actual.py`，比照 `import_cost_actuals.py` 的 CSV 匯入模式，用 `UNIQUE(store_id, business_date, employee_code)` 防重複。
- 還有 5 張照片（其他月份/店別）還沒謄打匯入，使用者說先不急著轉，之後有需要再說。

## 實際排班 vs 建議人力比對（2026-07-08 完成）

`scripts/compare_staffing.py` 的 `compare()` 函式，把兩邊算成同一個顆粒度（**整月平均**）才比較：
- 建議人力：`calculate_hourly_staffing()` 算出來的，本來就是整月平均
- 實際人力：該時段「有排班資料的天數」中，平均每天有幾人的班涵蓋這個時段（分母是「已謄打的天數」，不是整月天數——資料還沒補齊不會被拉低估計值）

已接進網頁「排班建議」頁面最下面一個區塊，唯讀顯示表格＋走勢圖（跟 CLI 版一致，用同一個 `working_config` 所以會跟着頁面上調整的參數連動）。CLI 版執行注意是 `python3 -m scripts.compare_staffing`（不是直接 `python3 scripts/compare_staffing.py`，這支腳本有 import 同目錄下的 calculate_staffing，直接執行會找不到 `scripts` 套件）。

**重要發現（2026-07-08，初步觀察）**：用現有這批資料（A 店 2026-06-16~30）跑出來，**13 個時段全部超編、0 個時段人力不足**，尤其中午 11-14 點，建議只要 1-2 人，實際卻排了 3.7 人左右。這個發現 2026-07-09 已經深入分析並量化，見下方「排班瓶頸分析與人力成本估算」一節——**不是產能估計錯**，是三個早班在午餐尖峰整天疊在一起、晚班沒有隨晚間客流量遞減收斂。之後把剩下 5 張照片也謄打進來，可以看這個超編模式是不是每個月都一樣。

## 待規劃：請假排休功能（尚未動工，使用者 2026-07-08 提出）

排班功能的下一個大階段，設計方向：
- 員工登入後可以自由填寫「當月想休假的日期」
- 每日休假人數上限由管理者設定（**可調整的參數**，比照排班頁面「可調參數」的做法）
- 員工端不擋填寫（自由填），最後由管理者審核/定奪超過上限的日期
- 需要新的資料表（例如 `leave_requests`）跟一個管理者審核用的網頁畫面，等「實際排班 vs 建議人力」這個做完再排進度

## 網頁介面（Streamlit，`app.py`）

技術選型 Streamlit（已與使用者確認：本機用 Streamlit 資安風險比 Flask 前後端分離更低，因為瀏覽器不會直接碰資料庫、沒有 API 端點暴露）。**先只在本機跑，尚未對外開放**，之後若要開放給其他人（如兼職排班用），會再評估內網/雲端部署與對應的資安等級。

啟動用 `./scripts/run_app.sh`（已內建 `--server.address localhost` + `--server.headless true` + `--browser.gatherUsageStats false`，不用自己記一長串參數）：
```
./scripts/run_app.sh
```
三個參數都是實測踩過的雷才加的：
- `--server.address localhost`：不加的話預設監聽 `0.0.0.0`，外部網路連得到
- `--server.headless true`：不加的話 Streamlit 第一次執行會跳出互動式的 email 詢問提示卡住（背景執行會直接卡死，前景執行也要多按一次 Enter）
- `--browser.gatherUsageStats false`：跟 headless 一起加，避免使用量統計的提示

帳號權限設計：
- `config/auth_config.yaml`（不進版控，範本是 `config/auth_config.example.yaml`）用 `roles` 欄位區分 `admin`（月盈虧＋排班）跟 `staff`（只有排班）
- 帳號一律用 `scripts/manage_accounts.py` 的 `add`/`remove`/`list`/`reset-password` 管理，不要手改 yaml
- 密碼用 `streamlit-authenticator`（0.4.2）雜湊儲存；登入後使用者可在側邊欄自助改密碼；忘記密碼沒有自動寄信復原（本機無郵件服務），一律由管理者用 `reset-password` 手動重設
- `cryptography` 套件釘住 `45.0.7`（見 `requirements.txt` 註解）：新版在這台 Python 3.14 + Intel Mac 上沒有預編譯包會編譯失敗

頁面設計：
- **月盈虧頁**：選店別 → 「手動輸入月營收」摺疊區塊（見下方獨立說明）→ 選月份 → 試算參數分成 4 個摺疊區塊（固定成本／變動成本比例／平台與金流費率／稅率），**11 項全部可調**（2026-07-08 前只有前 5 項可調，這次把 Ubereats/Foodpanda 抽成%、信用卡/其他電子支付手續費%、營業稅%、營所稅% 也開放了；稅率區塊有警語提醒是法定稅率，調整僅供試算）→ 用調整後的參數 import `scripts/calculate_pnl.py` 的 `calculate_one()` 現算現顯示（不寫入 `monthly_pnl`，只有「儲存為新的預設值」按鈕會寫回 `config/cost_rates.json`）；下面接歷史走勢圖（讀 `monthly_pnl` 既有紀錄，Altair 折線圖，營收/稅後淨利固定用藍/青綠兩色，**每個點都標數值**，營收標上方、稅後淨利標下方避免重疊）
  - **手動輸入月營收**（2026-07-08 新增）：輸入任意 YYYY-MM 月份 + 營收/Ubereats/Foodpanda/信用卡/其他電子支付五個數字，寫入 `monthly_revenue_manual`；若該月「完全沒有」POS 資料，`calculate_one()` 才會 fallback 用這裡的數字（POS 永遠優先，不會被覆蓋）；再次輸入同一個月份會自動預填現有值；有基本合理性檢查（平台+金流四項加總不可超過總營收，超過會擋存檔）；有「清除本月手動輸入」按鈕
- **排班建議頁**：選店別/月份 → 參數（單位產能、煮茶時間、班別時間窗、兼職時數）預設值來自 `config/staffing_rules.json`，可在頁面上現場調整即時試算；「儲存為新的預設值」按鈕會把調整結果寫回設定檔（保留原本的 note 等其他欄位，只覆蓋有改到的值）；頁面最下面「實際排班 vs 建議人力比對」區塊，跟頁面調整的參數連動

**注意**：`labor_base`/`cogs_pct_of_revenue`/`utilities_estimate` 這三項如果該月 `monthly_cost_actuals` 已經有實際數字，頁面上調整的值不會生效（`calculate_one()` 的邏輯是實際值優先），這是預期行為不是 bug——`rent`/`franchise_fee_amortization` 沒有實際值機制，永遠吃頁面上的值。

驗證方式：背景啟動 `streamlit run` + Playwright 開瀏覽器實際登入操作，跟 CLI 版腳本的輸出比對數字一致，不是只看程式碼或跑 import 測試。

## 待完成（下一步的候選方向，尚未定案）

- **熱銷品項分析**：銷量資料已到位（`raw_product_sales_monthly`），缺單品原物料成本（使用者稍後補），現在只能做「熱銷排行」，做不了「利潤」那一半
- **回頭客分析**（P3・待評估）：用發票載具號碼識別回購，涉及顧客識別，要先設計匿名化處理方式才能動工
- **請假排休功能**：見上方「待規劃：請假排休功能」一節

## 已知資料缺口

- A 店 4 月時段占比沒有提供
- ~~B 店 2026-03 收銀機明細／2026-04 發票明細／2025-09 發票明細缺失~~（2026-07-08 使用者已補上正確檔案，內容經逐日/逐筆時間驗證月份無誤，已重新匯入並全數通過跨來源比對。舊的誤植重複檔仍留在 `data/raw/` 供追溯，已改名加註「誤植重複檔」+ `.xlsx.bak`，不會被匯入程式讀到，見下方「B 店歷史資料回補」一節）
- ~~A 店 2026-04 發票明細沒有提供~~（2026-07-08 使用者補上檔案 `...A店＿發票明細.xlsx`，檔名帶「明細」二字跟既有慣例不同，`import_invoices.py` 的 `RAW_GLOBS` 補上第二種相容慣例；內容經交易時間核對月份無誤，4797 筆全數新增）
- 單品原物料成本尚未建立（熱銷利潤分析的地基，使用者稍後會補）

## B 店歷史資料回補（2026-07-08 完成）

使用者把 B 店 2025-09~2026-03 的收銀機明細／營收月報／發票明細補進 `data/raw/`（B 店原本資料庫裡只有 2026-04~06，跟 A 店的歷史深度差一大截）。處理方式：

1. 收銀機明細檔名少打「明細」兩字（例如「...B店＿收銀機.xlsx」而非「...收銀機明細.xlsx」），`import_cash_register.py` 的 `RAW_GLOBS` 補上第三種相容檔名慣例（見上方功能表格）
2. 用**跨來源金額比對**（跟 `validate_revenue.py` 同樣的邏輯，拿收銀機明細月加總當基準）抓出 2 份**檔名月份標錯**的 B 店營收月報，兩份都拿真正內容比對後改回正確檔名重新匯入：
   - 原檔名標 `202511`、內容其實是 `2025-10`（跟 2025-10 收銀機加總只差 0.22%，跟 2025-11 卻差 1.3%）
   - 原檔名標 `202601`、內容其實是 `2026-02`（跟 2026-02 收銀機加總只差 0.29%，跟 2026-01 卻差 0.9%）
   - 改完之後 `validate_revenue.py` 跑出來全部月份都在門檻內，沒有異常
3. 額外發現 2 份「誤植重複檔」（檔名月份跟內容對不上，但不是隨便標錯、而是內容跟別的月份**完全重複**，代表真正那個月的原始檔案還沒給），已改名加註 `誤植重複檔` + `.xlsx.bak` 副檔名（讓匯入程式的 glob 抓不到，避免之後重跑誤匯入）：
   - B 店 2026-03 收銀機明細（內容其實是 2026-02 逐日資料）
   - B 店 2026-04 發票明細（內容其實是 2026-05 資料，這個問題其實 2026-07-07 就發現過一次，這次回補歷史資料時原始檔案還是同一份錯的）
4. 重新執行 `import_revenue_monthly.py` / `import_invoices.py` / `import_product_sales.py` / `import_hourly_pattern.py` / `validate_revenue.py` / `calculate_pnl.py` 全部跑過一輪（皆為冪等操作，跟現有資料不衝突）。匯入前已備份 `db/riva_agent.db`。
5. **同一次對話裡使用者立刻補上正確檔案**：B 店 2026-03 收銀機明細（這次檔名錯字是「收銀機名細」，名/明打反，`RAW_GLOBS` 再補第四種相容慣例）、B 店 2026-04 發票明細、B 店 2025-09 發票明細（原本完全沒給的那個月）。三份都先用內容日期/交易時間逐筆核對月份無誤才匯入，匯入後 `validate_revenue.py` 全部月份差異都在門檻內。

**最終結果**：`monthly_pnl` 補到 A 店 10 筆／B 店 10 筆，兩店都是 2025-09~2026-06 完整無缺口。**網頁月盈虧頁的「B 店歷史走勢圖」不用改任何程式碼**，`app.py` 原本查 `monthly_pnl` 就是 `WHERE store_id = ?` 不限日期範圍，新資料存進去圖表就會自動顯示，已用 SQL 直接查表確認資料筆數與數字正確；受限於目前無法取得 `demo_admin` 的登入密碼（雜湊儲存、無法反查明文），沒有另外開瀏覽器登入截圖驗證，如需要視覺確認可以請使用者提供密碼或用 `scripts/manage_accounts.py reset-password` 重設。

## 更正：2026-04 實際成本數字其實是測試資料（2026-07-08）

`data/monthly_cost_actuals.csv` 原本有 A 店／B 店 2026-04 的人事/原物料/水電實際數字（A：150000/200000/8000，B：140000/190000/7500）。使用者確認**這是測試資料，不是真的**，已要求刪除。處理方式：

1. CSV 移除這兩列，只留下 A 店 2026-05 那筆「留空＋備註」的範例列
2. **`import_cost_actuals.py` 只會 upsert CSV 裡有的列，不會刪除 CSV 移除掉的舊資料**，所以直接對 `db/riva_agent.db` 下 `DELETE FROM monthly_cost_actuals WHERE year_month='2026-04'`，把資料庫裡的殘留列也清掉（這是這支腳本的既有限制，之後如果要「取消」某個月的實際數字，不能只改 CSV，要記得手動處理資料庫或幫腳本加同步刪除的邏輯）
3. 重新執行 `calculate_pnl.py`，讓 2026-04 兩店都改回吃 `config/cost_rates.json` 的概算值

**這次更正推翻了先前一份分析報告的核心結論**：拿掉測試資料後，A 店 2026-04 其實跟其他月份一樣是虧損（先前誤算成獲利），B 店 2026-04 只是跟 3、5 月同等級的小賺（先前誤算成異常尖峰）。目前 **10 個月、兩店都還沒有任何一個月的實際成本數字**，全部靠概算值試算。真實樣貌是：**A 店連續 10 個月都是虧損**，**B 店在盈虧線附近上下小幅擺盪**（具體金額不記錄在此檔案，屬於真實財務數字，見零洩漏原則；使用者可從 `monthly_pnl` 資料表或網頁歷史走勢圖查詢實際數字）。這比「4 月特別賺」的說法嚴重得多，值得使用者優先關注，也再次印證「儘快補真實成本數字」是目前最重要的待辦。

## 重要設計慣例（新頁面接手時要知道）

1. **店別判斷優先順序**：報表有門市名稱欄 → 查 `.env` 對照表；沒有欄位但檔名有「X店」標記 → 用標記；都沒有 → 慣例視為 A 店（A 是最早、唯一店時期留下的命名慣例，印出提示不會靜默發生）
2. **重跑匯入的防重複邏輯**：收銀機明細/發票明細靠資料庫 UNIQUE 約束 + `INSERT OR REPLACE`/`INSERT OR IGNORE`；營收月報/銷售統計/時段占比沒有 UNIQUE 約束，靠「先算出這次要匯入的 (store_id, year_month)，刪掉這個範圍的舊資料再重灌」，**刻意不用檔名比對**（訂正檔換檔名會導致舊資料變孤兒，這是之前 review 抓到的真實 bug）
3. **零洩漏落實方式**：所有匯入程式在「店名對照不到」時的錯誤訊息都刻意不印出真實店名
4. 所有 `raw_*` 表格是 Layer 1（貼近原始報表），`daily_revenue_validated`/`monthly_cost_actuals` 是 Layer 2（跨來源驗證過），下游分析模組原則上只查 Layer 2。`monthly_revenue_manual` 也算 Layer 2，但屬於「備援」而非「稽核過」，只有 POS 資料完全缺席時才會被使用，且 `monthly_pnl.revenue_source` 會忠實記錄來源
5. **改 `scripts/` 底下的檔案後，網頁要重啟才會生效**：Streamlit 每次互動只會重跑 `app.py` 本身，不會重新 import 已載入的子模組（例如 `scripts/calculate_pnl.py`），改完子模組要 `kill` 掉背景的 streamlit process 再重新 `./scripts/run_app.sh`，只改 `app.py` 本身則不用重啟（2026-07-08 踩過這個雷，症狀是網頁噴 `KeyError`／行為對不上新程式碼）

## 月盈虧上雲部署（進行中，2026-07-08 開始）

決策：月盈虧要用 GitHub + 雲端部署，排班建議「較機密」暫不上雲、繼續留在本機（Tailscale 內網之後再評估）。部署後會新增一個獨立入口 `app_pnl.py`（只 import 月盈虧相關程式），跟現有 `app.py`（本機用，含排班）分開，即使程式碼都在同一個 private repo，雲端網頁也連不到排班功能。

**里程碑 1（已完成）：Turso 雲端資料庫建立**
- 帳號：Turso（帳號代稱 `jessie`），資料庫名稱 `riva-agent-pnl`（空殼名稱，不含店家資訊）
- 連線資訊（`TURSO_DATABASE_URL`／`TURSO_AUTH_TOKEN`）存在本機 `.env`，已加進 `.env.example` 的空白樣板
- **重要踩雷記錄**：`libsql` 這個 Python 套件在 Python 3.14 + Intel Mac 上跟 `cryptography` 一樣，沒有現成編譯版本，當場編譯又缺 `cmake`／完整 Rust 工具鏈，嘗試用 pip 裝 `cmake` 套件本身也壞掉。**改用 Turso 的「SQL over HTTP」介面**（純 `requests` 打 `POST https://<db>.turso.io/v2/pipeline`，帶 JSON payload），已驗證建表/寫入/讀取/清除都成功。**之後接手這個功能的人不要再嘗試裝 `libsql`，直接用 HTTP 版本。**
- 影響：里程碑 2（把 `scripts/calculate_pnl.py` 等程式改成可連 Turso）不是簡單換一行連線字串，而是要寫一個小翻譯層，把現有 `conn.execute(sql, params)` 這種 sqlite3 風格呼叫轉成背後的 HTTP pipeline 請求

**里程碑 2（已完成）：翻譯層 + 雲端 schema**
- 新增 `scripts/turso_client.py`：`TursoConnection` 用純 HTTP（`requests`）模仿 sqlite3 的 `execute().fetchall()/.fetchone()` 介面，`commit()/close()` 只是介面相容用的空函式
- 新增 `db/schema_cloud.sql`：**只放月盈虧網頁需要的 5 張表**（`stores`／`daily_revenue_validated`／`monthly_cost_actuals`／`monthly_revenue_manual`／`monthly_pnl`），是 `db/schema.sql` 的子集，**刻意不含任何 Layer 1 原始報表表格（收銀機/發票/銷售明細）跟排班相關表格**——這些最貼近原始 POS 報表的細節資料完全不會離開本機，只有「稽核過的月彙總數字」會上雲，比原本規劃多了一層零洩漏保障
- 已建到 Turso 上，`stores` 塞了 `A`/`B` 兩個代號
- **關鍵驗證**：用明顯是假資料的測試月份（`2099-01`），直接把 `TursoConnection` 傳給 `scripts/calculate_pnl.py` 既有的 `calculate_one()`／`save_pnl_result()`，**完全沒改這兩個函式一行程式碼**，跑出來的月盈虧試算結果、寫入 `monthly_pnl` 都正確。測試資料已清除，雲端資料庫目前是乾淨狀態（5 張空表 + stores 兩筆代號）。這證明當初「翻譯層」的設計方向是對的：`calculate_pnl.py` 的商業邏輯本來就只依賴 `conn.execute()` 這個介面，不用因為換資料庫而重寫

**里程碑 3（已完成）：`app_pnl.py` 獨立入口**
- 新增 `app_pnl.py`：從 `app.py` 搬出月盈虧相關的部分（`render_pnl_page`／`render_manual_revenue_section`），排班相關程式碼完全沒有 import。資料庫連線改用 `TursoConnection`，優先讀 `st.secrets`（雲端部署後用），本機測試 fallback 讀 `.env`（`_get_secret()` 函式處理這個切換，兩邊共用同一份程式碼不用分岔）
- **效能優化**：`TursoConnection` 原本每次查詢都重新建立一次 HTTPS 連線，一頁面 9~10 次查詢會很慢；改成用 `requests.Session` 重複使用連線 (keep-alive)
- **已用 Playwright 實測驗證（本機 port 8502，非正式 demo 帳密，測試後已重設）**：登入 → 選店別 A 店 → 展開「手動輸入月營收」→ 輸入測試月份 `2099-01`／營收 100000 → 儲存 → 直接查 Turso 確認寫入成功 → 月份下拉選單正確出現 `2099-01` → 試算結果正確顯示（營收 100,000、稅後淨利 -353,000，跟本機固定成本設定相符）→ 歷史走勢區塊正常。**測試資料已清除，Turso 恢復乾淨狀態。**
- **效能結論（已於里程碑 5 驗證，問題已關閉）**：除錯機器測試量到 14 秒，一度懷疑要換 Turso 機房；部署到 Streamlit Cloud 後，使用者實測真實載入時間是 **1.3 秒**。證實當初的猜測是對的——慢的是「除錯機器所在網路 → 美國西岸 Turso」這段路徑本身，不是 Turso 或程式碼的問題，正式路徑（Streamlit Cloud 伺服器 → Turso）速度完全沒問題。**不用換機房，也不用做查詢批次化。**

**里程碑 4（已完成）：GitHub private repo**
- 安裝 GitHub CLI (`gh`，官方 zip 下載到 `~/.local/gh`，非 Homebrew)，用 `gh auth login --web` 完成裝置授權登入（帳號 `etmail-bit`）
- 推送前逐一檢查即將進版控的檔案清單（`git add -A -n` 預覽），並全文搜尋真實店名/員工姓名確認沒有漏網
- **推送前抓到兩個零洩漏疏漏，已修正**：
  1. `PROGRESS.md` 用真實存在的員工代碼對照當說明範例（不是巧合，是 `.env` 裡真的有這組對照），改成虛構名字「王小美→美」
  2. `PROGRESS.md` 一段更正紀錄裡寫了 A/B 兩店真實月淨利金額（違反本檔案自己訂的「真實財務數字不可進版控」規則），改成只保留質化結論、拿掉具體數字
- 建立 private repo `etmail-bit/riva-agent`，第一次 commit 28 個檔案已推送，**已用 `gh repo view` 確認 visibility 為 PRIVATE**
- 本機 git 目前綁定 remote `origin` → `https://github.com/etmail-bit/riva-agent.git`

**重大調整（2026-07-09）：repo 從 private 改成 public**

部署當下才發現 Streamlit Community Cloud 的**免費方案已經不支援部署私有 repo**（這是平台政策變更，最早 2025-03 就有人在官方論壇反映，不是我們操作錯誤）；私有 repo 現在要透過付費的 Snowflake 整合才能部署，設定複雜度也高很多（論壇上有人反映花了 4~5 小時）。

跟使用者確認後，決定**改用公開 repo**，理由：程式碼本身已經過徹底的零洩漏檢查（無真實店名/員工姓名/財務數字/密碼/金鑰），公開後別人看到的只是「計算邏輯」，看不到任何真實資料——網頁仍需帳密登入，Turso 連線金鑰只存在 Streamlit Secrets，不在程式碼裡。對作品集目的來說，公開 repo 反而是加分（面試官看得到程式碼品質）。

**轉為公開前多做的兩層防護**：
1. **重寫 git 歷史**：改公開後任何人都能翻 commit 歷史，不能只顧現在的檔案內容。逐一 commit 檢查後發現一個更早的疏漏——之前在 `PROGRESS.md` 寫「修了什麼」的說明文字時，為了解釋清楚又把真實的員工代碼對照重複打了一次（描述問題時又重犯問題）。決定不逐一修補，直接用 `git checkout --orphan` 建一個全新的乾淨 commit 取代原本 3 次 commit 的完整歷史，`git push --force` 覆蓋遠端（repo 剛建立、沒人 clone 過，重寫歷史無副作用）
2. **`CLAUDE.md` 排除在外**：這份給 AI 助理看的個人背景說明（副業經營狀況、個人生涯規劃等）跟程式碼無關，公開沒必要一起曝光，加進 `.gitignore`、從版控移除，只留在本機

現在 repo（`etmail-bit/riva-agent`）是 **PUBLIC**，只有一次乾淨的 commit，已用 GitHub API 直接確認歷史紀錄乾淨。

**里程碑 5（已完成）：Streamlit Community Cloud 部署**

第一次部署後立刻噴 `FileNotFoundError`：`scripts/calculate_pnl.py` 的 `load_config()` 直接讀本機 `config/cost_rates.json`（真實成本數字，被 `.gitignore` 排除），雲端主機讀不到。這是跟帳號設定檔（`AUTH_CONFIG_YAML`）同一種模式的坑，之前只顧著修帳密那個，漏了這個。

修法（已推送）：`app_pnl.py` 新增 `load_cost_rates_config()`，雲端讀 Secrets 的 `COST_RATES_JSON`（整份 cost_rates.json 內容），本機 fallback 讀檔案；「儲存為新的預設值」按鈕在雲端模式下停用（雲端硬碟重啟會還原，寫入沒意義），比照密碼重設的處理方式。

**驗證方式（重要，之後改動 app_pnl.py 都要照這個流程）**：不能只測本機模式，要「本機模擬雲端模式」——在本機建一個假的 `.streamlit/secrets.toml`（放真實 Turso 連線資訊 + 真實 auth/cost 設定內容），重啟 Streamlit，實際登入操作確認 `is_cloud=True` 分支真的能跑，測完把 `.streamlit/secrets.toml` 內容清空（該檔已加進 `.gitignore`，但本機殘留明文密碼雜湊/金鑰不是好習慣）。這次用這個方法在本機就抓到並驗證修好了 `COST_RATES_JSON` 的問題，沒有再讓使用者在雲端上重複試錯。

2026-07-09 部署成功，使用者已在自己的終端機重設 `demo_admin` 為正式密碼（全程沒有經過我，密碼只有使用者知道），並把 `AUTH_CONFIG_YAML`／`COST_RATES_JSON`／`TURSO_DATABASE_URL`／`TURSO_AUTH_TOKEN` 更新進 Streamlit Cloud 的 Secrets。實際登入畫面確認：登入成功、雲端模式提示正確顯示、「A 店目前沒有已驗證的營收資料」警告正確（Turso 上還是空的，符合預期）、沒有任何錯誤訊息。

**里程碑 6（已完成，2026-07-09）：真實資料搬遷上 Turso**

跟使用者確認風險（真實金額透過網路傳到第三方雲端服務）後執行。新增 `scripts/migrate_layer2_to_turso.py`，把本機 `daily_revenue_validated`（602 筆）／`monthly_cost_actuals`（1 筆）／`monthly_pnl`（20 筆，跟使用者另外確認過後追加）用 `INSERT ... ON CONFLICT DO UPDATE` 寫進 Turso（冪等可重跑，本機資料異動後重跑即可同步最新狀態）。刻意不搬 Layer 1 原始報表（收銀機/發票/銷售明細）與排班相關表格，永遠只留在本機。

驗證：三張表筆數與加總金額（`revenue_from_register`／`net_profit`）本機/雲端比對一致；用「本機模擬雲端模式」（見里程碑 5 驗證方式）登入雲端版，A 店 2026-06 試算結果跟本機完全一致、B 店房租正確顯示 override 值、**歷史走勢圖（含兩店合計線）跟彙整建議區塊都正常顯示，數字與本機一致**。

同一次順便完成的網頁優化（使用者於 2026-07-09 提出，已用 Playwright 實測驗證）：
1. **成本設定支援單店 override**：`config/cost_rates.json` 新增 `fixed_costs_monthly_overrides`（key 是 store_id），`scripts/calculate_pnl.py` 新增 `get_fixed_cost(config, store_id, key)` 統一查詢邏輯，app.py／app_pnl.py 的固定成本輸入框、what-if 試算、「儲存為新的預設值」按鈕都改用這個函式（後者的邏輯是：這個店這個項目已經有 override 就繼續存回 override，否則存回共用預設值）。用來讓 B 店房租跟共用預設值分開調整（真實數字只在 `config/cost_rates.json`，不進版控；過程中有一次輸入金額打錯位數，使用者當場發現並更正，已重算）。
2. **歷史走勢圖新增「兩店合計淨利」線**：只加總「所有店都有資料」的月份（`HAVING COUNT(DISTINCT store_id) = 店數`），避免只有單店資料的月份被誤算成合計數字；用第三種顏色（`#d97706`）跟數值標籤（`dy=28`，避開跟稅後淨利標籤重疊）。
3. **月盈虧頁新增「各店經營現況」＋「建議」彙整區塊**：新增 `scripts/pnl_insights.py`，`generate_pnl_insights(conn)` 從 `monthly_pnl`／`monthly_cost_actuals` 即時查詢計算（虧損月數、固定成本占營收比、近期趨勢、是否有手動輸入月份等），組成 Markdown 顯示在走勢圖下方，**不把任何真實數字寫死在程式碼裡**，調整成本設定後結論會自動跟著變、不用改程式碼。套用目前最新設定跑出來的質化結論（**具體金額/比例不記錄在此檔案，屬於真實財務數字，見零洩漏原則；使用者可從網頁「彙整」頁面查詢實際數字**）：A 店持續虧損，B 店損益接近打平但虧損月份仍占多數，兩店合計目前仍是虧損。彙整建議裡也連結了先前「實際排班 vs 建議人力比對」發現的普遍超編現象，提醒人事成本概算可能被低估。

**待做**：
- 待決：`validate_revenue.py`／`import_cost_actuals.py` 這兩支「本機處理、寫 Layer 2」的腳本，之後要嘛改寫 Turso（讓本機匯入直接寫雲端），要嘛維持寫本機、另外一支同步腳本推上雲，之後每次本機資料異動（訂正、新增月份）都要記得重跑 `scripts/migrate_layer2_to_turso.py` 才會同步——兩種都可行，尚未定案

**Streamlit Cloud 的 `COST_RATES_JSON` Secret 已由使用者手動更新（2026-07-09 確認網頁已生效）。**

## 「彙整」頁面＋原始明細營運報告（已完成，2026-07-09）

使用者提出：月盈虧頁除了 A/B 兩店，希望能有第三個「彙整」選項，看兩店合計盈虧趨勢，以及分析發票／營收／收銀機明細（Layer 1 原始明細）產出的營運報告與建議。**前提（使用者明確提出）：加盟店不能自行調價、原物料成本也是固定的，所以建議一律只談操作面槓桿，不談調價／砍原物料成本。**

1. **「彙整」選項**：app.py／app_pnl.py 的店別下拉選單新增第三個選項（`stores + ["彙整"]`），選中後呼叫 `render_combined_pnl_page()`，跳過單店可調參數試算的所有邏輯，只顯示：兩店合計盈虧趨勢圖（3 條線：A 店/B 店/兩店合計稅後淨利，各店用不同色）＋ `generate_pnl_insights()` 彙整建議。app_pnl.py（雲端）版本到此為止；app.py（本機）版本下面多一段「營運報告」。
2. **`scripts/analyze_operations.py`（新增，只在本機用）**：讀 `raw_invoice_transactions`／`raw_revenue_monthly`／`raw_cash_register_daily`／`raw_hourly_pattern_monthly`（Layer 1，僅本機資料庫，10~11 萬筆等級），分析三個角度：
   - **通路組合**：把 `raw_revenue_monthly.order_type` 分成「外送平台」（會被抽 35% 佣金）跟「自取/外帶」兩組，算佔營收比例
   - **客單價分布**：`raw_invoice_transactions`（只算 `tx_status='正常'`）的平均/中位數/25分位/75分位
   - **尖峰時段**：`raw_hourly_pattern_monthly` 依 `daily_avg_sales` 排序找每店前幾名時段
   - 加上兩店比較（哪店外送佔比較高、客單價差多少、尖峰時段是否重疊），跟依「不能調價/原物料成本固定」前提寫的建議（通路組合／尖峰時段人力配置／客單價提升手法）
   - 輸出到 `reports/operational_report_<YYYY-MM-DD>.md`（新目錄，已加進 `.gitignore`，報告內容含真實通路/客單價數字不可進版控）。用法：`python3 -m scripts.analyze_operations`
3. **app.py 讀取報告**：`load_latest_operational_report()` 抓 `reports/` 底下最新一份 `operational_report_*.md`，`render_combined_pnl_page()` 用 `st.markdown()` 顯示在彙整建議下方。**這支函式跟 `analyze_operations.py` 都只有 app.py 會 import，app_pnl.py 完全沒引用**，符合零洩漏原則（Layer 1 原始明細衍生的分析不上雲）。

**本機真實資料跑出來的質化發現**（供之後決策參考，具體百分比/金額不記錄在此檔案，屬於真實營運數字，見零洩漏原則；使用者可從網頁「彙整」頁面或重跑 `analyze_operations.py` 查詢實際數字）：A 店外送平台佔營收比例明顯高於 B 店，代表 A 店有較高比例營收被平台佣金抽走，是 A 店持續虧損的其中一個結構性原因（疊加在先前已知的「人力普遍超編」發現之上）；兩店尖峰時段高度重疊在中午時段；兩店客單價中位數都偏低。

驗證：CLI 執行 `analyze_operations.py` 確認輸出內容正確；本機 Playwright 選「彙整」確認走勢圖+建議+營運報告都正確顯示；雲端版（本機模擬雲端模式）選「彙整」確認只顯示走勢圖+建議、不含營運報告（符合設計）。

**逐月成本結構表格（已完成，2026-07-09）**：使用者想要「by 月分析盈虧原因」，選擇「每個月列一行走勢表格」的形式。`scripts/pnl_insights.py` 新增 `generate_monthly_breakdown(conn, store_id)`，回傳逐月的成本結構（原物料/人事/房租/水電/平台抽成都是**占當月營收 %**，不是金額，讓不同營收規模的月份可以直接比較），app.py／app_pnl.py 都在各店的「歷史走勢」圖下方加一個 `st.dataframe` 顯示這張表，本機 Playwright 已驗證欄位正確、能一眼看出哪個月哪項成本比例偏高。

**重要踩雷記錄（2026-07-09）**：這台機器沒裝 `watchdog` 套件，Streamlit 用備用的檔案監控機制，改完 `app.py`／`app_pnl.py` 本身（不只是 `scripts/` 子模組）之後，瀏覽器不一定會自動載入新版本，**保險做法是每次改完都手動 `kill` 背景 process 再重新 `./scripts/run_app.sh`**，不要只等自動偵測。另外使用者一度誤以為改本機檔案雲端網頁就會跟著變——**本機修改跟雲端部署是兩件事**，本機的 `app.py`／`app_pnl.py`／`scripts/` 改動只有 `git push` 到 GitHub 之後，Streamlit Community Cloud 才會偵測到並自動重新部署（通常 1~2 分鐘），純粹改 `config/cost_rates.json` 這種本機檔案則永遠不會自動同步到雲端，要嘛靠 Secrets 手動貼、要嘛之後設計成從 Turso 讀。

**手機版走勢圖標籤重疊修正（已完成，2026-07-09）**：使用者用手機看雲端網頁，回報「兩店合計盈虧趨勢」圖上數值標籤全部疊在一起看不清楚（截圖存在本機 `手機截圖.png`，已加進 `.gitignore` 的 `*.png` 規則不會進版控）。原因：原本設計是「每個資料點都標數值」，3 條線 x 10 個月 = 30 個標籤，手機窄螢幕完全放不下。用 `dataviz` skill 檢查後確認這是已知的圖表反模式（規則：「標籤只挑重點標，不是每個點都標」）。

修法：新增 `scripts/chart_helpers.py` 的 `build_trend_chart()`，統一給 app.py／app_pnl.py 的所有月盈虧走勢圖使用（單店頁面 + 彙整頁面，共 4 處重複程式碼收斂成 1 個函式），**只標每條線「最新月份」那一個點**，其餘數值靠滑鼠 hover 的 tooltip，或圖表工具列內建的「Show data」表格、或本次新增的「逐月成本結構」表格取得（`dataviz` skill 規定：拿掉大部分標籤時，數值一定要能從其他管道查到，不能只靠 tooltip）。同時把色票丟進 skill 的 `validate_palette.js` 驗證過，四色 CVD 分離度與明度都過關。已用 Playwright 分別在手機寬度（390px）跟桌面寬度（1280px）截圖確認清爽可讀。

**這支 `build_trend_chart()` 是之後任何新走勢圖都應該直接呼叫的共用函式，不要再複製貼上舊的「每點都標籤」寫法。**

「彙整」頁面另外補了「兩店合計逐月明細」表格（app.py／app_pnl.py 都有），是 `st.dataframe`，手機上天生支援左右/上下滑動查看完整數字——這是使用者確認手機版修正時額外要求的：圖表標籤有限沒關係，但一定要有地方能滑動看到完整數字，不能只靠 tooltip。

## 「各店經營現況」彙整建議去攏統化（已完成，2026-07-09）

延續上面的待辦：`generate_pnl_insights()` 的「各店經營現況」跟「建議」段落，照原本記錄的三個方向都做了：

1. **具體點出哪個月、哪個成本項目**：`pnl_insights.py` 新增 `_pinpoint_worst_month()`，直接用 `generate_monthly_breakdown()` 現成的逐月資料（不重新查資料庫），找出該店淨利最差的月份、以及當月哪個成本科目 % 明顯高於**該店自己**的歷史平均（跟自己比，不跟別店比，因為兩店固定成本結構本來不同）。偏離不到 2 個百分點時 fallback 回原本的「固定成本占營收 X%」通用句。
2. **跟通路/客單價/尖峰時段交叉引用**：新增本機資料表 `store_operational_insights`（store_id → 濃縮結論文字，只放聚合統計、不放逐筆明細）。`scripts/analyze_operations.py` 算完營運報告後，順手把每店的結論句（通路佔比／客單價中位數／尖峰時段）寫進這張表；`pnl_insights.py` 讀到就接進「各店經營現況」那句話。**這張表跟 Layer 1 原始明細一樣刻意只留在本機**（雲端 Turso DB 沒有，`_table_exists()` 查不到就優雅跳過，app_pnl.py 不會噴錯，只是看不到這段）。
3. **建議段落補比較基準**：兩店營收比較補上具體金額/百分比差距，並在有 `store_operational_insights` 時反向連結回「各店經營現況」段落，兩段不再各講各的。

驗證：本機 DB（有 raw 表＋新表）跟模擬雲端 DB（比照 `schema_cloud.sql`，無 raw 表也無新表）都各跑過一次 `generate_pnl_insights()`，本機顯示完整內容、模擬雲端優雅 fallback 不噴錯；`app.py`／`app_pnl.py` 兩邊 import 都正常。

## 排班瓶頸分析與人力成本估算（已完成，2026-07-09）

使用者要求「從 raw data 挖出真正可以改善的問題，用經營者/改善者角度診斷」，並把外場產能參數從 40 校正為 30 杯/hr（已更新 `config/staffing_rules.json`，`calculate_staffing.py`／`compare_staffing.py` 的建議人力輸出因此改變，屬預期內）。

**核心發現（A 店 2026-06，樣本約半個月，B 店目前完全沒有排班原始資料，具體杯量/人力數字不記錄在此檔案，屬於真實營運數字，見零洩漏原則）**：不是產能算錯，是整天人力幾乎跟客流量脫鉤——杯量尖峰/離峰差距很大，實際在場人力卻幾乎是平線，尖峰離峰沒什麼差別。兩個具體瓶頸：
1. 三個早班（開早/早一/早二）在午餐尖峰全部重疊在場，現場人力明顯超過杯量需求——不是人請太多，是三個全天班的起訖時間排法讓尖峰疊加。
2. 晚班沒有隨晚間客流量遞減收斂，客流量探底後現場人力沒有跟著減少，一路撐到打烊。
3. 附帶發現：`config/staffing_rules.json` 原本的班別範本（晚班時間跟現場對不上）已校正成跟現場一致，並修掉 `calculate_staffing.py` 一個連帶的 bug（`is_shift_active()` 原本只看小時整數，半點結束的班會漏算最後半小時）。

**人力成本量化**：新增 `scripts/estimate_staffing_cost.py`（`python3 -m scripts.estimate_staffing_cost`），用真實薪資結構（2026-07-09 使用者提供：店長/店員固定月薪＋不含勞健保，超過每天 8 小時算加班；兼職全額時薪，皆存在 `config/staffing_rules.json` 的 `wages`／`employee_roles`，代碼對應角色、不放真名，之後有新人直接改這個檔案）反推兩種情境：
- **保守版**：尖峰時段（`config` 的 `scenario.peak_hours`）維持額外緩衝人力（`scenario.peak_floor_staff`），因為尖峰杯量已經接近 2 人產能上限，全砍到操作下限風險較高
- **積極版**：尖峰也套用跟離峰一樣的操作下限，是純公式算出來的理論上限，不建議直接照做，只當參考基準

兩版估計可省下的變動成本、以及用真實排班反推的人事成本跟系統預設概算值的比較（**意外發現**：方向剛好跟原本寫死在 `pnl_insights.py` 的「概算值可能低估真實負擔」假設相反），具體金額不記錄在此檔案，見 `reports/staffing_cost_estimate_<日期>.md`（`.gitignore` 排除，不進版控不上雲端）或網頁月盈虧頁「建議」段落（A 店已接上真實反推結論）。

**兩份結論的接回方式（使用者明確要求）**：`pnl_insights.py` 的「建議」段落改成逐店判斷——A 店（有 `store_staffing_insights` 資料）顯示真實排班反推的具體結論（含保守/積極兩版金額）；B 店（還沒有排班原始資料）繼續用原本的通用估計句。跟上面的 `store_operational_insights` 同一套設計：只在本機累積、雲端沒這張表就 fallback，之後要不要同步上雲端另外處理。

驗證：本機／模擬雲端兩條路徑都重新跑過，`estimate_staffing_cost.py` 輸出數字跟手算交叉核對一致，`app.py`／`app_pnl.py` import 正常。

## 教訓：「結論文字」不等於「安全可公開」（2026-07-09）

上一節做完後，使用者同意把 `store_operational_insights`／`store_staffing_insights` 同步上 Turso（公開雲端 DB），前提是「raw data 千萬不要放上去」。第一次同步時判斷失誤：**只確認了表格結構放不進逐筆原始資料，沒有檢查結論文字的「內容」本身**——`estimate_staffing_cost.py` 產生的結論句裡直接寫了真實人事成本、固定薪資金額；`analyze_operations.py` 的結論句裡也寫了真實客單價金額、營收佔比。這兩者都上傳到 Turso 了，發現後立刻從 Turso 刪除（本機資料未受影響）。

跟使用者確認公開範圍後，改成：
- `store_staffing_insights` 拆成兩欄：`summary_text`（完整版，含真實金額，只留本機，`app.py` 用）／`public_summary_text`（只放「預估可節省金額」，這一欄才會被同步）。`scripts/migrate_layer2_to_turso.py` 的 `migrate_staffing_insights()` 只讀 `public_summary_text`。
- `store_operational_insights`（通路組合／客單價）使用者決定完全不公開，已從 `db/schema_cloud.sql` 移除、Turso 上的表也已 `DROP TABLE`，這支同步腳本完全不處理這張表。
- 已重新驗證 Turso 上 `store_staffing_insights.summary_text` 現在只有「預估可節省金額」，沒有其他真實數字，`store_operational_insights` 表已不存在。

**通用教訓（已記錄進 memory 的 feedback，跨專案適用）**：判斷「能不能公開」不能只看資料表欄位設計，要連**自由文字欄位裡實際寫了什麼內容**都要逐一檢查——欄位名稱叫「摘要」「結論」不代表內容一定安全，尤其是這種「目的就是要講出具體數字」的分析型文字。

## 逐時段落差明細＋正職/兼職拆分（2026-07-09 完成，供下次對話討論人力配置優化）

在上面「排班瓶頸分析」的彙整數字之外，使用者要求要能「逐時段」驗證，不能只給一個加總後的數字。`estimate_staffing_cost.py` 新增 `hourly_breakdown()`，每個時段列出：杯量、實際平均人力（拆成正職／兼職兩欄）、保守/積極版合理人力、落差。過程中有兩次方法論修正，都是使用者當場糾正的：

1. **休息時間該不該扣**：第一版想把班表區間裡的無薪休息時間（真實時數跟起訖區間的差額，例如 8.5 小時的班只算 8 小時）平均分攤到整班每小時，讓逐時段數字更「精確」跟彙整數字對得起來。使用者指出這樣不直覺、也不合理——沒有人會挑尖峰時段休息，均勻分攤等於連尖峰都被扣產能。改良版試過「休息優先放在最閒的時段」，仍然是使用者最後拍板：**乾脆不要猜休息時間，班表寫幾點到幾點就算幾個人**，跟「排班建議」頁本來的算法（`compare_staffing.py` 的 `calculate_actual_hourly_average()`）一致，只是因此逐時段加總會比彙整表的「可省成本」略高（差額約等於休息時數），這個差異已經寫進報告的說明文字裡，不是算錯。
2. **正職/兼職要分開看**：新增 `actual_hourly_average_by_role()`，依 `config/staffing_rules.json` 的 `employee_roles` 分組。第一版用「重疊分鐘數比例」算，導致正職＋兼職加總對不起「實際平均人力」欄位；修正成跟 `calculate_actual_hourly_average()` 同一套算法（只要班表跟該時段有重疊就算滿 1 人），兩者才能剛好加總一致（只有四捨五入造成的 0.01 誤差）。

**看出來的初步規律（具體數字不記錄在此檔案，屬真實營運數字，見零洩漏原則，可從 `reports/staffing_cost_estimate_<日期>.md` 或網頁查詢）**：正職人力幾乎整天維持平穩、沒有隨時段明顯調整；超編的落差主要是兼職／加班在填，而且晚間時段（18 點後）兼職占比明顯升高。換句話說，超編不完全是「請太多人」，比較像是**正職班表本身沒有彈性、用兼職補洞**——這正是使用者想另開新對話深入討論的「人力配置優化」切入點。

**下次對話可以延續的方向（尚未定案，供接手參考）**：
- 正職班表要不要重新設計出更貼近需求曲線的班別（例如錯開起訖時間），而不是靠兼職／加班補洞
- 兼職排班有 `config/staffing_rules.json` 的 `part_time.min_hours=3` 下限，任何新提案的兼職班次都要符合
- B 店排班原始資料還沒謄打進來，只有 A 店可以做這個層級的分析
- **`scripts/estimate_staffing_cost.py` 目前有未 commit 的異動**（逐時段明細＋正職/兼職拆分功能），下次對話開始前可以先確認要不要 commit + push（純程式邏輯，不含真實數字，可安全公開）

若開新對話，這份文件加上 memory 裡的 `project-roadmap-v1` 應該足以還原上下文。

## 人力配置優化：平日/假日拆分＋外送人力抽離（2026-07-10 完成）

延續上一節「正職班表沒彈性、靠兼職補洞」的討論，這次對話深入拆解到「平日/假日」層級，並修正了一個容量計算的既有缺口。

**資料補齊**：使用者補上 A 店 2026-01~06（02月過年排除）逐一星期六/日的**真實**時段占比報表（放在新資料夾 `data/raw/週末時段占比/`），以及 A 店 5、6、7月排班照片的謄打資料（`data/staffing_actual_raw.csv` 擴充到 2026-05-01~07-09，70天）。過程中用「內容比對」（而非檔名）抓到 6 個重複匯出的檔案，已排除；另抓到一筆真實異常值（單一天某時段杯數突出），使用者確認排除不列入計算。新員工「毅」（只在5月排班出現過）已建代碼並設為兼職角色。

**核心發現（具體數字不記錄在此檔案，屬真實營運數字，見零洩漏原則，可從 `reports/` 或網頁查詢）**：用真實假日杯數 + 月彙總杯數代數反推平日均，發現**平日全天稼働率沒有任何時段逼近2人產能上限，假日中午到下午則有多個時段逼近甚至超過上限**。這推翻了原本「尖峰時段（11-14點）不分平日假日都要加開兼職」的假設——實際上是**假日才真正需要加開兼位，平日的常態加開站不住腳**。`config/staffing_rules.json` 的 `scenario` 已新增 `weekday_peak_hours`／`weekend_peak_hours` 兩個欄位記錄這個結論，供人工排班參考（尚未接進 `calculate_staffing.py`／`estimate_staffing_cost.py` 的自動計算，因為杯數資料源頭本身還是月彙總、無法自動判斷平日假日）。

**外送人力抽離公式修正（重要）**：使用者提醒「外送單（店家自己送，非平台叫車）每單會抽走一個人力出去送，需要另外扣人力」，原本 `calculate_staffing.py` 假設外送單「已含在日均杯數裡，不用另外算」是錯的。已修正：
1. 修好一個既有 bug——`raw_hourly_pattern_monthly` 的 `delivery_count`／`platform_count` 兩欄存的是「當月累計總數」，不是日均值，之前只當參考欄印出來所以沒被抓到，現在要拿來做容量計算，已改成除以當月天數。
2. `calculate_staffing.py` 新增 `calculate_delivery_hours()`，`估計人力 = ceil(杯數/產能 + 外送單數×履約分鐘數/60)`，`estimate_staffing_cost.py` 的 `_required_staff_per_hour()` 共用同一個函式。兩支程式跟 `app.py` 排班頁都已測試正常運作。
3. **使用者口頭估計「每日約12單」，但系統實測的真實外送單數明顯低於這個數字**，原因未知（可能有電話叫貨等訂單沒被系統正確歸類進「外送」欄位）。目前公式採用系統實測的真實數字，這個落差記在 `config/staffing_rules.json` 的 `delivery.note`，之後需要使用者確認差距原因。

## 月盈虧新增「原物料損耗」成本線（2026-07-10 完成）

使用者以經理人角度檢視盈虧改善方向時，指示先用「原物料成本的 4%」概算原物料損耗/報廢（過期、備料超量、給料誤差等，目前無實際盤點數字）。已完成：

1. `monthly_pnl` 資料表新增 `material_waste` 欄位（`db/schema.sql`／`db/schema_cloud.sql`／實際 `db/riva_agent.db` 都已同步 ALTER）
2. `config/cost_rates.json` 新增 `material_waste_pct`（`.example.json` 同步更新範本）
3. `calculate_pnl.py` 的核心公式在「原物料」之後、「平台抽成」之前新增這條扣除線（`material_waste = cogs × material_waste_pct`），`save_pnl_result()`／`app.py`／`app_pnl.py` 的成本瀑布圖／`pnl_insights.py` 的逐月成本結構表都同步顯示這一項
4. `scripts/migrate_layer2_to_turso.py` 的 `migrate_monthly_pnl()` 欄位清單同步更新（**尚未實際跑同步，Turso 上的 `monthly_pnl` 表結構也需要之後另外 ALTER 才能接住新欄位**，云端部署的 `COST_RATES_JSON` Secret 之後也要記得補上 `material_waste_pct`，不然雲端版試算不會套用這項）
5. 已重新執行 `calculate_pnl.py`，20 筆歷史月份（兩店各10個月）都已重算納入這條新成本線，`monthly_pnl` 是既有紀錄會被覆蓋更新，不是新增獨立紀錄

**待確認**：使用者同時提到「平台抽成35%」的疑問，經確認 `config/cost_rates.json` 的 `platform_commission.ubereats/foodpanda` 本來就已經是這個真實費率、且已經在 `calculate_pnl.py` 裡實際套用在真實的 `ubereats_amount`/`foodpanda_amount`（來自 `raw_cash_register_daily`）——**這項不是缺口，是先前已經做好的部分，這次分析報告把它誤列為「還需要的資料」，屬於分析過度謹慎，已跟使用者澄清**。

## 排班平日/假日分析正式落地成網頁功能＋回頭客分析上網頁（2026-07-10 完成）

使用者確認「外送來客數」時段占比報表就是真實資料（不是使用者口頭估的12單/天），公式維持用系統實測值；接著指示把上面兩節的分析正式做成網頁功能，並把回頭客分析也放上網頁——**使用者明確表示總部已經有集點/會員制度，不需要另外設計會員方案，這次只上「發現」不做「行動方案」**。

**新增 `raw_hourly_pattern_daily` 資料表**（`db/schema.sql`／實際 db 都已建表）：存單一天（非月彙總）的時段占比樣本，目前只有 A 店的星期六/日抽樣。`scripts/import_hourly_pattern_daily.py` 負責匯入 `data/raw/週末時段占比/` 底下的檔案，從檔名「第N個星期六/天」+ 該月行事曆算出真實 `business_date`（假設檔名序號照日期排序），一樣用「內容比對」去重複。原本臨時分析裡發現的異常值（2026-05-02 14點的杯數）已直接從這張表刪掉那一格（不是在分析程式裡加排除清單），源頭乾淨，下游不用再處理。

**新增 `scripts/analyze_staffing_daytype.py`**：把之前臨時寫的分析整理成正式模組，`cup_stats_by_daytype()`／`roster_mode_by_weekday()` 兩個函式供 CLI 跟 `app.py` 共用。

**`app.py` 排班建議頁新增兩個區塊**：「平日/假日逐時段杯數」「星期幾 x 時段實際排班人力（正職/兼職眾數）」，都在原有的「班別彙總」跟「實際排班 vs 建議人力比對」中間，B 店目前沒有這兩塊資料時會顯示提示訊息、不會噴錯。

**過程中抓到並修好一個既有 bug**：`render_staffing_page()` 原本用一個手寫的最小 dict 當 `working_config`（只有 `capacity`/`tea_brewing`/`shifts` 三個 key），這次要讀 `employee_roles`（給 `roster_mode_by_weekday` 用）跟 `delivery`（給稍早新增的外送耗時公式用）時直接 `KeyError`。已改成 `copy.deepcopy(saved_config)` 再覆寫 UI 可調欄位，比照月盈虧頁本來就用的模式，兩頁現在做法一致。已用 Playwright 實測登入排班建議頁確認兩個新區塊正確顯示、且原有的「實際排班 vs 建議人力比對」沒有壞掉；B 店測過 `cup_stats_by_daytype()`/`roster_mode_by_weekday()` 直接呼叫回傳空結果、不噴錯。**測試過程中用 `manage_accounts.py` 暫時重設了 `demo_admin` 的密碼做登入驗證，使用者需要自己重設回想要的密碼。**

**`scripts/analyze_operations.py` 新增回頭客分析**：`repeat_customer_stats()` 用 `carrier_no` 算聚合統計（回頭客佔比、回頭客營收貢獻、逐月老客比例趨勢），比照既有函式的模式，只回傳聚合數字、不留任何個人層級資料。已接進 `build_report()`（單店段落＋兩店比較段落）跟 `build_operational_summary()`（給 `store_operational_insights` 交叉引用），`app.py` 的「彙整」頁本來就會載入最新的 `operational_report_*.md`，不用另外接線就會顯示。已用 CLI 跟 Playwright（彙整頁）都測過正常顯示。

**Excel 匯出**：`reports/staffing_daytype_summary_2026-07-10.xlsx`（本機限定），供使用者離線參考；這份是分析正式落地成網頁功能之前的過渡產物，之後網頁本身就能查看，不用再另外匯出 Excel。

## 建立 `monthly-ops-refresh` skill（2026-07-10 完成）

使用者要求把「每個月拿到新一批 POS 檔案→產出報告」這整套流程做成 skill，用 `skill-creator` 建立。位置：`.claude/skills/monthly-ops-refresh/`（`SKILL.md` + `references/pipeline_steps.md` + `references/zero_leakage_gates.md`），已加進 `.gitignore`（順便發現 `.claude/skills/` 之前沒被任何規則排除到，`.claude/settings.local.json`／`scheduled_tasks.lock` 是靠全域 gitignore 排除，這次補上專案層級的明確排除，不再只靠全域設定）。

Skill 內容涵蓋：完整 Layer 1→2→3 管線執行順序、排班照片謄打的信心分級規則、既有踩過的資料品質陷阱（檔名標錯月份、內容重複檔等）、零洩漏鐵則（**雲端同步 `migrate_layer2_to_turso.py` 絕對不能自己決定要不要跑，一定要當次明確經使用者同意**，附這個專案真實踩過的洩漏案例當「為什麼」）。使用者確認先不用跑正式的 eval/benchmark 流程，直接看草稿內容確認即可（skill-creator 本身支援這種輕量模式）。

**跟使用者確認繁體中文一致性**：檢查時發現 `SKILL.md` 的 frontmatter `description` 欄位原本寫成英文，跟 skill 本文與其他所有溝通不一致，已改成繁體中文（`name` 欄位維持技術識別碼慣例，保留小寫連字號格式）。確認之後這個專案所有溝通與 skill 內容都用繁體中文製作。

## 彙整頁圖表化＋新增「時段人力與杯數」獨立頁面（2026-07-10 完成）

使用者反饋「彙整頁的營運報告都是文字說明，圖表比較好懂」，同意後做了三個圖表（都在「彙整」頁，用真實資料即時算，不是讀寫死的報告文字）：
1. **通路組合長條圖**（外送平台 vs 自取/外帶佔營收 %，兩店並排比較）
2. **回訪次數分布長條圖**（1次/2次/3~5次/6~10次/11次以上，佔客數 % ，兩店並排比較）
3. **回頭客佔比逐月成長趨勢折線圖**（沿用既有的 `chart_helpers.build_trend_chart()`，這次順便把這支共用函式的 y 軸欄位/標題/數字格式改成可傳參數，預設值維持跟原本金額走勢圖一樣，向後相容）

`scripts/analyze_operations.py` 的 `repeat_customer_stats()` 為此新增兩個回傳欄位：`visit_buckets`（回訪次數分布）跟 `monthly_trend`（逐月回訪比例明細，之前只回傳最新一個月的數字，現在回傳整個月份序列給圖表用）。

**新增獨立頁面「時段人力與杯數」**（跟「月盈虧」「排班建議」同一層級的網頁選單項目，admin/staff 都看得到）：把原本埋在「排班建議」頁裡的「平日/假日逐時段杯數」跟「星期幾 x 時段實際排班人力（正職/兼職眾數）」兩個區塊搬出來獨立成一頁，讓「排班建議」頁專注在參數調整＋建議人力＋比對，這頁專注在純觀察用的實際資料。

已用 Playwright 實測「彙整」頁三個圖表跟「時段人力與杯數」頁都正確顯示、無錯誤。這次的新增內容也同步補進 `monthly-ops-refresh` skill 的 `SKILL.md`（第 5 步「網頁自動反映」）跟 `references/pipeline_steps.md`（`analyze_operations.py` 函式被 CLI 報告跟網頁圖表共用的說明）。
