-- Table des messages bruts Discord
CREATE TABLE IF NOT EXISTS messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    discord_message_id TEXT UNIQUE NOT NULL,
    guild_id TEXT NOT NULL,
    guild_name TEXT,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT,
    is_bot BOOLEAN DEFAULT FALSE,
    content TEXT,
    embeds JSONB DEFAULT '[]',
    attachments JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all for anon" ON messages FOR ALL USING (true) WITH CHECK (true);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_discord_id ON messages(discord_message_id);

-- Table des trades, structure identique au Google Sheet
CREATE TABLE IF NOT EXISTS trades (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Infos principales
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'cancelled')),
    trader TEXT,
    source_channel TEXT,
    discord_message_id TEXT,

    -- Entry Point 1
    ep1 NUMERIC,
    ep1_id TEXT,
    ep1_size_usdt NUMERIC,
    ep1_size_crypto NUMERIC,
    ep1_status TEXT DEFAULT 'waiting' CHECK (ep1_status IN ('filled', 'waiting', 'closed') OR ep1_status IS NULL),

    -- Entry Point 2
    ep2 NUMERIC,
    ep2_id TEXT,
    ep2_size_usdt NUMERIC,
    ep2_size_crypto NUMERIC,
    ep2_status TEXT CHECK (ep2_status IN ('filled', 'waiting', 'closed') OR ep2_status IS NULL),

    -- Entry Point 3
    ep3 NUMERIC,
    ep3_id TEXT,
    ep3_size_usdt NUMERIC,
    ep3_size_crypto NUMERIC,
    ep3_status TEXT CHECK (ep3_status IN ('filled', 'waiting', 'closed') OR ep3_status IS NULL),

    -- Stop Loss
    sl NUMERIC,
    sl_id TEXT,
    sl_status TEXT CHECK (sl_status IN ('filled', 'waiting', 'closed') OR sl_status IS NULL),

    -- Take Profit 1-6
    tp1 NUMERIC,
    tp1_id TEXT,
    tp1_status TEXT CHECK (tp1_status IN ('filled', 'waiting', 'closed') OR tp1_status IS NULL),

    tp2 NUMERIC,
    tp2_id TEXT,
    tp2_status TEXT CHECK (tp2_status IN ('filled', 'waiting', 'closed') OR tp2_status IS NULL),

    tp3 NUMERIC,
    tp3_id TEXT,
    tp3_status TEXT CHECK (tp3_status IN ('filled', 'waiting', 'closed') OR tp3_status IS NULL),

    tp4 NUMERIC,
    tp4_id TEXT,
    tp4_status TEXT CHECK (tp4_status IN ('filled', 'waiting', 'closed') OR tp4_status IS NULL),

    tp5 NUMERIC,
    tp5_id TEXT,
    tp5_status TEXT CHECK (tp5_status IN ('filled', 'waiting', 'closed') OR tp5_status IS NULL),

    tp6 NUMERIC,
    tp6_id TEXT,
    tp6_status TEXT CHECK (tp6_status IN ('filled', 'waiting', 'closed') OR tp6_status IS NULL)
);

-- Desactiver RLS pour simplifier (le bot utilise la cle anon)
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all for anon" ON trades FOR ALL USING (true) WITH CHECK (true);

-- Colonnes ajoutees pour le suivi des alerts
ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_reason TEXT
    CHECK (close_reason IN ('profit', 'small_profit', 'loss', 'breakeven', 'stopped_out', 'cancelled') OR close_reason IS NULL);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Index utiles
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader);
