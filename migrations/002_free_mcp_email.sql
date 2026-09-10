-- Free MCP server: the email lane (welcome note + cap note).
--
-- Idempotent, and safe to run before or after the code ships: every
-- statement is ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS, and
-- nothing currently deployed reads any of the new columns. Run it against
-- the same Neon database 001 was run against (the pooled `-pooler` host):
--
--     psql "$DATABASE_URL" -f mcp_server/migrations/002_free_mcp_email.sql
--
-- What it is for: `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md`
-- describes three sends. (a) the welcome, (b) the cap note, (c) the monthly
-- broadcast. (a) and (b) are transactional `POST /emails` and need no Resend
-- audience, so their consent, their triggers and their "already sent" guards
-- all have to live here.

-- ── the two send guards ──────────────────────────────────────────────────
-- `welcome_sent_at` already exists in 001; repeated here so a database that
-- was created from an older copy of 001 converges on the same shape. It is
-- the "once per user, ever" guard and is stamped by an
-- UPDATE ... WHERE welcome_sent_at IS NULL, so two instances racing one
-- first sign-in send exactly one note.
ALTER TABLE free_mcp_users
    ADD COLUMN IF NOT EXISTS welcome_sent_at timestamptz;

-- The cap note's guard. NOT "once ever": the sequence file allows one per
-- user per 30 days, whichever branch fired, so this is a timestamp and not a
-- boolean, and the claim is
-- UPDATE ... WHERE cap_note_sent_at IS NULL OR cap_note_sent_at < now() - 30d.
ALTER TABLE free_mcp_users
    ADD COLUMN IF NOT EXISTS cap_note_sent_at timestamptz;

-- The lane-wide "at most 20 cap notes per UTC day" guard reads this: a spike
-- in sign-ins must not turn into a mail blast, and the overflow simply waits
-- for the next day.
CREATE INDEX IF NOT EXISTS free_mcp_users_cap_note_idx
    ON free_mcp_users (cap_note_sent_at) WHERE cap_note_sent_at IS NOT NULL;

-- ── branch D: "the daily cap stopped a scan" ─────────────────────────────
-- Counted per UTC day, on the same row as the day's searches, because the
-- day cap rolls over at 00:00 UTC and a refusal recorded on another clock
-- would disagree with the counter that produced it. Branch D fires on 2
-- DISTINCT days with a hit inside a rolling 7 days -- so what matters is
-- that the day has a non-zero count, not how large the count is.
ALTER TABLE free_mcp_user_days
    ADD COLUMN IF NOT EXISTS cap_hits bigint NOT NULL DEFAULT 0;

-- ── which product to link ────────────────────────────────────────────────
-- The cap note prints exactly ONE URL, and the sequence file says it is
-- chosen by what the user actually searched: flights.flightpowers.com/mcp or
-- hotels.flightpowers.com/mcp. That needs a per-tool split, which
-- free_mcp_user_days does not have -- it has one number per day. Same shape
-- as that table with `tool` added to the key, written from the same
-- best-effort background task, so it is a floor and not a count. With no
-- rows at all the note defaults to flights.
CREATE TABLE IF NOT EXISTS free_mcp_user_tools (
    google_sub text   NOT NULL,
    day        date   NOT NULL,
    tool       text   NOT NULL,
    searches   bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (google_sub, day, tool)
);

CREATE INDEX IF NOT EXISTS free_mcp_user_tools_day_idx
    ON free_mcp_user_tools (google_sub, day);
