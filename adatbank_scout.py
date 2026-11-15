#!/usr/bin/env python3
"""Adatbank-scout: játékvezető (vagy más hivatalos személy) keresése az MLSZ adatbankban.

Használat (minden keresés: "Név; Szervező; Forduló", a szervező és a forduló elhagyható):
    python adatbank_scout.py "Bíró Ottó; Győr-Moson-Sopron; 15"
    python adatbank_scout.py "Bíró Ottó; Győr-Moson-Sopron; 15" "Kovács Gábor; Győr-Moson-Sopron; 14"
    python adatbank_scout.py "Bíró Ottó" --szervezo "Győr-Moson-Sopron"      (régi forma, továbbra is megy)

Ha a forduló meg van adva, csak azt az egy fordulót nézi.
Ha hiányzik, a ligak.json-ban lévő linkek fordulóját veszi alapul (±1 fordulóval).
Mindig az összes találatot kiírja. A ligak.json-ban szervezőnként azok a bajnoki
oldalak vannak, amiket átnéz.
"""
import argparse, json, re, sys, time, unicodedata
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup

BASE = "https://adatbank.mlsz.hu"
HEADERS = {"User-Agent": "adatbank-scout/0.1 (személyes használat)"}
LABELS = ["Játékvezető", "Tartalékjátékvezető", "Asszisztensek", "Alapvonali játékvezetők",
          "Videóbíró", "Videóbíró asszisztens", "Operátor", "Ellenőr", "Játékvezető ellenőr"]
LEAGUE_RE = re.compile(r"/league/(\d+)/(\d+)/(\d+)/(\d+)\.html")
MATCH_RE = re.compile(r"/match/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)\.html")
SHORT_RE = re.compile(r"/match/(\d+)\.html")
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?")
session = requests.Session()
session.headers.update(HEADERS)

ROUND_CACHE = {}  # fordulóoldal -> meccslinkek (több keresésnél nem töltjük le kétszer)
MATCH_CACHE = {}  # meccslink -> értelmezett adatok


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def get(url, pause):
    time.sleep(pause)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"  # az adatbank nem jelzi a kódolást, enélkül az ékezetek elromlanak
    return BeautifulSoup(r.text, "html.parser")


def round_urls(league_url, rounds_back, rounds_fwd, override=None):
    """A megadott forduló-linkből előállítja a szomszédos fordulók linkjeit."""
    m = LEAGUE_RE.search(league_url)
    if not m:
        raise ValueError(f"Nem liga-link: {league_url}")
    season, zero, league, rnd = m.groups()
    rnd = int(override) if override else int(rnd)
    return [(r, f"{BASE}/league/{season}/{zero}/{league}/{r}.html")
            for r in range(max(1, rnd - rounds_back), rnd + rounds_fwd + 1)]


def match_links(soup, rnd):
    links = set()
    for a in soup.find_all("a", href=True):
        m = MATCH_RE.search(a["href"])
        if m and int(m.group(4)) == rnd:  # a "forma" hivatkozásokat (más forduló) kihagyja
            links.add(f"{BASE}{m.group(0)}")
    # le nem játszott meccsekre a tabella "Következő ellenfél" oszlopa mutat (rövid link, képes hivatkozás)
    for a in soup.find_all("a", href=True):
        m = SHORT_RE.search(a["href"])
        if m and not a.get_text(strip=True) and a.find("img", attrs={"title": True}):
            links.add(f"{BASE}{m.group(0)}")
    return sorted(links)


def parse_match(soup):
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    clubs = []
    for a in soup.find_all("a", href=re.compile(r"/club/")):
        t = a.get_text(strip=True)
        if t and t not in clubs:
            clubs.append(t)
    teams = clubs[:2]
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    start = next((i for i, l in enumerate(lines) if l == title), 0)
    date_txt, dt, venue = "", None, ""
    for i in range(start, min(start + 8, len(lines))):
        m = DATE_RE.search(lines[i])
        if m:
            y, mo, d, hh, mm = m.groups()
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
            date_txt = lines[i]
            venue = lines[i + 1] if i + 1 < len(lines) else ""
            break
    # hivatalos személyek: címke sor, utána nevek a következő címkéig / csapatnévig
    officials, cur = {}, None
    first = next((i for i in range(start, len(lines)) if lines[i] == "Játékvezető"), None)
    if first is not None:
        for l in lines[first:]:
            if l in teams:
                break
            if l in LABELS:
                cur = l
                officials.setdefault(cur, [])
            elif cur:
                officials[cur] += [" ".join(n.split()) for n in l.split(",") if n.strip()]
    return {"title": title, "date": dt, "date_txt": date_txt, "venue": venue,
            "teams": teams, "officials": officials, "standings": parse_table(soup, teams)}


