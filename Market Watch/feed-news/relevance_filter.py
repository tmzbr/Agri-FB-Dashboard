from typing import List, Dict

# HIGH RELEVANCE = sinais que fazem um analista buy-side de ER revisar tese /
# estimativas / capex model / volumes / margens. Agrupados por classe de evento.
HIGH_RELEVANCE_TITLE = [
    # ─── CORPORATE ACTIONS (PT) ─────────────────────────────────────────────
    # Earnings & guidance (mudam modelo direto)
    # Nota: "lucro" e "prejuízo" sozinhos pegam artigos macro ("lucro da alta
    # do petróleo", "prejuízo do setor público") — usamos formas específicas
    # que ancoram em corporate earnings.
    "resultado", "resultados trimestrais", "receita líquida",
    "lucro líquido", "lucro operacional", "lucro do trimestre",
    "lucro do ano", "lucro acima", "lucro abaixo", "reporta lucro",
    "lucro de r$", "lucro de us$",
    "prejuízo líquido", "prejuízo operacional", "prejuizo liquido",
    "prejuízo no trimestre", "prejuízo do trimestre",
    "ebitda", "margem ebitda", "margem líquida", "margem operacional",
    "guidance", "revisão de guidance", "corte de guidance", "eleva guidance",
    "revisão de estimativa", "projeção de lucro", "projeção de receita",
    "fato relevante", "comunicado ao mercado",
    # M&A / reorganização
    "aquisição", "aquisicao", "fusão", "fusao", "incorporação",
    "cisão", "spin-off", "compra de", "venda de ativos", "desinvestimento",
    "joint venture", "parceria estratégica", "controle acionário",
    "mudança de controle", "tender offer", "oferta hostil",
    # Alocação de capital / recapitalização
    "ipo", "oferta pública", "follow-on", "oferta primária", "oferta secundária",
    "debêntures", "debentures", "emissão de bonds", "aumento de capital",
    "buyback", "recompra de ações", "dividendo extraordinário", "jcp extraordinário",
    # Crédito / rating / endividamento
    "rebaixamento", "downgrade de rating", "upgrade de rating",
    "perspectiva negativa", "perspectiva positiva",
    "recuperação judicial", "recuperação extrajudicial",
    "reestruturação de dívida", "inadimplência", "default",
    "quebra de covenants", "covenants",
    # Governança / management
    "novo ceo", "novo cfo", "renúncia do ceo", "renuncia do ceo",
    "novo presidente", "troca de presidente", "mudança no conselho",
    # Legal / regulatório enforcement
    "investigação", "cvm", "sec", "cade aprova", "cade reprova",
    "operação policial", "fraude", "escândalo", "multa",
    "auto de infração", "condenação", "tac ",
    "rescinde", "rescisão de contrato",

    # ─── OPERATIONAL EVENTS (PT) ────────────────────────────────────────────
    # Capex / expansão
    "capex", "nova planta", "nova unidade", "nova fábrica",
    "expansão de capacidade", "ampliação de capacidade",
    "greenfield", "brownfield", "moderniza planta",
    "anúncio de investimento", "anuncia investimento", "anuncia aporte",
    "plano de investimento", "programa de investimento", "ciclo de investimento",
    "novo aporte", "aporte de r$", "investirá r$", "vai investir r$",
    "investimento de r$", "investimentos de r$", "plano plurianual",
    # Status de planta / sanitário
    "habilitação de planta", "habilitação da planta", "habilitação frigorífico",
    "aprovação sanitária", "descredenciamento", "suspensão da planta",
    "interdição", "lacrada", "paralisação de planta",
    "recall", "embargo sanitário",
    # Restrições comerciais
    "suspensão de exportação", "suspensão de importação",
    "restrição de exportação", "restrição de importação",
    "embargo comercial", "embargo",
    "tarifa de importação", "imposto de exportação", "cota de exportação",
    "acordo comercial", "mercosul-ue",
    # Acidentes industriais / trabalhistas / cyber
    "incêndio em planta", "incêndio na fábrica", "explosão",
    "acidente industrial", "vazamento",
    "greve", "paralisação dos trabalhadores", "ciberataque", "ransomware",

    # ─── SAFRA / CLIMA / DOENÇA (PT) ────────────────────────────────────────
    # Rationale: qualquer evento que afeta produtividade/oferta primária muda tese
    # de SLC/BrasilAgro/São Martinho/Adecoagro etc., e afeta custo de input dos
    # processadores (JBS/Minerva/M. Dias/Camil). Coberto amplamente.
    # Estimativas / métricas de safra
    "estimativa de safra", "quebra de safra", "safra recorde", "supersafra",
    "perda de safra", "safra frustrada", "safra cheia",
    "produtividade", "produtividade da safra", "produtividade por hectare",
    "rendimento da lavoura", "rendimento por hectare", "rendimento médio",
    "potencial produtivo", "potencial da safra", "potencial do milho",
    "potencial da soja", "potencial produtivo da safra", "perda de produtividade",
    "plantio atrasado", "replantio", "atraso no plantio",
    "área plantada", "area plantada", "área de plantio", "redução de área",
    "lavoura", "lavouras", "condição da lavoura",
    # "cultivo" sozinho pega lifestyle ("cultivo caseiro", "dicas de cultivo") —
    # usar só formas específicas de agricultura comercial.
    "cultivo comercial", "cultivo de soja", "cultivo de milho",
    "cultivo de cana", "cultivo de trigo", "cultivo de algodão",
    "cultivo de café", "cultivo de arroz",
    # Clima (todos os tipos — seco, chuva em excesso, temperatura)
    "seca", "seco", "tempo seco", "clima seco", "tempo firme",
    "falta de chuva", "chuvas irregulares", "chuvas abaixo da média",
    "estiagem", "estresse hídrico", "estresse hidrico", "déficit hídrico",
    "deficit hidrico", "veranico",
    "chuvas intensas", "chuvas excessivas", "chuva intensa", "chuva excessiva",
    "enchente", "inundação", "alagamento", "excesso de chuvas",
    "geada", "geadas", "frio intenso", "onda de frio",
    "onda de calor", "calor extremo", "calor intenso", "temperaturas elevadas",
    "granizo", "ventania", "tornado", "ciclone",
    "la niña", "la nina", "el niño", "el nino", "enso",
    # Pragas / doenças vegetais
    "praga", "pragas", "ferrugem asiática", "ferrugem asiatica", "ferrugem",
    "doença", "doenças", "fungo", "fungos", "cigarrinha", "lagarta",
    "mofo", "mofo branco", "nematoide",
    # Doenças animais
    "gripe aviária", "gripe aviaria", "influenza aviária", "influenza aviaria",
    "h5n1", "h5n", "newcastle",
    "peste suína", "peste suina", "psa", "febre aftosa", "vaca louca",
    "encefalopatia espongiforme", "brucelose", "tuberculose bovina",
    "surto", "foco de", "focos de",

    # ─── SAFRINHA / SAFRA DE CANA (PT) ─────────────────────────────────────
    # Rationale: safrinha = segunda safra de milho (dez-mar) — qualquer sinal
    # de clima, área ou produtividade afeta oferta de milho para ração e etanol.
    # Moagem de cana = driver direto de EBITDA de SMTO3, JALL3, Raízen.
    "safrinha", "supersafrinha", "segunda safra",
    "moagem de cana", "moagem da cana", "safra de cana",
    "inicia moagem", "encerra moagem",
    "etanol de milho",              # corn ethanol structural theme FS/Inpasa/SMTO
    "biorrefinaria",                # corn/soy biorefinery (not petroleum)

    # ─── REGULATÓRIO / POLÍTICA SETORIAL (PT) ───────────────────────────────
    "plano safra", "crédito rural", "subvenção", "subsídio", "subsidio",
    "blend de biodiesel", "mistura obrigatória", "mistura de biodiesel",
    "postergação do blend", "adia aumento do blend", "adiamento do blend",
    "prorroga blend", "prorrogação do blend",
    "b15", "b14", "b13", "b16", "b17", "b20", "cnpe", "anp",
    "e32", "e27", "e25",            # ethanol mandates (% ethanol in gasoline)
    "cbio", "cbios", "renovabio",   # RenovaBio biofuel credit system
    "política de biodiesel", "política do etanol",
    "iof ", "pis/cofins", "mudança tributária", "reforma tributária",
    "tributação do agronegócio", "tributação de jcp", "funrural",
    "eudr",                         # EU Deforestation Regulation (soy market access)
    "moratória da soja", "moratoria da soja",  # soy moratorium (market access signal)

    # ─── CICLO PECUÁRIO / SINAIS DE OFERTA DE PROTEÍNA (PT) ────────────────
    # Rationale: ciclo do boi afeta diretamente receita de JBS/Minerva/MBRF
    # e custo de input dos processadores. Abate de vacas = liquidação = mais
    # oferta curto prazo mas menos longo prazo = mudança de ciclo.
    # Pintos de corte = indicador 40 dias à frente de oferta de frango.
    "ciclo do boi", "ciclo da pecuária", "ciclo pecuário", "ciclo pecu",
    "retenção de fêmeas", "retencao de femeas",
    "retenção de vacas", "retencao de vacas",
    "abate de vacas", "abate de fêmeas", "abate de femeas",
    "abate recorde", "recorde de abate",
    "abate de bovinos", "abate de frangos",
    "pintos de corte",              # poultry supply leading indicator (~40d lead)
    "heifers on feed", "heifer retention", "replacement heifers",
    "cattle on feed", "cattle herd", "cattle inventory", "cattle cycle",
    "packer margin", "packer margins",
    "beef cutout", "beef consumption", "beef production",
    "retail beef", "retail meat sales",
    "fed cattle",

    # ─── COTAS DE EXPORTAÇÃO / ACESSO A MERCADO (PT) ───────────────────────
    # Rationale: cotas da China para carne bovina afetam diretamente volume
    # e preço realizados por JBS/Minerva/MBRF — mudança de cota = revisão
    # imediata de receita estimada.
    "cota de carne", "cotas de carne",
    "cota chinesa", "cotas chinesas",
    "cota de exportação de carne", "cota para carne",
    "salvaguarda à carne", "salvaguarda da carne",
    "salvaguarda chinesa", "salvaguarda anti-dumping", "salvaguarda comercial",
    "abre mercado para carne", "abre mercado para frango",
    "abre mercado para bovinos", "abre mercado para suínos",

    # ─── INSUMOS / ECONOMIA DO PRODUTOR (PT) ────────────────────────────────
    # Rationale: relação de troca fertilizante/grão = squeeze de margem do
    # produtor → downstream para crédito rural, inadimplência, área plantada.
    # DDG = byproduct do etanol de milho → custo de ração para aves/suínos.
    "relação de troca", "relacao de troca",
    "ddg",                          # distillers dried grains — animal feed cost signal
    "processamento de soja", "processamento recorde",
    "esmagamento recorde",          # soy crushing volume (not just margin)
    "combustível de aviação", "querosene de aviação", "querosene sustentável",

    # ─── MERCADOS DE CAPITAIS (PT — gaps identificados) ─────────────────────
    # Rationale: empresas do setor (JBS, Minerva, MBRF, SMTO) captam via bond
    # frequentemente. "Emissão de bonds" já em HIGH mas "reabre mercado com bond"
    # não matchava. Formas usadas na imprensa brasileira.
    "bond de us$", "bond de r$", "emite bond", "capta com bond",
    "reabre mercado com bond", "reabertura de bond",
    "captação de us$", "captacao de us$",
    "recompra de us$", "recompra de r$",

    # ─── MERCADO / PRECIFICAÇÃO (PT) ────────────────────────────────────────
    # Rationale: qualquer movimento de preço de commodity que é INPUT (grão p/
    # processador) ou OUTPUT (safra do produtor) muda margem / receita / tese.
    # Cobertura completa de proteína animal (boi/frango/suíno/ovos) + grãos +
    # softs + laticínios.
    # Proteína animal — substring match exige variantes singular/plural e
    # "do boi/dos bois", "do suíno/dos suínos", etc. Brasileiro usa as 4 formas.
    "preço do boi", "preços do boi", "preços dos bois",
    "preço da arroba", "preços da arroba",
    "preço do gado", "preços do gado", "arroba do boi",
    "preço da vaca", "preço do bezerro",
    "preço do frango", "preços do frango", "preços dos frangos",
    "preço da carne", "preços da carne",
    "preço da carne de frango", "preços da carne de frango",
    "preço da carne bovina", "preços da carne bovina",
    "preço da carne suína", "preço da carne suina",
    "preços da carne suína", "preços da carne suina",
    "preço do suíno", "preço do suino",
    "preços do suíno", "preços do suino",
    "preços dos suínos", "preços dos suinos", "suínos vivos", "suinos vivos",
    "preço do porco", "preços do porco", "preços dos porcos", "preço do pernil",
    "preço dos ovos", "preços dos ovos", "preço do ovo",
    "preços dos bovinos", "preços dos ovinos",
    "cotação do boi", "cotação do frango", "cotação do suíno", "cotação dos ovos",
    "carne de frango", "carne bovina", "carne suína", "carne suina",
    # Grãos / oleaginosas (singular + plural)
    "preço da soja", "preços da soja",
    "preço do farelo de soja", "preço do óleo de soja", "preço do oleo de soja",
    "preço do milho", "preços do milho", "preço do sorgo",
    "preço do trigo", "preços do trigo",
    "preço do arroz", "preços do arroz",
    "preço do feijão", "preço do feijao",
    "preço do algodão", "preço do algodao", "preços do algodão",
    # Softs
    "preço do café", "preços do café", "preço do cafe",
    "preço do açúcar", "preços do açúcar", "preço do acucar",
    "preço do etanol", "preços do etanol",
    "preço do cacau", "preços do cacau", "preço do suco de laranja",
    # Laticínios / bebidas
    "preço do leite", "preços do leite", "preço do queijo", "preço da cerveja",
    # Custos de produção / insumos
    "preço do fertilizante", "preço dos fertilizantes", "preço da ureia",
    "preço do fosfato", "preço do defensivo", "preço do diesel rural",
    # Supply / demand data releases
    "relatório usda", "wasde", "relatório conab", "conab reporta",
    "pesquisa da conab", "boletim conab", "levantamento conab",
    "crop progress", "estoques americanos", "estoques mundiais",
    "estoques brasileiros", "estoques de soja", "estoques de milho",
    "estoques globais", "inventário", "inventario",
    "demanda global", "oferta global", "balance sheet", "balanço de oferta",
    "déficit de oferta", "deficit de oferta", "excesso de oferta",
    "consumo doméstico", "consumo domestico", "consumo mundial",
    # Margens / spreads
    "margem de esmagamento", "crushing spread", "esmagamento de soja",
    "margem da processadora", "margem do frigorífico", "margem do frigorifico",
    "margens pressionadas", "margens comprimidas",
    # Paridades / logística de exportação
    "paridade de exportação", "paridade de exportacao",
    "paridade portuária", "paridade portuaria", "fob santos", "fob paranaguá",
    "preço fob", "preço cif", "premium de exportação", "basis",
    "escoamento da safra", "gargalo logístico", "gargalo logistico",
    "porto de santos", "porto de paranaguá", "porto de paranagua",
    "terminal portuário", "terminal portuario",
    "frete marítimo", "frete maritimo", "frete ferroviário", "frete ferroviario",
    # Pressão de custo / repasse
    "alta de fertilizantes", "custo de produção", "custo de producao",
    "custo da ração", "custo da racao", "repasse de preço", "repasse de preco",
    "poder de barganha", "inflação de alimentos", "inflacao de alimentos",
    # Volumes de exportação / importação
    "exportação de soja", "exportação de milho", "exportação de açúcar",
    "exportação de carne", "embarque de soja", "embarque de milho",
    "embarques brasileiros", "volumes de exportação",
    # Crédito / saúde do produtor (buy-side care sobre default no canal)
    "endividamento do produtor", "inadimplência rural", "inadimplencia rural",
    "renegociação de dívida", "renegociacao de divida", "rolagem de dívida",
    "recuperação judicial de produtor", "default de produtor",
    "crédito rural", "credito rural", "financiamento rural",
    "inadimplência no agronegócio", "inadimplencia no agronegocio",

    # ─── CORPORATE ACTIONS (EN) ─────────────────────────────────────────────
    "earnings", "quarterly results", "annual results", "net income", "net loss",
    "revenue miss", "revenue beat", "guidance cut", "guidance raise",
    "profit warning", "material fact",
    "acquisition", "merger", "takeover", "asset sale", "divestiture",
    "spin-off", "joint venture",
    "secondary offering", "primary offering", "share buyback",
    "bond issuance", "capital raise",
    "ceo resigns", "ceo steps down", "new ceo", "cfo departure", "new cfo",
    "board reshuffle",
    "credit downgrade", "credit upgrade", "debt restructuring", "default",
    "chapter 11", "bankruptcy",
    "investigation", "fraud", "scandal", "antitrust", "class action",

    # ─── OPERATIONAL (EN) ───────────────────────────────────────────────────
    "capex plan", "capex program", "investment plan", "investment program",
    "announces investment", "new plant", "plant expansion", "greenfield",
    "plant shutdown", "plant closure", "facility approval", "facility suspension",
    "recall", "import ban", "export ban", "export restriction", "import restriction",
    "tariff", "quota", "trade deal", "sanction",
    "fire at plant", "industrial accident", "cyberattack", "ransomware", "strike",

    # ─── CROP / CLIMATE / DISEASE (EN) ──────────────────────────────────────
    "crop estimate", "crop condition", "crop forecast", "crop outlook",
    "usda report", "conab report", "wasde", "crop progress",
    "yield", "yield loss", "crop yield", "yield potential", "yield outlook",
    "planted area", "harvest", "planting delay",
    "drought", "dry weather", "dry spell", "lack of rain",
    "excessive rain", "heavy rain", "flooding", "frost", "freeze",
    "heat wave", "heat stress", "cold wave",
    "la nina", "el nino", "enso",
    "disease outbreak", "pest", "rust", "blight",
    "avian flu", "bird flu", "swine flu", "foot and mouth", "asf",
    "newcastle", "mad cow", "bse",

    # ─── REGULATORY / PRICING (EN) ──────────────────────────────────────────
    "biodiesel blend", "biodiesel mandate", "ethanol mandate",
    "subsidized loan", "crop credit",
    "beef price", "cattle price", "chicken price", "poultry price",
    "pork price", "hog price", "egg price", "dairy price", "milk price",
    "soybean price", "corn price", "wheat price", "rice price", "cotton price",
    "coffee price", "sugar price", "ethanol price", "cocoa price",
    "fertilizer price", "urea price", "phosphate price",
    "price falls", "price drops", "price rises", "price surges",
    "supply cut", "production cut", "supply glut", "oversupply",
    "crushing margin", "processor margin", "feed cost",
    "export volume", "import volume", "grain stocks", "ending stocks",
    "port logistics", "freight rate", "shipping cost",
    "farmer debt", "farmer default", "ag credit",
]

