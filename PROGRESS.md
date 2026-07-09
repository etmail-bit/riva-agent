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

外送單量已經包含在日均杯數裡，不重複疊加。煮茶時段（預設 07:30 起 1 小時）該人力不計入前場產能，另外標註 +1。參數在 `config/staffing_rules.json`：單位產能 40 杯/hr、4 個班別時間窗（煮茶班/早班1/早班2/晚班）、兼職 3~4 小時。**這些參數都是使用者給的估計值，還在校準，之後要能在網頁上調整。**

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

**重要發現（2026-07-08，待使用者後續核對，先記錄不深入討論）**：用現有這批資料（A 店 2026-06-16~30）跑出來，**13 個時段全部超編、0 個時段人力不足**，尤其中午 11-14 點，建議只要 1-2 人，實際卻排了 3.7 人左右。可能原因：①單位產能「40 杯/hr」估計偏高（實際做不到）②這些時段有出杯以外的工作（備料/訓練/交班）③本來就故意排多當緩衝。之後把剩下 5 張照片也謄打進來，可以看這個超編模式是不是每個月都一樣。

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
- 影響：里程碥 2（把 `scripts/calculate_pnl.py` 等程式改成可連 Turso）不是簡單換一行連線字串，而是要寫一個小翻譯層，把現有 `conn.execute(sql, params)` 這種 sqlite3 風格呼叫轉成背後的 HTTP pipeline 請求

**里程碑 2（已完成）：翻譯層 + 雲端 schema**
- 新增 `scripts/turso_client.py`：`TursoConnection` 用純 HTTP（`requests`）模仿 sqlite3 的 `execute().fetchall()/.fetchone()` 介面，`commit()/close()` 只是介面相容用的空函式
- 新增 `db/schema_cloud.sql`：**只放月盈虧網頁需要的 5 張表**（`stores`／`daily_revenue_validated`／`monthly_cost_actuals`／`monthly_revenue_manual`／`monthly_pnl`），是 `db/schema.sql` 的子集，**刻意不含任何 Layer 1 原始報表表格（收銀機/發票/銷售明細）跟排班相關表格**——這些最貼近原始 POS 報表的細節資料完全不會離開本機，只有「稽核過的月彙總數字」會上雲，比原本規劃多了一層零洩漏保障
- 已建到 Turso 上，`stores` 塞了 `A`/`B` 兩個代號
- **關鍵驗證**：用明顯是假資料的測試月份（`2099-01`），直接把 `TursoConnection` 傳給 `scripts/calculate_pnl.py` 既有的 `calculate_one()`／`save_pnl_result()`，**完全沒改這兩個函式一行程式碼**，跑出來的月盈虧試算結果、寫入 `monthly_pnl` 都正確。測試資料已清除，雲端資料庫目前是乾淨狀態（5 張空表 + stores 兩筆代號）。這證明當初「翻譯層」的設計方向是對的：`calculate_pnl.py` 的商業邏輯本來就只依賴 `conn.execute()` 這個介面，不用因為換資料庫而重寫

**里程碑 3（進行中）：`app_pnl.py` 獨立入口**
- 新增 `app_pnl.py`：從 `app.py` 搬出月盈虧相關的部分（`render_pnl_page`／`render_manual_revenue_section`），排班相關程式碼完全沒有 import。資料庫連線改用 `TursoConnection`，優先讀 `st.secrets`（雲端部署後用），本機測試 fallback 讀 `.env`（`_get_secret()` 函式處理這個切換，兩邊共用同一份程式碼不用分岔）
- **效能優化**：`TursoConnection` 原本每次查詢都重新建立一次 HTTPS 連線，一頁面 9~10 次查詢會很慢；改成用 `requests.Session` 重複使用連線 (keep-alive)
- **已用 Playwright 實測驗證（本機 port 8502，非正式 demo 帳密，測試後已重設）**：登入 → 選店別 A 店 → 展開「手動輸入月營收」→ 輸入測試月份 `2099-01`／營收 100000 → 儲存 → 直接查 Turso 確認寫入成功 → 月份下拉選單正確出現 `2099-01` → 試算結果正確顯示（營收 100,000、稅後淨利 -353,000，跟本機固定成本設定相符）→ 歷史走勢區塊正常。**測試資料已清除，Turso 恢復乾淨狀態。**
- **效能觀察（待里程碑 4 後重新評估，不要現在就下定論）**：從這台除錯用機器測試，整頁跑完約 14 秒（優化前 18 秒）。這個數字量測的是「除錯機器所在網路 → 美國西岸 Turso」的路徑，**不代表未來正式部署後的真實路徑**（正式路徑是「Streamlit Community Cloud 伺服器 → Turso」，兩邊很可能同樣在美國，速度可能完全不同）。目前先不因為這個不準的數字搬機房（Turso 有東京機房 `aws-ap-northeast-1` 可選，之後真的需要再搬，現在資料庫是空的搬遷成本很低）。**等里程碑 4 部署上去後，要重新實測一次真實載入速度，若還是慢再決定要不要換機房或做查詢批次化。**
**里程碑 3（已完成）：GitHub private repo**
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

