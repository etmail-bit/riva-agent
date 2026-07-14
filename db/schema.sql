-- 飲料店營運效能優化系統 — 資料庫結構
-- 本檔案只有表格定義，不含任何真實資料，可安全進版控。

PRAGMA foreign_keys = ON;

-- 店家主檔：只存代號，真實店名只存在 .env，這裡永遠看不到
CREATE TABLE stores (
    store_id TEXT PRIMARY KEY   -- 'A', 'B'
);

-- 員工主檔：只存代碼（姓名中最有辨識度的一個字），真實姓名只存在 .env，這裡永遠看不到
CREATE TABLE employees (
    employee_code TEXT PRIMARY KEY   -- 'A', '梅', '葉', ...
);

-- ============================================================
-- Layer 1：landing tables — 貼近原始報表，只做型別與店名代號正規化
-- 目的是保留可追溯性（哪個檔案、哪時候匯入的），跨月欄位名稱不一致
-- 的問題在「匯入程式」那一關就要處理掉，這裡看到的欄位名稱是統一過的。
-- ============================================================

CREATE TABLE raw_invoice_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    register_no TEXT,
    invoice_serial TEXT,
    invoice_no TEXT,
    tx_status TEXT,   -- 實測資料含 '正常'/'作廢'；作廢單金額仍會填。任何加總/跨來源比對一律改查下面的 invoice_transactions_valid view，不要直接查這張表
    tx_time TEXT NOT NULL,       -- ISO8601: 'YYYY-MM-DD HH:MM:SS'
    amount INTEGER NOT NULL,
    carrier_no TEXT,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 發票號在同一間店裡不能重複；用來防止同一張發票被不同檔案（例如誤植/重複匯出的月份檔）重複灌入
CREATE UNIQUE INDEX idx_invoice_unique ON raw_invoice_transactions(store_id, invoice_no);

CREATE TABLE raw_cash_register_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,   -- 'YYYY-MM-DD'
    register_no TEXT,
    gross_revenue INTEGER NOT NULL,
    ubereats_amount INTEGER NOT NULL DEFAULT 0,
    foodpanda_amount INTEGER NOT NULL DEFAULT 0,
    credit_card_amount INTEGER NOT NULL DEFAULT 0,
    other_electronic_amount INTEGER NOT NULL DEFAULT 0,
    taxable_revenue INTEGER NOT NULL DEFAULT 0,     -- 應稅：算營業稅只用這個欄位，不要用 gross_revenue
    tax_exempt_revenue INTEGER NOT NULL DEFAULT 0,  -- 免稅
    cash_outflow INTEGER NOT NULL DEFAULT 0,   -- 用途待查證，暫不納入成本計算
    payment_breakdown_json TEXT NOT NULL,      -- 原始 10+ 種支付欄位原封不動存 JSON，避免表格跟著報表細節一直改
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 自然鍵只有「店+日期+收銀機」，刻意不含 source_file：
    -- 這樣同一天的報表換了檔名重新匯入（例如訂正檔）時，INSERT OR REPLACE 才會真的覆蓋舊資料，
    -- 而不是被當成新的一列插入、導致跨來源比對時營收被悄悄加倍。
    UNIQUE(store_id, business_date, register_no)
);

CREATE TABLE raw_revenue_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,     -- 'YYYY-MM'
    order_type TEXT NOT NULL,     -- 單別/消費方式，跨月欄位名稱不同但值統一存這裡
    amount INTEGER NOT NULL,
    pct_of_total REAL,
    cup_count INTEGER,     -- 杯數（實際飲料杯數），跟 order_count 顆粒度不同，不能互相頂替
    order_count INTEGER,   -- 訂單數（一張訂單可能含多杯），只有部分月份報表有提供
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE raw_product_sales_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    hour_slot TEXT,    -- 只有 B 店的報表有拆時段（一個品項一個月會有多列）；A 店整月一列，這欄是 NULL
    amount INTEGER,    -- 只有 B 店的報表有金額；A 店這欄是 NULL，之後熱銷分析要注意不能兩店用同一種算法
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 實際排班紀錄：原始班表照抄，還沒跟 calculate_staffing.py 算出的建議人力比對過
CREATE TABLE raw_staffing_actual (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,          -- 'YYYY-MM-DD'
    employee_code TEXT NOT NULL REFERENCES employees(employee_code),
    shift_label TEXT,                     -- 原表班別代碼，如「早一」「開早」「假」「休」「公」「補」
    start_time TEXT,                      -- 'HH:MM'，當天沒上班（假/休等）為 NULL
    end_time TEXT,
    scheduled_hours REAL,                 -- 原表「時數」欄
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, business_date, employee_code)
);

