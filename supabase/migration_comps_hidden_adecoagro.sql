-- ═══════════════════════════════════════════════════════════════════
-- Migration: Comps Table — ocultar métricas por empresa + Adecoagro
-- ═══════════════════════════════════════════════════════════════════
-- 1. comps_company.hidden: lista do que NÃO mostrar daquela empresa, na
--    tabela consolidada e no detalhe. Chaves usadas pelo front:
--      target (preço-alvo + upside) · pe · ev_ebitda · fcfe_yld
--      revenue · ebitda · net_income
--    Ex.: '{pe}' esconde o P/E de uma empresa com lucro distorcido. O admin
--    marca na própria tela (seção "Display" do detalhe). Um novo upload do
--    modelo NÃO mexe nisso (comps_save_model não toca a coluna).
--
-- 2. Adecoagro (NYSE: AGRO, USD). Os números entram pelo upload do modelo.
--
-- Depende de: migration_comps_table.sql. Idempotente. Rodar no SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

alter table public.comps_company
  add column if not exists hidden text[] not null default '{}';

insert into public.comps_company (ticker, name, currency, price_symbol, display_order)
values ('AGRO', 'Adecoagro', 'USD', 'AGRO', 9)   -- junto da BrasilAgro (AGRO3, ordem 9)
on conflict (ticker) do nothing;

-- Verify:
-- select ticker, name, price_symbol, display_order, hidden from public.comps_company order by display_order, ticker;
