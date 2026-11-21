"""Швидка перевірка правил відбору: python agent/test_rules.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules as R  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

CASES = [
    ("Закупаем ПШЕНИЦУ ФУРАЖНУЮ.Самовывоз.Оплата", "farm"),
    ("Закупаем сою в г.Николаев (СРТ)", "delivery"),
    ("Купим: рожь. Условия: EXW, СРТ.", "farm"),
    ("Закупаем зерновые в портах Одессы", "delivery"),
    ("Закупівля соняшнику, CPT Чорноморськ", "delivery"),
    ("Куплю кукурудзу 3 клас", "unclear"),
    ("Купуємо сою з місця, заберемо своїм транспортом", "farm"),
    ("Приймаємо соняшник на нашому елеваторі", "delivery"),
]

ok = True
for text, expected in CASES:
    got, farm, deliv = R.classify(text)
    mark = "OK " if got == expected else "ПОМИЛКА"
    ok &= got == expected
    print(f"{mark} {expected:>8} <- {got:<8} {text}  {farm or deliv}")

assert R.do_not_disturb("Куплю соняшник. Посередникам не турбувати")
assert R.sane_price(*R.parse_price("Ціна 13 000 грн/т")) == (13000, "грн")
assert R.sane_price(55555, "грн") == (None, None)
assert R.parse_volume("від 500 т") == 500
assert R.company_or_none('ТОВ "ГРIНД-МIЛЛ" На сайті з Апр. 2015') == 'ТОВ "ГРIНД-МIЛЛ"'
assert R.company_or_none("ДП") is None and R.company_or_none("Юрий") is None
assert "+380" not in R.strip_contacts("Куплю сою +380 67 123 45 67")
print("Усе гаразд" if ok else "Є помилки")
sys.exit(0 if ok else 1)