def table_candidates(soup, teams):
    """Minden táblázatból kiszedi a két csapat sorát: (sorszám, fejléc, sorok, lejátszott meccsek összege)."""
    out = []
    for ti, t in enumerate(soup.find_all("table")):
        rows = []
        for tr in t.find_all("tr"):
            cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["td", "th"])]
            for i, c in enumerate(cells):
                if c and c in teams and cells and cells[0].isdigit():
                    nums = [x for x in cells[i + 1:] if re.fullmatch(r"-?\d+", x)][:8]
                    if len(nums) == 8:
                        rows.append((int(cells[0]), c, nums))
                    break
        if rows:
            out.append((ti, " ".join(t.get_text(" ", strip=True).split())[:70], rows, sum(int(r[2][0]) for r in rows)))
    return out


def parse_table(soup, teams):
    cands = table_candidates(soup, teams)
    if not cands:
        return []
    # a fő tabellában a legtöbb a lejátszott meccs (a hazai/idegen bontásban kevesebb); döntetlennél a korábbi
    best = max(cands, key=lambda c: (c[3], -c[0]))
    return sorted(best[2])


def find_hits(data, query):
    tokens = [norm(t) for t in query.split()]
    hits = []
    for role, names in data["officials"].items():
        for n in names:
            if all(t in norm(n) for t in tokens):
                hits.append((role, n))
    return hits


def show_short(data, hits):
    """Rövid kiírás: mérkőzés, hivatalos személyek, a két csapat helyezése."""
    hit_names = {n for _, n in hits}
    t = data["teams"] + ["?", "?"]
    print(f"\n{t[0]} – {t[1]}  |  {data['date_txt'] or 'ismeretlen időpont'}  |  {data['title']}")
    for role, names in data["officials"].items():
        marked = [n + "  <-- keresett" if n in hit_names else n for n in names]
        print(f"  {role}: {', '.join(marked) if marked else '–'}")
    if data["standings"]:
        print("  Tabella: " + "   ".join(f"{team}: {pos}. hely" for pos, team, _ in sorted(data["standings"], key=lambda r: data["teams"].index(r[1]) if r[1] in data["teams"] else 9)))
    else:
        print("  Tabella: nem található ezen az oldalon")


def show(url, data, hits):
    d = data["date_txt"] or "ismeretlen időpont"
    print(f"\n{data['title']}  |  {d}  |  {data['venue']}")
    print(f"{data['teams'][0] if data['teams'] else '?'} – {data['teams'][1] if len(data['teams']) > 1 else '?'}")
    print("Találat: " + "; ".join(f"{n} ({role})" for role, n in hits))
    print("Hivatalos személyek:")
    for role, names in data["officials"].items():
        print(f"  {role}: {', '.join(names) if names else '–'}")
    if data["standings"]:
        print("Tabella:  H.  Csapat                      M  Gy  D  V  LG  KG  GK   P")
        for pos, team, n in data["standings"]:
            print(f"          {pos:>2}.  {team:<26} " + " ".join(f"{x:>3}" for x in n))
    print(f"Link: {url}")


