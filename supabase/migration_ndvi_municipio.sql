-- ═══════════════════════════════════════════════════════════════════
-- Migration: ndvi_municipio — NDVI por município (MODIS Terra+Aqua; VIIRS depois)
-- ═══════════════════════════════════════════════════════════════════
-- Modelada no climate_cell: uma linha por (município, modelo, ano), cada variável
-- em base64 de int16[46] little-endian. As 46 posições são a grade de 8 dias do ano:
--     posição = (dia_do_ano_do_início_da_janela - 1) / 8      (divisão inteira)
--     dia do ano do início da janela = posição * 8 + 1
-- MODIS: Terra (MOD13Q1) ocupa as posições pares e Aqua (MYD13Q1) as ímpares, com
-- janelas de 16 dias. O VIIRS gera uma janela de 16 dias a cada 8 dias e ocupará todas
-- as posições; por isso entra como model = 'viirs', como o climate_cell separa 'nasa'
-- e 'era5'. -32768 = sem dado.
--
--   ndvi  NDVI médio dos pixels usados no município        × 10000  (0,4712 -> 4712)
--   p10   percentil 10 do NDVI entre os pixels              × 10000
--   p90   percentil 90 do NDVI entre os pixels              × 10000
--   pct   % dos pixels do município que foram usados        × 10     (97,3 % -> 973)
--         (pixels válidos com confiabilidade 0=bom ou 1=marginal; exclui nuvem e neve/gelo)
--   dobs  dias entre o início da janela e a data efetiva    × 1      (mediana dos pixels)
--
-- A janela de 16 dias é um composto: cada pixel traz a melhor observação do período,
-- e a data real dela pode cair até ~2 semanas depois do início. 'dobs' guarda isso para
-- alinhar com chuva e temperatura diárias.
--
-- Depende de: migration_rls_lockdown.sql (is_current_user_admin()). Idempotente.
-- ═══════════════════════════════════════════════════════════════════

create table if not exists public.ndvi_municipio (
  cod_ibge   integer  not null,
  model      text     not null default 'modis',
  year       smallint not null,
  ndvi       text,
  p10        text,
  p90        text,
  pct        text,
  dobs       text,
  updated_at timestamptz not null default now(),
  primary key (cod_ibge, model, year)
);

comment on table public.ndvi_municipio is
  'NDVI por município (código IBGE). Janelas de 16 dias do MODIS Terra+Aqua. Cada coluna = base64 de int16[46] na grade de 8 dias; -32768 = sem dado.';

alter table public.ndvi_municipio enable row level security;

drop policy if exists "select_authenticated" on public.ndvi_municipio;
drop policy if exists "modify_admin"         on public.ndvi_municipio;

-- Dado público entre usuários autenticados (medição de satélite). A escrita normal vem
-- do carregador com service role (que ignora RLS); a política abaixo cobre correção manual.
create policy "select_authenticated" on public.ndvi_municipio
  for select using (auth.uid() is not null);
create policy "modify_admin" on public.ndvi_municipio
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());