HIGH_RELEVANCE_SUMMARY = [
    # Corporate
    "resultado", "ebitda", "margem", "lucro", "receita", "guidance",
    "aquisição", "fusão", "venda", "ipo", "oferta",
    "investimento", "aporte", "capex", "rating", "recuperação judicial",
    "rescisão", "inadimplência", "default",
    # Operational / sanitary / trade
    "embargo", "restrição", "suspensão", "habilitação", "recall",
    "tarifa", "quota", "sanção",
    # Crop / climate / disease / yield
    "seca", "seco", "chuva", "chuvas", "geada", "granizo",
    "safra", "lavoura", "plantio", "colheita", "produtividade", "rendimento",
    "potencial", "quebra", "praga", "ferrugem",
    "gripe aviária", "gripe aviaria", "peste suína", "peste suina",
    "surto", "foco",
    # Pricing / supply-demand
    "preço", "preços", "cotação", "estoques", "oferta", "demanda",
    "exportação", "importação", "embarque", "paridade", "basis",
    "margem de esmagamento", "custo da ração", "repasse",
    # Regulatory
    "blend", "biodiesel", "plano safra", "cnpe", "anp", "mapa",
    "anvisa", "cade",
    # EN
    "earnings", "revenue", "acquisition", "merger", "drought", "rain",
    "tariff", "crop", "yield", "harvest", "planting",
    "usda", "conab", "wasde", "investment",
    "bankruptcy", "downgrade", "strike",
    "price", "prices", "stocks", "supply", "demand", "export", "import",
]

