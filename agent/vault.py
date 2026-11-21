"""Шифрування телефонів паролем власника.

Телефони зберігаються в публічному docs/data.json лише в зашифрованому вигляді
(AES-256-GCM, ключ з пароля через PBKDF2-SHA256). Сторінка розшифровує їх у браузері
тим самим паролем. Пароль зберігається тільки в секретах GitHub (PHONES_PASSWORD)
і в голові власника — ніколи у файлах проєкту.
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 250_000


def _key(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt, ITERATIONS)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": ITERATIONS, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(blob, password):
    """Повертає розшифрований об'єкт або None (немає даних / інший пароль)."""
    if not blob or not password:
        return None
    try:
        d = lambda k: base64.b64decode(blob[k])  # noqa: E731
        pt = AESGCM(_key(password, d("salt"), blob.get("iter", ITERATIONS))).decrypt(d("iv"), d("ct"), None)
        return json.loads(pt.decode("utf-8"))
    except Exception:
        return None
