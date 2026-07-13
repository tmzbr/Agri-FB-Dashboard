-- Migration: view exposing each user's last access (most recent access_logs
-- event), for the "Last Access" column in the admin User Management table.
--
-- Why a view instead of aggregating in the browser:
--   Computing max(created_at) per user in SQL returns ~one row per user
--   (tiny payload) and is correct regardless of any PostgREST row cap — a
--   client-side aggregate would need to pull the entire access_logs table.
--
-- security_invoker = true → the view runs with the *caller's* privileges, so
-- the existing "select_admin" RLS policy on access_logs still applies: only
-- admins can read it (same as the analytics pane). Non-admins get zero rows.
--
-- Safe to run more than once (idempotent).

create or replace view public.user_last_access
  with (security_invoker = true) as
  select lower(user_email) as user_email,
         max(created_at)   as last_access
  from public.access_logs
  where user_email is not null and btrim(user_email) <> ''
  group by lower(user_email);

grant select on public.user_last_access to authenticated;

-- Sanity check.
select * from public.user_last_access order by last_access desc limit 20;
