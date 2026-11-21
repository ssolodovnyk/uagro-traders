"""Збирачі оголошень про закупівлю з 4 сайтів. Кожен повертає список «сирих» оголошень."""
import json
import re
import time
import warnings
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from rules import RULES, extract_phones

# Оголошення, для яких телефон уже відомий (заповнює run.py). Для решти вже бачених
# Agrotender ще раз відкриває сторінку, щоб дізнатися телефон.
HAS_PHONE = set()
WANT_PHONES = [False]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8,en;q=0.5"})
PAUSE = RULES.get("pause_seconds", 0.8)
MAX_PAGES = RULES.get("max_pages_per_list", 8)


class SiteError(Exception):
    pass


# Ліміт часу на один сайт (секунди). run.py ставить його перед кожним сайтом,
# щоб повільний або заблокований сайт не зупинив увесь запуск.
DEADLINE = [None]


def set_deadline(seconds):
    DEADLINE[0] = time.time() + seconds if seconds else None


def fetch(url, tries=3):
    """Завантажує сторінку. Якщо у сайту прострочений сертифікат — читає без перевірки (лише читання)."""
    last = None
    for i in range(tries):
        if DEADLINE[0] and time.time() > DEADLINE[0]:
            raise SiteError("вичерпано час на цей сайт")
        if i:
            SESSION.close()  # після збою — нове з'єднання
        try:
            try:
                r = SESSION.get(url, timeout=(10, 25))
            except requests.exceptions.SSLError:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = SESSION.get(url, timeout=(10, 25), verify=False)
            if r.status_code == 200:
                r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
                time.sleep(PAUSE)
                return r.text
            last = f"код відповіді {r.status_code}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
        time.sleep(2 + 3 * i)
    raise SiteError(f"{url}: {last}")


PARTIAL = []  # сайти, з яких зібрано не все (час вийшов або сторінка не відкрилась)


def list_page(url, ads):
    """Сторінка списку. Якщо вже щось зібрали — при збої не губимо зібране, а позначаємо «частково»."""
    try:
        return fetch(url)
    except SiteError as e:
        if not ads:
            raise
        PARTIAL.append(str(e)[:150])
        return None


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _date_ru_uk(s, today):
    s = (s or "").strip().lower()
    if not s:
        return None
    if s.startswith(("сегодня", "сьогодні")):
        return today
    if s.startswith(("вчера", "вчора")):
        return today - timedelta(days=1)
    for rx, order in ((r"(\d{4})/(\d{2})/(\d{2})", "ymd"), (r"(\d{2})[./\-](\d{2})[./\-](\d{4})", "dmy")):
        m = re.search(rx, s)
        if m:
            a, b, c = map(int, m.groups())
            try:
                return date(a, b, c) if order == "ymd" else date(c, b, a)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- agrotender.com.ua
AT = "https://agrotender.com.ua"
AT_GROUPS = {"150": "Зернові", "16": "Олійні", "2": "Бобові"}


def agrotender(cutoff, today, is_seen):
    ads, seen_ids = [], set()
    for gid in AT_GROUPS:
        for p in range(1, MAX_PAGES + 1):
            url = f"{AT}/board/buy_t{gid}" + (f"_p{p}" if p > 1 else "")
            html = list_page(url, ads)
            if html is None:
                break
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select("div.postItem")
            if not items:
                break
            fresh_on_page = 0
            for it in items:
                a = it.select_one("h3.title a") or it.select_one("a[href*='/board/post-']")
                if not a:
                    continue
                m = re.search(r"post-(\d+)", a.get("href", ""))
                if not m or m.group(1) in seen_ids:
                    continue
                pid = m.group(1)
                seen_ids.add(pid)
                d = None
                for ds in it.select("span.date"):
                    d = d or _date_ru_uk(_txt(ds), today)
                is_top = bool(it.select_one(".ribbon"))
                if d and d >= cutoff and not is_top:
                    fresh_on_page += 1
                if not d or d < cutoff:
                    continue
                ads.append({
                    "source": "agrotender", "id": f"agrotender:{pid}", "url": f"{AT}/board/post-{pid}",
                    "title": _txt(a), "rubric": _txt(it.select_one("span.rubric")),
                    "price_text": _txt(it.select_one("span.price")), "volume_text": _txt(it.select_one("span.unit")),
                    "author": _txt(it.select_one("span.author")), "region_text": _txt(it.select_one("span.region")),
                    "date": d, "text": "",
                })
            if fresh_on_page == 0:
                break
    # спочатку нові оголошення, потім — уже бачені без телефону
    order = [a for a in ads if not is_seen(a["id"])] + [a for a in ads if is_seen(a["id"])]
    for ad in order:
        if is_seen(ad["id"]) and (not WANT_PHONES[0] or ad["id"] in HAS_PHONE):
            continue
        try:
            soup = BeautifulSoup(fetch(ad["url"]), "html.parser")
        except SiteError:
            if not is_seen(ad["id"]):
                ad["detail_failed"] = "time" if DEADLINE[0] and time.time() > DEADLINE[0] else "error"  # опис не завантажився — спробуємо наступного ранку
            continue
        ad["text"] = _txt(soup.select_one("div.desc p.text") or soup.select_one("div.desc"))
        comp = soup.select_one("a[href*='/kompanii/comp-']")
        ad["company_hint"] = _txt(comp)
        tels = " ".join(a.get("href", "") for a in soup.select("a[href^='tel:']"))
        ad["phones"] = extract_phones(tels, ad["text"])
    return ads


