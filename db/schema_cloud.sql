-- 雲端（Turso）子集 schema：只放 app_pnl.py（月盈虧＋排班摘要）需要讀寫的表格。
-- 這是 schema.sql 的子集，刻意不包含 Layer 1 原始報表表格（收銀機/發票/銷售明細等）。
--
-- 2026-07-10 排班摘要上雲後的邊界：raw_hourly_pattern_monthly/daily 是月/日彙總資料，
-- 本來就不含員工代碼或逐員工明細，這兩張可以整表同步。但 raw_staffing_actual（逐日逐
-- 員工的實際排班原始表）跟 config 的 employee_roles/wages（真實角色對照／薪資）永遠
-- 不上雲——凡是需要用到這兩者才能算出的結果（實際 vs 建議人力比對、正職/兼職眾數），
-- 一律在本機算完，只把彙總後的結果（不含 employee_code、不含 business_date）存進
-- staffing_hourly_comparison／staffing_roster_mode 這兩張雲端專用表，見
-- scripts/migrate_layer2_to_turso.py 的說明。
--
-- 若本機 schema.sql 這幾張表的結構有異動，要記得同步更新這裡（唯一事實來源仍是 schema.sql）。

CREATE TABLE IF NOT EXISTS stores (
    store_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS daily_revenue_validated (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,
    revenue_from_register INTEGER NOT NULL,
    revenue_from_monthly_report INTEGER,
    discrepancy INTEGER,
    ubereats_amount INTEGER NOT NULL DEFAULT 0,
    foodpanda_amount INTEGER NOT NULL DEFAULT 0,
    credit_card_amount INTEGER NOT NULL DEFAULT 0,
    other_electronic_amount INTEGER NOT NULL DEFAULT 0,
    taxable_revenue INTEGER NOT NULL DEFAULT 0,
    UNIQUE(store_id, business_date)
);

CREATE TABLE IF NOT EXISTS monthly_cost_actuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    labor_actual INTEGER,
    cogs_actual INTEGER,
    utilities_actual INTEGER,
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

CREATE TABLE IF NOT EXISTS monthly_revenue_manual (
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

CREATE TABLE IF NOT EXISTS monthly_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    revenue INTEGER NOT NULL,
    cogs INTEGER NOT NULL,
    material_waste INTEGER NOT NULL DEFAULT 0,
    labor_cost INTEGER NOT NULL,
    labor_cost_source TEXT NOT NULL DEFAULT 'estimate',
    rent INTEGER NOT NULL,
    utilities INTEGER NOT NULL,
    franchise_amortization INTEGER NOT NULL,
    platform_commission INTEGER NOT NULL,
    payment_processing_fee INTEGER NOT NULL,
    business_tax INTEGER NOT NULL,
    pretax_profit INTEGER NOT NULL,
    income_tax_estimate INTEGER NOT NULL,
    net_profit INTEGER NOT NULL,
    revenue_source TEXT NOT NULL DEFAULT 'pos',
    calculated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, year_month)
);

