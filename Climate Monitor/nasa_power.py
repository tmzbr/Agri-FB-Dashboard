#!/usr/bin/env python3
"""
Defesa compartilhada pelos downloaders do NASA POWER (point/daily).

Bug da API medido em 21/10/2026: às vezes a resposta vem com PRECTOTCORR
multiplicado por 24 (o cabeçalho continua dizendo "mm/day"; o T2M vem certo).
Um ×24 é o que sai de somar médias horárias em vez de tirar a média, então o
erro mora na agregação do servidor. É intermitente e preso à URL:
  · 2010→hoje de uma vez veio errado em todas as tentativas do dia; os mesmos
    dias pedidos ano a ano vieram certos (conferido em 6.138 dias, zero
    divergência);
  · uma janela só de 2026 veio errada por ~20 min seguidos e depois certa —
    repetir a MESMA URL não adianta enquanto o servidor serve a resposta ruim.

Por isso:
  windows()  — o pedido normal já sai em [início → 31/12 do ano passado] +
               [ano corrente], que evita o caso 2010→hoje;
  check()    — reconhece a escala errada: chuva diária > 400 mm (acima do
               recorde brasileiro numa célula de ~55 km) ou ano > 6.000 mm;
  fetch()    — janela reprovada no check é repedida ANO A ANO (URLs novas),
               cada ano com algumas tentativas; se ainda assim não passar,
               levanta erro — é melhor o job falhar do que gravar chuva ×24.
"""
import datetime, time

MAX_DAILY_MM = 400
MAX_YEAR_MM = 6000


def windows(y0, end=None):
    """[(start, end), ...] em AAAAMMDD, cobrindo y0-01-01 até end (hoje)."""
    end = end or datetime.date.today()
    if y0 >= end.year:
        return [(f"{y0}0101", f"{end:%Y%m%d}")]
    return [(f"{y0}0101", f"{end.year - 1}1231"), (f"{end.year}0101", f"{end:%Y%m%d}")]


def problem(param):
    """Texto do defeito de escala, ou None se a resposta é plausível."""
    p = param.get("PRECTOTCORR") or {}
    bad = [(d, v) for d, v in p.items() if v is not None and v > MAX_DAILY_MM]
    if bad:
        return f"{len(bad)} dias > {MAX_DAILY_MM} mm (ex.: {bad[0]})"
    anos = {}
    for d, v in p.items():
        if v is not None and v > -900:
            anos[d[:4]] = anos.get(d[:4], 0) + v
    alto = {y: round(s) for y, s in anos.items() if s > MAX_YEAR_MM}
    if alto:
        return f"total anual > {MAX_YEAR_MM} mm em {alto}"
    return None


def check(param, lat, lon):
    msg = problem(param)
    if msg:
        raise ValueError(f"PRECTOTCORR com escala errada em {lat},{lon}: {msg}")
    return param


def fetch(get, lat, lon, y0, end=None, tries=4, pause=20):
    """get(start, end) -> dict de parâmetros (com o próprio retry de rede).
    Devolve {"T2M": {...}, "PRECTOTCORR": {...}} de y0 até end, já conferido."""
    end = end or datetime.date.today()
    out = {"T2M": {}, "PRECTOTCORR": {}}

    def add(par):
        for k in out:
            out[k].update(par[k])

    for a, b in windows(y0, end):
        par = get(a, b)
        if not problem(par):
            add(par); continue
        print(f"    escala errada em {lat},{lon} [{a}-{b}] — repedindo ano a ano", flush=True)
        for y in range(int(a[:4]), int(b[:4]) + 1):
            ya, yb = f"{y}0101", (b if y == int(b[:4]) else f"{y}1231")
            for t in range(tries):
                par = get(ya, yb)
                if not problem(par):
                    break
                time.sleep(pause * (t + 1))
            add(check(par, lat, lon))
    return out
