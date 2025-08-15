
import re
import math
from functools import lru_cache

import requests
import streamlit as st

# ---------- Helpers ----------
def american_to_decimal(american: float) -> float:
    if american is None:
        return None
    a = float(american)
    if a > 0:
        return 1.0 + (a / 100.0)
    elif a < 0:
        return 1.0 - (100.0 / a)
    return None

def decimal_to_american(decimal_odds: float):
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100.0)
    return round(-100.0 / (decimal_odds - 1.0))

def implied_prob_from_american(american: float):
    dec = american_to_decimal(american)
    return (1.0 / dec) if dec and dec > 0 else None

def devig_two_way(p_home: float, p_away: float):
    if p_home is None or p_away is None:
        return None, None
    s = p_home + p_away
    if s <= 0:
        return None, None
    return p_home / s, p_away / s

def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()

def slug(s: str) -> str:
    return normalize(s).lower()

@lru_cache(maxsize=1)
def guest_key() -> str:
    cfg = requests.get("https://www.pinnacle.com/config/app.json", timeout=15).json()
    try:
        return cfg["api"]["haywire"]["apiKey"]
    except Exception:
        # fallback: best-effort search
        import collections
        def scan(x):
            if isinstance(x, dict):
                for v in x.values():
                    y = scan(v)
                    if y: return y
            elif isinstance(x, list):
                for v in x:
                    y = scan(v)
                    if y: return y
            elif isinstance(x, str) and len(x) >= 16:
                return x
        k = scan(cfg)
        if not k:
            raise RuntimeError("Couldn't find guest API key")
        return k

def headers():
    return {
        "X-API-Key": guest_key(),
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.pinnacle.com/"
    }

@st.cache_data(ttl=300)
def get_sports():
    return requests.get("https://guest.api.arcadia.pinnacle.com/0.1/sports", headers=headers(), timeout=15).json()

@st.cache_data(ttl=300)
def get_leagues(sport_id: int):
    return requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{sport_id}/leagues?all=false", headers=headers(), timeout=15).json()

@st.cache_data(ttl=30)
def get_matchups(league_id: int):
    return requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/matchups", headers=headers(), timeout=15).json()

@st.cache_data(ttl=30)
def get_straight_markets(league_id: int):
    return requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/markets/straight", headers=headers(), timeout=15).json()

def build_team_index(matchups):
    idx = {}
    for m in matchups:
        mid = m.get("id")
        parts = [p for p in m.get("participants", []) if p.get("type") == "matchup"]
        if len(parts) < 2: 
            continue
        # Attempt to identify home/away
        home = next((p for p in parts if slug(p.get("alignment"))=="home"), parts[0])
        away = next((p for p in parts if slug(p.get("alignment"))=="away"), parts[1 if len(parts)>1 else 0])
        def add(p, opp, side):
            name = normalize(p.get("name",""))
            idx.setdefault(name, []).append(dict(
                matchupId=mid, side=side, team=name, opponent=normalize(opp.get("name",""))
            ))
        add(home, away, "home")
        add(away, home, "away")
    return idx

def get_ml_prices(markets, matchup_id):
    for mk in markets:
        if mk.get("type") == "moneyline" and mk.get("matchupId") == matchup_id:
            out = {}
            for pr in mk.get("prices", []):
                d = slug(pr.get("designation",""))
                if d in ("home","away"):
                    out[d] = pr.get("price")
            return out
    return {}

def compute_leg(selection, markets):
    prices = get_ml_prices(markets, selection["matchupId"])
    if not prices: 
        return None
    p_home = implied_prob_from_american(prices.get("home"))
    p_away = implied_prob_from_american(prices.get("away"))
    if p_home is None or p_away is None:
        return None
    f_home, f_away = devig_two_way(p_home, p_away)
    fair = f_home if selection["side"]=="home" else f_away
    return dict(
        **selection,
        price_home=prices.get("home"),
        price_away=prices.get("away"),
        selected_price=prices.get(selection["side"]),
        fair_prob=fair,
        fair_decimal=(1.0/fair) if fair else None,
        fair_american=decimal_to_american((1.0/fair) if fair else None)
    )

# ---------- UI ----------
st.set_page_config(page_title="Parlay Fair Odds (No‑Vig)", page_icon="🎯")
st.title("🎯 Parlay FAIR Value (no‑vig) — Picker Mode")
st.caption("Pick from currently listed teams to avoid name mismatches.")

sports = get_sports()
sport_names = [s["name"] for s in sports]
default_sport = next((i for i,n in enumerate(sport_names) if "baseball" in n.lower()), 0)
sport_i = st.selectbox("Sport", sport_names, index=default_sport)
sport_id = sports[sport_names.index(sport_i)]["id"]

leagues = get_leagues(sport_id)
league_names = [l["name"] for l in leagues]
default_league = next((i for i,n in enumerate(league_names) if "mlb" in n.lower()), 0) if league_names else 0
league_i = st.selectbox("League", league_names, index=default_league if league_names else 0)
league_id = leagues[league_names.index(league_i)]["id"] if leagues else None

if not league_id:
    st.stop()

matchups = get_matchups(league_id)
markets = get_straight_markets(league_id)

team_index = build_team_index(matchups)
all_team_names = sorted(team_index.keys())

if not all_team_names:
    st.warning("No current matchups found for this league (could be offseason or no games scheduled). Try a different league or sport.")
    st.stop()

st.markdown("**Available teams right now:**")
st.write(", ".join(all_team_names))

selected_teams = st.multiselect("Choose teams for your parlay (moneyline)", options=all_team_names, default=[])

if st.button("Calculate FAIR value"):

    if not selected_teams:
        st.warning("Pick at least one team.")
        st.stop()

    results = []
    for name in selected_teams:
        selection = team_index[name][0]  # choose the first listing by default
        res = compute_leg(selection, markets)
        if res:
            results.append(res)

    if not results:
        st.error("Couldn't locate moneyline prices for the selected teams.")
        st.stop()

    st.subheader("Individual legs")
    for r in results:
        st.markdown(
            f"- **{r['team']}** vs **{r['opponent']}** ({r['side']})  "
            f"ML: {r['selected_price']:+}  →  **Fair prob** {r['fair_prob']:.3f}  "
            f"(**Fair dec** {r['fair_decimal']:.3f}, **Fair Amer** {r['fair_american']:+})"
        )

    # Parlay multiplicative
    fair_prob_parlay = 1.0
    fair_dec_parlay = 1.0
    for r in results:
        fair_prob_parlay *= r["fair_prob"]
        fair_dec_parlay *= r["fair_decimal"]

    st.divider()
    st.subheader("Parlay FAIR value")
    st.metric("Fair decimal odds", f"{fair_dec_parlay:.3f}", delta=f"Fair American {decimal_to_american(fair_dec_parlay):+}")
    st.caption(f"Fair parlay probability ≈ {fair_prob_parlay:.4f}")