LOW_RELEVANCE_TITLE = [
    # Daily market noise
    "cotação do dia", "preço do dia", "fechamento do mercado", "abertura do mercado",
    "variação do dia", "bolsa hoje", "mercado hoje",
    # Commodity overview / daily session chatter (múltiplas commodities listadas
    # + "operam em alta/baixa/mistos" = recap de sessão, não evento tradable)
    "operam em alta", "operam em baixa", "operam mistos", "operam estáveis",
    "operam estaveis", "sobem na bolsa", "caem na bolsa",
    "opera em alta", "opera em baixa", "opera misto",
    "cai no pregão", "sobe no pregão",
    # Events / appearances
    "em entrevista ao", "em entrevista para", "em palestra", "em evento",
    "participa de congresso", "participa de fórum", "participa de seminário",
    "podcast", "ao vivo:", "série:", "episódio",
    # EN equivalents
    "daily price", "market close", "price today", "commodity prices today",
    "appeared on", "keynote at", "conference appearance", "interview on",
    # Commodity market close recaps — APENAS quando multi-commodity ou genérico.
    # Nota: "fecha em alta" / "fecha em baixa" REMOVIDOS daqui porque em
    # notícias agri quase sempre aparecem como sinal de preço de commodity única
    # (ex.: "Soja fecha em alta em Chicago") — substring match não distingue.
    # Os padrões "operam em alta/baixa/mistos" já cobrem os recaps de sessão.
    # Day trading noise
    "trade do dia", "operação do dia", "de lucro nesta", "oportunidade de trade",
    "sugestão de trade", "trade de hoje", "stock pick do dia",
    "compra para hoje", "venda para hoje",
    # Retail stock picking
    "ação para comprar", "ações para comprar", "melhores ações para",
    "vale a pena comprar", "vale a pena investir",
    "onde investir", "como investir em",
    "para iniciantes", "guia do investidor",
    "carteira de ações", "carteira recomendada", "carteira de dividendos",
    "minha carteira", "top ações",
    # Technical analysis (not useful for fundamental ER)
    "análise técnica", "suporte e resistência", "rompimento de resistência",
    "figura de reversão", "padrão gráfico", "candle de",
    "ifr", "macd", "média móvel",
    # Retail price targets / coverage from non-institutional sources
    "quanto vale", "quanto vai custar", "quanto vai render",
    "subir % em", "cair % em",
    # Fixed income / irrelevant products
    "renda fixa", "tesouro direto", " cdb ", " lci ", " lca ",
    "fundo imobiliário", " fii ", "fiagro",
    # Retail site content patterns
    "vale a pena", "comprar ou vender", "hora de comprar",
    "hora de vender", "devo comprar", "momento de comprar",
    # Branded content / native advertising (disguised propaganda)
    "conteúdo patrocinado", "conteudo patrocinado",
    "conteúdo de marca", "conteudo de marca",
    "branded content", "patrocinado por", "patrocínio de",
    "especial publicitário", "especial publicitario",
    "publieditorial", "publi editorial", "matéria paga", "materia paga",
    "deixa de ser detalhe", "passa a definir",
    "apresenta a solução", "oferece a solução",
    "parceria com apoio de",
    # Corporate PR / prêmios (soft content, sem impacto em tese)
    "great place to work", "gptw",
    "melhor empresa para trabalhar", "melhores empresas para",
    "eleita como", "premiada como", "recebe prêmio", "recebe premio",
    "vence prêmio", "vence premio", "selo de",
    "reconhecida como", "reconhecido como",
    "ranking de empresas", "top employer",
    # Marketing / lançamento de marca (RP sem impacto material)
    "lançamento de embalagem", "nova embalagem", "novo rótulo", "novo rotulo",
    "campanha publicitária", "nova campanha", "filme publicitário",
    "patrocínio esportivo", "patrocina time", "patrocina clube",
    # Projetos sociais / ESG soft (gestão de imagem, raramente tradable)
    "doação de", "doacao de", "ação social", "acao social",
    "voluntariado", "plantio de mudas", "reflorestamento",
    "projeto social", "iniciativa social",
    # Personalidades / perfis (sem impacto operacional)
    "comemora aniversário", "completa anos",
    "conheça a trajetória", "biografia de", "história do empresário",
    "dia a dia do ceo", "rotina do ceo",
    # Cotidiano / dicas de consumo (não é tese de investimento)
    # Nota: "receita com" e "receita de" REMOVIDOS pois "receita" em português
    # também = revenue ("receita com biológicos", "receita de R$ 2 bilhões") —
    # falsos negativos confirmados no dataset histórico. Usar padrões mais
    # específicos de culinária abaixo.
    "confira a receita", "confira as receitas", "receita para fazer",
    "receita de bolo", "receita de frango", "receita de carne",
    "como preparar", "5 formas de", "10 formas de",
    "dicas para", "dicas de", "confira as dicas", "confira dicas",
    "confira receitas", "confira cortes", "confira os cortes",

    # ─── NOVO: LIFESTYLE / CULINÁRIA / HOBBY AGRÍCOLA ───────────────────────
    # Rationale: dicas de preparo, receita, cultivo caseiro, harmonização,
    # dia comemorativo de comida/bebida = conteúdo de consumidor, sem signal
    # pra modelar P&L de JBS/Minerva/M.Dias/Ambev.
    "dicas de corte", "dicas de cortes", "dicas de preparo", "dicas de preparos",
    "cortes preparos acompanhamentos",
    "harmonização de", "harmonizacao de", "harmoniza com", "combina com",
    "qual cerveja combina", "qual vinho combina",
    "gastronomia", "gastronômico", "gastronomico", "gastronomicamente",
    "chef de cozinha", "chef premiado", "restaurante premiado",
    "dia do churrasco: confira", "dia do hambúrguer: confira",
    "dia do café: confira", "dia do vinho: confira",
    "pancs", "plantas alimentícias não convencionais",
    "plantas alimenticias nao convencionais",
    "plantas alimentícias", "plantas alimenticias",
    "horta caseira", "horta urbana", "horta em casa", "horta doméstica",
    "cultivo caseiro", "cultivo em casa", "cultivo em vaso",
    "cultivo de plantas", "dicas para cultivo", "dicas de cultivo",
    "dicas para plantar", "dicas de plantio",
    "dia nacional do", "dia nacional da", "dia mundial do", "dia mundial da",
    "veja dicas", "veja como fazer", "veja como preparar",

    # ─── NOVO: AWARDS / RANKINGS / SOFT PR (expandido) ──────────────────────
    # Rationale: prêmios de concurso/gastronomia/ranking de empresa são media
    # content, não movem tese. Coloca qualquer formato de "X recebe prêmio Y"
    # ou "Brasil leva nota máxima em Z" fora do feed.
    "leva nota máxima", "leva nota maxima", "nota máxima em", "nota maxima em",
    "nota máxima no", "nota máxima do", "nota maxima no", "nota maxima do",
    "prêmio europeu", "premio europeu", "prêmio internacional",
    "premio internacional", "prêmio nacional", "premio nacional",
    "jamais visto na história", "jamais visto na historia",
    "algo jamais visto", "inédito na história", "inedito na historia",
    "medalha de ouro", "medalha de prata", "medalha de bronze",
    "melhor azeite", "melhor vinho", "melhor café", "melhor cafe",
    "melhor cerveja artesanal", "melhor queijo",
    "world's best", "best of the", "best in class",
    "top 10 empresas", "top 100 empresas", "top rankings",
    "concurso de azeite", "concurso de vinho", "concurso de queijo",
    "concurso internacional", "concurso nacional",
    "hall of fame",

    # ─── NOVO: COMMODITIES FORA DA COBERTURA ────────────────────────────────
    # Rationale: cobertura inclui grãos (soja/milho/trigo/arroz/algodão), cana/etanol/
    # açúcar, proteína animal (boi/frango/suíno/ovos), leite, cerveja, insumos.
    # Notícia sobre limão, laranja, citrus, hortaliça, vinho, azeite, pescado,
    # frutas nativas etc. NÃO afeta P&L de nenhuma coberta — noise.
    # (Manter cacau e café em HIGH só porque são commodities globais com impacto
    # macro/FX e algumas coberta tangencialmente os consomem — ex.: Ambev usa
    # café em alguns produtos. Se virar noise recorrente, remover.)
    "preço do limão", "preço do limao", "preços do limão", "preços do limao",
    "preço da laranja", "preços da laranja",
    "citricultura", "citricultor", "limoneira", "laranjeira",
    "preço do tomate", "preços do tomate",
    "preço da cebola", "preço da batata", "preço do alface", "preço da alface",
    "preço do chuchu", "preço do pepino", "preço da cenoura", "preço do repolho",
    "preço da manga", "preço da banana", "preço da uva", "preço da maçã",
    "preço da maca", "preço do morango", "preço do abacate", "preço do mamão",
    "preço do coco",
    "hortaliças", "hortalicas", "hortifrúti", "hortifruti",
    "preços das hortaliças", "preços das hortalicas",
    "fruticultura", "fruticultor", "pomar",
    "azeite", "azeites", "olive oil", "olivicultura", "olivicultor", "oliveira",
    "vinho brasileiro", "vinho do brasil", "vinho fino", "vinícola", "vinicola",
    "vitivinicultura", "enologia",
    "cervejaria artesanal", "microcervejaria", "cerveja artesanal",
    "pescado", "pescados", "aquicultura", "piscicultura", "tilápia",
    "preço do peixe", "preço do camarão",
    "cacau fino", "chocolate fino", "chocolate gourmet",
    "café especial", "cafe especial", "café gourmet", "cafe gourmet",
    "queijo artesanal", "queijo de autor",

    # ─── NOVO: MINERAÇÃO / ENERGIA NUCLEAR / PETRÓLEO / RENOVÁVEIS ─────────
    # Rationale: nenhum dos nomes cobertos opera em mineração, petróleo,
    # gás, nuclear, metais industriais, terras raras, eólica/solar.
    #
    # ATENÇÃO: "petróleo"/"petroleo" REMOVIDOS do LOW porque causavam false
    # negatives confirmados no dataset histórico — artigos relevantes de preço
    # de commodities e biocombustíveis que mencionam o petróleo como referência
    # (ex.: "Açúcar bruto tem menor valor em 5 anos por ampla oferta e quedas
    # do petróleo"). Petróleo como input de custo/referência de preço É relevante
    # para cobertura agri/F&B. Usar formas específicas da indústria:
    "terras raras", "rare earth",
    "minério de ferro", "minerio de ferro", "mineração", "mineracao",
    "minerador", "siderurgia", "siderúrgica", "siderurgica",
    "lítio", " litio ", "cobalto", "nióbio", "niobio",
    "cobre refinado", "cobre catodo",
    "usina nuclear", "energia nuclear", "reator nuclear", "urânio", "uranio",
    "petrolífera", "petrolifera", "petroquímica", "petroquimica",
    "refinaria de petróleo", "refinaria de petroleo",  # petroleum refinery only (not biorrefinaria)
    "exploração de petróleo", "exploracao de petroleo",
    "produção de petróleo", "producao de petroleo",
    "poço de petróleo", "poco de petroleo",
    "pré-sal", "pre-sal",
    "gás natural", "gas natural", "shale gas", "fracking",
    "hidrogênio verde", "hidrogenio verde",
    "energia eólica", "energia eolica", "parque eólico", "parque eolico",
    "energia solar fotovoltaica", "painel solar",
    "defesa militar", "indústria de defesa", "industria de defesa",
    "armamento", "blindados", "mísseis", "misseis",
]

