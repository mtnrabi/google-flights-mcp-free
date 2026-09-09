-- Free MCP server: Google sign-in.
--
-- Idempotent, and safe to run BEFORE the code ships: every table here is new
-- and nothing currently deployed reads or writes any of it. Run it against
-- the same Neon database the paid server uses (the pooled `-pooler` host):
--
--     psql "$DATABASE_URL" -f mcp_server/migrations/001_free_mcp_signin.sql
--
-- Why the OAuth tables are `free_mcp_oauth_*` and not the paid server's
-- `mcp_oauth_*`: a token minted by the free, ad-carrying server must never be
-- a token the paid server would accept, and the cheapest way to guarantee
-- that is for neither to be able to read the other's rows. The `resource`
-- audience check in oauth.py would also catch it; two mechanisms is the right
-- number for the one property that keeps ad revenue and paid revenue apart
-- (CLAUDE.md rule 5).

-- ── who signed in ────────────────────────────────────────────────────────
-- This table IS the upsell list. Resend's plan caps this account at three
-- audiences/segments and all three are in use, so there is nowhere to put
-- these addresses in Resend today; consent, opt-out and send history live
-- here and the transactional sends go out through POST /emails, which needs
-- no audience. See mcp_server/src/maillist.py.
CREATE TABLE IF NOT EXISTS free_mcp_users (
    google_sub        text PRIMARY KEY,
    email             text        NOT NULL DEFAULT '',
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz NOT NULL DEFAULT now(),
    -- Backend searches spent while signed in, all time. Written from a
    -- best-effort background task, so it is a FLOOR and not an exact count:
    -- a serverless instance that freezes between answering a tool call and
    -- writing this loses that increment. The exact per-client numbers are
    -- the Upstash fair-use counters.
    call_count        bigint      NOT NULL DEFAULT 0,
    -- The MCP client the account approved on the consent page: "Claude Code",
    -- "Cursor", "Smithery". Reporting only.
    client_kind       text        NOT NULL DEFAULT '',
    -- pending | added | skipped | failed -- whether the address ever reached
    -- a Resend audience. Stays `pending` for every row while phase 1 runs,
    -- which is correct: nothing has been synced.
    resend_state      text        NOT NULL DEFAULT 'pending',
    -- They pressed unsubscribe. Nothing is ever sent to this address again,
    -- by any lane, whatever Resend thinks.
    email_opt_out     boolean     NOT NULL DEFAULT false,
    -- What makes the one-click link work with no session and no confirmation
    -- click. Unguessable and not derivable from the address: an address must
    -- not be enough to unsubscribe a stranger.
    unsubscribe_token text        NOT NULL DEFAULT '',
    -- The "once per user, ever" guard on the welcome note. Stamped by an
    -- UPDATE ... WHERE welcome_sent_at IS NULL, so two instances racing one
    -- first sign-in send exactly one note.
    welcome_sent_at   timestamptz
);

CREATE INDEX IF NOT EXISTS free_mcp_users_email_idx
    ON free_mcp_users (email) WHERE email <> '';
CREATE UNIQUE INDEX IF NOT EXISTS free_mcp_users_unsub_idx
    ON free_mcp_users (unsubscribe_token) WHERE unsubscribe_token <> '';

-- ── what they spent, per UTC day ─────────────────────────────────────────
-- One row per account per day. UTC because the day cap rolls over at 00:00
-- UTC, and a usage row on a different clock would disagree with the counter
-- that refused the call it describes. This is what the daily read reports
-- signed-in usage from, and what the phase-2 cap-note triggers are evaluated
-- over.
CREATE TABLE IF NOT EXISTS free_mcp_user_days (
    google_sub text NOT NULL,
    day        date NOT NULL,
    searches   bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (google_sub, day)
);

CREATE INDEX IF NOT EXISTS free_mcp_user_days_day_idx
    ON free_mcp_user_days (day);