-- store_operational_insights：2026-07-09 曾決定完全不公開，2026-07-14 改為只同步
-- public_summary_text（analyze_operations.public_operational_summary() 的 JSON，
-- 通路組合/回頭客用百分比、客單價用相對指數，不含真實金額/真實客數）。本機的
-- summary_text（含真實客單價）不會同步，只存在本機 db/riva_agent.db。
CREATE TABLE IF NOT EXISTS store_operational_insights (
    store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
    public_summary_text TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
--
-- store_staffing_insights：欄位表面上看起來只是「一段結論文字」，但文字內容本身
-- 可能藏著真實金額——2026-07-09 曾經誤把含真實人事成本的完整版同步上來過，發現後
-- 已從 Turso 刪除。這裡的 summary_text 現在**只會被寫入公開安全版**（只有「預估可
-- 節省金額」，見 scripts/migrate_layer2_to_turso.py 的 migrate_staffing_insights()
-- 只讀本機表的 public_summary_text 欄位）——之後改這支同步腳本時要記得維持這個界線，
-- 不要因為兩個欄位長得很像就查錯欄位。
CREATE TABLE IF NOT EXISTS store_staffing_insights (
    store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
    summary_text TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 月彙總的逐時段杯數/外送單數，跟本機 schema.sql 同名但只取「排班建議公式」用得到的
-- 欄位（不含 sales_amount/walkin_count 等用不到的欄位，減少曝光面）。本來就不含員工
-- 資料，可以整表安全同步。表名跟本機同名，讓 scripts/calculate_staffing.py 的
-- get_hourly_data() 原封不動指到這張表就能在雲端跑，不用另外寫雲端專用版本。
CREATE TABLE IF NOT EXISTS raw_hourly_pattern_monthly (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    daily_avg_cups INTEGER,
    delivery_count INTEGER,
    UNIQUE(store_id, year_month, hour_slot)
);

-- 單日（非月彙總）的逐時段杯數樣本，同樣跟本機同名、只取用得到的欄位，供
-- scripts/analyze_staffing_daytype.py 的 cup_stats_by_daytype() 原封不動使用。
CREATE TABLE IF NOT EXISTS raw_hourly_pattern_daily (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    business_date TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    cups INTEGER,
    UNIQUE(store_id, business_date, hour_slot)
);

-- 「實際排班 vs 建議人力」整月彙總結果快照，只在本機用 scripts/compare_staffing.py 的
-- compare() 算完才同步上來，雲端本身查不到 raw_staffing_actual，不會即時重算。
CREATE TABLE IF NOT EXISTS staffing_hourly_comparison (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year_month TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    recommended INTEGER NOT NULL,
    actual REAL,
    diff REAL,
    UNIQUE(store_id, year_month, hour_slot)
);

-- 「實際排班 vs 建議人力」平日/假日拆分快照（2026-07-14 新增），只在本機用
-- scripts/compare_staffing.py 的 compare_daytype() 算完才同步上來——杯量用真實
-- 星期六/日單日樣本反推，實際人力依真實 business_date 分平日(一~五)/假日(六日)，
-- 這裡只存彙總後的數字，不含任何逐日/逐員工細節。
CREATE TABLE IF NOT EXISTS staffing_hourly_comparison_daytype (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    daytype TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    cups REAL,
    recommended INTEGER,
    formula TEXT,
    actual REAL,
    diff REAL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, daytype, hour_slot)
);

-- 「每張發票約幾杯」轉換比例（2026-07-14 新增），供
-- scripts/estimate_staffing_by_weekday.py 的星期幾建議人力估計在雲端使用。
-- 只有這一個數字，不含任何逐筆發票/杯數明細。
CREATE TABLE IF NOT EXISTS staffing_cup_invoice_ratio (
    store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
    ratio REAL NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 星期幾彙整安全版（2026-07-14 新增）：analyze_operations.public_weekday_summary()
-- 的結果，真實營業額/發票張數改成「相對星期五中位數的百分比差異」，不含任何真實金額，
-- 只有最大/最小值發生的日期（不是金額）保留。
CREATE TABLE IF NOT EXISTS staffing_weekday_summary_public (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    weekday TEXT NOT NULL,
    days INTEGER,
    revenue_median_vs_friday REAL,
    revenue_min_vs_friday REAL,
    revenue_min_date TEXT,
    revenue_max_vs_friday REAL,
    revenue_max_date TEXT,
    invoice_median_vs_friday REAL,
    invoice_min_vs_friday REAL,
    invoice_min_date TEXT,
    invoice_max_vs_friday REAL,
    invoice_max_date TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, weekday)
);

-- 「實際排班 vs 建議人力」跨月彙總（同一年、排除農曆年節月份、排除未過完的當月）快照，
-- 只在本機用 scripts/compare_staffing.py 的 compare_aggregate() 算完才同步上來。
-- year 是這批彙總涵蓋的年份（例如 "2026"），months_included 記錄實際納入哪些月份
-- （逗號分隔字串，例如 "2026-01,2026-03,2026-04"），方便網頁顯示納入範圍。
CREATE TABLE IF NOT EXISTS staffing_hourly_comparison_yearly (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    year TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    recommended REAL,
    actual REAL,
    diff REAL,
    cups REAL,
    months_included TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, year, hour_slot)
);

-- 「星期幾 x 時段」發票張數/營業額快照，只在本機用
-- scripts/analyze_operations.py 的 hourly_channel_by_weekday() 算完才同步上來。
-- 來源是 raw_invoice_transactions（逐筆交易明細），但這裡只存彙總後的日均值，
-- 不含 carrier_no、不含單筆交易金額、不含 business_date，符合零洩漏原則。
CREATE TABLE IF NOT EXISTS staffing_channel_by_weekday (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    weekday TEXT NOT NULL,
    hour_slot TEXT NOT NULL,
    invoice_count REAL,
    revenue REAL,
    sample_days INTEGER,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, weekday, hour_slot)
);

-- 「星期幾 x 時段」正職/兼職人數眾數快照，只在本機用
-- scripts/analyze_staffing_daytype.py 的 roster_mode_by_weekday() 算完才同步上來
-- （該函式需要 config["employee_roles"] 才能算，這個 config 段落本身不上雲）。
-- 刻意不含 employee_code、不含 business_date，只有彙總後的人數。
CREATE TABLE IF NOT EXISTS staffing_roster_mode (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    hour_slot TEXT NOT NULL,
    weekday TEXT NOT NULL,
    full_time_count INTEGER,
    part_time_count INTEGER,
    consistency_ratio TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(store_id, hour_slot, weekday)
);
