-- The audit trail's category vocabulary, derived rather than listed.
--
-- The screen this serves used to filter `audit_log` through a hand-written list
-- of 37 action names while the table held 132, so 95 types were invisible and
-- nothing said anything was missing. Inverting that filter fixed the hiding and
-- exposed the real problem: one chip per action type is a list somebody has to
-- remember to add to, and nobody ever did.
--
-- So the category is DERIVED from the action name by an ordered pattern table,
-- first match wins, and an action nobody has written a pattern for lands in
-- `unclassified` BY CONSTRUCTION rather than by omission. A new action type
-- inherits a category for free; the ones that do not are visible as a count on
-- the screen instead of being absorbed into a bucket they do not belong to.
--
-- Two dimensions come out of the same pass:
--   category — what KIND OF THING was touched. An auditor always arrives asking
--              about a kind of thing ("who turned masking off on that column").
--   effect   — what happened to it. `Data protection x Read` is every time
--              somebody looked at unmasked personal data, which is the highest-
--              sensitivity question the trail can answer and which no single
--              chip in the old vocabulary could ask.
--
-- Measured when this was written: 47 patterns classify 121 of the 132 action
-- types. The remaining 8 types (10 rows) stay unclassified on purpose — a rule
-- that exists to catch ONE action name is the per-action list this table
-- replaces, so they are left visible instead.

CREATE TABLE IF NOT EXISTS audit_action_category (
    ord       integer PRIMARY KEY,
    pattern   text    NOT NULL,
    category  text,
    effect    text,
    note      text,
    CONSTRAINT audit_action_category_category_check
        CHECK (category IS NULL OR category IN
               ('requests', 'protection', 'access', 'connections', 'config', 'usage')),
    CONSTRAINT audit_action_category_effect_check
        CHECK (effect IS NULL OR effect IN
               ('added', 'changed', 'removed', 'read', 'decided', 'ran')),
    -- A row that sets neither dimension would be dead weight that still costs a
    -- comparison on every action name.
    CONSTRAINT audit_action_category_not_empty
        CHECK (category IS NOT NULL OR effect IS NOT NULL)
);

COMMENT ON TABLE audit_action_category IS
    'Ordered patterns that derive an audit row''s category and effect from its '
    'action name. First match wins PER DIMENSION: a rule may set one and leave '
    'the other to a later rule. Order is the whole design — narrow before broad.';


CREATE OR REPLACE FUNCTION audit_classify(p_action text)
RETURNS text[] AS $$
DECLARE
    a text := lower(coalesce(p_action, ''));
    c text;
    e text;
    r record;
BEGIN
    FOR r IN SELECT pattern, category, effect
               FROM audit_action_category ORDER BY ord LOOP
        IF position(r.pattern IN a) > 0 THEN
            IF c IS NULL AND r.category IS NOT NULL THEN c := r.category; END IF;
            IF e IS NULL AND r.effect   IS NOT NULL THEN e := r.effect;   END IF;
            EXIT WHEN c IS NOT NULL AND e IS NOT NULL;
        END IF;
    END LOOP;
    -- `unclassified` is a real member of the vocabulary and says so on screen.
    -- `changed` is the effect fallback because an action that changed nothing
    -- would not be worth an audit row.
    RETURN ARRAY[coalesce(c, 'unclassified'), coalesce(e, 'changed')];
END $$ LANGUAGE plpgsql STABLE;

-- STABLE, not IMMUTABLE, although the caller asked for immutable: this function
-- reads a table, and IMMUTABLE would licence the planner to fold a call into a
-- constant that survives a rule change. STABLE is the strongest marking a
-- table-reading function may honestly carry, and it is enough for the planner
-- to call it once per distinct action name.
COMMENT ON FUNCTION audit_classify(text) IS
    'Returns {category, effect} for an action name, from audit_action_category. '
    'Callers classify DISTINCT action names and join, so the cost is per name '
    '(132) and not per row (26k).';


-- The seed. Order matters more than any individual line:
--   * protection is carved out FIRST, because masking and unmasking rows would
--     otherwise read as access — and they are the more sensitive of the two.
--   * `auto_approved` (a request was decided) sits before `auto_approve` (a
--     window was created), because the second is a prefix of the first.
--   * connections before access, because a target/schema/credential row is
--     about the connection even when its name also contains a grant word.
--   * `config` last among the category rules: it is the broadest word here.
--   * verb-only rules at the end set the effect for whatever category matched.
INSERT INTO audit_action_category (ord, pattern, category, effect, note) VALUES
    (10, 'unmask',        'protection', 'read',    'somebody looked at unmasked personal data'),
    (11, 'redaction',     'protection', 'changed', NULL),
    (12, 'downloaded',    'protection', 'read',    'a result left the product'),
    (13, 'pii',           'protection', NULL,      'masking rules and exemptions'),
    (20, '_opened',       'usage',      'read',    'a screen was opened; no authority changed'),
    (21, 'viewed',        'usage',      'read',    NULL),
    (30, 'auto_approved', 'requests',   'decided', 'a REQUEST admitted without a human'),
    (31, 'auto_approve',  'access',     NULL,      'the WINDOW that admits them'),
    (32, 'import_',       'requests',   NULL,      NULL),
    (33, 'execution_',    'requests',   'ran',     NULL),
    (34, 'submitted',     'requests',   'added',   NULL),
    (35, 'completed',     'requests',   'ran',     NULL),
    (36, 'failed',        'requests',   'ran',     NULL),
    (37, 'cancel',        'requests',   'removed', NULL),
    (38, 'withdraw',      'requests',   'removed', 'the requester''s own decision, not a rejection'),
    (39, 'approved',      'requests',   'decided', NULL),
    (40, 'rejected',      'requests',   'decided', NULL),
    (41, 'escalated',     'requests',   'decided', NULL),
    (42, 'dispatched',    'requests',   'ran',     NULL),
    (43, 'feedback',      'requests',   'added',   NULL),
    (50, 'target',        'connections', NULL,     NULL),
    (51, 'connection',    'connections', NULL,     NULL),
    (52, 'schema',        'connections', NULL,     NULL),
    (53, 'migration',     'connections', NULL,     NULL),
    (54, 'credential',    'connections', NULL,     NULL),
    (60, 'grant',         'access',     NULL,      NULL),
    (61, 'revoke',        'access',     'removed', NULL),
    (62, 'role',          'access',     NULL,      NULL),
    (63, 'team',          'access',     NULL,      NULL),
    (64, 'pod',           'access',     NULL,      NULL),
    (65, 'requester',     'access',     NULL,      NULL),
    (66, 'user_',         'access',     NULL,      NULL),
    (67, 'login',         'access',     'read',    NULL),
    (68, 'signout',       'access',     'read',    NULL),
    (69, 'access_',       'access',     NULL,      NULL),
    (70, 'admin',         'access',     NULL,      NULL),
    (80, 'config',        'config',     'changed', NULL),
    (81, 'kill',          'config',     'changed', 'the fleet-wide stop'),
    (90, '_added',        NULL,         'added',   NULL),
    (91, '_granted',      NULL,         'added',   NULL),
    (92, '_created',      NULL,         'added',   NULL),
    (93, '_register',     NULL,         'added',   NULL),
    (94, '_onboard',      NULL,         'added',   NULL),
    (95, '_imported',     NULL,         'added',   NULL),
    (96, '_removed',      NULL,         'removed', NULL),
    (97, '_revoked',      NULL,         'removed', NULL),
    (98, '_offboarded',   NULL,         'removed', NULL)
ON CONFLICT (ord) DO NOTHING;
