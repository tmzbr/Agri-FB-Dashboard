-- ============================================================================
-- BZ Chicken Spread Tracker (id 10) now has two tabs: Exports and Domestic
-- ============================================================================
-- Card text only — the module file (DASH_MODULES[10]) is unchanged; the
-- Domestic tab lives inside it (chicken_bz_domestic_dashboard.html).
-- Safe to re-run. Run in the Supabase SQL editor.
-- ----------------------------------------------------------------------------
update public.dashboards set
  description = 'Exports and Domestic tabs. Chicken price vs. grain basket spread — SECEX export price (NCM 0207, BCB PTAX FX) and CEPEA chilled chicken wholesale SP (BRL/kg, daily). Corn 66% + Soy PNA 34% (BRL/sc60kg), 2-month lag.',
  source      = 'MDIC · CEPEA/ESALQ · BCB',
  tags        = '{"Brazil","Chicken","Spread","SECEX","CEPEA","Domestic"}'
where id = 10;
