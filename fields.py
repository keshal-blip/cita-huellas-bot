"""Upfront answers for the toma de huellas form, and label matching."""

from __future__ import annotations

import re
import unicodedata

REQUIRED = (
    "province",
    "office",
    "doc_type",
    "doc_number",
    "full_name",
    "nationality",
    "birth_year",
    "phone",
    "email",
)

PROMPTS = {
    "province": (
        "Провинция, где будешь делать huellas.\n"
        "Например: Madrid, Barcelona, Valencia."
    ),
    "office": (
        "Комиссария. Нажми «Любая» или пришли номер из списка.\n"
        "Любая — бот возьмёт ту, где раньше всего есть место."
    ),
    "tramite": "На сайте несколько trámites про huellas. Пришли номер нужного.",
    "doc_type": "Документ: NIE, паспорт или DNI.",
    "doc_number": "Номер документа без пробелов. Для NIE это вид Y1234567X.",
    "full_name": "Имя и фамилия полностью, как в документе.",
    "nationality": (
        "Гражданство. Можно по-русски или как на сайте по-испански: "
        "Ucrania, Rusia, Marruecos, Colombia."
    ),
    "birth_year": "Год рождения, 4 цифры. Впишу, только если сайт спросит.",
    "phone": "Телефон для cita: 9 цифр, без +34.",
    "email": "Почта. Её нужно ввести дважды — второй раз повторю сама.",
}

PROVINCE_ALIASES = {
    "мадрид": "madrid",
    "барселона": "barcelona",
    "валенсия": "valencia",
    "аликанте": "alicante",
    "малага": "malaga",
    "севилья": "sevilla",
    "севилия": "sevilla",
    "бильбао": "bizkaia",
    "вискайя": "bizkaia",
    "бискайя": "bizkaia",
    "жираона": "girona",
    "герона": "girona",
    "таррагона": "tarragona",
    "сарагоса": "zaragoza",
    "мурсия": "murcia",
    "пальма": "balears",
    "балеары": "balears",
    "майорка": "balears",
    "лас пальмас": "las palmas",
    "тенерифе": "tenerife",
    "кадис": "cadiz",
    "гранада": "granada",
    "кордова": "cordoba",
    "кордоба": "cordoba",
    "астурия": "asturias",
    "наварра": "navarra",
    "алава": "araba",
    "гипускоа": "gipuzkoa",
    "ла корунья": "coruna",
    "корунья": "coruna",
}

COUNTRY_ALIASES = {
    "украина": "ucrania",
    "ukraine": "ucrania",
    "россия": "rusia",
    "рф": "rusia",
    "russia": "rusia",
    "беларусь": "bielorrusia",
    "белоруссия": "bielorrusia",
    "казахстан": "kazajstan",
    "узбекистан": "uzbekistan",
    "кыргызстан": "kirguistan",
    "киргизия": "kirguistan",
    "таджикистан": "tayikistan",
    "туркменистан": "turkmenistan",
    "азербайджан": "azerbaiyan",
    "армения": "armenia",
    "грузия": "georgia",
    "молдова": "moldavia",
    "молдавия": "moldavia",
    "турция": "turquia",
    "марокко": "marruecos",
    "алжир": "argelia",
    "тунис": "tunez",
    "египет": "egipto",
    "китай": "china",
    "индия": "india",
    "пакистан": "pakistan",
    "бангладеш": "banglades",
    "колумбия": "colombia",
    "перу": "peru",
    "эквадор": "ecuador",
    "боливия": "bolivia",
    "чили": "chile",
    "аргентина": "argentina",
    "венесуэла": "venezuela",
    "бразилия": "brasil",
    "мексика": "mexico",
    "куба": "cuba",
    "доминикана": "dominicana",
    "гондурас": "honduras",
    "парагвай": "paraguay",
    "уругвай": "uruguay",
    "нигерия": "nigeria",
    "сенегал": "senegal",
    "румыния": "rumania",
    "польша": "polonia",
    "германия": "alemania",
    "франция": "francia",
    "италия": "italia",
    "португалия": "portugal",
    "великобритания": "reino unido",
    "англия": "reino unido",
    "сша": "estados unidos",
    "соединенные штаты": "estados unidos",
}

LABELS = {
    "провинция": "province",
    "комиссария": "office",
    "офис": "office",
    "трамит": "tramite",
    "tramite": "tramite",
    "документ": "doc_type",
    "тип документа": "doc_type",
    "номер": "doc_number",
    "имя": "full_name",
    "имя и фамилия": "full_name",
    "фио": "full_name",
    "гражданство": "nationality",
    "национальность": "nationality",
    "год рождения": "birth_year",
    "год": "birth_year",
    "телефон": "phone",
    "почта": "email",
    "email": "email",
}