**里程碑 4（已完成）：Streamlit Community Cloud 部署**

第一次部署後立刻噴 `FileNotFoundError`：`scripts/calculate_pnl.py` 的 `load_config()` 直接讀本機 `config/cost_rates.json`（真實成本數字，被 `.gitignore` 排除），雲端主機讀不到。這是跟帳號設定檔（`AUTH_CONFIG_YAML`）同一種模式的坑，之前只顧著修帳密那個，漏了這個。

修法（已推送）：`app_pnl.py` 新增 `load_cost_rates_config()`，雲端讀 Secrets 的 `COST_RATES_JSON`（整份 cost_rates.json 內容），本機 fallback 讀檔案；「儲存為新的預設值」按鈕在雲端模式下停用（雲端硬碟重啟會還原，寫入沒意義），比照密碼重設的處理方式。

**驗證方式（重要，之後改動 app_pnl.py 都要照這個流程）**：不能只測本機模式，要「本機模擬雲端模式」——在本機建一個假的 `.streamlit/secrets.toml`（放真實 Turso 連線資訊 + 真實 auth/cost 設定內容），重啟 Streamlit，實際登入操作確認 `is_cloud=True` 分支真的能跑，測完把 `.streamlit/secrets.toml` 內容清空（該檔已加進 `.gitignore`，但本機殘留明文密碼雜湊/金鑰不是好習慣）。這次用這個方法在本機就抓到並驗證修好了 `COST_RATES_JSON` 的問題，沒有再讓使用者在雲端上重複試錯。

2026-07-09 部署成功，使用者已在自己的終端機重設 `demo_admin` 為正式密碼（全程沒有經過我，密碼只有使用者知道），並把 `AUTH_CONFIG_YAML`／`COST_RATES_JSON`／`TURSO_DATABASE_URL`／`TURSO_AUTH_TOKEN` 更新進 Streamlit Cloud 的 Secrets。實際登入畫面確認：登入成功、雲端模式提示正確顯示、「A 店目前沒有已驗證的營收資料」警告正確（Turso 上還是空的，符合預期）、沒有任何錯誤訊息。

**待做**：
- 部署成功後**要重新實測載入速度**（見上方效能觀察那段），問使用者實際操作時的主觀感受
- 里程碥 5：把本機真實資料的 Layer 2 彙總結果（`daily_revenue_validated`／`monthly_cost_actuals`）從本機 `db/riva_agent.db` 灌一份進 Turso，正式開始使用（這步會動到真實財務數字上雲，需要跟使用者再次確認）
- 待決：`validate_revenue.py`／`import_cost_actuals.py` 這兩支「本機處理、寫 Layer 2」的腳本，之後要嘛改寫 Turso（讓本機匯入直接寫雲端），要嘛維持寫本機、另外一支同步腳本推上雲——兩種都可行，尚未定案，等里程碥 4 全部做完（先確認整條路能通）再決定

## 這次對話最後在討論的方向

2026-07-08 完成四項網頁優化，皆已用 Playwright 實測驗證：
1. 月盈虧頁試算參數從 5 項可調擴大到 11 項（開放平台/金流費率、稅率），分組成 4 個摺疊區塊
2. 新增 `monthly_revenue_manual` 表 + `monthly_pnl.revenue_source` 欄位，`calculate_pnl.py` 改成 POS 優先、無資料才 fallback 手動輸入
3. 月盈虧頁新增「手動輸入月營收」表單（預填既有值、合理性檢查、可清除）
4. 月盈虧歷史走勢圖加數值標籤

月盈虧、排班建議兩個網頁頁面（皆含可調參數 what-if 試算），加上「實際排班 vs 建議人力比對」跟啟動腳本，都已完成並驗證過。下一步在「待完成」列的候選方向裡挑，尚未定案要先做哪一個。若開新對話，這份文件加上 memory 裡的 `project-roadmap-v1` 應該足以還原上下文。
