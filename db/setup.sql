-- ============================================================
-- CRYPTO AI AGENT — Supabase Schema  (v2)
-- Run this ONCE in your Supabase SQL editor.
-- If you already ran v1, run only the ALTER TABLE section at the bottom.
-- ============================================================

-- 1. Signal snapshots (raw data every cycle)
CREATE TABLE IF NOT EXISTS signal_snapshots (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pair       TEXT NOT NULL,
  signals    JSONB,       -- all computed indicators
  raw_data   JSONB,       -- price, fear_greed, news count, ob imbalance
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 2. AI reasoning log (full chain-of-thought from Groq)
CREATE TABLE IF NOT EXISTS agent_reasoning (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pair               TEXT NOT NULL,
  context_sent       TEXT,       -- the full prompt sent to Groq
  direction          TEXT,       -- BUY | SELL | HOLD
  confidence         NUMERIC,    -- 0-100
  hypothesis         TEXT,
  signal_alignment   TEXT,       -- strong | mixed | contradictory
  risk_level         TEXT,       -- LOW | MEDIUM | HIGH
  market_regime      TEXT,       -- trending | ranging | strong_trend
  reasoning_text     TEXT,
  prediction_correct BOOLEAN,    -- filled in by feedback loop
  created_at         TIMESTAMPTZ DEFAULT now()
);

-- 3. Trade history
CREATE TABLE IF NOT EXISTS trade_history (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pair               TEXT NOT NULL,
  side               TEXT NOT NULL,   -- BUY | SELL
  entry_price        NUMERIC,
  quantity           NUMERIC,
  usdt_value         NUMERIC,
  stop_loss_price    NUMERIC,
  take_profit_price  NUMERIC,
  confidence         NUMERIC,
  direction          TEXT,
  reasoning_id       UUID REFERENCES agent_reasoning(id),
  is_dry_run         BOOLEAN DEFAULT true,
  oco_protected      BOOLEAN DEFAULT false,
  binance_order_id   TEXT,
  closed_at          TIMESTAMPTZ,
  actual_exit_price  NUMERIC,
  pnl_pct            NUMERIC,
  outcome            TEXT,        -- win | loss | neutral
  prediction_correct BOOLEAN,
  created_at         TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_signal_pair_time    ON signal_snapshots(pair, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reasoning_pair_time ON agent_reasoning(pair, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_pair_open    ON trade_history(pair, closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_trades_closed_at    ON trade_history(pair, closed_at DESC);

-- ============================================================
-- If upgrading from v1, run just these ALTER TABLE statements:
-- ============================================================
-- ALTER TABLE agent_reasoning ADD COLUMN IF NOT EXISTS market_regime TEXT;
-- ALTER TABLE trade_history   ADD COLUMN IF NOT EXISTS oco_protected BOOLEAN DEFAULT false;

-- ============================================================
-- Views
-- ============================================================

-- Prediction accuracy by pair
CREATE OR REPLACE VIEW prediction_accuracy AS
SELECT
  pair,
  COUNT(*) FILTER (WHERE prediction_correct = true)  AS correct,
  COUNT(*) FILTER (WHERE prediction_correct = false) AS wrong,
  COUNT(*) FILTER (WHERE prediction_correct IS NULL) AS pending,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE prediction_correct = true)
    / NULLIF(COUNT(*) FILTER (WHERE prediction_correct IS NOT NULL), 0),
    1
  ) AS accuracy_pct
FROM agent_reasoning
GROUP BY pair;

-- Daily PnL summary
CREATE OR REPLACE VIEW daily_pnl AS
SELECT
  pair,
  DATE(closed_at) AS trade_date,
  COUNT(*)                                                  AS total_trades,
  COUNT(*) FILTER (WHERE outcome = 'win')                   AS wins,
  COUNT(*) FILTER (WHERE outcome = 'loss')                  AS losses,
  ROUND(SUM(pnl_pct)::NUMERIC, 2)                          AS total_pnl_pct,
  ROUND(AVG(confidence)::NUMERIC, 1)                        AS avg_confidence
FROM trade_history
WHERE closed_at IS NOT NULL AND is_dry_run = false
GROUP BY pair, DATE(closed_at)
ORDER BY trade_date DESC;

-- Unprotected open positions that need manual attention
CREATE OR REPLACE VIEW unprotected_positions AS
SELECT id, pair, side, entry_price, quantity, usdt_value,
       stop_loss_price, take_profit_price, created_at
FROM trade_history
WHERE closed_at IS NULL
  AND oco_protected = false
  AND is_dry_run = false;