CONTEXT_BOOST = [
    # Rationale: verbos / expressões de fato concreto (hard news) vs.
    # opinião / lifestyle. Inclui movimento de preço / indicadores, para
    # premiar notícias que reportam variação factual (não análise especulativa).
    # Corporate / regulatório
    "anuncia", "reporta", "divulga", "registra", "apresenta", "aprova",
    "suspende", "embarga", "retira", "cancela", "atingiu", "superou",
    "adquire", "incorpora", "arremata", "vende para", "fecha acordo",
    "rebaixa", "eleva", "reduz projeção", "corta guidance",
    "habilita", "descredencia", "abre mercado", "restringe",
    "adia", "posterga", "antecipa", "prorroga", "rescinde",
    "abaixo do esperado", "acima do esperado", "supera expectativas",
    # Movimento de preço / indicador (hard data)
    "caem", "cai", "despenca", "despencam", "desaba", "derrete",
    "sobem", "sobe", "dispara", "disparam", "avança", "avançam",
    "avanca", "avancam",
    "recua", "recuam", "retrocede", "retrocedem",
    "acelera", "aceleram", "desacelera", "desaceleram",
    "aumenta", "aumentam", "diminui", "diminuem", "reduz", "reduzem",
    # Projeções / estimativas / alertas (analítico-factual)
    "projeta", "prevê", "estima", "alerta", "indica", "confirma",
    "revela", "mostra", "sinaliza", "aponta",
    # Safra / clima / oferta (reporta fato)
    "traz alívio", "traz alivio", "agrava", "piora", "melhora",
    "compromete", "prejudica", "beneficia",
    # EN
    "reports", "announces", "discloses", "suspends", "cancels",
    "acquires", "sells", "raises", "cuts", "approves",
    "beat estimates", "missed estimates", "above expectations",
    "postpones", "delays", "downgrades", "upgrades",
    "falls", "drops", "plunges", "sinks", "tumbles",
    "rises", "surges", "soars", "climbs", "jumps",
    "narrows", "widens", "eases", "tightens",
    "hurts", "helps", "boosts", "weighs on",
]

