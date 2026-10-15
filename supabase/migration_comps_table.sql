-- ═══════════════════════════════════════════════════════════════════
-- Migration: Comps Table (Market Watch, id 35) — admin-only por ora
-- ═══════════════════════════════════════════════════════════════════
-- Três tabelas:
--
--   comps_company  — 1 linha por empresa coberta: rating, preço-alvo,
--                    # ações e o preço que estava no modelo. Vem do upload
--                    do Excel, conferido pelo admin antes de gravar.
--   comps_estimate — 1 linha por (empresa, ano fiscal 2026-30): receita,
--                    EBITDA, lucro líquido, FCFE, dívida líquida e
--                    minoritários. Tudo em milhões da moeda do modelo.
--   comps_price    — fechamento diário por símbolo (ABEV3.SA, JBS...),
--                    gravado 1x/dia pelo workflow comps_prices.yml com a
--                    service role (ignora RLS).
--
-- Os múltiplos NÃO são gravados: o front recalcula com o último fechamento
--   Mkt Cap = preço D-1 × # ações
--   EV_t    = Mkt Cap + Dívida Líquida_t + Minoritários_t
--   P/E_t   = Mkt Cap / Lucro Líquido_t
--   EV/EBITDA_t = EV_t / EBITDA_t
--   FCFE Yield_t = FCFE_t / Mkt Cap
-- que é a mesma conta dos blocos de múltiplos dos modelos.
--
-- Leitura e escrita: só admin, enquanto o módulo é admin-only. Ao abrir o
-- módulo para usuários, trocar as policies "select_admin" por
-- "select_authenticated" (using true) — a visibilidade do card sozinha não
-- protege o dado, a API do Supabase continua acessível a quem tem JWT.
--
-- Depende de: migration_rls_lockdown.sql (is_current_user_admin()).
-- Idempotente. Rodar no Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

create table if not exists public.comps_company (
  ticker          text primary key,
  name            text not null,
  currency        text not null default 'BRL',
  price_symbol    text not null,              -- símbolo no Yahoo Finance
  display_order   int  not null default 100,
  recommendation  text,                       -- OP / MP / UP
  target_price    numeric,
  target_label    text,                       -- ex.: YE27
  model_price     numeric,                    -- preço usado no modelo (fallback se não houver fechamento)
  shares_mn       numeric,                    -- # ações (milhões) do bloco de valuation
  model_date      date,
  source_file     text,
  sources         jsonb,                      -- célula de origem de cada número + avisos do parser
  uploaded_at     timestamptz,
  uploaded_by     uuid references auth.users(id) on delete set null
);

create table if not exists public.comps_estimate (
  ticker        text not null references public.comps_company(ticker) on delete cascade,
  year          smallint not null,            -- ano fiscal de término (SMTO3 2025/26 = 2026)
  period_label  text,                         -- rótulo original do modelo
  revenue       numeric,
  ebitda        numeric,
  net_income    numeric,
  fcfe          numeric,
  net_debt      numeric,
  minorities    numeric,
  sources       jsonb,
  primary key (ticker, year)
);

create table if not exists public.comps_price (
  price_symbol  text not null,
  date          date not null,
  close         numeric not null,
  currency      text,
  source        text,
  updated_at    timestamptz not null default now(),
  primary key (price_symbol, date)
);

alter table public.comps_company  enable row level security;
alter table public.comps_estimate enable row level security;
alter table public.comps_price    enable row level security;

drop policy if exists "select_admin" on public.comps_company;
drop policy if exists "modify_admin" on public.comps_company;
drop policy if exists "select_admin" on public.comps_estimate;
drop policy if exists "modify_admin" on public.comps_estimate;
drop policy if exists "select_admin" on public.comps_price;
drop policy if exists "modify_admin" on public.comps_price;

create policy "select_admin" on public.comps_company  for select to authenticated using (public.is_current_user_admin());
create policy "modify_admin" on public.comps_company  for all    to authenticated using (public.is_current_user_admin()) with check (public.is_current_user_admin());
create policy "select_admin" on public.comps_estimate for select to authenticated using (public.is_current_user_admin());
create policy "modify_admin" on public.comps_estimate for all    to authenticated using (public.is_current_user_admin()) with check (public.is_current_user_admin());
create policy "select_admin" on public.comps_price    for select to authenticated using (public.is_current_user_admin());
create policy "modify_admin" on public.comps_price    for all    to authenticated using (public.is_current_user_admin()) with check (public.is_current_user_admin());