def _debug(url, pause):
    """Hibakereső: elmenti a nyers oldalt, és kiírja, mit lát belőle a program."""
    time.sleep(pause)
    r = session.get(url, timeout=30)
    r.encoding = "utf-8"
    name = "debug_" + re.sub(r"\W+", "_", url.split("mlsz.hu/")[-1]) + ".html"
    Path(name).write_text(r.text, encoding="utf-8")
    print(f"HTTP {r.status_code}, {len(r.text)} karakter, elmentve: {name}")
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    print("\n--- 'játékvezet' szövegű sorok és környezetük ---")
    shown = 0
    for i, l in enumerate(lines):
        if "játékvezet" in l.lower() and shown < 6:
            shown += 1
            print(f"[{i}]", " | ".join(lines[max(0, i - 2):i + 6]))
    print("\n--- mérkőzés-linkek ---")
    seen = set()
    for a in soup.find_all("a", href=True):
        if "/match" in a["href"] and a["href"] not in seen and len(seen) < 40:
            seen.add(a["href"])
            print(a["href"], "|", a.get_text(" ", strip=True)[:40])
    print("\n--- a program értelmezése ---")
    d = parse_match(soup)
    print("csapatok:", d["teams"], "| dátum:", d["date_txt"], "| helyszín:", d["venue"])
    print("hivatalos személyek:", d["officials"])
    print("\n--- táblázatok, ahol a két csapat szerepel ---")
    for ti, head, rows, sm in table_candidates(soup, d["teams"]):
        print(f"[{ti}] sorok: {len(rows)}, M-összeg: {sm}, fejléc: {head}")


