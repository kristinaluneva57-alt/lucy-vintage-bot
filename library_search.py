"""Бесплатный поиск по библиотеке на русском языке.

Русский запрос превращается в поисковые термины на языках книг
(английский, итальянский, немецкий, французский, чешский, польский,
русский) с помощью встроенного словаря ювелирных терминов.
Латинские слова (Trifari, Coro, bakelite) ищутся как есть.
Поиск идёт по уже распознанному тексту страниц, без внешних API.

Найденные фрагменты показываются в оригинале — это точный текст
страницы после распознавания, а не перевод и не пересказ.
"""

import re
import sqlite3

# Каждая запись: (русские основы слов — все должны быть в запросе,
#                 термины на языках книг — достаточно любого).
# Термины ищутся как начало слова: "spill" найдёт spilla и spille.
# Термин с "=" на конце ищется как целое слово.
GLOSSARY = [
    # Материалы
    (("бакелит",), ["bakelit", "bachelite", "bakélite", "catalin", "бакелит"]),
    (("целлулоид",), ["celluloid", "zelluloid", "celluloïd", "celuloid", "целлулоид"]),
    (("люсит",), ["lucite", "plexiglas", "perspex", "acrylic", "люсит"]),
    (("пластик",), ["plastic", "plastica", "kunststoff", "plastique", "tworzyw", "пластик"]),
    (("пластмасс",), ["plastic", "plastica", "kunststoff", "plastique", "tworzyw", "пластмасс"]),
    (("гагат",), ["jet=", "whitby", "giaietto", "gagat", "jais=", "гагат"]),
    (("джет",), ["jet=", "whitby", "giaietto", "gagat", "jais=", "гагат"]),
    (("чешск", "стекл"), ["czech glass", "bohemian glass", "vetro di boemia", "vetro boemo",
                          "böhmisches glas", "verre de bohême", "české sklo", "českého skla",
                          "czeskie szkło", "szkło czeskie", "gablonz", "jablonec", "jablonz"]),
    (("богемск", "стекл"), ["bohemian glass", "czech glass", "vetro di boemia", "böhmisches glas",
                            "verre de bohême", "české sklo", "czeskie szkło"]),
    (("стекл",), ["glass", "vetro", "glas=", "glasperl", "verre", "sklo", "skla=", "skleně",
                  "szkło", "szkla", "szklan", "стекл"]),
    (("мурано",), ["murano", "мурано"]),
    (("яблонец",), ["jablonec", "jablonz", "gablonz", "яблонец"]),
    (("габлонц",), ["gablonz", "jablonec", "jablonz", "габлонц"]),
    (("страз",), ["rhinestone", "strass", "štras", "cyrkon", "страз"]),
    (("горн", "хрустал"), ["rock crystal", "cristallo di rocca", "bergkristall",
                           "cristal de roche", "křišťál", "kryształ górski", "горный хрусталь"]),
    (("хрустал",), ["crystal", "cristall", "kristall", "cristal", "křišťál", "kryształ", "хрустал"]),
    (("позолот",), ["gilt", "gilding", "gold plat", "gold-plat", "goldplat", "gold filled",
                    "gold-filled", "dorat", "placcat", "vergoldet", "vergoldung", "doré",
                    "plaqué or", "zlacen", "pozłac", "позолот"]),
    (("золочен",), ["gilt", "gilding", "gold plat", "dorat", "vergoldet", "doré", "zlacen",
                    "pozłac", "золочен"]),
    (("золот",), ["gold", "oro=", "zlat", "złot", "золот"]),
    (("серебр",), ["silver", "sterling", "argent", "silber", "stříbr", "srebr", "серебр"]),
    (("латун",), ["brass", "ottone", "messing", "laiton", "mosaz", "mosiądz", "латун"]),
    (("родий",), ["rhodium", "rodio", "rhodiniert", "rhodié", "родий", "родир"]),
    (("родиров",), ["rhodium", "rodio", "rhodiniert", "rhodié", "родий", "родир"]),
    (("эмал",), ["enamel", "smalt", "email=", "émail", "emaill", "emalia", "эмал"]),
    (("гильош",), ["guilloch", "гильош"]),
    (("филигран",), ["filigree", "filigran", "filigrane", "филигран"]),
    (("искусствен", "жемчуг"), ["faux pearl", "simulated pearl", "imitation pearl", "glass pearl",
                                "perle finte", "perle sintetich", "kunstperl", "wachsperl",
                                "fausses perles", "umělé perl", "sztuczne perł",
                                "искусственный жемчуг"]),
    (("жемчуг",), ["pearl", "perla", "perle", "perl=", "perly", "perł", "жемчуг"]),
    (("перламутр",), ["mother of pearl", "mother-of-pearl", "madreperla", "perlmutt", "nacre",
                      "perleť", "masa perłowa", "macica perłowa", "перламутр"]),
    (("янтар",), ["amber", "ambra", "bernstein", "ambre", "jantar", "bursztyn", "янтар"]),
    (("коралл",), ["coral", "corall", "koralle", "corail", "korál", "koral", "коралл"]),
    (("бирюз",), ["turquoise", "turchese", "türkis", "tyrkys", "turkus", "бирюз"]),
    (("марказит",), ["marcasite", "marcasit", "markasit", "markazit", "markasyt", "марказит"]),
    (("гематит",), ["hematite", "haematite", "ematite", "hämatit", "hématite", "hematyt",
                    "hematit", "гематит"]),
    (("гранат",), ["garnet", "granat", "grenat", "гранат"]),
    (("черепах",), ["tortoiseshell", "tortoise shell", "tartaruga", "schildpatt", "écaille",
                    "želvovin", "szylkret", "черепах"]),
    (("слонов", "кост"), ["ivory", "avorio", "elfenbein", "ivoire", "slonovin",
                          "kość słoniowa", "слоновая кость"]),
    (("микромозаик",), ["micromosaic", "micro mosaic", "micro-mosaic", "micromosaico",
                        "mikromosaik", "микромозаик"]),
    (("мозаик",), ["mosaic", "mosaico", "mosaik", "mosaïque", "mozaik", "мозаик"]),
    # Виды украшений и детали
    (("брош",), ["brooch", "spill", "brosch", "broche", "brož", "brosz", "брош"]),
    (("серьг",), ["earring", "ear clip", "orecchin", "ohrring", "ohrclip", "boucle d'oreille",
                  "boucles d'oreilles", "náušnic", "kolczyk", "серьг"]),
    (("клипс",), ["clip-on", "ear clip", "clips", "klips", "orecchini a clip", "ohrclip", "клипс"]),
    (("браслет",), ["bracelet", "bangle", "braccial", "armband", "armreif", "náramek", "náramk",
                    "bransolet", "браслет"]),
    (("колье",), ["necklace", "collier", "collana", "collane", "halskette", "collier",
                  "náhrdelník", "naszyjnik", "колье"]),
    (("ожерель",), ["necklace", "collier", "collana", "halskette", "náhrdelník", "naszyjnik",
                    "ожерель"]),
    (("бус",), ["bead", "beads", "perline", "perlenkette", "korálk", "korale", "бус"]),
    (("кольц",), ["ring=", "rings=", "anell", "fingerring", "bague", "prsten", "pierścion",
                  "pierscion", "кольц"]),
    (("перстен",), ["ring=", "rings=", "anell", "bague", "prsten", "sygnet", "перстен"]),
    (("кулон",), ["pendant", "ciondol", "anhänger", "pendentif", "přívěs", "wisior", "кулон"]),
    (("подвеск",), ["pendant", "ciondol", "anhänger", "pendentif", "přívěs", "wisior", "подвеск"]),
    (("медальон",), ["locket", "medallion", "medaglion", "medaillon", "médaillon", "medailon",
                     "medalion", "медальон"]),
    (("камея",), ["cameo", "cammeo", "camei", "kamee", "camée", "kamej", "kamea", "камея", "камеи"]),
    (("камеи",), ["cameo", "cammeo", "camei", "kamee", "camée", "kamej", "kamea", "камея", "камеи"]),
    (("камей",), ["cameo", "cammeo", "camei", "kamee", "camée", "kamej", "kamea", "камея", "камеи"]),
    (("запонк",), ["cufflink", "cuff link", "gemelli", "manschettenknöpf", "boutons de manchette",
                   "manžetov", "spinki do mankiet", "запонк"]),
    (("диадем",), ["tiara", "diadem", "диадем"]),
    (("тиар",), ["tiara", "diadem", "тиар"]),
    (("застежк",), ["clasp", "fermagl", "chiusur", "verschluss", "fermoir", "zapínání",
                    "zapięc", "застежк", "застёжк"]),
    (("замоч",), ["clasp", "fermagl", "chiusur", "verschluss", "fermoir", "zapięc", "замоч"]),
    (("булавк",), ["pin=", "pins=", "pin back", "pinback", "nadel", "épingle", "jehlic",
                   "szpil", "булавк"]),
    (("шарнир",), ["hinge", "cerniera", "scharnier", "charnière", "kloub", "zawias", "шарнир"]),
    # Клейма, подписи, патенты
    (("клейм",), ["mark", "hallmark", "signed", "signature", "marchio", "marchi=", "punzon",
                  "punze", "stempel", "poinçon", "poincon", "signé", "značk", "punc", "cecha",
                  "sygnatur", "клейм"]),
    (("маркировк",), ["mark", "hallmark", "signed", "marchio", "punzon", "punze", "poinçon",
                      "značk", "punc", "cecha", "маркировк"]),
    (("подпис",), ["signed", "signature", "firmato", "firma=", "signiert", "signé", "signatur",
                   "podpis", "sygnowan", "подпис"]),
    (("патент",), ["patent", "pat. pend", "pat.pend", "pat pend", "des. ", "brevet", "патент"]),
    # Эпохи и стили
    (("ар-деко",), ["art deco", "art déco", "ар-деко", "ар деко"]),
    (("арт-деко",), ["art deco", "art déco", "ар-деко", "ар деко"]),
    (("ардеко",), ["art deco", "art déco", "ар-деко", "ар деко"]),
    (("ар", "деко"), ["art deco", "art déco", "ар-деко", "ар деко"]),
    (("ар-нуво",), ["art nouveau", "jugendstil", "liberty", "secese", "secesj", "ар-нуво", "модерн"]),
    (("нуво",), ["art nouveau", "jugendstil", "liberty", "secese", "secesj", "ар-нуво", "модерн"]),
    (("модерн",), ["art nouveau", "jugendstil", "liberty", "secese", "secesj", "модерн"]),
    (("викториан",), ["victorian", "vittorian", "viktorian", "victorien", "wiktoria", "викториан"]),
    (("эдвардиан",), ["edwardian", "edoardian", "эдвардиан"]),
    (("траур",), ["mourning", "lutto", "trauerschmuck", "trauer", "deuil", "smuteč", "żałob",
                  "траур"]),
    (("бижутер",), ["costume jewel", "bijoux", "bigiotteria", "modeschmuck", "bijouterie",
                    "bižuteri", "sztuczna biżuteria", "biżuteria sztuczna", "бижутер"]),
    (("ювелир",), ["jewel", "gioiell", "schmuck", "bijou", "šperk", "biżuter", "ювелир"]),
    (("украшен",), ["jewel", "gioiell", "schmuck", "bijou", "šperk", "biżuter", "украшен"]),
]

