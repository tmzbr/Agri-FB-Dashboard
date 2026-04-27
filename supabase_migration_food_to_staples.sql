-- ═══════════════════════════════════════════════════════════════════
-- Migration: rename subsector 'food' back to 'staples'
-- ═══════════════════════════════════════════════════════════════════
-- Context:
--   The frontend sub-tab under "Food & Beverage" was renamed from
--   "Food" (id='food') to "Staples" (id='staples'). The original
--   seed data already uses subsector='staples', but any rows
--   inserted by the admin UI between the rename cycles may carry
--   subsector='food'. This migration brings them back in line.
--
-- Idempotent: no-op if no rows match.
-- Run this in the Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

update public.dashboards  set subsector = 'staples' where subsector = 'food';
update public.access_logs set subsector = 'staples' where subsector = 'food';

-- Verify (expect zero rows with subsector='food'):
select 'dashboards'  as table_name, sector, subsector, count(*) as row_count
  from public.dashboards  group by sector, subsector
union all
select 'access_logs' as table_name, sector, subsector, count(*) as row_count
  from public.access_logs group by sector, subsector
order by table_name, sector, subsector;
