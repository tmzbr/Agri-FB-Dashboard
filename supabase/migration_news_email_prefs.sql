-- Migration: add the Sector News email preferences to team_settings.
--
-- The Sector News module builds a Daily News draft (.eml) whose subject and
-- "*** ... ***" header used to carry a hardcoded house name, and whose footer
-- used to carry a hardcoded list of analyst contacts. Both now come from here,
-- so the house branding lives in the database (admin-editable) instead of in
-- the repository. Only admins can write these — the editor sits inside the
-- admin panel, and RLS on team_settings already restricts writes to admins.
--
--   news_email_title     → subject line + centered header of the Daily News email
--   news_email_signature → footer contacts, as a JSON array of
--                          {"name": "...", "phone": "...", "email": "..."}.
--                          The first entry renders with a bold name, the rest
--                          in grey — matching the layout the email always had.
--                          Empty array = header only, no contacts.
--
-- Both defaults are deliberately house-neutral: the real values are typed by an
-- admin in the portal (Settings → Admin Panel → Sector News), so no house name
-- or corporate address ever reaches this repository.
--
-- Safe to run more than once (idempotent).

alter table public.team_settings
  add column if not exists news_email_title text;

alter table public.team_settings
  add column if not exists news_email_signature jsonb;

update public.team_settings
set news_email_title = 'Agribusiness, Food & Beverage – Daily News'
where id = 1 and (news_email_title is null or btrim(news_email_title) = '');

update public.team_settings
set news_email_signature = '[]'::jsonb
where id = 1 and news_email_signature is null;

-- Sanity check.
select news_email_title, news_email_signature
from public.team_settings where id = 1;