# Substring matched against source name (case-insensitive). First match wins.
SOURCE_WEIGHTS = {
    # Tier 1 — price-moving, institutional quality
    "valor econômico": 0.18,
    "valor economico": 0.18,
    "brazil journal": 0.18,
    "pipeline": 0.15,
    "bloomberg": 0.15,
    "reuters": 0.15,
    "estadão": 0.12,
    "estadao": 0.12,
    # Tier 2 — sector-specific quality (industry bodies, ag-specialized media)
    "agribiz": 0.14,
    "theagribiz": 0.14,
    "beefpoint": 0.12,
    "globo rural": 0.12,
    "broadcast": 0.12,
    "canal rural": 0.10,
    "canalrural": 0.10,
    "summit agro": 0.10,
    "summitagro": 0.10,
    "farmnews": 0.10,
    "farm news": 0.10,
    "abpa": 0.12,
    "abiec": 0.12,
    "abrafrigo": 0.12,
    "g1 agro": 0.08,
    "e-investidor": 0.08,
    "infomoney": 0.06,
    "exame": 0.06,
    "cnn brasil": 0.08,
    "cnnbrasil": 0.08,
    "agfeed": 0.10,
    "scot consult": 0.10,
    "notícias agrícolas": 0.08,
    "noticias agricolas": 0.08,
    # Sources confirmed in 243-day historical dataset (missing from weights):
    "money times": 0.10,    # 359 items in dataset — tier 2 financial/agri
    "moneytimes": 0.10,
    "avisite": 0.12,         # 136 items — specialized Brazil poultry data/analysis
    "beef magazine": 0.10,   # 143 items — US cattle industry, JBS/Tyson/Minerva relevant
    "beefmagazine": 0.10,
    "agrolink": 0.08,        # 169 items — agri generalist, quality reporting
    "agro link": 0.08,
    "neofeed": 0.12,         # quality corporate/M&A journalism
    "neo feed": 0.12,
    "usda": 0.12,            # USDA official reports (WASDE, Livestock Outlook)
    "folha": 0.05,
    "globo": 0.04,
    # Penalized — retail investor / noise sources
    "euqueroinvestir": -0.25,
    "eu quero investir": -0.25,
    "financenews": -0.20,
    "nord invest": -0.20,
    "nord research": -0.20,
    "bahia econôm": -0.20,
    "bahia econom": -0.20,
    "mkt esportivo": -0.25,
    "toro invest": -0.15,
    "empiricus": -0.15,
    "suno": -0.15,
    "rico invest": -0.15,
    "clear invest": -0.12,
    "modalmais": -0.12,
    "genial invest": -0.15,
    "modal invest": -0.12,
    "inter invest": -0.10,
    "nova futura": -0.15,
    "capital research": -0.10,
}


