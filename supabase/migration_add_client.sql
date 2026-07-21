-- Migration: add a "client" (owning house) column to portal_users.
--
-- The dashboard shows Client between Name and Email in the User Management
-- table. When a row's client is NULL/empty the UI derives a suggestion from
-- the email domain (token after @, before the first dot, capitalized). This
-- migration adds the column and backfills existing rows with that same derived
-- value so nothing has to be filled in by hand.
--
-- Safe to run more than once (idempotent).

alter table public.portal_users
  add column if not exists client text;

-- Backfill only rows that don't already have a client set.
update public.portal_users
set client = case
    when split_part(split_part(email, '@', 2), '.', 1) <> ''
      then upper(left(split_part(split_part(email, '@', 2), '.', 1), 1))
           || substr(split_part(split_part(email, '@', 2), '.', 1), 2)
    else null
  end
where client is null or btrim(client) = '';

-- Sanity check.
select client, count(*) as users
from public.portal_users
group by client
order by users desc;
