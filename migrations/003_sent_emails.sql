-- Free MCP server: the send ledger for the two SCHEDULED notes.
--
-- Idempotent and safe to run before or after the code ships: one
-- CREATE TABLE IF NOT EXISTS and two indexes, and nothing currently
-- deployed reads any of it. Run it against the same Neon database 001 and
-- 002 were run against (the pooled `-pooler` host):
--
--     psql "$DATABASE_URL" -f mcp_server/migrations/003_sent_emails.sql
--
-- What it is for. 001 and 002 gave each of the two EVENT-driven sends its
-- own column on the user row: `welcome_sent_at` (once ever) and
-- `cap_note_sent_at` (once per 30 days). That shape does not extend. The
-- day-2 activation note and the cap-day note are evaluated by a job
-- (`POST /email/run-sequence`, `src/sequence.py`) that can be re-run by
-- hand, re-run by a retried cron, or run twice concurrently, and every note
-- added after these two would be another column and another claim
-- statement. So sends get a ledger.
--
-- `state/gtm/email/FREE-SIGNIN-SEQUENCE-2026-09-09.md`: "Idempotency is a
-- database row, not a header."

CREATE TABLE IF NOT EXISTS sent_emails (
    -- The Google `sub`, the same key `free_mcp_users` is keyed by. NOT the
    -- address: a person who changes their Google email is the same person
    -- and must not get the sequence a second time.
    user_sub  text        NOT NULL,
    -- The kind IS the window, which is what makes the unique key below do
    -- all the work:
    --   'day2'            once per account, ever
    --   'cap:YYYY-MM-DD'  at most one cap note per account per UTC day
    -- UTC because the day cap this note is about rolls over at 00:00 UTC,
    -- so a ledger on another clock would disagree with the refusal that
    -- produced the row. See `freeusers.cap_kind`.
    kind      text        NOT NULL,
    -- Stamped at CLAIM time, before the Resend call. A row that exists with
    -- an empty `resend_id` means "a send was claimed and we never learned
    -- how it ended" -- a timeout, or an instance frozen mid-call. Those are
    -- deliberately NOT retried: Resend may have delivered the message, and
    -- a duplicate of an email nobody asked for is the failure that costs a
    -- sending domain.
    sent_at   timestamptz NOT NULL DEFAULT now(),
    -- Resend's message id, filled in after a 200. The only handle we have
    -- for looking a send back up in their dashboard.
    resend_id text        NOT NULL DEFAULT '',
    PRIMARY KEY (user_sub, kind)
);

-- The primary key already gives the uniqueness the claim relies on
-- (INSERT ... ON CONFLICT (user_sub, kind) DO NOTHING); named explicitly so
-- a database built from an older copy of this file, or one where the table
-- was created by hand, converges on the same constraint.
CREATE UNIQUE INDEX IF NOT EXISTS sent_emails_user_kind_idx
    ON sent_emails (user_sub, kind);

-- "How many notes went out today, across the whole lane" and "what did this
-- lane send in the last 14 days" -- the kill-rule reads, which are the only
-- reason this table is worth querying by anything other than its key.
CREATE INDEX IF NOT EXISTS sent_emails_sent_at_idx
    ON sent_emails (sent_at);
