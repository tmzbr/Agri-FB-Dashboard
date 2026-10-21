-- ============================================================================
-- Card texts: BRL instead of R$
-- ============================================================================
-- The portal now writes Brazilian reais as "BRL" everywhere (dashboards,
-- units, tables). The Short Interest card (id 26) still said "R$ value" in
-- its description. This swaps R$ → BRL in every card text field, keeping any
-- other edit made in the admin panel. Safe to re-run.
-- ----------------------------------------------------------------------------

update public.dashboards set
  title       = replace(title,       'R$', 'BRL'),
  description = replace(description, 'R$', 'BRL'),
  source      = replace(source,      'R$', 'BRL'),
  note        = replace(note,        'R$', 'BRL'),
  footer      = replace(footer,      'R$', 'BRL')
where concat_ws(' ', title, description, source, note, footer) like '%R$%';

-- Verify (expect 0 rows):
-- select id, title from public.dashboards
-- where concat_ws(' ', title, description, source, note, footer) like '%R$%';
