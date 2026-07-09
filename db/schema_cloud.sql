-- 雲端（Turso）子集 schema：只放月盈虧網頁 (app_pnl.py) 需要讀寫的表格。
-- 這是 schema.sql 的子集，刻意不包含任何 Layer 1 原始報表表格（收銀機/發票/銷售明細等）
-- 跟排班相關表格——這些繼續只存在本機 db/riva_agent.db，不會離開這台電腦。
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
    labor_cost INTEGER NOT NULL,
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

-- store_operational_insights（通路組合／客單價，含真實客單價金額）2026-07-09 使用者
-- 決定不公開，刻意不放進雲端 schema。完整版只存在本機 db/riva_agent.db，供 app.py 用。
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