-- ── the OAuth flow ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS free_mcp_oauth_clients (
    client_id                  text PRIMARY KEY,
    client_name                text NOT NULL DEFAULT '',
    redirect_uris              text[] NOT NULL DEFAULT '{}',
    token_endpoint_auth_method text NOT NULL DEFAULT 'none',
    scope                      text NOT NULL DEFAULT '',
    -- SHA-256 hex, never the secret. '' for a public client, which is what
    -- nearly every MCP client registers as; PKCE is what protects those and
    -- this server requires it from everybody.
    client_secret_hash         text NOT NULL DEFAULT '',
    created_at                 timestamptz NOT NULL DEFAULT now(),
    metadata                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- For the durable per-address registration cap. Spoofable (it comes from
    -- a forwarded header), which is why it is one of two caps and not the
    -- only one.
    registered_ip              text NOT NULL DEFAULT '',
    -- Set the first time a human is shown a consent page for this client.
    -- The sweep only deletes registrations that never got that far.
    last_authorized_at         timestamptz
);

CREATE INDEX IF NOT EXISTS free_mcp_oauth_clients_created_idx
    ON free_mcp_oauth_clients (created_at);

CREATE TABLE IF NOT EXISTS free_mcp_oauth_codes (
    -- SHA-256 hex of a 32-byte urandom code. A database dump holds nothing
    -- replayable.
    code_hash      text PRIMARY KEY,
    client_id      text NOT NULL,
    redirect_uri   text NOT NULL,
    -- The S256 challenge. The verifier is never stored: storing it would
    -- defeat the point of PKCE.
    code_challenge text NOT NULL,
    scope          text NOT NULL DEFAULT '',
    user_sub       text NOT NULL,
    provider       text NOT NULL DEFAULT 'google',
    resource       text NOT NULL DEFAULT '',
    expires_at     timestamptz NOT NULL,
    user_email     text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS free_mcp_oauth_codes_expiry_idx
    ON free_mcp_oauth_codes (expires_at);
CREATE INDEX IF NOT EXISTS free_mcp_oauth_codes_client_idx
    ON free_mcp_oauth_codes (client_id);

CREATE TABLE IF NOT EXISTS free_mcp_oauth_tokens (
    token_hash text PRIMARY KEY,
    kind       text NOT NULL,           -- 'access' | 'refresh'
    client_id  text NOT NULL,
    user_sub   text NOT NULL,
    provider   text NOT NULL DEFAULT 'google',
    scope      text NOT NULL DEFAULT '',
    resource   text NOT NULL DEFAULT '',
    expires_at timestamptz NOT NULL,
    user_email text NOT NULL DEFAULT '',
    -- Every token descended from one authorization shares this. Refresh
    -- rotation carries it forward, so a replayed refresh token revokes the
    -- whole line in one statement.
    family_id  text NOT NULL DEFAULT '',
    -- Set when a refresh token is rotated out. The row is KEPT rather than
    -- deleted: a deleted row and a stolen-and-replayed one look identical,
    -- and telling them apart is the whole point of reuse detection.
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS free_mcp_oauth_tokens_expiry_idx
    ON free_mcp_oauth_tokens (expires_at);
CREATE INDEX IF NOT EXISTS free_mcp_oauth_tokens_user_idx
    ON free_mcp_oauth_tokens (user_sub, provider);
CREATE INDEX IF NOT EXISTS free_mcp_oauth_tokens_family_idx
    ON free_mcp_oauth_tokens (family_id) WHERE family_id <> '';
CREATE INDEX IF NOT EXISTS free_mcp_oauth_tokens_client_idx
    ON free_mcp_oauth_tokens (client_id);

-- Housekeeping the sweep on /oauth/register performs, written out so it can
-- also be run by hand or by a Neon scheduled job:
--   DELETE FROM free_mcp_oauth_codes  WHERE expires_at < now();
--   DELETE FROM free_mcp_oauth_tokens WHERE expires_at < now();
