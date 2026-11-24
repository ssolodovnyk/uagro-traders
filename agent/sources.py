"""Збирачі оголошень про закупівлю з 5 сайтів. Кожен повертає список «сирих» оголошень."""
import json
import re
import time
import warnings
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
    return _au_engine("agro-ukraine", f"{AU}/ua/trade/", AU_RUBRICS, cutoff, today, is_seen)


# ---------------------------------------------------------------- zernoua.com (той самий «рушій», що й Agro-Ukraine)
ZU = "https://zernoua.com"
ZU_RUBRICS = {"14": "Пшениця", "5": "Кукурудза", "25": "Ячмінь", "11": "Соняшник", "21": "Соя", "17": "Ріпак",
              "4": "Гречка", "12": "Просо", "19": "Жито", "9": "Овес", "2": "Горох, нут", "23": "Квасоля",
              "95": "Гірчиця", "97": "Льон", "96": "Коріандр", "20": "Сорго", "22": "Тритикале", "24": "Сочевиця",
              "1": "Вика", "7": "Люпин", "94": "Зернові відходи"}


def zernoua(cutoff, today, is_seen):
    return _au_engine("zernoua", f"{ZU}/ua/board/", ZU_RUBRICS, cutoff, today, is_seen)


def _au_engine(source, base, rubrics, cutoff, today, is_seen):
    """Дошки на «рушії» Agro-Ukraine: пошук «Куплю» (types_id=1) за рубриками, найновіші зверху."""
    ads, seen_ids = [], set()
    for rid in rubrics:
        for p in range(1, MAX_PAGES + 1):
            url = f"{base}?adv_search=1&r_id={rid}&r_id_all_sub=1&types_id=1&sort_mode=1" + (f"&page={p}" if p > 1 else "")
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
                href = a.get("href") or ""
                if href.startswith("/"):
                    href = base.split("/ua/")[0] + href
                ads.append({
                    "source": source, "id": f"{source}:{pid}", "url": href,
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


# ---------------------------------------------------------------- zerno.agrotender.com.ua/purchases
# Заявки трейдерів на закупівлю. Без входу сайт показує 10 найсвіжіших — їх і беремо
# (вони вбудовані в сторінку як JSON). Телефон схований кнопкою «Показати номер» — його не беремо,
# лише якщо номер написано в коментарі.
ZA = "https://zerno.agrotender.com.ua"
KYIV_TZ = ZoneInfo("Europe/Kyiv")
_ZA_NOT_GRAIN = ("олія", "шрот", "макух", "жмих", "лузг")


def zerno_agrotender(cutoff, today, is_seen):
    html = fetch(f"{ZA}/purchases")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise SiteError("не вдалося прочитати заявки (сайт змінив вигляд?)")
    try:
        queries = json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
        posts = [p for q in queries for page in (q["state"]["data"] or {}).get("pages", []) for p in page.get("posts", [])]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SiteError("не вдалося прочитати заявки (сайт змінив вигляд?)")
    ads = []
    for o in posts:
        culture = o.get("culture") or ""
        if any(w in culture.lower() for w in _ZA_NOT_GRAIN):
            continue
        try:
            d = datetime.fromisoformat((o.get("date") or "").replace("Z", "+00:00")).astimezone(KYIV_TZ).date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        regions = o.get("region_list") or []
        price_text = ""
        for r in regions:
            pr = r.get("price") or {}
            for k, cur in (("uah", "грн"), ("usd", "$"), ("eur", "€")):
                if pr.get(k):
                    price_text = f"{pr[k]} {cur}"
                    break
            if price_text:
                break
        comment = o.get("comment") or ""
        user = o.get("user") or {}
        ads.append({
            "source": "zerno-agrotender", "id": f"zerno-agrotender:{o.get('_id')}", "url": f"{ZA}/purchases",
            "title": f"Купую: {culture}", "rubric": culture, "price_text": price_text, "volume_text": "",
            "author": user.get("name") or "", "company_hint": user.get("name") or "",
            "region_text": ", ".join(r.get("name", "") for r in regions[:3]) + (" обл." if regions else ""),
            "date": d, "text": comment, "phones": extract_phones(comment),
        })
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


# ---------------------------------------------------------------- agro-exchange.com.ua (life AGROS)
# Сайт малює оголошення через JavaScript, тому відкриваємо його звичайним браузером
# (Playwright, без входу в акаунт) і читаємо картки так, як їх бачить відвідувач.
AX = "https://agro-exchange.com.ua"

_AX_CARDS_JS = r"""() => {
  const sel = 'a[href^="/ogoloshennya/"]';
  const card = a => { let c = a; while (c.parentElement && c.parentElement.querySelectorAll(sel).length === 1) c = c.parentElement; return c; };
  const out = [];
  for (const a of document.querySelectorAll(sel)) {
    if (a.closest('.recommendation-card')) continue;  // карусель «Рекомендовані» — не дошка
    out.push({href: a.getAttribute('href'), lines: card(a).innerText.split('\n').map(s => s.trim()).filter(Boolean)});
  }
  return out;
}"""
_AX_HAS_CARDS_JS = """() => [...document.querySelectorAll('a[href^="/ogoloshennya/"]')].some(a => !a.closest('.recommendation-card'))"""


def _ax_parse(lines, today):
    """Рядки картки: [★ ТОП] / автор / КУПЛЮ / текст… / дата / Показати номер."""
    low = [s.lower() for s in lines]
    if "куплю" not in low:
        return None
    k = low.index("куплю")
    head = [s for s in lines[:k] if "топ" not in s.lower()]
    rest = lines[k + 1:]
    d, body = None, []
    for s in rest:
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
            d = _date_ru_uk(s, today)
        elif s.lower() not in ("показати номер", "показать номер"):
            body.append(s)
    return {"author": " ".join(head), "text": " ".join(body), "date": d, "is_top": len(head) < k}


def _ax_wait(page, url, timeout_ms):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_function(_AX_HAS_CARDS_JS, timeout=timeout_ms)


def agro_exchange(cutoff, today, is_seen):
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        raise SiteError("не встановлено Playwright (потрібен, бо сайт показує оголошення через JavaScript)")
    ads, seen_ids = [], set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(user_agent=UA, locale="uk-UA")
            for p in range(1, MAX_PAGES + 1):
                if DEADLINE[0] and time.time() > DEADLINE[0]:
                    if not ads:
                        raise SiteError("вичерпано час на цей сайт")
                    PARTIAL.append("вичерпано час на цей сайт")
                    break
                url = f"{AX}/?board=buy" + (f"&page={p}" if p > 1 else "")
                try:
                    _ax_wait(page, url, 40000 if p == 1 else 20000)
                except PwError as e:
                    if p == 1:
                        raise SiteError(f"{url}: оголошення не з'явились ({str(e).splitlines()[0][:100]})")
                    break  # сторінок більше немає
                time.sleep(PAUSE)
                fresh_on_page, new_on_page = 0, 0
                for c in page.evaluate(_AX_CARDS_JS):
                    m = re.search(r"/ogoloshennya/([0-9a-f\-]{20,})", c["href"])
                    if not m or m.group(1) in seen_ids:
                        continue
                    pid = m.group(1)
                    seen_ids.add(pid)
                    new_on_page += 1
                    info = _ax_parse(c["lines"], today)
                    if not info:
                        continue
                    d = info["date"]
                    if d and d >= cutoff and not info["is_top"]:
                        fresh_on_page += 1
                    if not d or d < cutoff:
                        continue
                    ads.append({
                        "source": "agro-exchange", "id": f"agro-exchange:{pid}", "url": f"{AX}/ogoloshennya/{pid}",
                        "title": "", "rubric": "", "price_text": "", "volume_text": "",
                        "author": info["author"], "region_text": "", "date": d, "text": info["text"],
                    })
                if fresh_on_page == 0 or new_on_page == 0:
                    break
            # обрізаний текст («…») дочитуємо зі сторінки оголошення — лише для нових
            for ad in ads:
                if is_seen(ad["id"]) or not ad["text"].endswith(("...", "…")):
                    continue
                if DEADLINE[0] and time.time() > DEADLINE[0]:
                    ad["detail_failed"] = "time"
                    continue
                try:
                    page.goto(ad["url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_selector("text=Опубліковано", timeout=20000)
                    time.sleep(PAUSE)
                    lines = [s.strip() for s in page.inner_text("main").split("\n") if s.strip()]
                    low = [s.lower() for s in lines]
                    i = low.index("опубліковано") + 2  # після підпису й дати
                    j = low.index("відкрити оголошення") if "відкрити оголошення" in low else len(lines)
                    full = " ".join(lines[i:j])
                    if len(full) > len(ad["text"]) - 3:
                        ad["text"] = full
                except (PwError, ValueError):
                    ad["detail_failed"] = "error"
        finally:
            browser.close()
    for ad in ads:
        # телефон схований кнопкою «Показати номер» — її не натискаємо;
        # беремо номер лише якщо автор сам написав його в тексті
        ad["phones"] = extract_phones(ad["text"])
    return ads


SOURCES = [
    ("agrotender", "Agrotender", "https://agrotender.com.ua/board", agrotender),
    ("agro-ukraine", "Agro-Ukraine", "https://agro-ukraine.com/", agro_ukraine),
    ("clunya", "Clunya", "https://clunya.com/orders/page/1", clunya),
    ("agrorynok", "Агроринок", "https://agrorynok.com.ua/bbs/", agrorynok),
    ("agro-exchange", "life AGROS", "https://agro-exchange.com.ua/?board=buy", agro_exchange),
    ("zernoua", "ZernoUA", "https://zernoua.com/ua/board/", zernoua),
    ("zerno-agrotender", "Agrotender Закупівля", "https://zerno.agrotender.com.ua/purchases", zerno_agrotender),
]
