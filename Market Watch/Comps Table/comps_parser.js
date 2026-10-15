/* ═══════════════════════════════════════════════════════════════════
   Comps Table — parser dos modelos Excel de cobertura
   ═══════════════════════════════════════════════════════════════════
   Roda no browser (SheetJS) e no Node (testes). Não grava nada: devolve o
   que SERIA armazenado, com a célula de origem de cada número, para o admin
   conferir antes de confirmar.

   Só duas abas, por decisão do Bruno:
     · Output    — séries anuais (P&L, fluxo de caixa, DL) e os múltiplos que
                   o próprio modelo calcula (só para conferência);
     · Valuation — ou DCF, quando o modelo chama assim: preço-alvo, preço do
                   modelo, # ações, moeda e horizonte do TP.
   Nada de Mask-UploadModel nem Cover.

   Cada série é guardada como LINHA + COLUNA POR ANO da Output. As colunas
   dos anos são sempre contíguas (FY27 = coluna do FY26 + 1), então o admin
   corrige uma série trocando a linha ou a coluna de um único ano — os outros
   anos andam junto (seriesAt + shiftCols).

   Detecção automática (ponto de partida que o admin confere):
   rótulo → cabeçalho de anos mais próximo ACIMA, com ano na coluna logo à
   direita do rótulo. Uma mesma linha pode ser dado no bloco da esquerda e
   cabeçalho no da direita (SLCE3 linha 36), então cabeçalho é por coluna.

   Ano fiscal: "2025/26" vira FY26 (ano de término).
   ═══════════════════════════════════════════════════════════════════ */
