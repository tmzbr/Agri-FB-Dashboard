-- Migration: add an "invite_template" column to team_settings.
--
-- The admin User Management panel has a "Client Invite Message" box where
-- admins can customize the copy-paste welcome message shown when clicking
-- "Copy Invite" next to a user. The template supports {{name}}, {{email}}
-- and {{link}} tokens, substituted client-side. This migration adds the
-- column and backfills it with the app's default template so existing
-- installs don't show a blank box.
--
-- Safe to run more than once (idempotent).

alter table public.team_settings
  add column if not exists invite_template text;

update public.team_settings
set invite_template = $$Hi {{name}},

You now have access to the dashboard.

Link: {{link}}
Login email: {{email}}

No password is required — just enter the email above on the sign-in screen.$$
where id = 1 and (invite_template is null or btrim(invite_template) = '');
