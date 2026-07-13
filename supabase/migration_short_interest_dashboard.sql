-- ============================================================================
-- Make "Short Interest" (id 26) an admin-editable dashboard
-- ============================================================================
-- The Short Interest module was the only Market Watch module still hardcoded
-- as a "static" entry in index.html (STATIC_DASHBOARDS). Because there was no
-- row for id 26 in this table, the admin panel's display_order / visibility
-- edits hit 0 rows and reverted on reload (F5).
--
-- Buyback Monitor (24) and Corporate Insider Trading Tracker (25) already live
-- in this table (orders 3 and 4), leaving a gap at order 2 — where Short
-- Interest belongs. The app merges Supabase dashboards over the hardcoded ones
-- by id, so once id 26 exists here it becomes fully panel-editable (order,
-- visibility, title, description, tags), while DASH_MODULES[26] still loads
-- Market Watch/Short Interest/short_interest.html.
--
-- Safe to re-run (idempotent via ON CONFLICT). Run in the Supabase SQL editor
-- (the service role there bypasses RLS).
-- ----------------------------------------------------------------------------

insert into public.dashboards
  (id, sector, subsector, title, description, source, note, footer, url,
   tags, display_order, visible_to_all, coming_soon)
values
  (26, 'market-watch', '',
   'Short Interest',
   'Securities-lending open positions (short interest) for the covered agri & F&B names — shares on loan, R$ value, % of shares outstanding and % of free float, plus borrow rate. Source: B3 Boletim Diário do Mercado.',
   'B3 · Empréstimos de Ativos',
   '', '', '#',
   '{"Brazil","Short Interest","Equity","B3"}',
   2,        -- display_order: 2nd in Market Watch (after Sector News = 1)
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

-- Keep the id sequence ahead of every explicit id so new dashboards created
-- from the admin panel never collide with an existing id.
select setval(
  pg_get_serial_sequence('public.dashboards', 'id'),
  (select max(id) from public.dashboards)
);

-- Verify:
-- select id, title, sector, display_order, visible_to_all
-- from public.dashboards where sector = 'market-watch' order by display_order;