CREATE TABLE raw_hourly_pattern_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    hour_slot TEXT NOT NULL,        -- '09', '10', ...
    walkin_count INTEGER,
    pickup_count INTEGER,
    delivery_count INTEGER,
    platform_count INTEGER,
    sales_amount INTEGER,
    pct_of_total REAL,
    daily_avg_sales INTEGER,
    daily_avg_cups INTEGER,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2026-07-10 新增：單一天（非月彙總）的時段占比樣本，目前只有使用者額外提供的
-- 星期六/日抽樣（data/raw/週末時段占比/），用來反推真實的平日/假日逐時段杯數
-- （raw_hourly_pattern_monthly 是月彙總、無法區分平日假日）。business_date 是從
-- 檔名「第N個星期六/日」+ 該月是否為 2026-02 例外，配合當月行事曆算出來的，
-- 假設檔名的序號是照日期由小到大排的。
CREATE TABLE raw_hourly_pattern_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,    -- 'YYYY-MM-DD'
    hour_slot TEXT NOT NULL,
    walkin_count INTEGER,
    pickup_count INTEGER,
    delivery_count INTEGER,
    platform_count INTEGER,
    sales_amount INTEGER,
    pct_of_total REAL,
    cups INTEGER,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, business_date, hour_slot)
);

-- ============================================================
-- Layer 2：稽核允收資料 — 跨來源比對過，之後所有分析模組
-- （月盈虧、排班、熱銷分析）都只查這一層，不直接碰 Layer 1。
-- ============================================================

CREATE TABLE daily_revenue_validated (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,
    revenue_from_register INTEGER NOT NULL,       -- 收銀機明細加總
    revenue_from_monthly_report INTEGER,          -- 營收月報換算比對用，抓不到日顆粒度時可為 NULL
    discrepancy INTEGER,                          -- 兩來源差異，超過閾值要人工檢查
    ubereats_amount INTEGER NOT NULL DEFAULT 0,
    foodpanda_amount INTEGER NOT NULL DEFAULT 0,
    credit_card_amount INTEGER NOT NULL DEFAULT 0,
    other_electronic_amount INTEGER NOT NULL DEFAULT 0,
    taxable_revenue INTEGER NOT NULL DEFAULT 0,   -- 算營業稅只用這個欄位，不要用 revenue_from_register
    UNIQUE(store_id, business_date)
);

CREATE TABLE monthly_cost_actuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    labor_actual INTEGER,      -- 使用者每月底提供的真實人事數字；NULL = 還沒填，改用 config 預設值概算
    cogs_actual INTEGER,       -- 使用者每月底提供的真實原物料數字；NULL = 還沒填，改用 config 預設值概算
    utilities_actual INTEGER,  -- 使用者提供的真實水電帳單數字；NULL = 還沒填，改用 config 預設值概算（水電逐月波動，不能只吃固定估值）
    -- 以下 8 欄是 2026-07-08 新增：讓網頁「試算參數」可以針對單一月份存實際值，
    -- 不用每次都覆蓋 config/cost_rates.json 的全域預設值。NULL = 還沒填，fallback 用 config。
    rent_actual INTEGER,
    franchise_amortization_actual INTEGER,
    ubereats_commission_pct_actual REAL,
    foodpanda_commission_pct_actual REAL,
    credit_card_fee_pct_actual REAL,
    other_electronic_fee_pct_actual REAL,
    business_tax_pct_actual REAL,
    corporate_income_tax_pct_actual REAL,
    notes TEXT,
    UNIQUE(store_id, year_month)
);

