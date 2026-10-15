/* ═══════════════════════════════════════════════════════════════════
   Comps Table — parser dos modelos Excel de cobertura
   ═══════════════════════════════════════════════════════════════════
   Roda no browser (SheetJS) e no Node (testes). Não grava nada: devolve o
   que SERIA armazenado, com a célula de origem de cada número, para o admin
   conferir antes de confirmar.

   Os modelos seguem o template Itaú BBA, mas a aba Output não tem layout fixo
   (blocos lado a lado, rótulos que mudam: "Adj. EBITDA", "EBITDA Adjusted",
   "Adjusted EBITDA"...). Por isso tudo é achado por RÓTULO, nunca por célula:

   1. Bloco de valuation (DCF / Valuation / NAV): # ações, preço-alvo, preço do
      modelo, dívida líquida e minoritários usados no TP.
   2. Séries anuais (Output): para cada rótulo, o cabeçalho de anos é o mais
      próximo ACIMA que tenha ano na coluna logo à direita do rótulo. Uma
      mesma linha pode ser dado no bloco da esquerda e cabeçalho no da direita
      (SLCE3 linha 36), então cabeçalho é por coluna, não por linha.
   3. Mask-UploadModel (linkada por fórmula ao Model, vai até 2030): só
      completa anos/métricas que a Output não tem, e só se bater com a Output
      nos anos em comum — prova de que a convenção de ano é a mesma.

   Ano fiscal: "2025/26" vira FY26 (ano de término), igual à Mask da SMTO3.
   ═══════════════════════════════════════════════════════════════════ */