# ---------------------------------------------------------------- agro-ukraine.com
AU = "https://agro-ukraine.com"
AU_RUBRICS = {"5": "Зернові", "18": "Зернобобові", "19": "Олійні"}


def agro_ukraine(cutoff, today, is_seen):
    ads, seen_ids = [], set()
    for rid in AU_RUBRICS:
        for p in range(1, MAX_PAGES + 1):
            url = f"{AU}/ua/trade/?adv_search=1&r_id={rid}&r_id_all_sub=1&types_id=1&sort_mode=1" + (f"&page={p}" if p > 1 else "")
            html = list_page(url, ads)
            if html is None:
                break
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select("div.i_l_i_c_mode3")
            if not items:
                break
            fresh_on_page = 0
            for it in items:
                pid = it.get("id", "")
                a = it.select_one("a.i_title")
                if not pid or not a or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                descr = it.select_one("span.descr")
                dtext = _txt(descr)
                d = _date_ru_uk(dtext, today)
                if "Куплю" not in dtext:
                    continue
                if d and d >= cutoff:
                    fresh_on_page += 1
                if not d or d < cutoff:
                    continue
                reg = descr.select_one("span") if descr else None
                ads.append({
                    "source": "agro-ukraine", "id": f"agro-ukraine:{pid}", "url": a.get("href"),
                    "title": _txt(a), "rubric": _txt(it.select_one("a.rubric1")),
                    "price_text": _txt(it.select_one("span.i_price")), "volume_text": "",
                    "author": "", "region_text": _txt(reg), "date": d,
                    "text": _txt(it.select_one("div.i_text")),
                })
            if fresh_on_page == 0:
                break
    for ad in ads:
        if is_seen(ad["id"]) or not ad["text"].endswith("..."):
            continue
        try:
            soup = BeautifulSoup(fetch(ad["url"]), "html.parser")
        except SiteError:
            ad["detail_failed"] = "time" if DEADLINE[0] and time.time() > DEADLINE[0] else "error"
            continue
        meta = soup.select_one("meta[name=description]") or soup.select_one("meta[property='og:description']")
        body = soup.select_one(".i_text, .ad_text, .text_c, [itemprop=description]")
        full = _txt(body) or (meta.get("content", "") if meta else "")
        if len(full) > len(ad["text"]) - 3:
            ad["text"] = full
    for ad in ads:
        # телефон на Agro-Ukraine захищений кнопкою «показати» — його не обходимо;
        # беремо номер лише якщо автор сам написав його в тексті
        ad["phones"] = extract_phones(ad["text"])
    return ads


# ---------------------------------------------------------------- clunya.com
CL = "https://clunya.com"