-- Grava empresa + estimativas numa transação só: um upload pela metade
-- (empresa nova com ano faltando) deixaria a tabela consolidada errada.
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
    (ticker, name, currency, price_symbol, display_order, recommendation, target_price,
     target_label, model_price, shares_mn, model_date, source_file, sources, uploaded_at, uploaded_by)
  values
    (p_company->>'ticker', p_company->>'name', coalesce(p_company->>'currency', 'BRL'),
     p_company->>'price_symbol', coalesce((p_company->>'display_order')::int, 100),
     p_company->>'recommendation', (p_company->>'target_price')::numeric, p_company->>'target_label',
     (p_company->>'model_price')::numeric, (p_company->>'shares_mn')::numeric,
     (p_company->>'model_date')::date, p_company->>'source_file', p_company->'sources',
     now(), auth.uid())
  on conflict (ticker) do update set
    name = excluded.name, currency = excluded.currency, price_symbol = excluded.price_symbol,
    recommendation = excluded.recommendation, target_price = excluded.target_price,
    target_label = excluded.target_label, model_price = excluded.model_price,
    shares_mn = excluded.shares_mn, model_date = excluded.model_date,
    source_file = excluded.source_file, sources = excluded.sources,
    uploaded_at = excluded.uploaded_at, uploaded_by = excluded.uploaded_by;
    -- display_order fica: é escolha de layout, não vem do modelo.

  delete from public.comps_estimate where ticker = p_company->>'ticker';
  insert into public.comps_estimate
    (ticker, year, period_label, revenue, ebitda, net_income, fcfe, net_debt, minorities, sources)
  select p_company->>'ticker', (e->>'year')::smallint, e->>'period_label',
         (e->>'revenue')::numeric, (e->>'ebitda')::numeric, (e->>'net_income')::numeric,
         (e->>'fcfe')::numeric, (e->>'net_debt')::numeric, (e->>'minorities')::numeric, e->'sources'
    from jsonb_array_elements(p_estimates) e;
end;
$$;

-- Cobertura atual, para a tabela já nascer com uma linha (e um botão de
-- upload) por empresa. Os números entram pelo upload.
insert into public.comps_company (ticker, name, currency, price_symbol, display_order) values
  ('ABEV3', 'Ambev',          'BRL', 'ABEV3.SA',  1),
  ('MDIA3', 'M. Dias Branco', 'BRL', 'MDIA3.SA',  2),
  ('CAML3', 'Camil',          'BRL', 'CAML3.SA',  3),
  ('VITT3', 'Vittia',         'BRL', 'VITT3.SA',  4),
  ('JBS',   'JBS',            'USD', 'JBS',       5),
  ('MBRF3', 'MBRF',           'BRL', 'MBRF3.SA',  6),
  ('BEEF3', 'Minerva',        'BRL', 'BEEF3.SA',  7),
  ('SLCE3', 'SLC Agrícola',   'BRL', 'SLCE3.SA',  8),
  ('AGRO3', 'BrasilAgro',     'BRL', 'AGRO3.SA',  9),
  ('SOJA3', 'Boa Safra',      'BRL', 'SOJA3.SA', 10),
  ('TTEN3', '3tentos',        'BRL', 'TTEN3.SA', 11),
  ('SMTO3', 'São Martinho',   'BRL', 'SMTO3.SA', 12),
  ('JALL3', 'Jalles Machado', 'BRL', 'JALL3.SA', 13)
on conflict (ticker) do nothing;

-- Card no Market Watch. visible_to_all = false: só admin vê.
-- O painel admin cria dashboards pela sequence; se o 35 já tiver sido usado
-- por outro card, aborta em vez de sobrescrever — trocar o id aqui e em
-- DASH_MODULES (index.html).
do $$
begin
  if exists (select 1 from public.dashboards where id = 35 and title <> 'Comps Table') then
    raise exception 'dashboards.id 35 já existe com outro título — escolha outro id';
  end if;
end $$;

insert into public.dashboards
  (id, sector, subsector, title, description, source, note, footer, url,
   tags, display_order, visible_to_all, coming_soon)
values
  (35, 'market-watch', '',
   'Comps Table',
   'Coverage comps built from our Excel models — target price, upside, revenue, EBITDA, net income, P/E, EV/EBITDA and FCFE yield FY26–FY28, re-priced daily at the previous close.',
   'Itaú BBA models · Yahoo Finance',
   '', '', '#',
   '{"Brazil","Comps","Valuation","Equity"}',
   5,
   false,    -- visible_to_all: admin-only por enquanto
   false)
on conflict (id) do update set
  sector         = excluded.sector,
  subsector      = excluded.subsector,
  title          = excluded.title,
  description    = excluded.description,
  source         = excluded.source,
  tags           = excluded.tags,
  visible_to_all = excluded.visible_to_all,
  coming_soon    = excluded.coming_soon;

select setval(
  pg_get_serial_sequence('public.dashboards', 'id'),
  (select max(id) from public.dashboards)
);

-- Verify:
-- select id, title, display_order, visible_to_all from public.dashboards where id = 35;
-- select ticker, name, price_symbol from public.comps_company order by display_order;
