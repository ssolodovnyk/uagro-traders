"""Щоденний запуск агента: збирає оголошення, відбирає «з місця», прибирає дублі,
оновлює сторінку (docs/data.json) і надсилає підсумок у Telegram.

Запуск:  python agent/run.py            — звичайний запуск
         python agent/run.py --no-send  — без повідомлення в Telegram (для перевірки)
"""
import hashlib
import hmac
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.stdout.reconfigure(line_buffering=True)  # журнал запуску видно одразу, а не в кінці
sys.path.insert(0, str(Path(__file__).parent))
import rules as R  # noqa: E402
import sources as S  # noqa: E402
from sources import SOURCES  # noqa: E402

ROOT = Path(os.environ.get("AGENT_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "docs" / "data.json"
SEEN = ROOT / "state" / "seen.json"
KYIV = ZoneInfo("Europe/Kyiv")
PAGE_URL = os.environ.get("PAGE_URL", "")
PHONES_PASSWORD = os.environ.get("PHONES_PASSWORD", "")  # лише із секретів GitHub


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def fingerprint(ad, crops):
    """Відбиток для пошуку того самого оголошення на різних сайтах / повторних публікацій."""
    body = R.norm(R.strip_contacts(ad.get("text") or ad.get("title") or ""))
    body = re.sub(r"[^a-zа-яіїєґ]", "", body)[:160]
    if len(body) < 25:  # занадто короткий текст — не порівнюємо, щоб не злити різні оголошення
        return None
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def build_card(ad, today):
    full = " ".join(x for x in (ad.get("title"), ad.get("rubric"), ad.get("text")) if x)
    if R.is_excluded(full):
        return None, "excluded"
    crops = R.detect_crops(" ".join(x for x in (ad.get("rubric"), ad.get("title"), ad.get("text")) if x))
    if not crops:
        return None, "not_crop"
    terms, farm_hits, deliv_hits = R.classify(" ".join(x for x in (ad.get("title"), ad.get("text")) if x))
    if terms == "delivery":
        title = R.strip_contacts(ad.get("title") or "") or "Куплю " + ", ".join(c.lower() for c in crops[:3])
        return {"id": ad["id"], "source": ad["source"], "url": ad["url"], "title": title[:110],
                "signs": deliv_hits[:3]}, "delivery"

    price, cur = R.sane_price(*R.parse_price(ad.get("price_text") or ""))
    if price is None:
        price, cur = R.sane_price(*R.parse_price(full))
    volume = R.parse_volume(ad.get("volume_text") or "") or R.parse_volume(" ".join(x for x in (ad.get("title"), ad.get("text")) if x))
    region = R.detect_region(ad.get("region_text") or "") or R.detect_region(full)
    district = R.detect_district(ad.get("text") or "")
    company = R.company_or_none(ad.get("company_hint")) or R.company_or_none(ad.get("author"))
    title = R.strip_contacts(ad.get("title") or "")
    if not title:
        title = "Куплю " + ", ".join(c.lower() for c in crops[:3])
    if len(title) > 110:
        title = title[:107].rsplit(" ", 1)[0] + "…"

    card = {
        "id": ad["id"], "source": ad["source"], "url": ad["url"], "title": title,
        "crops": crops, "price": price, "currency": cur, "volume_t": volume,
        "region": region, "district": district, "company": company,
        "terms": terms, "signs": farm_hits[:3], "also_delivery": bool(deliv_hits) and terms == "farm",
        "dnd": R.do_not_disturb(full), "posted": ad["date"].isoformat(), "first_seen": today.isoformat(),
    }
    return card, terms


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram не налаштовано — повідомлення лише тут]\n" + text)
        return False
    # TELEGRAM_CHAT_ID може містити кілька отримувачів через кому, наприклад «111,222»
    sent = 0
    for n, cid in enumerate([c.strip() for c in chat.split(",") if c.strip()], 1):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": cid, "text": text, "disable_web_page_preview": "true"}, timeout=30)
            ok = r.ok and r.json().get("ok")
            print(f"Telegram (отримувач {n}):", "надіслано" if ok else f"помилка {r.status_code} — чи натиснув отримувач Start у боті?")
            sent += bool(ok)
        except requests.RequestException as e:
            print(f"Telegram (отримувач {n}): помилка", type(e).__name__)
    return sent > 0


def main():
    no_send = "--no-send" in sys.argv
    now = datetime.now(KYIV)
    today = now.date()
    cutoff = today - timedelta(days=R.RULES.get("days_back", 7))

    seen = load(SEEN, {"ids": {}, "fps": {}})
    data = load(DATA, {"ads": []})
    old_ads = data.get("ads", [])
    is_seen = lambda i: i in seen["ids"]  # noqa: E731

    # телефони: зашифрована таблиця «оголошення → номери»; без пароля телефони не збираються взагалі
    phones = {}
    if PHONES_PASSWORD:
        import vault
        phones = vault.decrypt(data.get("phones_enc"), PHONES_PASSWORD) or {}
        S.WANT_PHONES[0] = True
        S.HAS_PHONE.clear()
        S.HAS_PHONE.update(phones)

    # «Вже показані номери»: щоб той самий трейдер не з'являвся вдруге з новим оголошенням.
    # Номери зберігаються не відкрито, а як ключ HMAC з паролем PHONES_PASSWORD (без пароля їх не відновити).
    def phone_key(p):
        return hmac.new(PHONES_PASSWORD.encode(), p.encode(), hashlib.sha256).hexdigest()[:20]
    if PHONES_PASSWORD and "phones" not in seen:
        first = {a["id"]: a.get("first_seen", today.isoformat()) for a in old_ads}
        seen["phones"] = {}
        for aid, ps in phones.items():
            for p in ps:
                k = phone_key(p)
                d = first.get(aid, today.isoformat())
                if k not in seen["phones"] or d < seen["phones"][k]:
                    seen["phones"][k] = d

    statuses, new_cards, rejected = [], [], []
    counts = {"farm": 0, "unclear": 0, "delivery": 0, "duplicate": 0, "dup_phone": 0}

    for key, name, home, fn in SOURCES:
        st = {"key": key, "name": name, "url": home, "ok": True, "error": "", "found": 0, "new": 0}
        print(f"→ {name}: починаю", flush=True)
        S.PARTIAL.clear()
        S.set_deadline(R.RULES.get("seconds_per_site", 480))
        try:
            raw = fn(cutoff, today, is_seen)
            if S.PARTIAL:
                st["partial"] = True
                st["error"] = "зібрано не все: " + S.PARTIAL[0]
            st["found"] = len(raw)
            for ad in raw:
                if PHONES_PASSWORD and ad.get("phones"):
                    phones[ad["id"]] = ad["phones"]
                if is_seen(ad["id"]):
                    continue
                if ad.get("detail_failed"):
                    tries = seen.setdefault("retry", {}).get(ad["id"], 0)
                    if ad["detail_failed"] != "time":  # брак часу — не збій, просто дочитаємо завтра
                        tries += 1
                        seen["retry"][ad["id"]] = tries
                    if tries < 3:  # опис не відкрився — спробуємо завтра (максимум 2 рази)
                        st["retry_later"] = st.get("retry_later", 0) + 1
                        continue
                card, verdict = build_card(ad, today)
                seen["ids"][ad["id"]] = today.isoformat()
                seen.get("retry", {}).pop(ad["id"], None)
                if verdict == "delivery":
                    counts["delivery"] += 1
                    rejected.append(card)
                    continue
                if not card:
                    continue
                fp = fingerprint(ad, card["crops"])
                if fp and fp in seen["fps"]:
                    counts["duplicate"] += 1
                    continue
                if fp:
                    seen["fps"][fp] = today.isoformat()
                if PHONES_PASSWORD and ad.get("phones"):
                    keys = [phone_key(p) for p in ad["phones"]]
                    if any(k in seen["phones"] for k in keys):
                        counts["dup_phone"] += 1  # цей номер уже був — трейдер відомий, не показуємо вдруге
                        continue
                    for k in keys:
                        seen["phones"][k] = today.isoformat()
                counts[verdict] += 1
                st["new"] += 1
                new_cards.append(card)
        except Exception as e:  # сайт не відкрився або змінився — пишемо про це, а не мовчимо
            st["ok"] = False
            st["error"] = str(e)[:200]
            traceback.print_exc()
        statuses.append(st)
        S.set_deadline(None)
        print(f"{name}: {'OK' if st['ok'] else 'ПОМИЛКА ' + st['error']} — знайдено {st['found']}, нових {st['new']}"
              + (f", відкладено на завтра {st['retry_later']}" if st.get("retry_later") else "")
              + (" (частково)" if st.get("partial") else ""), flush=True)

    keep_from = (today - timedelta(days=R.RULES.get("keep_days", 45))).isoformat()
    ads = new_cards + [a for a in old_ads if a.get("first_seen", "") >= keep_from and a["id"] not in {c["id"] for c in new_cards}]
    ads.sort(key=lambda a: (a.get("posted", ""), a.get("first_seen", "")), reverse=True)

    # чистимо старі записи «вже бачених», щоб файл не ріс безкінечно (180 днів)
    old = (today - timedelta(days=180)).isoformat()
    seen["ids"] = {k: v for k, v in seen["ids"].items() if v >= old}
    seen["fps"] = {k: v for k, v in seen["fps"].items() if v >= old}
    if "phones" in seen:
        seen["phones"] = {k: v for k, v in seen["phones"].items() if v >= old}

    failed = [s["name"] for s in statuses if not s["ok"]]
    run = {
        "generated_at": now.isoformat(timespec="minutes"), "run_date": today.isoformat(),
        "new_total": len(new_cards), "new_farm": counts["farm"], "new_unclear": counts["unclear"],
        "rejected_delivery": counts["delivery"], "duplicates": counts["duplicate"], "failed": failed,
        "dup_phones": counts["dup_phone"],
    }
    out = {"run": run, "sources": statuses, "ads": ads, "rejected_today": rejected}
    if PHONES_PASSWORD:
        ids = {a["id"] for a in ads}
        phones = {k: v for k, v in phones.items() if k in ids}  # лише для оголошень на сторінці
        for a in ads:
            a["has_phone"] = a["id"] in phones
        out["phones_enc"] = vault.encrypt(phones, PHONES_PASSWORD)
        run["with_phone"] = len(phones)
    save(DATA, out)
    save(SEEN, seen)

    msg = (f"🌾 Зернотрейдери «з місця» — {today.strftime('%d.%m.%Y')}\n"
           f"Нових: {len(new_cards)} (з місця: {counts['farm']}, неясні: {counts['unclear']})\n"
           f"Відсіяно CPT/доставка: {counts['delivery']}, дублів: {counts['duplicate']}, "
           f"номерів, що вже були: {counts['dup_phone']}")
    if failed:
        msg += "\n⚠️ Не відкрився: " + ", ".join(failed)
    partial = [s["name"] for s in statuses if s["ok"] and s.get("partial")]
    if partial:
        msg += "\n⚠️ Зібрано не все (сайт повільний, решту дочитаю завтра): " + ", ".join(partial)
    if PAGE_URL:
        msg += f"\n{PAGE_URL}"
    if no_send:
        print("\n--- повідомлення (не надсилалось) ---\n" + msg)
    else:
        send_telegram(msg)


if __name__ == "__main__":
    main()
