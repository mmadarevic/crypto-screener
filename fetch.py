#!/usr/bin/env python3
"""
Krypto-Screener — holt Marktdaten von CoinGecko und Sozialdaten von LunarCrush,
führt sie zusammen und schreibt sie als JSON ins Repository.

Läuft täglich über GitHub Actions. Schlüssel kommen aus Umgebungsvariablen,
niemals aus dem Code.

Erzeugt:
  data/latest.json    — aktueller Stand, nach Größenbändern sortiert
  data/changes.json   — was sich seit dem letzten Lauf verändert hat
  data/categories.txt — einmalig: alle verfügbaren CoinGecko-Kategorien
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────────────────────

CG_KEY = os.environ.get("COINGECKO_KEY", "").strip()
LC_KEY = os.environ.get("LUNARCRUSH_KEY", "").strip()

CG_BASE = "https://api.coingecko.com/api/v3"
# Hinweis: Basis-URL und Pfad gegen die aktuelle LunarCrush-Dokumentation
# prüfen. Konnte beim Schreiben nicht getestet werden.
LC_BASE = "https://lunarcrush.com/api4"

# Kategorien, die gescreent werden. Die IDs müssen exakt zu CoinGecko passen —
# der erste Lauf schreibt alle verfügbaren nach data/categories.txt.
CATEGORIES = [
    "artificial-intelligence",
    "depin",
    "real-world-assets-rwa",
    "payment-solutions",
    "oracle",
    "liquid-staking-tokens",
    "prediction-markets",
    "decentralized-finance-defi",
]

# Wird ein Token ausschließlich in diesen Kategorien geführt, fliegt es raus.
EXCLUDE_HINTS = [
    "meme", "dog", "cat", "frog", "elon", "celebrity",
    "gambling", "casino", "parody",
]

# Größenbänder in USD
BANDS = [
    ("5-50",    5_000_000,    50_000_000),
    ("50-150",  50_000_000,   150_000_000),
    ("150-500", 150_000_000,  500_000_000),
]

MIN_VOLUME = 100_000        # 24h-Volumen darunter: zu illiquide zum Handeln
OUT_DIR = Path("data")

# ─────────────────────────────────────────────────────────────
#  HTTP mit Wiederholung
# ─────────────────────────────────────────────────────────────

def get_json(url, headers=None, tries=3, pause=2.0):
    """Holt JSON. Gibt None zurück statt zu werfen — der Aufrufer entscheidet."""
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            print(f"  HTTP {e.code} bei Versuch {attempt}/{tries}: {body}")
            if e.code == 429:              # Rate Limit — länger warten
                time.sleep(pause * attempt * 3)
                continue
            if e.code in (401, 403):       # Schlüssel falsch — Wiederholung zwecklos
                return None
        except Exception as e:
            print(f"  Fehler bei Versuch {attempt}/{tries}: {e}")
        if attempt < tries:
            time.sleep(pause * attempt)
    return None


# ─────────────────────────────────────────────────────────────
#  CoinGecko
# ─────────────────────────────────────────────────────────────

def cg_headers():
    return {"x-cg-demo-api-key": CG_KEY, "accept": "application/json"} if CG_KEY else {"accept": "application/json"}


def dump_categories():
    """Einmalig: alle Kategorie-IDs auflisten, damit die Konfiguration stimmt."""
    print("Hole Kategorieliste …")
    data = get_json(f"{CG_BASE}/coins/categories/list", headers=cg_headers())
    if not data:
        print("  Kategorieliste nicht erhalten.")
        return
    lines = [f"{c.get('category_id','')}\t{c.get('name','')}" for c in data]
    (OUT_DIR / "categories.txt").write_text("\n".join(sorted(lines)), encoding="utf-8")
    print(f"  {len(lines)} Kategorien nach data/categories.txt geschrieben.")

    known = {c.get("category_id") for c in data}
    missing = [c for c in CATEGORIES if c not in known]
    if missing:
        print(f"  ACHTUNG — diese konfigurierten Kategorien existieren nicht: {missing}")


def fetch_category(cat):
    """Bis zu 250 Coins einer Kategorie. Ein Credit pro Aufruf."""
    url = (f"{CG_BASE}/coins/markets?vs_currency=usd&category={cat}"
           f"&order=market_cap_desc&per_page=250&page=1&sparkline=false"
           f"&price_change_percentage=7d,30d")
    data = get_json(url, headers=cg_headers())
    if data is None:
        print(f"  {cat}: keine Daten")
        return []
    print(f"  {cat}: {len(data)} Coins")
    return data


# ─────────────────────────────────────────────────────────────
#  LunarCrush
# ─────────────────────────────────────────────────────────────

def fetch_social():
    """Ein einziger Aufruf liefert Sozialmetriken für alle verfolgten Coins."""
    if not LC_KEY:
        print("Kein LunarCrush-Schlüssel gesetzt — Sozialdaten werden übersprungen.")
        return {}
    print("Hole Sozialdaten …")
    data = get_json(f"{LC_BASE}/public/coins/list/v2",
                    headers={"Authorization": f"Bearer {LC_KEY}"})
    if not data:
        print("  Keine Sozialdaten erhalten.")
        return {}
    rows = data.get("data", data if isinstance(data, list) else [])
    out = {}
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        out[sym] = {
            "galaxy":       r.get("galaxy_score"),
            "altrank":      r.get("alt_rank"),
            "sentiment":    r.get("sentiment"),
            "mentions":     r.get("social_volume_24h") or r.get("interactions_24h"),
            "contributors": r.get("social_contributors") or r.get("contributors_active"),
            "dominance":    r.get("social_dominance"),
        }
    print(f"  Sozialdaten für {len(out)} Symbole")
    return out


# ─────────────────────────────────────────────────────────────
#  Zusammenführen und filtern
# ─────────────────────────────────────────────────────────────

def band_of(cap):
    for name, lo, hi in BANDS:
        if lo <= cap < hi:
            return name
    return None


def build(raw_by_cat, social):
    """Dedupliziert, filtert, reichert an."""
    merged = {}
    for cat, rows in raw_by_cat.items():
        for r in rows:
            cid = r.get("id")
            if not cid:
                continue
            if cid in merged:
                merged[cid]["categories"].append(cat)
                continue
            merged[cid] = {**r, "categories": [cat]}

    out, skipped = [], {"cap": 0, "volume": 0, "excluded": 0}
    for r in merged.values():
        cap = r.get("market_cap") or 0
        vol = r.get("total_volume") or 0

        band = band_of(cap)
        if band is None:
            skipped["cap"] += 1
            continue
        if vol < MIN_VOLUME:
            skipped["volume"] += 1
            continue

        blob = f"{r.get('id','')} {r.get('name','')} {' '.join(r['categories'])}".lower()
        if any(h in blob for h in EXCLUDE_HINTS):
            skipped["excluded"] += 1
            continue

        sym = (r.get("symbol") or "").upper()
        soc = social.get(sym, {})

        out.append({
            "id":         r.get("id"),
            "symbol":     sym,
            "name":       r.get("name"),
            "band":       band,
            "categories": sorted(set(r["categories"])),
            "cap":        round(cap),
            "price":      r.get("current_price"),
            "volume24h":  round(vol),
            "vol_to_cap": round(vol / cap, 4) if cap else None,
            "chg24h":     r.get("price_change_percentage_24h"),
            "chg7d":      r.get("price_change_percentage_7d_in_currency"),
            "chg30d":     r.get("price_change_percentage_30d_in_currency"),
            "ath_chg":    r.get("ath_change_percentage"),
            "supply_pct": (round(r["circulating_supply"] / r["total_supply"] * 100, 1)
                           if r.get("circulating_supply") and r.get("total_supply") else None),
            "social":     soc or None,
        })

    out.sort(key=lambda x: -x["cap"])
    print(f"\nGefiltert: {len(out)} behalten · {skipped['cap']} außerhalb der Bänder "
          f"· {skipped['volume']} zu illiquide · {skipped['excluded']} ausgeschlossen")
    return out


# ─────────────────────────────────────────────────────────────
#  Änderungsprotokoll — der Ticker
# ─────────────────────────────────────────────────────────────

def diff(old, new):
    o = {r["id"]: r for r in old}
    n = {r["id"]: r for r in new}
    ch = {"neu": [], "raus": [], "cap": [], "social": [], "band": []}

    for cid, r in n.items():
        if cid not in o:
            ch["neu"].append({"symbol": r["symbol"], "name": r["name"],
                              "cap": r["cap"], "band": r["band"],
                              "categories": r["categories"]})
            continue
        p = o[cid]

        if p.get("cap"):
            d = (r["cap"] - p["cap"]) / p["cap"] * 100
            if abs(d) >= 25:
                ch["cap"].append({"symbol": r["symbol"], "von": p["cap"],
                                  "nach": r["cap"], "pct": round(d, 1)})

        if p.get("band") != r.get("band"):
            ch["band"].append({"symbol": r["symbol"], "von": p.get("band"), "nach": r["band"]})

        ps, rs = (p.get("social") or {}), (r.get("social") or {})
        if ps.get("galaxy") is not None and rs.get("galaxy") is not None:
            d = rs["galaxy"] - ps["galaxy"]
            if abs(d) >= 15:
                ch["social"].append({"symbol": r["symbol"], "metrik": "galaxy",
                                     "von": ps["galaxy"], "nach": rs["galaxy"],
                                     "delta": round(d, 1)})

    for cid, r in o.items():
        if cid not in n:
            ch["raus"].append({"symbol": r["symbol"], "name": r["name"], "cap": r.get("cap")})

    return ch


# ─────────────────────────────────────────────────────────────
#  Hauptlauf
# ─────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(exist_ok=True)
    print(f"Lauf {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"CoinGecko-Schlüssel: {'gesetzt' if CG_KEY else 'FEHLT'} · "
          f"LunarCrush-Schlüssel: {'gesetzt' if LC_KEY else 'fehlt'}\n")

    if not CG_KEY:
        print("Ohne CoinGecko-Schlüssel geht nichts. Abbruch.")
        sys.exit(1)

    if not (OUT_DIR / "categories.txt").exists():
        dump_categories()

    print("\nHole Kategorien …")
    raw = {}
    for cat in CATEGORIES:
        raw[cat] = fetch_category(cat)
        time.sleep(1.5)                    # Freistufe: 100 Anfragen pro Minute

    if not any(raw.values()):
        print("\nKeine einzige Kategorie geliefert — vorhandene Daten bleiben unangetastet.")
        sys.exit(1)

    social = fetch_social()
    rows = build(raw, social)

    # Schutz: nie einen guten Stand mit einem schlechten überschreiben
    prev_path = OUT_DIR / "latest.json"
    prev = []
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8")).get("tokens", [])
        except Exception:
            pass
    if prev and len(rows) < len(prev) * 0.5:
        print(f"\nNur {len(rows)} statt zuvor {len(prev)} Werte — sieht nach Fehler aus. "
              f"Datei bleibt unverändert.")
        sys.exit(1)

    changes = diff(prev, rows)

    by_band = {}
    for name, _, _ in BANDS:
        by_band[name] = sum(1 for r in rows if r["band"] == name)

    payload = {
        "generated":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count":      len(rows),
        "by_band":    by_band,
        "categories": CATEGORIES,
        "bands":      {n: {"min": lo, "max": hi} for n, lo, hi in BANDS},
        "with_social": sum(1 for r in rows if r.get("social")),
        "tokens":     rows,
    }
    prev_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    (OUT_DIR / "changes.json").write_text(json.dumps({
        "generated": payload["generated"],
        "summary": {k: len(v) for k, v in changes.items()},
        **changes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nGeschrieben: {len(rows)} Token")
    for n, c in by_band.items():
        print(f"  {n} Mio.: {c}")
    print(f"  mit Sozialdaten: {payload['with_social']}")
    print(f"Änderungen: " + " · ".join(f"{k} {len(v)}" for k, v in changes.items()))


if __name__ == "__main__":
    main()
