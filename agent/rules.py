"""Правила відбору: з місця / CPT / неясно, культура, область, ціна, обсяг, очищення від контактів."""
import json
import re
from pathlib import Path

RULES = json.loads((Path(__file__).parent / "rules.json").read_text(encoding="utf-8"))

L = "a-zа-яіїєґё0-9"  # символи, що вважаються частиною слова


def _compile(term):
    """'=слово' -> лише ціле слово; інакше -> початок слова."""
    if term.startswith("="):
        t = re.escape(term[1:])
        return re.compile(rf"(?<![{L}]){t}(?![{L}])")
    return re.compile(rf"(?<![{L}]){re.escape(term)}")


def _compile_list(terms):
    return [(t.lstrip("="), _compile(t)) for t in terms]


FROM_FARM = _compile_list(RULES["from_farm"])
DELIVERY = _compile_list(RULES["delivery"])
DND = _compile_list(RULES["do_not_disturb"])
EXCLUDE = _compile_list(RULES["exclude"])
CROPS = [(name, _compile_list(terms)) for name, terms in RULES["crops"].items()]
REGIONS = [(name, _compile_list(terms)) for name, terms in RULES["regions"].items()]


def norm(text):
    t = (text or "").lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'").replace("`", "'")
    return re.sub(r"\s+", " ", t)


def _hits(text, compiled):
    return [label for label, rx in compiled if rx.search(text)]


def _words(text, compiled):
    """Як _hits, але повертає повне слово з тексту (наприклад «самовивозом», а не «самовив»)."""
    out = []
    for _, rx in compiled:
        m = rx.search(text)
        if m:
            end = m.end()
            while end < len(text) and re.match(rf"[{L}'\-]", text[end]):
                end += 1
            w = text[m.start():end]
            if w not in out:
                out.append(w)
    return out


def classify(text):
    """Повертає ('farm'|'unclear'|'delivery', ознаки_з_місця, ознаки_доставки)."""
    t = norm(text)
    farm = _words(t, FROM_FARM)
    deliv = _words(t, DELIVERY)
    if farm:
        return "farm", farm, deliv
    if deliv:
        return "delivery", farm, deliv
    return "unclear", farm, deliv


def do_not_disturb(text):
    return bool(_hits(norm(text), DND))


def is_excluded(text):
    return bool(_hits(norm(text), EXCLUDE))


def detect_crops(text):
    t = norm(text)
    found = [name for name, terms in CROPS if any(rx.search(t) for _, rx in terms)]
    if len(found) > 1 and "Зернові (різні)" in found:
        found.remove("Зернові (різні)")
    return found


def detect_region(text):
    t = norm(text)
    best = None
    for name, terms in REGIONS:
        for _, rx in terms:
            m = rx.search(t)
            if m and (best is None or m.start() < best[1]):
                best = (name, m.start())
    return best[0] + " обл." if best else None


def detect_district(text):
    m = re.search(r"([А-ЯІЇЄҐ][а-яіїєґ'’\-]{2,})(?:ий|ський|цький)?\s+(?:р-н|р-ну|район)", text or "")
    if m:
        return m.group(0).strip()
    m = re.search(r"(?:р-н|район)\s+([А-ЯІЇЄҐ][а-яіїєґ'’\-]{2,})", text or "")
    return m.group(0).strip() if m else None


CUR = {"грн": "грн", "uah": "грн", "₴": "грн", "$": "$", "usd": "$", "дол": "$", "€": "€", "eur": "€", "євро": "€", "евро": "€"}


def parse_price(text):
    """Шукає ціну за тонну у тексті. Повертає (число, валюта) або (None, None)."""
    t = norm(text).replace(" ", " ")
    m = re.search(r"(\d{1,3}(?:[ .]\d{3})+|\d{3,6})(?:[.,]\d+)?\s*(грн|uah|₴|\$|usd|дол|€|eur|євро|евро)", t)
    if m:
        return int(re.sub(r"[ .]", "", m.group(1))), CUR[m.group(2)]
    m = re.search(r"(\$|€)\s*(\d{2,5})", t)
    if m:
        return int(m.group(2)), CUR[m.group(1)]
    return None, None


def parse_volume(text):
    t = norm(text).replace(" ", " ")
    m = re.search(r"(?:від|от|до)?\s*(\d{1,3}(?:[ ]\d{3})+|\d{1,6})\s*(?:т|тн|тонн\w*|тон\w*)(?![a-zа-яіїєґ])", t)
    if m:
        return int(m.group(1).replace(" ", ""))
    return None


PHONE = re.compile(r"(?:\+?\d[\d\s\-()]{7,}\d)")
EMAIL = re.compile(r"[\w.\-]+@[\w\-]+\.[\w.\-]+")
URL = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+|@\w{4,}")


def strip_contacts(text):
    t = EMAIL.sub("", text or "")
    t = URL.sub("", t)
    t = PHONE.sub("", t)
    t = re.sub(r"(тел\.?|телефон|тел:|viber|вайбер|telegram|телеграм|whatsapp)[:\s]*", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" ,.;:-")


LEGAL = re.compile(
    r"(?<![A-Za-zА-Яа-яІіЇїЄєҐґ])(ТОВ|ТзОВ|ООО|ПП|ЧП|ФГ|ФХ|СФГ|ПрАТ|ПАТ|АТ|ТД|ДП|СТОВ|СВК|LLC|Ltd)(?![A-Za-zА-Яа-яІіЇїЄєҐґ])"
    r"|агро|зерно|трейд|груп|холдинг|oil|ойл|оіл|оил|компан|agro|grain|trade|group|елеватор|элеватор|млин|мельн|кхп|[«\"]",
    re.I,
)


def company_or_none(name):
    """Назва компанії, якщо схожа на компанію (а не на ім'я людини)."""
    name = re.split(r"\s+На сайті|\s+На сайте", (name or "").strip())[0]
    name = strip_contacts(name)
    if not name or len(name) > 80:
        return None
    bare = re.sub(r"[\s\"«»'.,]", "", name)
    if len(bare) < 4:  # лише «ТОВ», «ДП» тощо — це не назва
        return None
    return name if LEGAL.search(name) else None


PRICE_RANGE = {"грн": (1000, 200000), "$": (50, 3000), "€": (50, 3000)}


def sane_price(value, cur):
    """Відкидає явно помилкові ціни (55555, 100000000 тощо)."""
    if value is None or cur not in PRICE_RANGE:
        return None, None
    lo, hi = PRICE_RANGE[cur]
    if not (lo <= value <= hi) or re.fullmatch(r"(\d)\1{3,}", str(value)):
        return None, None
    return value, cur
