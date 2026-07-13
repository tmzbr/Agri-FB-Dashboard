-- ============================================================================
-- Align Buyback (24) & Insider (25) dashboard metadata with the modules
-- ============================================================================
-- These two rows carried older, generic copy ("Buyback Monitor", "CVM Form
-- 358", etc.) that no longer matches what the modules actually display. This
-- aligns title / description / source / tags with the canonical module
-- definitions. display_order (3, 4) and visible_to_all are left untouched.
--
-- Optional & idempotent. Run in the Supabase SQL editor. (You can also edit
-- these same fields from the admin panel — this just does both at once.)
-- ----------------------------------------------------------------------------

update public.dashboards set
  title       = 'CVM Buybacks',
  description = 'Treasury repurchase programs and buyback activity for 13 Brazilian agri tickers. Source: CVM IPE Individual.',
  source      = 'CVM · IPE Individual',
  tags        = '{"Brazil","Buybacks","Equity","CVM"}'
where id = 24;

update public.dashboards set
  title       = 'CVM Insider Trading',
  description = 'Controller, management and board holdings and transactions for 13 Brazilian agri tickers. Source: CVM Formulário Consolidado.',
  source      = 'CVM · Formulário Consolidado',
  tags        = '{"Brazil","Insider","Equity","CVM"}'
where id = 25;

-- Verify:
-- select id, title, source, display_order, visible_to_all
-- from public.dashboards where sector = 'market-watch' order by display_order;