(function (root) {
  'use strict';

  var YEARS = [2026, 2027, 2028, 2029, 2030];
  var METRICS = ['revenue', 'ebitda', 'net_income', 'fcfe', 'net_debt'];
  var VALUATION_SHEETS = ['DCF', 'Valuation', 'NAV', 'Output', 'Cover'];
  var READ_SHEETS = ['Cover', 'Output', 'DCF', 'Valuation', 'NAV', 'Mask-UploadModel'];

  // Prioridade importa: a primeira regra que achar série com dado vence.
  // Dentro da mesma regra, ganha a linha com mais anos 2026-30 preenchidos.
  var EXCL_RATIO = /margin|yield|\/|%|ev\b|per share|growth|to equity|fin exp|\(x\)/i;
  var SERIES_RULES = {
    revenue: [
      [/^net revenues?\b/i],
      [/^(total )?revenues?\b/i],
    ],
    ebitda: [
      [/^(adj(usted)?\.?\s*ebitda|ebitda\s*adj(usted)?\.?)(\s*\(.*\))?$/i, /hedge|ex-leasing/i],
      [/^(adj(usted)?\.?\s*ebitda|ebitda\s*adj(usted)?\.?)/i],
      [/^ebitda(\s*\(.*\))?$/i],
    ],
    net_income: [
      [/^(adj(usted)?\.?\s*net income|net income\s*adj(usted)?)/i],
      [/^net income\s*\(controlling\)/i],
      [/^net income(\s*\((brl|usd)[^)]*\))?$/i],
      [/^net income/i, /total|minorit/i],
    ],
    fcfe: [
      [/^(\(=\)\s*)?fcfe(\s*\(before changes in debt\))?$/i],
      [/^fcfe generation$/i],
      [/^fcfe\b/i, /recurring|ex-|yield/i],
      [/^fcfe\b.*(recurring|ex-)/i, /yield/i],
      // TTEN3: "FCF Generation" = EBITDA − capex − resultado fin. caixa − ΔWK − IR
      [/^fcf generation$/i],
    ],
    net_debt: [
      [/^net debt(\s*\((r\$|brl|usd)[^)]*\))?$/i],
      [/^net debt\b/i, /fully diluted|fiagro/i],
      [/^(adj\.\s*)?net debt\b/i],
    ],
  };
  var MASK_LABELS = {
    revenue: ['Net Revenues Adjusted', 'Net Revenues'],
    ebitda: ['EBITDA Adjusted', 'EBITDA'],
    net_income: ['Net income Adjusted', 'Net income'],
    fcfe: ['FCFE'],
    net_debt: ['Net debt'],
  };

  // ── utilidades de grade ────────────────────────────────────────────
  function grid(XLSX, ws) {
    if (!ws || !ws['!ref']) return [];
    // Ancorado em A1: sem isso, uma aba cujo !ref começa em B desloca todos
    // os endereços de origem mostrados ao admin em uma coluna.
    var e = XLSX.utils.decode_range(ws['!ref']).e;
    return XLSX.utils.sheet_to_json(ws, { header: 1, raw: true, defval: null, blankrows: true, range: { s: { r: 0, c: 0 }, e: e } });
  }
  function addr(XLSX, sheet, r, c) { return sheet + '!' + XLSX.utils.encode_cell({ r: r, c: c }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function txt(v) { return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : ''; }

  // 2026 · "2026E" · "2026a" · "2025/26" (→ 2026) · "FY26"
  function yearOf(v) {
    if (isNum(v)) return (Number.isInteger(v) && v >= 2000 && v <= 2045) ? { y: v, label: String(v) } : null;
    var s = txt(v);
    if (!s) return null;
    var m = s.match(/^(20\d\d)\s*\/\s*(\d\d)\s*[AaEe]?$/);
    if (m) return { y: 2000 + Number(m[2]), label: s };
    m = s.match(/^(20\d\d)\s*[AaEe]?$/);
    if (m) return { y: Number(m[1]), label: s };
    m = s.match(/^FY\s*'?(\d\d)\s*[AaEe]?$/i);
    if (m) return { y: 2000 + Number(m[1]), label: s };
    return null;
  }

  // Corridas de ≥3 anos crescentes e consecutivos numa linha = cabeçalho.
  function headerRuns(rows) {
    var runs = [];
    rows.forEach(function (row, r) {
      if (!row) return;
      var cur = null;
      for (var c = 0; c <= row.length; c++) {
        var y = c < row.length ? yearOf(row[c]) : null;
        if (y && cur && y.y === cur.years[cur.years.length - 1].y + 1) { cur.years.push(y); continue; }
        if (cur && cur.years.length >= 3) runs.push(cur);
        cur = y ? { r: r, c0: c, years: [y] } : null;
      }
    });
    return runs;
  }

  // Cabeçalho de uma célula de rótulo: o mais próximo acima cuja corrida
  // começa logo à direita do rótulo (até 3 colunas, p/ colunas de unidade).
  function headerFor(runs, r, c) {
    var best = null;
    runs.forEach(function (h) {
      if (h.r >= r) return;
      if (h.c0 <= c || h.c0 > c + 3) return;
      if (!best || h.r > best.r) best = h;
    });
    return best;
  }

  // ── séries anuais da Output ───────────────────────────────────────
  function seriesCandidates(XLSX, rows, runs, sheet) {
    var out = [];
    rows.forEach(function (row, r) {
      if (!row) return;
      row.forEach(function (v, c) {
        var label = txt(v);
        if (!label || label.length > 60 || yearOf(v)) return;
        var h = headerFor(runs, r, c);
        if (!h) return;
        // "FCFE | 2023 | 2024 | ..." (SOJA3 Output!B30) é cabeçalho, não dado.
        if (runs.some(function (k) { return k.r === r && k.c0 >= h.c0 && k.c0 < h.c0 + h.years.length; })) return;
        // Linha logo acima de "EV": é a dívida líquida que o próprio modelo
        // usa no múltiplo (JBS W40 inclui leasing, a W27 não; SLCE3 tem
        // "Net Debt" e "Net Debt (Inc. Leasing)" e o EV usa a de baixo, a
        // mais colada no EV). evGap = distância até o EV; 0 = longe.
        var evGap = 0;
        var evRow = -1;
        for (var d = 1; d <= 4 && rows[r + d]; d++) {
          if (/^(ev|enterprise value)$/i.test(txt(rows[r + d][c]))) { evGap = d; evRow = r + d; break; }
        }
        var vals = {};
        var n = 0;
        h.years.forEach(function (y, i) {
          var cc = h.c0 + i;
          var val = row[cc];
          if (isNum(val)) {
            vals[y.y] = { v: val, src: addr(XLSX, sheet, r, cc), period: y.label };
            if (YEARS.indexOf(y.y) >= 0) n++;
          }
        });
        // Minoritários entre a dívida líquida e o EV = o modelo soma no EV
        // (JBS W41, MBRF3 O38). Sem essa linha, o EV do modelo não soma
        // (SOJA3: somar os −645 do DCF derrubaria o EV/EBITDA de 5,2x p/ 3,0x).
        var evMin = false;
        for (var m = r + 1; m < evRow; m++) { if (/^min/i.test(txt(rows[m][c]))) evMin = true; }
        if (n) out.push({ label: label, cell: addr(XLSX, sheet, r, c), r: r, c: c, vals: vals, n: n, evGap: evGap, evRow: evRow, evMin: evMin });
      });
    });
    return out;
  }

  // Ordem: mais anos 2026-30 preenchidos > vizinha de "EV" > prioridade da
  // regra > primeira linha. Cobertura vem antes da regra porque a linha
  // "preferida" às vezes só existe no bloco de múltiplos, que pula o ano
  // corrente quando ele já é realizado (AGRO3 2026a).
  function pickSeries(cands, rules) {
    var hits = [];
    rules.forEach(function (rule, i) {
      cands.forEach(function (k, j) {
        if (!rule[0].test(k.label) || (rule[1] && rule[1].test(k.label))) return;
        if (EXCL_RATIO.test(k.label.replace(/\((brl|usd|r\$)[^)]*\)/ig, ''))) return;
        if (hits.some(function (h) { return h.k === k; })) return;
        hits.push({ k: k, rule: i, row: j });
      });
    });
    if (!hits.length) return null;
    hits.sort(function (a, b) {
      var ga = a.k.evGap || 99;
      var gb = b.k.evGap || 99;
      return (b.k.n - a.k.n) || (ga - gb) || (a.rule - b.rule) || (a.row - b.row);
    });
    return hits[0].k;
  }

  // ── Mask-UploadModel ──────────────────────────────────────────────
  function readMask(XLSX, ws) {
    var rows = grid(XLSX, ws);
    if (rows.length < 6) return null;
    var head = rows[0] || [];
    var hv = rows[1] || [];
    var meta = {};
    head.forEach(function (k, c) { if (txt(k)) meta[txt(k)] = { v: hv[c], src: addr(XLSX, 'Mask-UploadModel', 1, c) }; });
    var periodRow = -1;
    for (var r = 2; r < Math.min(rows.length, 8); r++) {
      if (rows[r] && rows[r].filter(function (v) { return yearOf(v); }).length >= 3) { periodRow = r; break; }
    }
    if (periodRow < 0) return { meta: meta, series: {} };
    var periods = rows[periodRow];
    var series = {};
    for (var rr = periodRow + 1; rr < rows.length; rr++) {
      var row = rows[rr];
      if (!row) continue;
      var lab = null;
      var labCol = -1;
      for (var c = 0; c < 4; c++) { if (txt(row[c])) { lab = txt(row[c]); labCol = c; break; } }
      if (!lab) continue;
      var vals = {};
      for (var cc = labCol + 1; cc < periods.length; cc++) {
        var y = yearOf(periods[cc]);
        if (y && isNum(row[cc])) vals[y.y] = { v: row[cc], src: addr(XLSX, 'Mask-UploadModel', rr, cc), period: y.label };
      }
      series[lab.toLowerCase()] = vals;
    }
    return { meta: meta, series: series };
  }

  function maskSeries(mask, metric) {
    if (!mask) return null;
    var labs = MASK_LABELS[metric];
    for (var i = 0; i < labs.length; i++) {
      var s = mask.series[labs[i].toLowerCase()];
      if (s && Object.keys(s).length) return { label: labs[i], vals: s };
    }
    return null;
  }

  // Compara Mask × Output nos anos em comum (≤1,5%). Devolve, por métrica,
  // quantos anos conferiram e quais divergiram.
  function compareMask(ms, os) {
    var r = { checked: 0, bad: [] };
    if (!ms || !os) return r;
    YEARS.concat([2023, 2024, 2025]).forEach(function (y) {
      var a = os.vals[y];
      var b = ms.vals[y];
      if (!a || !b || !a.v) return;
      r.checked++;
      if (Math.abs(b.v - a.v) / Math.abs(a.v) > 0.015) r.bad.push(y);
    });
    return r;
  }

  // A Mask só entra se receita/EBITDA baterem com a Output — prova de que a
  // convenção de ano é a mesma. Depois, cada métrica é checada de novo: a
  // Mask da MBRF3 bate em receita mas a dívida líquida dela inclui leasing,
  // então ela não pode completar 2029 de uma série que a Output define sem.
  function maskAligned(mask, picked) {
    var checked = 0;
    var bad = [];
    ['revenue', 'ebitda'].forEach(function (m) {
      var c = compareMask(maskSeries(mask, m), picked[m]);
      checked += c.checked;
      c.bad.forEach(function (y) { bad.push(m + ' ' + y); });
    });
    return { ok: checked > 0 && !bad.length, checked: checked, bad: bad };
  }

  // ── bloco de valuation ────────────────────────────────────────────
  // Primeiro número à direita do rótulo (pula unidades tipo "BRL m").
  function findValue(XLSX, wb, sheets, include, exclude) {
    for (var s = 0; s < sheets.length; s++) {
      var rows = grid(XLSX, wb.Sheets[sheets[s]]);
      for (var r = 0; r < Math.min(rows.length, 200); r++) {
        var row = rows[r];
        if (!row) continue;
        for (var c = 0; c < row.length; c++) {
          var label = txt(row[c]);
          if (!label || label.length > 70 || !include.test(label) || (exclude && exclude.test(label))) continue;
          for (var k = c + 1; k < Math.min(row.length, c + 6); k++) {
            if (isNum(row[k])) return { v: row[k], src: addr(XLSX, sheets[s], r, k), label: label };
            if (txt(row[k]) && !/^(brl|usd|r\$)|m shares|\/sh|^%$|^x$/i.test(txt(row[k]))) break;
          }
        }
      }
    }
    return null;
  }

  function detectCurrency(XLSX, wb, labels) {
    var probe = labels.join(' ');
    var rows = grid(XLSX, wb.Sheets.Output).slice(0, 8);
    rows.forEach(function (row) { (row || []).forEach(function (v) { probe += ' ' + txt(v); }); });
    if (/\busd\b|us\$/i.test(probe)) return 'USD';
    return 'BRL';
  }

  // ── entrada ───────────────────────────────────────────────────────
  function parseWorkbook(XLSX, wb, fileName) {
    var warnings = [];
    var stem = String(fileName || '').replace(/\.[^.]+$/, '').toUpperCase();

    // Séries da Output
    var outRows = grid(XLSX, wb.Sheets.Output);
    if (!outRows.length) warnings.push('Aba "Output" não encontrada — usando só a Mask-UploadModel.');
    var runs = headerRuns(outRows);
    var cands = seriesCandidates(XLSX, outRows, runs, 'Output');
    var picked = {};
    METRICS.forEach(function (m) { picked[m] = pickSeries(cands, SERIES_RULES[m]); });

    // Mask: valida alinhamento e completa buracos
    var mask = wb.Sheets['Mask-UploadModel'] ? readMask(XLSX, wb.Sheets['Mask-UploadModel']) : null;
    var align = mask ? maskAligned(mask, picked) : null;
    if (mask && !align.ok) {
      warnings.push(align.checked
        ? 'Mask-UploadModel diverge da Output (' + align.bad.join(', ') + ') — ignorada.'
        : 'Mask-UploadModel sem anos em comum com a Output para validar — ignorada.');
    }

    var years = {};
    YEARS.forEach(function (y) { years[y] = { period: null }; });
    METRICS.forEach(function (m) {
      var os = picked[m];
      var ms = (mask && align.ok) ? maskSeries(mask, m) : null;
      var cmp = compareMask(ms, os);
      if (ms && os && (cmp.bad.length || !cmp.checked)) {
        warnings.push(m + ': Mask-UploadModel ' + (cmp.bad.length ? 'diverge da Output em ' + cmp.bad.join(', ')
          : 'sem anos em comum com a Output') + ' — não usada para completar esta série.');
        ms = null;
      }
      YEARS.forEach(function (y) {
        var o = os && os.vals[y];
        var k = ms && ms.vals[y];
        if (o) {
          years[y][m] = { v: o.v, src: o.src, from: os.label };
          years[y].period = years[y].period || o.period;
        } else if (k) {
          years[y][m] = { v: k.v, src: k.src, from: 'Mask: ' + ms.label };
          years[y].period = years[y].period || k.period;
        } else {
          years[y][m] = null;
        }
      });
      if (!os && !ms) warnings.push('Sem série de ' + m + ' (nem Output, nem Mask).');
      else if (!os) warnings.push(m + ' veio inteiro da Mask-UploadModel (' + ms.label + ').');
    });

    // Valuation
    var mm = (mask && mask.meta) || {};
    var shares = findValue(XLSX, wb, VALUATION_SHEETS,
      /^(#\s*(of\s*)?shares|number (of )?shares outstanding|shares outstanding)/i, /^m shares$/i);
    var target = (mm.Target && isNum(mm.Target.v)) ? { v: mm.Target.v, src: mm.Target.src, label: 'Mask: Target' }
      : findValue(XLSX, wb, VALUATION_SHEETS, /^(target price|equity value\s*\/\s*share)/i, /calculation|jbss|bdr/i);
    var price = findValue(XLSX, wb, VALUATION_SHEETS, /^(current (stock )?price|share price|current price\s*\/\s*share)/i);
    var vNetDebt = findValue(XLSX, wb, ['DCF', 'Valuation', 'NAV'], /^(\(-\)\s*|-\s*)?(adj\.\s*)?net debt\b/i, /\/|ebitda/i);
    var vMin = findValue(XLSX, wb, ['DCF', 'Valuation', 'NAV'], /^(\(-\)\s*|-\s*)?minorit/i);

    // Output costuma ter "Shares Outstanding" desatualizado (MDIA3: 113 vs
    // 335,6 pós-desdobramento). O bloco de valuation manda; só avisa.
    var outShares = findValue(XLSX, wb, ['Output'], /^(number (of )?shares outstanding|shares outstanding)/i);
    if (shares && outShares && isNum(outShares.v) && Math.abs(outShares.v / shares.v - 1) > 0.05) {
      warnings.push('# ações diverge entre ' + shares.src + ' (' + shares.v.toFixed(1) + ') e ' + outShares.src +
        ' (' + outShares.v.toFixed(1) + ') — usando ' + shares.src + '.');
    }
    if (!shares) warnings.push('# ações não encontrado — preencha manualmente.');
    if (!target) warnings.push('Preço-alvo não encontrado — preencha manualmente.');

    // Rating é texto (OP/MP/UP): Mask primeiro, senão a célula ao lado de
    // "Recommendation"/"Rating" na Output/Cover (ABEV3 Output!Q9).
    var RECS = { outperform: 'OP', 'market perform': 'MP', underperform: 'UP', op: 'OP', mp: 'MP', up: 'UP' };
    var rec = null;
    if (mm.Recommendation && RECS[txt(mm.Recommendation.v).toLowerCase()]) {
      rec = { v: RECS[txt(mm.Recommendation.v).toLowerCase()], src: mm.Recommendation.src };
    }
    ['Output', 'Cover'].forEach(function (sn) {
      if (rec) return;
      grid(XLSX, wb.Sheets[sn]).slice(0, 40).forEach(function (row, r) {
        (row || []).forEach(function (v, c) {
          if (rec || !/\b(recommendation|rating)\b/i.test(txt(v))) return;
          for (var k = c + 1; k < Math.min(row.length, c + 20); k++) {
            var t = txt(row[k]).toLowerCase();
            if (RECS[t]) { rec = { v: RECS[t], src: addr(XLSX, sn, r, k) }; return; }
          }
        });
      });
    });
    if (!rec) warnings.push('Recomendação não encontrada no modelo — preencha na conferência.');

    var ye = mm.YearEnd && isNum(mm.YearEnd.v) ? mm.YearEnd.v : null;
    var tlab = target && target.label ? target.label.match(/YE\s*'?(\d{2,4})/i) : null;
    var targetLabel = ye ? 'YE' + String(ye).slice(-2) : (tlab ? 'YE' + tlab[1].slice(-2) : null);

    // Cover do template: B12 = nome, B17 = ticker (BEEF3, SOJA3 e VITT3 não
    // têm Mask, só a Cover).
    var cover = grid(XLSX, wb.Sheets.Cover);
    var coverName = txt(((cover[11] || [])[1]));
    var coverTicker = txt(((cover[16] || [])[1]));
    var ticker = (mm.Ticker && txt(mm.Ticker.v)) || (/^[A-Z]{3,5}\d{0,2}$/.test(coverTicker) ? coverTicker : '') || stem;
    var name = (mm['Company Name'] && txt(mm['Company Name'].v)) || coverName || null;
    if (!name) {
      var t2 = outRows.slice(0, 6).map(function (r) { return txt((r || [])[1]); }).filter(function (s) { return /financials$/i.test(s); })[0];
      name = t2 ? t2.replace(/\s*financials$/i, '') : ticker;
    }
    var modelDate = mm.LastUpdate && mm.LastUpdate.v ? mm.LastUpdate.v : null;
    if (modelDate instanceof Date) modelDate = modelDate.toISOString().slice(0, 10);
    else if (isNum(modelDate)) modelDate = new Date(Math.round((modelDate - 25569) * 864e5)).toISOString().slice(0, 10);
    else modelDate = null;

    // Minoritários no EV, por ano. Segue o bloco de múltiplos do modelo:
    //   · linha de minoritários colada no EV → usa a série dela (JBS W41;
    //     MBRF3 O38, National Beef @6,5x: 901 em 26, 1.389 em 27);
    //   · DL colada no EV sem minoritários no meio → o modelo não soma (SOJA3);
    // senão, definição padrão: soma o minoritário do bloco de valuation.
    var nd = picked.net_debt;
    var minRow = cands.filter(function (k) { return /^min/i.test(k.label) && k.evGap && k.evGap <= 2; })[0] || null;
    var minNote;
    YEARS.forEach(function (y) {
      if (minRow && minRow.vals[y]) years[y].minorities = { v: minRow.vals[y].v, src: minRow.vals[y].src, from: minRow.label };
      else if (nd && nd.evGap && !nd.evMin) years[y].minorities = { v: 0, src: null, from: 'modelo não soma minoritários no EV' };
      else if (vMin) years[y].minorities = { v: vMin.v, src: vMin.src, from: vMin.label };
      else years[y].minorities = { v: 0, src: null, from: 'sem minoritários no modelo' };
    });
    if (minRow) minNote = 'série ' + minRow.cell;
    else if (nd && nd.evGap && !nd.evMin) minNote = 'não entram (bloco de múltiplos ' + nd.cell + ' não soma)';
    else minNote = vMin ? vMin.src : 'não encontrados (0)';

    // O Market Cap do modelo às vezes usa outro # de ações (BEEF3 varia por
    // ano; MDIA3 exclui tesouraria). Avisa para o admin decidir.
    var mcRow = cands.filter(function (k) { return /^market cap/i.test(k.label); })[0];
    if (mcRow && price && shares) {
      var mc26 = mcRow.vals[2026] || mcRow.vals[2027];
      if (mc26 && Math.abs(mc26.v / (price.v * shares.v) - 1) > 0.01) {
        warnings.push('Market Cap do modelo (' + mc26.src + ') implica ' + (mc26.v / price.v).toFixed(1) +
          'm ações; estamos usando ' + shares.v.toFixed(1) + 'm de ' + shares.src + '.');
      }
    }

    // Múltiplos que o PRÓPRIO modelo calcula (com o preço dele), para a tela
    // de conferência mostrar lado a lado com o que vamos calcular.
    function modelMultiple(re) {
      var hit = cands.filter(function (k) { return re.test(k.label); })
        .sort(function (a, b) { return b.n - a.n; })[0];
      if (!hit) return null;
      var o = {};
      YEARS.forEach(function (y) { if (hit.vals[y]) o[y] = { v: hit.vals[y].v, src: hit.vals[y].src }; });
      return o;
    }
    var modelMultiples = {
      pe: modelMultiple(/^p\s*\/\s*e\b(?!.*(po|target))/i),
      ev_ebitda: modelMultiple(/^ev\s*\/\s*(adj(usted)?\.?\s*)?ebitda(\s*adj(usted)?\.?)?(\s*\(x\))?$/i),
      fcfe_yield: modelMultiple(/^fcfe yield(\s*\(%\))?$/i),
    };

    var currency = detectCurrency(XLSX, wb, [target && target.label, price && price.label].filter(Boolean));

    return {
      ticker: ticker,
      name: name,
      currency: currency,
      price_symbol: /\d$/.test(ticker) ? ticker + '.SA' : ticker,
      recommendation: rec,
      target_price: target,
      target_label: targetLabel,
      model_price: price,
      shares_mn: shares,
      valuation_net_debt: vNetDebt,
      minorities_note: minNote,
      model_multiples: modelMultiples,
      model_date: modelDate,
      mask_used: !!(mask && align.ok),
      years: years,
      series_labels: METRICS.reduce(function (o, m) { o[m] = picked[m] ? picked[m].cell + ' "' + picked[m].label + '"' : null; return o; }, {}),
      warnings: warnings,
    };
  }

  var api = { parseWorkbook: parseWorkbook, READ_SHEETS: READ_SHEETS, YEARS: YEARS, METRICS: METRICS, yearOf: yearOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CompsParser = api;
})(typeof window !== 'undefined' ? window : this);