# Hard-blocked sources: items from these outlets are dropped entirely (never
# inserted in fetch, purged via /api/news/purge-blocked). Rationale: local /
# hyperlocal Brazilian outlets with no editorial quality for institutional
# analysis; retail/aggregator financial sites; non-hub foreign (Vietnam, India)
# whose local news doesn't drive global commodity trade.
# Matches substring (case-insensitive) against source name OR URL hostname.
# Add new patterns here as you spot them — do NOT rely on negative scoring
# for these; they need to be eliminated at the source.
BLOCKED_SOURCES = [
    # ── Explicitly cited by user ──
    "brasil 61",
    "arede",
    "rádio guaíba", "radio guaiba",
    "boca do povo",
    "olhar alerta",
    "novidades mt",
    "o presente rural",
    "cenáriomt", "cenario mt", "cenariomt",
    "folha de campo grande",

    # ── Non-hub foreign / auto-translated (noise for global commodity trade) ──
    "vietnam",
    ".vn/", ".vn ",  # URL-based catch for Vietnamese sources
    "rediff",                 # India financial aggregator
    "yahoo finance singapore",

    # ── Hyperlocal / regional Brazilian outlets (sem peso editorial) ──
    "união metropolitana", "uniao metropolitana",
    "sapicuá", "sapicua",
    "dourados news",
    "tudo rondônia", "tudo rondonia",
    "diário do estado", "diario do estado",
    "rádio itatiaia", "radio itatiaia",
    "governo do paraná", "governo do parana",
    "jornal grande bahia",
    "região noroeste", "regiao noroeste",
    "diário do município", "diario do municipio",
    "jornal do vale",
    "radar digital brasília", "radar digital brasilia",
    "o correio news",
    "tnh1",
    "agência gbc", "agencia gbc",
    "primeira página", "primeira pagina",
    "mundial.fm",
    "18horas", "capitalnews.com", "revistarpanews",
    "vgnoticias", "rdmonline",
    "portal lj",
    "só notícia boa", "so noticia boa",
    "terra brasil notícias", "terra brasil noticias",
    "radio frontera", "gzh",  # regional south

    # ── Retail / aggregator financial noise ──
    "whalesbook", "stock titan", "stocktitan",
    "insider monkey", "marketbeat",
    "tradingview",
    "business moment", "businessmoment",

    # ── Generic clickbait / off-topic ──
    "blitz - fears",
    "bol.uol",                # UOL BOL portal, quality issues for our needs

    # ── Verticais fora de cobertura ──
    "hortidaily",             # horticulture
    "zero carbon analytics",  # ESG specialized
    "cpg click",              # petroleum/gas (off-topic setor)
]