# Слишком общие понятия: помогают ранжировать, но сами по себе
# не делают страницу подходящей, если в запросе есть что-то конкретнее.
GENERIC_STEMS = {"ювелир", "украшен", "бижутер"}

# Русские написания известных марок → латиница.
BRANDS = {
    "трифари": "trifari",
    "коро": "coro",
    "напье": "napier",
    "напьер": "napier",
    "моне": "monet",
    "ковентри": "coventry",
    "хаскелл": "haskell",
    "хаскел": "haskell",
    "айзенберг": "eisenberg",
    "эйзенберг": "eisenberg",
    "сваровски": "swarovski",
    "шанель": "chanel",
    "диор": "dior",
    "скиапарелли": "schiaparelli",
    "шиапарелли": "schiaparelli",
    "вайс": "weiss",
    "ланвен": "lanvin",
    "живанши": "givenchy",
    "лизнер": "lisner",
    "регенси": "regency",
    "хобе": "hobe",
    "мазер": "mazer",
    "мейзер": "mazer",
}

# Короткие служебные слова, которые не нужно переводить.
STOP_WORDS = {
    "что", "такое", "как", "какой", "какая", "какие", "каких", "про", "для", "или",
    "это", "эта", "этот", "эти", "где", "когда", "кто", "чем", "чего", "есть",
    "найди", "найти", "ищу", "покажи", "расскажи", "нужно", "нужна", "нужен",
    "информация", "информацию", "книга", "книге", "книги", "книгах", "всё", "все",
    "вся", "весь", "мне", "меня", "по", "на", "из", "от", "до", "без", "под",
    "над", "при", "the", "and", "for", "with", "von", "und", "der", "die", "das",
    "del", "della", "di", "le", "la", "les", "des",
}