ANY_OFFICE = {
    "любая",
    "любой",
    "любое",
    "cualquier",
    "any",
    "все",
    "без разницы",
    "0",
}


def norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("@", "a").casefold()
    text = text.replace("ё", "е")
    return " ".join(text.split())


def _alias(table: dict[str, str], query: str) -> str:
    key = norm(query)
    folded = {norm(k): v for k, v in table.items()}
    return folded.get(key, query)


def has_phrase(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def is_placeholder(label: str, value: str) -> bool:
    name = norm(label)
    if "cualquier" in name:
        return False
    if any(word in name for word in ("selecc", "elija", "escoge", "escoja", "elige")):
        return True
    return value.strip() in {"", "0", "-1"}


def match_label(query: str, options: list[dict], *, aliases: dict[str, str] | None = None) -> dict | None:
    expanded = _alias(aliases, query) if aliases else query
    wanted = norm(expanded)
    real = [item for item in options if not is_placeholder(item.get("label", ""), item.get("value", ""))]
    exact = [item for item in real if norm(item["label"]) == wanted]
    if len(exact) == 1:
        return exact[0]
    hits = [item for item in real if has_phrase(norm(item["label"]), wanted)]
    if len(hits) == 1:
        return hits[0]
    return None


def is_huellas(label: str) -> bool:
    name = norm(label)
    return "toma de huellas" in name and ("expedicion" in name or "tarjeta" in name)


def parse_doc_type(text: str) -> str | None:
    raw = norm(text)
    if raw in {"1", "nie", "n.i.e", "n.i.e.", "ние"} or raw.startswith("nie"):
        return "nie"
    if raw in {"2", "паспорт", "pasaporte", "passport"} or raw.startswith("pas"):
        return "pasaporte"
    if raw in {"3", "dni", "дн"} or "dni" in raw:
        return "dni"
    return None


def clean_doc(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def clean_phone(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if digits.startswith("0034"):
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) > 9:
        digits = digits[2:]
    return digits


def validate_field(key: str, raw: str) -> tuple[str | None, str | None]:
    """Return (stored_value, error)."""
    text = (raw or "").strip()
    if key == "province":
        if len(norm(text)) < 3:
            return None, "Напиши провинцию, например Madrid."
        return text, None
    if key == "office":
        if norm(text) in ANY_OFFICE:
            return "cualquier", None
        if len(text) < 2:
            return None, "Напиши «любая» или номер комиссарии."
        return text, None
    if key == "tramite":
        if len(text) < 2:
            return None, "Пришли номер trámite из списка."
        return text, None
    if key == "doc_type":
        kind = parse_doc_type(text)
        if not kind:
            return None, "Нужно NIE, паспорт или DNI."
        return kind, None
    if key == "doc_number":
        doc = clean_doc(text)
        if len(doc) < 5:
            return None, "Номер слишком короткий."
        return doc, None
    if key == "full_name":
        name = " ".join(text.split())
        if len(name.split()) < 2:
            return None, "Нужны и имя, и фамилия, как в документе."
        return name.upper(), None
    if key == "nationality":
        if len(norm(text)) < 3:
            return None, "Напиши страну гражданства."
        return text, None
    if key == "birth_year":
        if not re.fullmatch(r"(19|20)\d{2}", text):
            return None, "Год рождения — четыре цифры, например 1994."
        return text, None
    if key == "phone":
        phone = clean_phone(text)
        if not re.fullmatch(r"\d{9}", phone):
            return None, "Телефон — 9 цифр, без +34."
        return phone, None
    if key == "email":
        email = text.replace(" ", "").lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return None, "Это не похоже на почту."
        return email, None
    return text, None


def validate_doc_number(doc_type: str, number: str) -> str | None:
    if doc_type == "nie" and not re.fullmatch(r"[XYZ]\d{7}[A-Z]", number):
        return "NIE выглядит иначе: буква X, Y или Z, 7 цифр и буква. Пример: Y1234567X."
    if doc_type == "dni" and not re.fullmatch(r"\d{8}[A-Z]", number):
        return "DNI — 8 цифр и буква."
    return None


def parse_bundle(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        key = LABELS.get(norm(left))
        if key and right.strip():
            found[key] = right.strip()
    return found


def mask_doc(number: str) -> str:
    if len(number) <= 4:
        return "****"
    return number[0] + "…" + number[-3:]


def format_options(options: list[str]) -> str:
    lines = [f"{index}. {label}" for index, label in enumerate(options, start=1)]
    text = "\n".join(lines)
    if len(text) > 3500:
        return text[:3500] + "\n…"
    return text