# Distinctive phrases that signal branded content / native ads. Matched against
# title+summary — if any hits, the item is auto-excluded on insert (is_excluded=1).
# Keep this list tight: only patterns that almost never appear in real news.
BRANDED_CONTENT_PATTERNS = [
    "conteúdo patrocinado", "conteudo patrocinado",
    "conteúdo de marca", "conteudo de marca",
    "branded content", "publieditorial", "publi editorial",
    "matéria paga", "materia paga", "especial publicitário",
    "patrocinado por",
    "deixa de ser detalhe", "passa a definir",
]

# URL path segments that outlets use exclusively for branded content / native ads.
# Matching any of these in item URL auto-excludes (is_excluded=1).
# Globo Rural uses /conteudo-de-marca/pressworks/; Valor uses /patrocinado/;
# Estadão uses /publicidade/; UOL uses /conteudopatrocinado/; etc.
BRANDED_URL_PATTERNS = [
    "/conteudo-de-marca/", "/conteudo-patrocinado/", "/conteudopatrocinado/",
    "/pressworks/", "/patrocinado/", "/branded/", "/publieditorial/",
    "/especial-publicitario/", "/especial-publicitário/", "/publipost/",
    "/advertorial/", "/publicidade/", "/publi/", "/ads/",
    "/estudio-globo/", "/estudioglobo/", "/studio-folha/",
    "/conteudo-patrocinado", "/marca-apresenta/",
]