(function (root) {
  'use strict';

  var YEARS = [2026, 2027, 2028, 2029, 2030];
  var METRICS = ['revenue', 'ebitda', 'net_income', 'fcfe', 'net_debt', 'minorities'];
  var OUT = 'Output';
  var READ_SHEETS = ['Output', 'Valuation', 'DCF'];

  // Prioridade importa. Ordem final: mais anos 2026-30 preenchidos > linha
  // colada no "EV" > prioridade da regra > primeira linha.
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
  function colIdx(letters) {
    var s = String(letters || '').trim().toUpperCase();
    if (!/^[A-Z]{1,3}$/.test(s)) return -1;
    var n = 0;
    for (var i = 0; i < s.length; i++) n = n * 26 + s.charCodeAt(i) - 64;
    return n - 1;
  }

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
  function seriesCandidates(rows, runs) {
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
        // Linha logo acima de "EV": é a DL que o próprio modelo usa no
        // múltiplo (JBS W40 inclui leasing, a W27 não; SLCE3 usa a "Inc.
        // Leasing", a mais colada no EV). evGap = distância até o EV; 0 = longe.
        var evGap = 0;
        var evRow = -1;
        for (var d = 1; d <= 4 && rows[r + d]; d++) {
          if (/^(ev|enterprise value)$/i.test(txt(rows[r + d][c]))) { evGap = d; evRow = r + d; break; }
        }
        // Minoritários entre a DL e o EV = o modelo soma no EV (JBS W41).
        var evMin = false;
        for (var m = r + 1; m < evRow; m++) { if (/^min/i.test(txt(rows[m][c]))) evMin = true; }
        var cols = {};
        var n = 0;
        h.years.forEach(function (y, i) {
          if (YEARS.indexOf(y.y) < 0) return;
          cols[y.y] = h.c0 + i;
          if (isNum(row[h.c0 + i])) n++;
        });
        // Anos fora do cabeçalho (ex.: Output até 2028) continuam a sequência.
        var anchor = YEARS.filter(function (y) { return cols[y] != null; })[0];
        if (anchor != null) cols = shiftCols(anchor, cols[anchor]);
        if (n) out.push({ label: label, r: r, c: c, cols: cols, n: n, evGap: evGap, evRow: evRow, evMin: evMin });
      });
    });
    return out;
  }

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

  // Colunas contíguas a partir de um ano âncora: FY(y) = col + (y − ano).
  function shiftCols(year, col) {
    var o = {};
    YEARS.forEach(function (y) { var c = col + (y - year); if (c >= 0) o[y] = c; });
    return o;
  }

  // Lê uma série dada a linha (0-based) e as colunas por ano. Para cada ano
  // devolve também o rótulo do cabeçalho de anos acima daquela coluna, para o
  // admin ver se "FY27" está mesmo caindo embaixo de "2027e".
  function seriesAt(XLSX, wb, r, cols) {
    var rows = grid(XLSX, wb.Sheets[OUT]);
    var row = rows[r] || [];
    var runs = headerRuns(rows);
    var minC = Math.min.apply(null, YEARS.map(function (y) { return cols[y] != null ? cols[y] : 1e9; }));
    var label = '';
    for (var c = Math.min(minC, row.length) - 1; c >= 0; c--) {
      // Rótulo = texto de verdade; pula "-" dos anos históricos e unidades.
      if (/[a-z]{2}/i.test(txt(row[c])) && !yearOf(row[c]) && !/^(brl|usd|r\$)\b/i.test(txt(row[c]))) { label = txt(row[c]); break; }
    }
    var out = { label: label, row: r, years: {} };
    YEARS.forEach(function (y) {
      var cc = cols[y];
      if (cc == null || cc < 0) { out.years[y] = null; return; }
      var head = null;
      runs.forEach(function (k) {
        if (k.r < r && cc >= k.c0 && cc < k.c0 + k.years.length && (!head || k.r > head.r)) head = k;
      });
      var hy = head ? head.years[cc - head.c0] : null;
      var v = row[cc];
      // Só lê se o cabeçalho acima da coluna for do MESMO ano: coluna sem
      // cabeçalho (além do último ano da Output) ou de outro ano (bloco
      // vizinho, SOJA3 FY30 cairia no "2023" do balanço) não é desta série.
      out.years[y] = {
        v: hy && hy.y === y && isNum(v) ? v : null,
        noHeader: !hy,
        src: addr(XLSX, OUT, r, cc),
        col: XLSX.utils.encode_col(cc),
        header: hy ? hy.label : null,
        mismatch: !!(hy && hy.y !== y),
      };
    });
    return out;
  }

  // ── aba de valuation ──────────────────────────────────────────────
  function valuationSheet(wb) {
    return wb.Sheets.Valuation ? 'Valuation' : (wb.Sheets.DCF ? 'DCF' : null);
  }

  // Primeiro número à direita do rótulo (pula unidades tipo "BRL m").
  function findValue(XLSX, wb, sheet, include, exclude) {
    var rows = grid(XLSX, wb.Sheets[sheet]);
    for (var r = 0; r < Math.min(rows.length, 200); r++) {
      var row = rows[r];
      if (!row) continue;
      for (var c = 0; c < row.length; c++) {
        var label = txt(row[c]);
        if (!label || label.length > 70 || !include.test(label) || (exclude && exclude.test(label))) continue;
        for (var k = c + 1; k < Math.min(row.length, c + 6); k++) {
          if (isNum(row[k])) return { v: row[k], src: addr(XLSX, sheet, r, k), label: label };
          if (txt(row[k]) && !/^(brl|usd|r\$)|m shares|\/sh|^%$|^x$/i.test(txt(row[k]))) break;
        }
      }
    }
    return null;
  }

  // Número de uma célula da aba de valuation ("E29", "$E$29" ou "DCF!E29").
  function readValuationCell(XLSX, wb, ref) {
    var sheet = valuationSheet(wb);
    var m = String(ref || '').trim().match(/^(?:'?[^'!]+'?!)?\$?([A-Z]{1,3})\$?(\d+)$/i);
    if (!sheet) return { error: 'Modelo sem aba Valuation/DCF' };
    if (!m) return { error: 'Use a célula da aba ' + sheet + ', ex.: E29' };
    var r = Number(m[2]) - 1;
    var c = colIdx(m[1]);
    var v = (grid(XLSX, wb.Sheets[sheet])[r] || [])[c];
    var src = addr(XLSX, sheet, r, c);
    if (!isNum(v)) return { error: src + ' não é número (' + (txt(v) || 'vazia') + ')', src: src };
    return { v: v, src: src };
  }

  // ── entrada ───────────────────────────────────────────────────────
  function parseWorkbook(XLSX, wb, fileName) {
    var warnings = [];
    var stem = String(fileName || '').replace(/\.[^.]+$/, '').trim().toUpperCase();
    var vs = valuationSheet(wb);
    if (!wb.Sheets[OUT]) warnings.push('Aba "Output" não encontrada.');
    if (!vs) warnings.push('Aba "Valuation"/"DCF" não encontrada.');

    var outRows = grid(XLSX, wb.Sheets[OUT]);
    var runs = headerRuns(outRows);
    var cands = seriesCandidates(outRows, runs);

    var series = {};
    ['revenue', 'ebitda', 'net_income', 'fcfe', 'net_debt'].forEach(function (m) {
      var k = pickSeries(cands, SERIES_RULES[m]);
      series[m] = k ? { row: k.r, cols: k.cols } : null;
      if (!k) warnings.push('Série de ' + m + ' não encontrada na Output — aponte a linha.');
    });

    // Minoritários no EV: a linha de minoritários colada no EV do bloco de
    // múltiplos (JBS W41; MBRF3 O38, National Beef @6,5x, muda todo ano).
    // Sem ela o modelo não soma minoritários no EV (SOJA3) → fica vazio (= 0).
    var minRow = cands.filter(function (k) { return /^min/i.test(k.label) && k.evGap && k.evGap <= 2; })[0];
    series.minorities = minRow ? { row: minRow.r, cols: minRow.cols } : null;

    // Valuation
    var shares = vs && findValue(XLSX, wb, vs, /^(#\s*(of\s*)?shares|number (of )?shares outstanding|shares outstanding)/i, /^m shares$/i);
    var target = vs && findValue(XLSX, wb, vs, /^(target price|equity value\s*\/\s*share|npv\s*\/\s*sh)/i, /calculation|jbss|bdr/i);
    var price = vs && findValue(XLSX, wb, vs, /^(current (stock )?price|share price|current price\s*\/\s*share)/i);
    if (!shares) warnings.push('# ações não encontrado na aba ' + vs + ' — aponte a célula.');
    if (!target) warnings.push('Preço-alvo não encontrado na aba ' + vs + ' — aponte a célula.');

    // Moeda e horizonte: dos rótulos da aba de valuation ("Target Price
    // (USD)", "Target Price (YE27)", "Equity Value (YE 2027)").
    var vtext = grid(XLSX, wb.Sheets[vs]).slice(0, 80).map(function (r) { return (r || []).map(txt).join(' '); }).join(' ');
    var currency = /\busd\b/i.test(((target && target.label) || '') + ' ' + ((price && price.label) || '')) ? 'USD'
      : (/\busd\b/i.test(vtext) && !/\bbrl\b|r\$/i.test(vtext) ? 'USD' : 'BRL');
    var ye = ((target && target.label) || '').match(/YE\s*'?(\d{2,4})/i) || vtext.match(/(?:target|equity value)[^|]{0,40}YE\s*'?(\d{2,4})/i);
    if (!ye) {
      // ABEV3 só traz o horizonte na Output ("Target Price YE27 (BRL)").
      var tOut = outRows.slice(0, 15).map(function (r) { return (r || []).map(txt).join(' '); }).join(' ');
      ye = tOut.match(/target price[^|]{0,10}YE\s*'?(\d{2,4})/i);
    }
    var targetLabel = ye ? 'YE' + ye[1].slice(-2) : null;

    // Rating (texto OP/MP/UP) ao lado de "Recommendation"/"Rating", na
    // Output ou na Valuation. A maioria dos modelos não tem → admin escolhe.
    var RECS = { outperform: 'OP', 'market perform': 'MP', underperform: 'UP', op: 'OP', mp: 'MP', up: 'UP' };
    var rec = null;
    [OUT, vs].forEach(function (sn) {
      if (rec || !sn) return;
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

    // Nome: título da Output ("Minerva Financials"); ticker: nome do arquivo.
    var title = outRows.slice(0, 6).map(function (r) { return txt((r || [])[1]); }).filter(function (s) { return /financials$/i.test(s); })[0];
    var name = title ? title.replace(/\s*financials$/i, '') : '';

    // Múltiplos que o PRÓPRIO modelo calcula, para conferência.
    function modelMultiple(re) {
      var hit = cands.filter(function (k) { return re.test(k.label); }).sort(function (a, b) { return b.n - a.n; })[0];
      if (!hit) return null;
      var s = seriesAt(XLSX, wb, hit.r, hit.cols);
      var o = {};
      YEARS.forEach(function (y) { var c = s.years[y]; if (c && c.v != null) o[y] = { v: c.v, src: c.src }; });
      return o;
    }

    return {
      ticker: stem,
      name: name,
      currency: currency,
      price_symbol: /\d$/.test(stem) ? stem + '.SA' : stem,
      recommendation: rec,
      target_price: target,
      target_label: targetLabel,
      model_price: price,
      shares_mn: shares,
      valuation_sheet: vs,
      series: series,
      model_multiples: {
        pe: modelMultiple(/^p\s*\/\s*e\b(?!.*(po|target))/i),
        ev_ebitda: modelMultiple(/^ev\s*\/\s*(adj(usted)?\.?\s*)?ebitda(\s*adj(usted)?\.?)?(\s*\(x\))?$/i),
        fcfe_yield: modelMultiple(/^fcfe yield(\s*\(%\))?$/i),
      },
      warnings: warnings,
    };
  }

  var api = {
    parseWorkbook: parseWorkbook, seriesAt: seriesAt, shiftCols: shiftCols, colIdx: colIdx,
    readValuationCell: readValuationCell, READ_SHEETS: READ_SHEETS, YEARS: YEARS, METRICS: METRICS, yearOf: yearOf,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CompsParser = api;
})(typeof window !== 'undefined' ? window : this);