def debug(url, pause):
    """Lefuttatja a hibakeresőt, és a kiírást egy szövegfájlba is elmenti (debug_meccs.txt / debug_liga.txt)."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            _debug(url, pause)
        except Exception as e:  # a hibát is bele akarjuk tenni a fájlba
            print(f"HIBA: {e!r}")
    text = buf.getvalue()
    name = "debug_meccs.txt" if "/match" in url else "debug_liga.txt"
    Path(name).write_text(text, encoding="utf-8")
    print(text)
    print(f"\nKész. A kiírás ide is el lett mentve: {name}")


def parse_query(arg, default_org, default_round):
    """'Név; Szervező; Forduló' -> (név, szervező, forduló vagy None). A szervező és a forduló elhagyható."""
    parts = [p.strip() for p in arg.split(";")]
    name = parts[0]
    org = parts[1] if len(parts) > 1 and parts[1] else default_org
    rnd = default_round
    if len(parts) > 2 and parts[2]:
        if not parts[2].isdigit():
            sys.exit(f'Hibás forduló ebben: "{arg}" – a fordulónak számnak kell lennie (pl. 15).')
        rnd = int(parts[2])
    if not name:
        sys.exit(f'Hiányzik a név ebben: "{arg}"')
    if not org:
        sys.exit(f'Hiányzik a szervező ebben: "{arg}". Írd így: "Név; Győr-Moson-Sopron; 15"')
    return name, org, rnd


def resolve_org(cfg, org):
    """A szervező nevét ékezet- és kisbetű-függetlenül, részletre is megtalálja a ligak.json-ban."""
    if org in cfg:
        return org
    n = norm(org)
    m = [k for k in cfg if n and n in norm(k)]
    if len(m) == 1:
        return m[0]
    if not m:
        sys.exit(f"Nincs ilyen szervező a ligak.json-ban: {org}. Van: {', '.join(cfg)}")
    sys.exit(f"A(z) {org} több szervezőre illik ({', '.join(m)}), írd ki pontosabban.")


def search(cfg, name, org, rnd, a):
    """Egy keresés: az összes találatot visszaadja (dátum szerint), a megnézett meccsek számát és a hibákat."""
    urls = cfg[resolve_org(cfg, org)]
    found, seen, seen_keys, errors, scanned = [], set(), set(), 0, []
    # megadott fordulónál csak azt az egyet nézi; forduló nélkül a link fordulója körül --vissza/--elore (alap: ±1)
    back, fwd = (0, 0) if rnd else (a.vissza, a.elore)
    for lurl in urls:
        for r, rurl in round_urls(lurl, back, fwd, rnd):
            try:
                if rurl not in ROUND_CACHE:
                    rsoup = get(rurl, a.szunet)
                    h1 = rsoup.find("h1")
                    ROUND_CACHE[rurl] = (h1.get_text(" ", strip=True) if h1 else rurl, match_links(rsoup, r))
                rtitle, links = ROUND_CACHE[rurl]
                scanned.append((rtitle, len(links)))
            except Exception as e:
                errors += 1
                scanned.append((f"{rurl} (HIBA)", 0))
                print(f"! {rurl}: {e}", file=sys.stderr)
                continue
            print(f"{rurl}  ->  {len(links)} mérkőzés", file=sys.stderr)
            for murl in links:
                if murl in seen:
                    continue
                seen.add(murl)
                try:
                    if murl not in MATCH_CACHE:
                        MATCH_CACHE[murl] = parse_match(get(murl, a.szunet))
                    data = MATCH_CACHE[murl]
                except Exception as e:
                    errors += 1
                    print(f"! {murl}: {e}", file=sys.stderr)
                    continue
                hits = find_hits(data, name)
                if not hits:
                    continue
                key = (data["date_txt"], tuple(data["teams"]))  # ugyanaz a meccs hosszú és rövid linkről
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                found.append((data["date"] or datetime.min, murl, data, hits))
    found.sort(key=lambda x: x[0])
    return found, len(seen), errors, scanned


def main():
    ap = argparse.ArgumentParser(description="Játékvezető keresése az MLSZ adatbankban")
    ap.add_argument("nev", nargs="*", help='keresés(ek): "Név; Szervező; Forduló" (a szervező és a forduló elhagyható)')
    ap.add_argument("--szervezo", help="alapértelmezett szervező azokhoz a keresésekhez, ahol nincs megadva")
    ap.add_argument("--debug", metavar="URL", help="hibakereső mód egy oldalra")
    ap.add_argument("--ligak", default=str(Path(__file__).with_name("ligak.json")))
    ap.add_argument("--fordulo", type=int, help="alapértelmezett forduló azokhoz a keresésekhez, ahol nincs megadva")
    ap.add_argument("--vissza", type=int, default=1, help="hány fordulót nézzen vissza (alap: 1)")
    ap.add_argument("--elore", type=int, default=1, help="hány fordulót nézzen előre (alap: 1)")
    ap.add_argument("--reszletes", action="store_true", help="teljes kiírás (teljes tabellasor, link)")
    ap.add_argument("--mind", action="store_true", help="(már alapból az összes találatot kiírja, ez csak a régi parancsok miatt maradt)")
    ap.add_argument("--szunet", type=float, default=0.5, help="másodperc két lekérés között")
    a = ap.parse_args()
    if a.debug:
        return debug(a.debug, a.szunet)
    if not a.nev:
        ap.error('add meg a keresést, pl. "Bíró Ottó; Győr-Moson-Sopron; 15"')

    queries = [parse_query(q, a.szervezo, a.fordulo) for q in a.nev]
    cfg = json.loads(Path(a.ligak).read_text(encoding="utf-8"))
    for _, org, _ in queries:
        resolve_org(cfg, org)  # hibás szervezőnél még a lekérések előtt szóljon
    print(f"Adatbank-scout v4 | ligak.json: {Path(a.ligak).resolve()}", file=sys.stderr)
    for k, v in cfg.items():
        print(f"  {k}: {len(v)} bajnoki link", file=sys.stderr)

    for name, org, rnd in queries:
        found, n_seen, errors, scanned = search(cfg, name, org, rnd, a)
        rnd_txt = f"{rnd}. forduló" if rnd else f"a ligak.json-beli fordulók (-{a.vissza}/+{a.elore})"
        print(f"\n===== {name}  |  {resolve_org(cfg, org)}  |  {rnd_txt} =====")
        print("Átnézett oldalak:")
        for rt, n in scanned:
            print(f"  - {rt}: {n} mérkőzés")
        if not found:
            print(f"Nincs találat ({n_seen} mérkőzést néztem át, {errors} hiba).")
            if n_seen == 0:
                print("0 mérkőzést talált a fordulókon, ellenőrizd a ligak.json linkjeit.")
            continue
        print(f"{len(found)} találat ({n_seen} mérkőzést néztem át, {errors} hiba).")
        for _, murl, data, hits in found:
            if a.reszletes:
                show(murl, data, hits)
            else:
                show_short(data, hits)


if __name__ == "__main__":
    main()