def _source_adjustment(source: str) -> float:
    src = source.lower()
    for keyword, weight in SOURCE_WEIGHTS.items():
        if keyword in src:
            return weight
    return 0.0


class RelevanceFilter:
    def __init__(self, config):
        self.config = config
        self.threshold = config.get("relevance", {}).get("score_threshold", 0.4)

    def score(self, news_item: Dict, exclusions: List[Dict] = None) -> float:
        title = (news_item.get("title") or "").lower()
        summary = (news_item.get("summary") or "").lower()
        source = news_item.get("source") or ""

        score = 0.40

        # Learned exclusions from user feedback (strong negative signal)
        if exclusions:
            for excl in exclusions:
                pattern = (excl.get("pattern") or "").lower().strip()
                if len(pattern) >= 4 and pattern in title:
                    score -= 0.35

        # High relevance in title (strong positive)
        for kw in HIGH_RELEVANCE_TITLE:
            if kw in title:
                score += 0.20
                break

        # High relevance in summary (mild positive)
        for kw in HIGH_RELEVANCE_SUMMARY:
            if kw in summary:
                score += 0.07
                break

        # Low relevance in title (negative)
        for kw in LOW_RELEVANCE_TITLE:
            if kw in title:
                score -= 0.25
                break

        # Context boost: article is actively about the entity
        for kw in CONTEXT_BOOST:
            if kw in title or kw in summary:
                score += 0.06
                break

        # Source credibility adjustment
        score += _source_adjustment(source)

        return max(0.0, min(1.0, round(score, 3)))

    def is_relevant(self, score: float) -> bool:
        return score >= self.threshold

    def is_branded_content(self, news_item: Dict) -> bool:
        url = (news_item.get("url") or "").lower()
        if any(p in url for p in BRANDED_URL_PATTERNS):
            return True
        text = ((news_item.get("title") or "") + " " + (news_item.get("summary") or "")).lower()
        return any(p in text for p in BRANDED_CONTENT_PATTERNS)

    def is_blocked_source(self, news_item: Dict) -> bool:
        """True if the item is from a hard-blocked source (skip insert, purge DB)."""
        src = (news_item.get("source") or "").lower()
        url = (news_item.get("url") or "").lower()
        for p in BLOCKED_SOURCES:
            if p in src or p in url:
                return True
        return False
