"""Щоденний запуск агента: збирає оголошення, відбирає «з місця», прибирає дублі,
оновлює сторінку (docs/data.json) і надсилає підсумок у Telegram.

Запуск:  python agent/run.py            — звичайний запуск
         python agent/run.py --no-send  — без повідомлення в Telegram (для перевірки)
"""
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).parent))
import rules as R  # noqa: E402
from sources import SOURCES  # noqa: E402

ROOT = Path(os.environ.get("AGENT_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "docs" / "data.json"
SEEN = ROOT / "state" / "seen.json"
KYIV = ZoneInfo("Europe/Kyiv")
PAGE_URL = os.environ.get("PAGE_URL", "")


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
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=30)
        ok = r.ok and r.json().get("ok")
        print("Telegram:", "надіслано" if ok else f"помилка {r.status_code}")
        return bool(ok)
    except requests.RequestException as e:
        print("Telegram: помилка", type(e).__name__)
        return False


def main():
    no_send = "--no-send" in sys.argv
    now = datetime.now(KYIV)
    today = now.date()
    cutoff = today - timedelta(days=R.RULES.get("days_back", 7))

    seen = load(SEEN, {"ids": {}, "fps": {}})
    data = load(DATA, {"ads": []})
    old_ads = data.get("ads", [])
    is_seen = lambda i: i in seen["ids"]  # noqa: E731

    statuses, new_cards, rejected = [], [], []
    counts = {"farm": 0, "unclear": 0, "delivery": 0, "duplicate": 0}

    for key, name, home, fn in SOURCES:
        st = {"key": key, "name": name, "url": home, "ok": True, "error": "", "found": 0, "new": 0}
        try:
            raw = fn(cutoff, today, is_seen)
            st["found"] = len(raw)
            for ad in raw:
                if is_seen(ad["id"]):
                    continue
                if ad.get("detail_failed"):
                    tries = seen.setdefault("retry", {}).get(ad["id"], 0) + 1
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
                counts[verdict] += 1
                st["new"] += 1
                new_cards.append(card)
        except Exception as e:  # сайт не відкрився або змінився — пишемо про це, а не мовчимо
            st["ok"] = False
            st["error"] = str(e)[:200]
            traceback.print_exc()
        statuses.append(st)
        print(f"{name}: {'OK' if st['ok'] else 'ПОМИЛКА ' + st['error']} — знайдено {st['found']}, нових {st['new']}")

    keep_from = (today - timedelta(days=R.RULES.get("keep_days", 45))).isoformat()
    ads = new_cards + [a for a in old_ads if a.get("first_seen", "") >= keep_from and a["id"] not in {c["id"] for c in new_cards}]
    ads.sort(key=lambda a: (a.get("posted", ""), a.get("first_seen", "")), reverse=True)

    # чистимо старі записи «вже бачених», щоб файл не ріс безкінечно (180 днів)
    old = (today - timedelta(days=180)).isoformat()
    seen["ids"] = {k: v for k, v in seen["ids"].items() if v >= old}
    seen["fps"] = {k: v for k, v in seen["fps"].items() if v >= old}

    failed = [s["name"] for s in statuses if not s["ok"]]
    run = {
        "generated_at": now.isoformat(timespec="minutes"), "run_date": today.isoformat(),
        "new_total": len(new_cards), "new_farm": counts["farm"], "new_unclear": counts["unclear"],
        "rejected_delivery": counts["delivery"], "duplicates": counts["duplicate"], "failed": failed,
    }
    save(DATA, {"run": run, "sources": statuses, "ads": ads, "rejected_today": rejected})
    save(SEEN, seen)

    msg = (f"🌾 Зернотрейдери «з місця» — {today.strftime('%d.%m.%Y')}\n"
           f"Нових: {len(new_cards)} (з місця: {counts['farm']}, неясні: {counts['unclear']})\n"
           f"Відсіяно CPT/доставка: {counts['delivery']}, дублів: {counts['duplicate']}")
    if failed:
        msg += "\n⚠️ Не відкрився: " + ", ".join(failed)
    if PAGE_URL:
        msg += f"\n{PAGE_URL}"
    if no_send:
        print("\n--- повідомлення (не надсилалось) ---\n" + msg)
    else:
        send_telegram(msg)


if __name__ == "__main__":
    main()
