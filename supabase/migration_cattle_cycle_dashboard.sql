-- ============================================================================
-- Register "Cattle Cycle" (id 34, F&B › Beef) as a dashboard row
-- ============================================================================
-- Until this row exists the module only shows up for admins, through the
-- STATIC_DASHBOARDS fallback in index.html. Once inserted it becomes
-- panel-editable (order, visibility, title, description, tags), while
-- DASH_MODULES[34] keeps loading Beef/Cattle Cycle/cattle_cycle_dashboard.html.
--
-- Safe to re-run (idempotent via ON CONFLICT). Run in the Supabase SQL editor.
-- ----------------------------------------------------------------------------

insert into public.dashboards
  (id, sector, subsector, title, description, source, note, footer, url,
   tags, display_order, visible_to_all, coming_soon)
values
  (34, 'fnb', 'beef',
   'Cattle Cycle',
   'Female share of cattle slaughter — (cows + heifers) ÷ (cows + heifers + bulls + steers) — as a herd liquidation/retention gauge. Brazil: IBGE quarterly slaughter survey, monthly detail since 1997 (heads and carcass weight).',
   'IBGE · SIDRA 1092',
   '', '', '#',
   '{"Brazil","Beef","Cattle Cycle","Slaughter","IBGE"}',
   7,        -- after the BZ / U.S. beef spread trackers
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
