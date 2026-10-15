-- ═══════════════════════════════════════════════════════════════════
-- Migration: Comps Table — setor (Food & Beverage × Agribusiness)
-- ═══════════════════════════════════════════════════════════════════
-- A visão consolidada passa a ser duas tabelas, uma por setor. O setor
-- fica na empresa (comps_company.sector: 'fnb' | 'agri') e é escolhido na
-- conferência do upload, para empresa nova cair no grupo certo.
-- display_order segue a ordem pedida pelo Bruno dentro de cada setor.
--
-- comps_save_model é recriada para gravar o setor; um upload sem setor
-- mantém o que já estava.
--
-- Depende de: migration_comps_table.sql (+ _hidden_adecoagro, para a AGRO).
-- Idempotente. Rodar no Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

alter table public.comps_company
  add column if not exists sector text check (sector in ('fnb', 'agri'));

update public.comps_company c set sector = v.sector, display_order = v.ord
from (values
  ('ABEV3', 'fnb',  1), ('MDIA3', 'fnb',  2), ('CAML3', 'fnb',  3),
  ('JBS',   'fnb',  4), ('BEEF3', 'fnb',  5), ('MBRF3', 'fnb',  6),
  ('SLCE3', 'agri', 1), ('AGRO3', 'agri', 2), ('TTEN3', 'agri', 3),
  ('VITT3', 'agri', 4), ('SOJA3', 'agri', 5), ('SMTO3', 'agri', 6),
  ('JALL3', 'agri', 7), ('AGRO',  'agri', 8)
) as v(ticker, sector, ord)
where c.ticker = v.ticker;

create or replace function public.comps_save_model(p_company jsonb, p_estimates jsonb)
returns void
language plpgsql
security invoker
as $$
begin
  if not public.is_current_user_admin() then
    raise exception 'admin only';
  end if;

  insert into public.comps_company as c
    (ticker, name, currency, price_symbol, display_order, sector, recommendation, target_price,
     target_label, model_price, shares_mn, model_date, source_file, sources, uploaded_at, uploaded_by)
  values
    (p_company->>'ticker', p_company->>'name', coalesce(p_company->>'currency', 'BRL'),
     p_company->>'price_symbol', coalesce((p_company->>'display_order')::int, 100),
     nullif(p_company->>'sector', ''),
     p_company->>'recommendation', (p_company->>'target_price')::numeric, p_company->>'target_label',
     (p_company->>'model_price')::numeric, (p_company->>'shares_mn')::numeric,
     (p_company->>'model_date')::date, p_company->>'source_file', p_company->'sources',
     now(), auth.uid())
  on conflict (ticker) do update set
    name = excluded.name, currency = excluded.currency, price_symbol = excluded.price_symbol,
    sector = coalesce(excluded.sector, c.sector),
    recommendation = excluded.recommendation, target_price = excluded.target_price,
    target_label = excluded.target_label, model_price = excluded.model_price,
    shares_mn = excluded.shares_mn, model_date = excluded.model_date,
    source_file = excluded.source_file, sources = excluded.sources,
    uploaded_at = excluded.uploaded_at, uploaded_by = excluded.uploaded_by;
    -- display_order e hidden ficam: são escolhas de layout, não vêm do modelo.

  delete from public.comps_estimate where ticker = p_company->>'ticker';
  insert into public.comps_estimate
    (ticker, year, period_label, revenue, ebitda, net_income, fcfe, net_debt, minorities, sources)
  select p_company->>'ticker', (e->>'year')::smallint, e->>'period_label',
         (e->>'revenue')::numeric, (e->>'ebitda')::numeric, (e->>'net_income')::numeric,
         (e->>'fcfe')::numeric, (e->>'net_debt')::numeric, (e->>'minorities')::numeric, e->'sources'
    from jsonb_array_elements(p_estimates) e;
end;
$$;

-- Verify:
-- select sector, display_order, ticker, name from public.comps_company order by sector, display_order;
