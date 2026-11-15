#!/usr/bin/env python3
"""Adatbank-scout adatgyűjtő: a ligak.json bajnokságainak fordulóit végignézi, és a meccsek adatait
(hivatalos személyek, tabella) jelszóval titkosítva a docs/adatok.enc fájlba menti. A keresőoldal (docs/index.html) ebből dolgozik.
A GitHub Actions időnként automatikusan lefuttatja. Az adatbank_scout.py függvényeit használja.
"""
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import adatbank_scout as sc  # noqa: E402

LIGAK = HERE / "ligak.json"
OUT = HERE / "docs" / "adatok.enc"  # jelszóval titkosított adat, csak a keresőoldal tudja visszafejteni
ITER = 250_000
BACK = int(os.environ.get("VISSZA", "30"))  # a ligak.json-beli fordulótól hány fordulót néz vissza (30 = az egész szezon, az 1. fordulóig)
FWD = int(os.environ.get("ELORE", "4"))     # és hányat előre
WORKERS = 8
PAUSE = 0.1
REUSE_AFTER_DAYS = 2  # ennél régebbi meccset nem tölt le újra, az előző futás adatát megtartja


def _key(pw, salt, iters):
    return PBKDF2HMAC(hashes.SHA256(), 32, salt, iters).derive(pw.encode("utf-8"))


def encrypt(obj, pw):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(pw, salt, ITER)).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "data": b(ct)})


def decrypt(text, pw):
    d = json.loads(text)
    a = lambda x: base64.b64decode(x)
    return json.loads(AESGCM(_key(pw, a(d["salt"]), d["iter"])).decrypt(a(d["iv"]), a(d["data"]), None).decode("utf-8"))


def is404(e):
    return getattr(getattr(e, "response", None), "status_code", None) == 404


def fetch(url):
    last = None
    for i in range(3):
        try:
            return sc.get(url, PAUSE)
        except Exception as e:
            last = e
            if is404(e):
                break
            time.sleep(1 + 2 * i)
    raise last


def record(murl, org, d):
    return {"url": murl, "szervezo": org, "cim": d["title"],
            "datum": d["date"].isoformat(timespec="minutes") if d["date"] else "",
            "datum_txt": d["date_txt"], "csapatok": d["teams"],
            "szemelyek": d["officials"],
            "tabella": {team: pos for pos, team, _ in d["standings"]}}


def main():
    pw = os.environ.get("ADATBANK_JELSZO", "")
    if not pw:
        sys.exit("Hiányzik az ADATBANK_JELSZO (GitHub: Settings, Secrets and variables, Actions).")
    cfg = json.loads(LIGAK.read_text(encoding="utf-8"))
    prev, prev_payload = {}, None
    if OUT.exists():
        try:  # az előző futás adata (ha a jelszó megváltozott, nem fejthető vissza, akkor mindent újra letölt)
            prev_payload = decrypt(OUT.read_text(encoding="utf-8"), pw)
            for m in prev_payload.get("meccsek", []):
                prev[m["url"]] = m
        except Exception:
            pass
    now = datetime.now(ZoneInfo("Europe/Budapest"))
    cutoff = (now - timedelta(days=REUSE_AFTER_DAYS)).replace(tzinfo=None)

    rpairs = {}
    for org, urls in cfg.items():
        for lurl in urls:
            for r, rurl in sc.round_urls(lurl, BACK, FWD, None):
                rpairs.setdefault(rurl, (r, org))

    errors = [0]

    def get_round(item):
        rurl, (r, org) = item
        try:
            soup = fetch(rurl)
        except Exception as e:
            if is404(e):  # nincs ilyen forduló (pl. a szezon végén)
                return
            errors[0] += 1
            print(f"! {rurl}: {e}", file=sys.stderr)
            return
        sc.ROUND_CACHE[rurl] = sc.match_links(soup, r)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(get_round, rpairs.items()))

    mmap = {}
    for rurl, (r, org) in rpairs.items():
        for murl in sc.ROUND_CACHE.get(rurl, []):
            mmap.setdefault(murl, org)
    print(f"{len(rpairs)} fordulóoldal, {len(mmap)} mérkőzés, {errors[0]} hiba a fordulóknál")

    def get_match(item):
        murl, org = item
        old = prev.get(murl)
        if old and old.get("datum") and datetime.fromisoformat(old["datum"]) < cutoff:
            return old
        try:
            return record(murl, org, sc.parse_match(fetch(murl)))
        except Exception as e:
            errors[0] += 1
            print(f"! {murl}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(WORKERS) as ex:
        recs = [r for r in ex.map(get_match, mmap.items()) if r]

    seen, out = set(), []
    for r in sorted(recs, key=lambda r: (r["datum"] or "9", r["url"])):
        key = (r["datum_txt"], tuple(r["csapatok"]))  # ugyanaz a meccs hosszú és rövid linkről
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    if not out:
        sys.exit("Nem sikerült egyetlen mérkőzést sem letölteni, a régi adat megmarad.")
    OUT.parent.mkdir(exist_ok=True)
    # ha az adat nem változott és az előző mentés 24 óránál frissebb, nem írjuk újra a fájlt (kisebb tároló-előzmény)
    if prev_payload and prev_payload.get("meccsek") == out:
        try:
            age = now - datetime.fromisoformat(prev_payload["frissitve_iso"])
            if age < timedelta(hours=24):
                print(f"Nincs változás ({len(out)} mérkőzés), a fájl marad.")
                return
        except Exception:
            pass
    OUT.write_text(encrypt({"frissitve": now.strftime("%Y.%m.%d %H:%M"),
                            "frissitve_iso": now.isoformat(timespec="minutes"),
                            "hibak": errors[0], "meccsek": out}, pw), encoding="utf-8")
    print(f"Kész: {len(out)} mérkőzés mentve, {errors[0]} hiba.")


if __name__ == "__main__":
    main()