LANGUAGE_NAMES = {
    "eng": "английский", "en": "английский",
    "ita": "итальянский", "it": "итальянский",
    "deu": "немецкий", "de": "немецкий",
    "fra": "французский", "fr": "французский",
    "ces": "чешский", "cs": "чешский",
    "pol": "польский", "pl": "польский",
    "rus": "русский", "ru": "русский",
}

MAX_CANDIDATES = 3000
MAX_RESULTS = 3
LETTER = r"[^\W\d_]"


class SearchPlan:
    def __init__(self):
        # Группы: (подпись для пользователя, [термины], вес).
        self.groups = []
        self.unknown_words = []

    @property
    def all_terms(self):
        seen = []
        for _, terms, _ in self.groups:
            for term in terms:
                if term not in seen:
                    seen.append(term)
        return seen


def _normalize(text):
    return text.lower().replace("ё", "е")


def _tokens(text):
    return re.findall(r"[\w'’\-.]+", _normalize(text))


def build_search_plan(query):
    """Разбирает запрос и строит группы поисковых терминов."""
    plan = SearchPlan()
    tokens = [t.strip(".-'’") for t in _tokens(query)]
    tokens = [t for t in tokens if t]

    cyrillic = [t for t in tokens if re.search("[а-я]", t)]
    latin = [t for t in tokens if not re.search("[а-я]", t)]

    used = set()

    # Сначала многословные понятия («чешское стекло»), потом простые.
    for stems, terms in sorted(GLOSSARY, key=lambda item: -len(item[0])):
        positions = []
        for stem in stems:
            match = next(
                (
                    i for i, token in enumerate(cyrillic)
                    if i not in used and token.startswith(stem)
                ),
                None,
            )
            if match is None:
                break
            positions.append(match)
        else:
            used.update(positions)
            label = " ".join(cyrillic[i] for i in sorted(positions))
            weight = 0.3 if stems[0] in GENERIC_STEMS else 1.0
            plan.groups.append((label, list(terms), weight))

    for i, token in enumerate(cyrillic):
        if i in used:
            continue
        brand = BRANDS.get(token)
        if brand:
            plan.groups.append((token, [brand], 1.5))
            used.add(i)
        elif token not in STOP_WORDS and len(token) >= 3:
            plan.unknown_words.append(token)

    for token in latin:
        if token in STOP_WORDS or len(token) < 2 or token.isdigit():
            continue
        plan.groups.append((token, [token], 1.5))

    return plan


