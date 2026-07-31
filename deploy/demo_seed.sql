-- Demo TARGET database: what a developer queries through QueryHub.
--
-- This is not the metadata database (that one is created by setup_db.sql and
-- filled by the migrations). This is a stand-in for "production": a small
-- e-commerce schema with enough rows to make paging, PII masking and the tier
-- model visible, plus the three login roles the executor expects.
--
-- Used by docker-compose.yml, and it doubles as a fixture for anyone who wants
-- to exercise a DB-touching change locally.
--
-- Every value is generated. There is no real personal data here, and the email
-- domain is example.com (RFC 2606) on purpose.

-- ---------------------------------------------------------------- roles
-- One login per tier, exactly as a real install has. The demo password is
-- deliberately obvious: this database is meant to be thrown away.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'queryhub_ro') THEN
        CREATE ROLE queryhub_ro LOGIN PASSWORD 'demo-ro';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'queryhub_rw') THEN
        CREATE ROLE queryhub_rw LOGIN PASSWORD 'demo-rw';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'queryhub_ddl') THEN
        CREATE ROLE queryhub_ddl LOGIN PASSWORD 'demo-ddl';
    END IF;
END $$;

-- ---------------------------------------------------------------- schema
CREATE TABLE IF NOT EXISTS users (
    id          bigserial PRIMARY KEY,
    email       text        NOT NULL,
    full_name   text        NOT NULL,
    phone       text,
    country     text        NOT NULL,
    status      text        NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz
);
COMMENT ON TABLE users IS
    'Demo customers. email/full_name/phone are the columns PII masking fires on.';

CREATE TABLE IF NOT EXISTS orders (
    id          bigserial PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES users(id),
    total_cents bigint      NOT NULL,
    currency    text        NOT NULL DEFAULT 'EUR',
    status      text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    id          bigserial PRIMARY KEY,
    order_id    bigint      NOT NULL REFERENCES orders(id),
    -- Stored as a masked stub, the way a real system should: the demo exists to
    -- show masking on the way OUT, not to model card storage.
    card_last4  text        NOT NULL,
    iban        text,
    amount_cents bigint     NOT NULL,
    status      text        NOT NULL,
    paid_at     timestamptz
);

CREATE TABLE IF NOT EXISTS events (
    id          bigserial PRIMARY KEY,
    user_id     bigint      REFERENCES users(id),
    kind        text        NOT NULL,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    actor       text        NOT NULL,
    action      text        NOT NULL,
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- data
-- ~10k users and a proportional spread of orders / payments / events. Seeded
-- deterministically (setseed) so two people running the demo see the same
-- numbers and can compare notes.
SELECT setseed(0.42);

INSERT INTO users (email, full_name, phone, country, status, created_at, last_seen_at)
SELECT
    'user' || g || '@example.com',
    (ARRAY['Ada','Grace','Alan','Edsger','Barbara','Ken','Donald','Margaret',
           'Linus','Radia'])[1 + (g % 10)] || ' ' ||
    (ARRAY['Lovelace','Hopper','Turing','Dijkstra','Liskov','Thompson','Knuth',
           'Hamilton','Torvalds','Perlman'])[1 + ((g / 10) % 10)],
    '+49 30 ' || lpad(((g * 7919) % 10000000)::text, 7, '0'),
    (ARRAY['DE','FR','NL','ES','IT','PL','SE'])[1 + (g % 7)],
    CASE WHEN g % 23 = 0 THEN 'suspended'
         WHEN g % 11 = 0 THEN 'inactive'
         ELSE 'active' END,
    now() - (g % 900) * interval '1 day',
    CASE WHEN g % 7 = 0 THEN NULL
         ELSE now() - (g % 60) * interval '1 hour' END
FROM generate_series(1, 10000) AS g
ON CONFLICT DO NOTHING;

INSERT INTO orders (user_id, total_cents, currency, status, created_at)
SELECT
    1 + (g % 10000),
    ((g * 137) % 45000) + 500,
    (ARRAY['EUR','EUR','EUR','USD','GBP'])[1 + (g % 5)],
    (ARRAY['paid','paid','paid','pending','refunded','cancelled'])[1 + (g % 6)],
    now() - (g % 400) * interval '1 day'
FROM generate_series(1, 24000) AS g
ON CONFLICT DO NOTHING;

INSERT INTO payments (order_id, card_last4, iban, amount_cents, status, paid_at)
SELECT
    o.id,
    lpad(((o.id * 3607) % 10000)::text, 4, '0'),
    -- A structurally valid-looking IBAN so the content detector has something
    -- to catch; the check digits are not real.
    'DE' || lpad(((o.id * 97) % 100)::text, 2, '0')
          || '5001051' || lpad(((o.id * 7919) % 10000000)::text, 10, '0'),
    o.total_cents,
    CASE WHEN o.status = 'paid' THEN 'settled' ELSE o.status END,
    CASE WHEN o.status = 'paid'
         THEN o.created_at + interval '3 minutes' ELSE NULL END
FROM orders o
WHERE o.id % 3 <> 0
ON CONFLICT DO NOTHING;

INSERT INTO events (user_id, kind, payload, created_at)
SELECT
    1 + (g % 10000),
    (ARRAY['login','logout','view_item','add_to_cart','checkout','password_reset'])
        [1 + (g % 6)],
    jsonb_build_object('session', md5(g::text), 'ip',
                       '10.' || (g % 256) || '.' || ((g / 256) % 256) || '.1'),
    now() - (g % 720) * interval '1 hour'
FROM generate_series(1, 40000) AS g
ON CONFLICT DO NOTHING;

INSERT INTO audit_log (actor, action, detail, created_at)
SELECT
    'ops' || (1 + (g % 4)),
    (ARRAY['role_granted','config_changed','user_suspended','export_run'])[1 + (g % 4)],
    'demo audit entry ' || g,
    now() - g * interval '2 hours'
FROM generate_series(1, 500) AS g
ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
ANALYZE;

-- ---------------------------------------------------------------- grants
-- The tier model, for real: RO can only read, RW can write rows but not change
-- structure, DDL can do both. A query classified RO literally cannot write
-- here, because the credential it runs under has no such privilege.
GRANT USAGE ON SCHEMA public TO queryhub_ro, queryhub_rw, queryhub_ddl;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO queryhub_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO queryhub_rw;
GRANT ALL ON ALL TABLES IN SCHEMA public TO queryhub_ddl;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO queryhub_rw, queryhub_ddl;
GRANT CREATE ON SCHEMA public TO queryhub_ddl;

-- Same for anything created later (e.g. by a DDL demo query).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO queryhub_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO queryhub_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO queryhub_ddl;