def _clunya_objects(html):
    """Витягує JSON-об'єкти оголошень, вбудовані в сторінку (Next.js)."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    payload = ""
    for c in chunks:
        try:
            payload += json.loads('"' + c + '"')
        except ValueError:
            continue
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\{"id":\d+,"title"', payload):
        try:
            obj, _ = dec.raw_decode(payload, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and "slug" in obj and "type" in obj:
            out.append(obj)
    return out


def clunya(cutoff, today, is_seen):
    ads, seen_ids = [], set()
    for p in range(1, MAX_PAGES + 1):
        html = list_page(f"{CL}/orders/page/{p}", ads)
        if html is None:
            break
        objs = _clunya_objects(html)
        if not objs:
            if p == 1:
                raise SiteError("не вдалося прочитати список оголошень (сайт змінив вигляд?)")
            break
        fresh_on_page = 0
        for o in objs:
            if o["id"] in seen_ids:
                continue
            seen_ids.add(o["id"])
            try:
                d = datetime.fromisoformat(o.get("publishedAt", "").replace("Z", "+00:00")).date()
            except ValueError:
                d = None
            if d and d >= cutoff and o.get("tier") != "premium":
                fresh_on_page += 1
            if o.get("type") != "buy" or not d or d < cutoff:
                continue
            cat = o.get("category") or {}
            reg = o.get("region") or {}
            price = o.get("price")
            cur = {"uah": "грн", "usd": "$", "eur": "€"}.get((o.get("currency") or "").lower(), "")
            qty = o.get("amount") or o.get("minQuantity")
            ads.append({
                "source": "clunya", "id": f"clunya:{o['id']}", "url": f"{CL}/orders/{o['slug']}",
                "title": o.get("title", ""), "rubric": f"{cat.get('parent', '')} {cat.get('name', '')}",
                "price_text": f"{price} {cur}" if price else "", "volume_text": f"{qty} т" if qty and (o.get("units") in (None, "ton")) else "",
                "author": "", "region_text": (reg.get("name") or "").capitalize(), "date": d,
                "text": o.get("description") or "",
                "phones": extract_phones(o.get("firstPhone") or "", o.get("secondPhone") or "", o.get("description") or ""),
            })
        if fresh_on_page == 0 and p > 1:
            break
    return ads


# ---------------------------------------------------------------- agrorynok.com.ua
AR = "https://agrorynok.com.ua"


def agrorynok(cutoff, today, is_seen):
    ads = []
    for p in range(1, MAX_PAGES + 1):
        url = f"{AR}/bbs/" if p == 1 else f"{AR}/bbs/{p}?words="
        html = list_page(url, ads)
        if html is None:
            break
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("div.bbs_item")
        if not items:
            if p == 1:
                raise SiteError("не знайдено оголошень на сторінці (сайт змінив вигляд?)")
            break
        fresh_on_page = 0
        for it in items:
            pid = (it.get("id") or "").replace("an", "")
            body = it.select_one("div.bbs_item_body") or it
            m = re.search(r"(\d{2}/\d{2}/\d{4})", _txt(it))
            d = _date_ru_uk(m.group(1), today) if m else None
            if d and d >= cutoff:
                fresh_on_page += 1
            if _txt(it.select_one("div.bbs_cat")) != "Купівля" or not d or d < cutoff or not pid:
                continue
            author = _txt(body.select_one("span[style*=bold], b, strong"))
            desc = ""
            for div in body.find_all("div", recursive=False):
                if not div.get("class"):
                    desc = _txt(div)
                    break
            # рядок «Автор, Місто, Область» — текст між жирним ім'ям і описом
            head = ""
            bold = body.select_one("span[style*=bold]")
            if bold and bold.next_sibling:
                head = str(bold.next_sibling).strip(" ,\n")
            head = head.replace("oblast_0", "").strip(" ,")
            tags = _txt(it.select_one("div.tags"))
            ads.append({
                "source": "agrorynok", "id": f"agrorynok:{pid}", "url": f"{AR}/bbs/{pid}.html",
                "title": "", "rubric": tags, "price_text": "", "volume_text": "",
                "author": author, "region_text": head, "date": d, "text": desc,
                "phones": extract_phones(_txt(it.select_one("div.contacts")), desc),
            })
        if fresh_on_page == 0:
            break
    return ads


SOURCES = [
    ("agrotender", "Agrotender", "https://agrotender.com.ua/board", agrotender),
    ("agro-ukraine", "Agro-Ukraine", "https://agro-ukraine.com/", agro_ukraine),
    ("clunya", "Clunya", "https://clunya.com/orders/page/1", clunya),
    ("agrorynok", "Агроринок", "https://agrorynok.com.ua/bbs/", agrorynok),
]
