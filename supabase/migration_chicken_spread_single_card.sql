-- ============================================================================
-- Brazil Chicken Spread Tracker: one card (id 10) with Exports | Domestic subtabs
-- ============================================================================
-- Supersedes migration_chicken_spread_brazil_domestic_card.sql. The Domestic
-- page (id 33) no longer has a card: index.html hides it via SUBTAB_DASH_IDS
-- and it is reached only through the Domestic subtab inside card 10. The row
-- stays so the subtab switch is still logged as its own dash_open; it becomes
-- visible_to_all because RLS would otherwise drop it for non-admins and their
-- subtab switches would go unlogged (the page itself was already reachable
-- from the Exports subtab by everyone).
--
-- Also renames the Capitals climate card (id 31) to Beer Score — Climate Monitor.
-- Safe to re-run. Run in the Supabase SQL editor.
-- ----------------------------------------------------------------------------

update public.dashboards set
  title       = 'Brazil Chicken Spread Tracker',
  description = 'Chicken price vs. grain basket spread, in two subtabs. Exports: weekly and monthly SECEX export price (NCM 0207, BCB PTAX FX). Domestic: daily, weekly and monthly CEPEA fresh chicken wholesale price (SP, BRL/kg). Corn 66% + Soy PNA 34% (BRL/sc60kg), 2-month lag.',
  source      = 'MDIC · CEPEA/ESALQ · BCB',
  tags        = '{"Brazil","Chicken","Spread","SECEX","CEPEA","Domestic"}'
where id = 10;

update public.dashboards set
  visible_to_all = true
where id = 33;

update public.dashboards set
  title = 'Beer Score — Climate Monitor'
where id = 31;

-- Verify:
-- select id, title, visible_to_all from public.dashboards where id in (10, 31, 33);
