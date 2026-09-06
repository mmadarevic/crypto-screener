#!/usr/bin/env python3
"""
Krypto-Screener v2

Drei Datenquellen:
  1 CoinGecko Märkte   — ein Aufruf je Kategorie, liefert Kurs, Cap, FDV, Historie
  2 CoinGecko Details  — nur für die gefilterten Werte: Entwickleraktivität, Links
  3 DefiLlama          — Protokollgebühren und -umsätze, frei und ohne Schlüssel
  4 LunarCrush         — optional, Sozialmetriken

Erzeugt:
  data/latest.json    — aktueller Stand
  data/changes.json   — Änderungen seit dem letzten Lauf
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
LC_BASE = "https://lunarcrush.com/api4"
DL_BASE = "https://api.llama.fi"          # frei, kein Schlüssel

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

EXCLUDE_HINTS = ["meme", "dog", "cat", "frog", "elon", "celebrity", "gambling", "casino", "parody"]

BANDS = [
    ("5-50",    5_000_000,   50_000_000),
    ("50-150",  50_000_000,  150_000_000),
    ("150-500", 150_000_000, 500_000_000),
]

MIN_VOLUME = 100_000

# Für welche Werte lohnt der teure Detailabruf?
# Ein Credit je Token — deshalb nur die, die den Grobfilter überstehen.
DETAIL_MIN_VOL_TO_CAP = 0.02      # mindestens 2% Tagesumschlag
DETAIL_MAX_TOKENS     = 60        # Obergrenze, schützt das Credit-Budget

OUT_DIR = Path("data")

# ─────────────────────────────────────────────────────────────
#  HTTP
# ─────────────────────────────────────────────────────────────

def get_json(url, headers=None, tries=3, pause=2.0, quiet=False):
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if not quiet:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:160]
                except Exception:
                    pass
                print(f"  HTTP {e.code} ({attempt}/{tries}): {body}")
            if e.code == 429:
                time.sleep(pause * attempt * 3)
                continue
            if e.code in (401, 403, 404):
                return None
        except Exception as e:
            if not quiet:
                print(f"  Fehler ({attempt}/{tries}): {e}")
        if attempt < tries:
            time.sleep(pause * attempt)
    return None


def cg_headers():
    h = {"accept": "application/json"}
    if CG_KEY:
        h["x-cg-demo-api-key"] = CG_KEY
    return h


# ─────────────────────────────────────────────────────────────
#  1 — CoinGecko Märkte
# ─────────────────────────────────────────────────────────────

def dump_categories():
    print("Hole Kategorieliste …")
    data = get_json(f"{CG_BASE}/coins/categories/list", headers=cg_headers())
    if not data:
        return
    lines = [f"{c.get('category_id','')}\t{c.get('name','')}" for c in data]
    (OUT_DIR / "categories.txt").write_text("\n".join(sorted(lines)), encoding="utf-8")
    print(f"  {len(lines)} Kategorien gespeichert.")
    known = {c.get("category_id") for c in data}
    missing = [c for c in CATEGORIES if c not in known]
    if missing:
        print(f"  ACHTUNG — unbekannte Kategorien konfiguriert: {missing}")


def fetch_category(cat):
    url = (f"{CG_BASE}/coins/markets?vs_currency=usd&category={cat}"
           f"&order=market_cap_desc&per_page=250&page=1&sparkline=false"
           f"&price_change_percentage=7d,30d,1y")
    data = get_json(url, headers=cg_headers())
    if data is None:
        print(f"  {cat}: keine Daten")
        return []
    print(f"  {cat}: {len(data)}")
    return data


# ─────────────────────────────────────────────────────────────
#  2 — CoinGecko Details (nur gefilterte Werte)
# ─────────────────────────────────────────────────────────────

def fetch_detail(coin_id):
    """Ein Credit. Liefert Entwickleraktivität, Links und Beobachtungslisten."""
    url = (f"{CG_BASE}/coins/{coin_id}?localization=false&tickers=false"
           f"&market_data=false&community_data=true&developer_data=true&sparkline=false")
    d = get_json(url, headers=cg_headers(), tries=2, quiet=True)
    if not d:
        return None

    dev = d.get("developer_data") or {}
    com = d.get("community_data") or {}
    lnk = d.get("links") or {}
    repos = (lnk.get("repos_url") or {}).get("github") or []

    return {
        "genesis":       d.get("genesis_date"),
        "watchlist":     d.get("watchlist_portfolio_users"),
        "sentiment_up":  d.get("sentiment_votes_up_percentage"),
        "chains":        sorted((d.get("platforms") or {}).keys())[:6],
        # Entwickleraktivität — der beste Hinweis, ob noch jemand arbeitet
        "commits_4w":    dev.get("commit_count_4_weeks"),
        "stars":         dev.get("stars"),
        "forks":         dev.get("forks"),
        "contributors":  dev.get("pull_request_contributors"),
        "issues_open":   dev.get("total_issues"),
        "issues_closed": dev.get("closed_issues"),
        "has_repo":      bool(repos),
        # Kanäle
        "twitter":       lnk.get("twitter_screen_name") or None,
        "telegram":      lnk.get("telegram_channel_identifier") or None,
        "homepage":      next((u for u in (lnk.get("homepage") or []) if u), None),
        "tw_followers":  com.get("twitter_followers"),
        "tg_users":      com.get("telegram_channel_user_count"),
    }


# ─────────────────────────────────────────────────────────────
#  3 — DefiLlama: echte Protokollgebühren
# ─────────────────────────────────────────────────────────────

def fetch_fees():
    """Frei, kein Schlüssel. Ein Aufruf für alle Protokolle."""
    print("Hole Gebührendaten von DefiLlama …")
    out = {}
    for kind in ("fees", "revenue"):
        d = get_json(f"{DL_BASE}/overview/{kind}?excludeTotalDataChart=true"
                     f"&excludeTotalDataChartBreakdown=true", quiet=True)
        if not d:
            print(f"  {kind}: nichts erhalten")
            continue
        for p in d.get("protocols", []):
            sym = (p.get("symbol") or "").upper().lstrip("$")
            if not sym or sym == "-":
                continue
            e = out.setdefault(sym, {})
            e[f"{kind}_24h"] = p.get("total24h")
            e[f"{kind}_30d"] = p.get("total30d")
            e[f"{kind}_1y"]  = p.get("total1y")
            if p.get("name"):
                e["dl_name"] = p["name"]
        print(f"  {kind}: {len(d.get('protocols', []))} Protokolle")
    return out


# ─────────────────────────────────────────────────────────────
#  4 — LunarCrush (optional)
# ─────────────────────────────────────────────────────────────

def fetch_social():
    if not LC_KEY:
        print("Kein LunarCrush-Schlüssel — Sozialdaten übersprungen.")
        return {}
    print("Hole Sozialdaten …")
    d = get_json(f"{LC_BASE}/public/coins/list/v2",
                 headers={"Authorization": f"Bearer {LC_KEY}"})
    if not d:
        return {}
    rows = d.get("data", d if isinstance(d, list) else [])
    out = {}
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if sym:
            out[sym] = {
                "galaxy":       r.get("galaxy_score"),
                "altrank":      r.get("alt_rank"),
                "sentiment":    r.get("sentiment"),
                "mentions":     r.get("social_volume_24h") or r.get("interactions_24h"),
                "contributors": r.get("social_contributors") or r.get("contributors_active"),
                "dominance":    r.get("social_dominance"),
            }
    print(f"  {len(out)} Symbole")
    return out


# ─────────────────────────────────────────────────────────────
#  Zusammenführen
# ─────────────────────────────────────────────────────────────

def band_of(cap):
    for name, lo, hi in BANDS:
        if lo <= cap < hi:
            return name
    return None


def year_of(iso):
    try:
        return int(str(iso)[:4])
    except Exception:
        return None


def build(raw_by_cat, social, fees):
    merged = {}
    for cat, rows in raw_by_cat.items():
        for r in rows:
            cid = r.get("id")
            if not cid:
                continue
            if cid in merged:
                merged[cid]["categories"].append(cat)
            else:
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
        fdv = r.get("fully_diluted_valuation")
        circ, tot, mx = r.get("circulating_supply"), r.get("total_supply"), r.get("max_supply")

        out.append({
            "id":         r.get("id"),
            "symbol":     sym,
            "name":       r.get("name"),
            "band":       band,
            "rank":       r.get("market_cap_rank"),
            "categories": sorted(set(r["categories"])),

            "cap":        round(cap),
            "fdv":        round(fdv) if fdv else None,
            # Anteil der Cap an der voll verwässerten Bewertung.
            # 1.0 heißt: keine Verwässerung mehr. 0.2 heißt: 80% kommen noch.
            "cap_fdv":    round(cap / fdv, 3) if fdv else None,
            "price":      r.get("current_price"),
            "volume24h":  round(vol),
            "vol_to_cap": round(vol / cap, 4) if cap else None,

            "chg24h":     r.get("price_change_percentage_24h"),
            "chg7d":      r.get("price_change_percentage_7d_in_currency"),
            "chg30d":     r.get("price_change_percentage_30d_in_currency"),
            "chg1y":      r.get("price_change_percentage_1y_in_currency"),
            "ath_chg":    r.get("ath_change_percentage"),
            "ath_date":   r.get("ath_date"),
            "atl_date":   r.get("atl_date"),
            # Näherung für das Alter: ein 2026 gestarteter Token hat sein Tief in 2026
            "first_seen": year_of(r.get("atl_date")),

            "supply_pct": round(circ / tot * 100, 1) if circ and tot else None,
            "capped":     bool(mx),

            "fees":       fees.get(sym) or None,
            "social":     social.get(sym) or None,
            "detail":     None,          # wird in Stufe zwei gefüllt
        })

    out.sort(key=lambda x: -x["cap"])
    print(f"\nGefiltert: {len(out)} behalten · {skipped['cap']} außerhalb der Bänder "
          f"· {skipped['volume']} zu illiquide · {skipped['excluded']} ausgeschlossen")
    return out


def enrich(rows):
    """Stufe zwei: Detailabruf für die Werte mit echtem Handelsinteresse."""
    cand = [r for r in rows
            if (r.get("vol_to_cap") or 0) >= DETAIL_MIN_VOL_TO_CAP]
    cand.sort(key=lambda r: -(r.get("vol_to_cap") or 0))
    cand = cand[:DETAIL_MAX_TOKENS]

    if not cand:
        return 0
    print(f"\nDetailabruf für {len(cand)} Werte (je ein Credit) …")
    ok = 0
    for i, r in enumerate(cand, 1):
        d = fetch_detail(r["id"])
        if d:
            r["detail"] = d
            ok += 1
        if i % 15 == 0:
            print(f"  {i}/{len(cand)}")
        time.sleep(1.5)
    print(f"  {ok} von {len(cand)} erfolgreich")
    return ok


# ─────────────────────────────────────────────────────────────
#  Änderungsprotokoll
# ─────────────────────────────────────────────────────────────

def diff(old, new):
    o = {r["id"]: r for r in old}
    n = {r["id"]: r for r in new}
    ch = {"neu": [], "raus": [], "cap": [], "band": [], "social": [], "gebuehren": []}

    for cid, r in n.items():
        if cid not in o:
            ch["neu"].append({"symbol": r["symbol"], "name": r["name"], "cap": r["cap"],
                              "band": r["band"], "jahr": r.get("first_seen"),
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
                ch["social"].append({"symbol": r["symbol"], "von": ps["galaxy"],
                                     "nach": rs["galaxy"], "delta": round(d, 1)})

        pf, rf = (p.get("fees") or {}), (r.get("fees") or {})
        a, b = pf.get("fees_30d"), rf.get("fees_30d")
        if a and b and a > 0:
            d = (b - a) / a * 100
            if abs(d) >= 40:
                ch["gebuehren"].append({"symbol": r["symbol"], "von": round(a),
                                        "nach": round(b), "pct": round(d, 1)})

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
    print(f"CoinGecko: {'ok' if CG_KEY else 'FEHLT'} · LunarCrush: {'ok' if LC_KEY else 'fehlt'}\n")

    if not CG_KEY:
        print("Ohne CoinGecko-Schlüssel geht nichts.")
        sys.exit(1)

    if not (OUT_DIR / "categories.txt").exists():
        dump_categories()

    print("\nStufe 1 — Kategorien:")
    raw = {}
    for cat in CATEGORIES:
        raw[cat] = fetch_category(cat)
        time.sleep(1.5)

    if not any(raw.values()):
        print("\nKeine Kategorie geliefert — Datei bleibt unverändert.")
        sys.exit(1)

    fees   = fetch_fees()
    social = fetch_social()
    rows   = build(raw, social, fees)

    enriched = enrich(rows)

    prev_path = OUT_DIR / "latest.json"
    prev = []
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8")).get("tokens", [])
        except Exception:
            pass
    if prev and len(rows) < len(prev) * 0.5:
        print(f"\nNur {len(rows)} statt {len(prev)} Werte — sieht nach Fehler aus. Abbruch.")
        sys.exit(1)

    changes = diff(prev, rows)

    payload = {
        "generated":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count":        len(rows),
        "by_band":      {n: sum(1 for r in rows if r["band"] == n) for n, _, _ in BANDS},
        "categories":   CATEGORIES,
        "bands":        {n: {"min": lo, "max": hi} for n, lo, hi in BANDS},
        "with_social":  sum(1 for r in rows if r.get("social")),
        "with_fees":    sum(1 for r in rows if r.get("fees")),
        "with_detail":  enriched,
        "tokens":       rows,
    }
    prev_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    (OUT_DIR / "changes.json").write_text(json.dumps({
        "generated": payload["generated"],
        "summary": {k: len(v) for k, v in changes.items()},
        **changes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nGeschrieben: {len(rows)} Token")
    for n, c in payload["by_band"].items():
        print(f"  {n} Mio.: {c}")
    print(f"  mit Gebührendaten: {payload['with_fees']}")
    print(f"  mit Detaildaten:   {payload['with_detail']}")
    print(f"  mit Sozialdaten:   {payload['with_social']}")
    print("Änderungen: " + " · ".join(f"{k} {len(v)}" for k, v in changes.items()))


if __name__ == "__main__":
    main()