def _term_regex(term):
    exact = term.endswith("=")
    term = term.rstrip("=")
    pattern = rf"(?<!{LETTER})" + re.escape(term).replace(r"\ ", r"\s+")
    if exact:
        pattern += rf"(?!{LETTER})"
    return re.compile(pattern, re.IGNORECASE)


def _like_value(term):
    return "%" + term.rstrip("=").replace("%", "").replace("_", "") + "%"


def search_pages(db_path, plan, limit=MAX_RESULTS):
    """Ищет страницы по плану. Возвращает список результатов по убыванию
    релевантности: dict(title, language, pdf_page, printed_page, text,
    matched=[термины], position)."""
    terms = plan.all_terms
    if not terms:
        return []

    where = " OR ".join(
        "COALESCE(p.cleaned_text, p.original_text) LIKE ?" for _ in terms
    )
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT d.title, d.language, p.pdf_page, p.printed_page,
                   COALESCE(p.cleaned_text, p.original_text)
            FROM library_pages p
            JOIN library_documents d ON d.id = p.document_id
            WHERE COALESCE(d.storage_key, '') LIKE 'books/%'
              AND ({where})
            ORDER BY d.id, p.pdf_page
            LIMIT {MAX_CANDIDATES}
            """,
            [_like_value(term) for term in terms],
        ).fetchall()
    finally:
        conn.close()

    regexes = [
        (weight, [(term.rstrip("="), _term_regex(term)) for term in group_terms])
        for _, group_terms, weight in plan.groups
    ]
    has_specific = any(weight >= 1 for weight, _ in regexes)

    scored = []
    for title, language, pdf_page, printed_page, text in rows:
        if not text:
            continue
        matched = []
        first_position = None
        position_weight = -1.0
        groups_hit = 0
        score = 0.0
        specific_hit = False
        for weight, group in regexes:
            hit = False
            for term, regex in group:
                found = regex.search(text)
                if found:
                    hit = True
                    if term not in matched:
                        matched.append(term)
                    # Фрагмент строим вокруг самого конкретного совпадения.
                    if weight > position_weight:
                        first_position = found.start()
                        position_weight = weight
            if hit:
                groups_hit += 1
                score += weight
                specific_hit = specific_hit or weight >= 1
        if groups_hit and (specific_hit or not has_specific):
            scored.append((
                -score,
                {
                    "title": title,
                    "language": language,
                    "pdf_page": pdf_page,
                    "printed_page": printed_page,
                    "text": text,
                    "matched": matched,
                    "position": first_position or 0,
                    "groups_hit": groups_hit,
                },
            ))

    scored.sort(key=lambda item: item[0])
    return [result for _, result in scored[:limit]]


def excerpt(text, position, before=250, after=450):
    start = max(0, position - before)
    end = min(len(text), position + after)
    fragment = text[start:end].strip()
    if start > 0:
        fragment = "…" + fragment
    if end < len(text):
        fragment += "…"
    return fragment


def language_name(code):
    return LANGUAGE_NAMES.get((code or "").lower(), code or "язык не указан")
