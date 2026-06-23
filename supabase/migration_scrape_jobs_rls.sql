-- ═══════════════════════════════════════════════════════════════════
-- Migration: enable RLS on public.scrape_jobs  (fixes rls_disabled_in_public)
-- ═══════════════════════════════════════════════════════════════════
-- Context:
--   scrape_jobs is created and used ONLY by the Market Watch clipping
--   worker (Market Watch/feed-news/app.py) through a DIRECT Postgres
--   connection (pg8000, user = postgres). That connection bypasses RLS,
--   so the worker keeps working regardless of policies.
--
--   The dashboard frontend never touches this table via the Supabase
--   anon key — it only calls the worker's own HTTP endpoints
--   (/api/scrape-trigger, /api/scrape-result, /api/scrape-status).
--
--   Therefore the correct fix is to enable RLS with NO policies: this
--   blocks ALL access through the public PostgREST API (anon /
--   authenticated roles), which is exactly what the linter flags, while
--   the worker's direct connection is unaffected.
--
-- Safe to run on existing data: no table contents are touched.
-- Idempotent.
-- Run this in the Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

-- Ensure the table exists (no-op if it already does); mirrors app.py.
create table if not exists public.scrape_jobs (
  job_id     text primary key,
  status     text default 'pending',
  results    text default '',
  created_at text default to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS'),
  updated_at text default to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS')
);

-- Enable RLS. No policies → no anon/authenticated access via PostgREST.
-- The worker connects directly as `postgres`, which bypasses RLS.
alter table public.scrape_jobs enable row level security;

-- Defensive: drop any leftover permissive policies from earlier attempts.
drop policy if exists "read_all"   on public.scrape_jobs;
drop policy if exists "write_anon" on public.scrape_jobs;

-- ── Verify (rls_enabled = true, zero policies) ─────────────────────
select relname, relrowsecurity as rls_enabled
  from pg_class where relname = 'scrape_jobs';
select count(*) as policy_count
  from pg_policies
  where schemaname = 'public' and tablename = 'scrape_jobs';
