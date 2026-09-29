-- ============================================================================
-- Register "BZ Chicken Domestic Spread Tracker" (id 33) as a dashboard row
-- ============================================================================
-- Until this row exists the module only shows up for admins, through the
-- STATIC_DASHBOARDS fallback in index.html. Once inserted it becomes
-- panel-editable (order, visibility, title, description, tags) like the other
-- trackers, while DASH_MODULES[33] keeps loading
-- Chicken/BZ Tracker/chicken_bz_domestic_dashboard.html.
--
-- Safe to re-run (idempotent via ON CONFLICT). Run in the Supabase SQL editor.
-- ----------------------------------------------------------------------------

insert into public.dashboards
  (id, sector, subsector, title, description, source, note, footer, url,
   tags, display_order, visible_to_all, coming_soon)
values
  (33, 'fnb', 'chicken',
   'BZ Chicken Domestic Spread Tracker',
   'Daily, weekly and monthly CEPEA chilled chicken wholesale price (SP, BRL/kg) vs. grain basket spread (margin %). Corn 66% + Soy PNA 34% (BRL/sc60kg), 2-month lag.',
   'CEPEA/ESALQ',
   '', '', '#',
   '{"Brazil","Chicken","Spread","Domestic","CEPEA"}',
   11,       -- right after the export tracker (id 10, order 10)
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
