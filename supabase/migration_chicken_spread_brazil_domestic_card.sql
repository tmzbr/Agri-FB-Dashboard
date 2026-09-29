-- ============================================================================
-- Brazil Chicken Spread Tracker: Exports (id 10) and Domestic (id 33) as two cards
-- ============================================================================
-- Each card opens its own page; inside both, the Exports | Domestic subtabs
-- still switch between the two pages.
--   id 10 → Chicken/BZ Tracker/chicken_bz_dashboard.html          (Exports)
--   id 33 → Chicken/BZ Tracker/chicken_bz_domestic_dashboard.html (Domestic)
-- Supersedes migration_chicken_bz_domestic_tab.sql (id 10 text back to
-- exports-only). Safe to re-run. Run in the Supabase SQL editor.
-- ----------------------------------------------------------------------------

update public.dashboards set
  title       = 'Brazil Chicken Spread Tracker — Exports',
  description = 'Weekly and monthly SECEX chicken export price vs. grain basket spread. Corn 66% + Soy PNA 34% (BRL/sc60kg), 2-month lag. NCM 0207 with BCB PTAX FX.',
  source      = 'MDIC · CEPEA/ESALQ · BCB',
  tags        = '{"Brazil","Chicken","Spread","SECEX","CEPEA"}'
where id = 10;

insert into public.dashboards
  (id, sector, subsector, title, description, source, note, footer, url,
   tags, display_order, visible_to_all, coming_soon)
values
  (33, 'fnb', 'chicken',
   'Brazil Chicken Spread Tracker — Domestic',
   'Daily, weekly and monthly CEPEA fresh chicken wholesale price (SP, BRL/kg) vs. grain basket spread. Corn 66% + Soy PNA 34% (BRL/sc60kg), 2-month lag.',
   'CEPEA/ESALQ',
   '', '', '#',
   '{"Brazil","Chicken","Spread","Domestic","CEPEA"}',
   11,       -- right after the Exports card (order 10)
   false,    -- visible_to_all: admin-only for now (flip in the panel when ready)
   false)    -- coming_soon
on conflict (id) do update set
  sector         = excluded.sector,
  subsector      = excluded.subsector,
  title          = excluded.title,
  description    = excluded.description,
  source         = excluded.source,
  tags           = excluded.tags,
  display_order  = excluded.display_order,
  visible_to_all = excluded.visible_to_all,
  coming_soon    = excluded.coming_soon;

select setval(
  pg_get_serial_sequence('public.dashboards', 'id'),
  (select max(id) from public.dashboards)
);