-- 月營收人工輸入備援：只在該店該月完全沒有 daily_revenue_validated（POS 稽核過）資料時才會被
-- calculate_pnl.py 拿來用，POS 資料永遠優先。全額視為應稅營收（沒有拆免稅欄位，先簡化）。
CREATE TABLE monthly_revenue_manual (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    revenue INTEGER NOT NULL,
    ubereats_amount INTEGER NOT NULL DEFAULT 0,
    foodpanda_amount INTEGER NOT NULL DEFAULT 0,
    credit_card_amount INTEGER NOT NULL DEFAULT 0,
    other_electronic_amount INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, year_month)
);

-- 稽核允收的發票查詢入口：排除作廢單，任何要用發票明細做分析/比對的地方都查這裡，不要直接查 raw_invoice_transactions
CREATE VIEW invoice_transactions_valid AS
SELECT * FROM raw_invoice_transactions
WHERE tx_status = '正常';

CREATE TABLE monthly_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    revenue INTEGER NOT NULL,
    cogs INTEGER NOT NULL,
    material_waste INTEGER NOT NULL DEFAULT 0,  -- 原物料損耗/報廢，2026-07-10 新增，= cogs × material_waste_pct
    labor_cost INTEGER NOT NULL,
    labor_cost_source TEXT NOT NULL DEFAULT 'estimate',  -- 2026-07-14 新增：'real_payroll'（calculate_payroll.py 真實薪資彙總，含雇主保費，不再乘概算保費率）／'manual_actual'（手動輸入底薪×概算保費率）／'estimate'（全概算）
    rent INTEGER NOT NULL,
    utilities INTEGER NOT NULL,
    franchise_amortization INTEGER NOT NULL,
    platform_commission INTEGER NOT NULL,
    payment_processing_fee INTEGER NOT NULL,
    business_tax INTEGER NOT NULL,
    pretax_profit INTEGER NOT NULL,
    income_tax_estimate INTEGER NOT NULL,
    net_profit INTEGER NOT NULL,
    revenue_source TEXT NOT NULL DEFAULT 'pos',  -- 'pos'（daily_revenue_validated 稽核過）或 'manual'（monthly_revenue_manual 備援輸入）
    calculated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, year_month)
);

-- analyze_operations.py 算出的通路/客單價/尖峰時段「濃縮結論」快取（一兩句話等級的聚合統計句，
-- 不存任何逐筆明細），給 pnl_insights.py 的「各店經營現況」交叉引用用。
-- 之所以另外存一張只含結論文字的表，是因為 Layer 1 原始明細表（raw_invoice_transactions 等）
-- 只存在本機、刻意不同步到雲端 Turso DB；這張表之後若要同步上雲端，只會送出聚合後的結論文字，
-- 不會把逐筆發票/通路明細送上去。2026-07-09 使用者決定：先只在本機累積，何時同步上雲端另外處理，
-- 所以目前刻意不加進 schema_cloud.sql。
-- 通路組合／客單價／尖峰時段的濃縮結論。summary_text 含真實客單價（元），只留本機。
-- public_summary_text（2026-07-14 新增，取代 2026-07-09「不公開」的舊決定）是
-- analyze_operations.public_operational_summary() 的 JSON：通路組合/回頭客用百分比，
-- 客單價改成「相對指數」（平均值=100）不露真實金額，這欄會被 build_cloud_snapshot.py
-- 同步上雲端，summary_text 不會。
CREATE TABLE store_operational_insights (
    store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
    summary_text TEXT NOT NULL,
    public_summary_text TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- scripts/estimate_staffing_cost.py 算出的「實際排班 vs 合理人力」結論快取。
-- 2026-07-09 使用者決定的公開範圍：只有「預估可節省金額」這一項可以同步上雲端
-- （見 public_summary_text），真實人事成本／固定薪資／實際排班時數等底片數字
-- 一律不公開，只留在 summary_text（本機專用，供 app.py 顯示完整版）。
-- 哪個店有這張表的資料，pnl_insights.py 的建議段落就用真實排班反推的結論；
-- 沒有的店（例如還沒謄打排班資料的店）繼續用原本的通用估計句子。
CREATE TABLE store_staffing_insights (
    store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
    summary_text TEXT NOT NULL,
    public_summary_text TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
