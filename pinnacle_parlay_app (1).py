
import re
import math
import time
from functools import lru_cache

import requests
import streamlit as st

# ----------------------------
# Helpers
# ----------------------------
def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal odds."""
    if american is None:
        return None
    try:
        a = float(american)
    except Exception:
        return None
    if a > 0:
        return 1.0 + (a / 100.0)
    elif a < 0:
        return 1.0 - (100.0 / a)
    else:
        return None

def decimal_to_american(decimal_odds: float) -> float:
    """Convert decimal odds to American odds (rounded to nearest whole)."""
    if decimal_odds is None or decimal_odds <= 1.0:
        return None
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100.0)
    else:
        return round(-100.0 / (decimal_odds - 1.0))

def implied_prob_from_american(american: float) -> float:
    dec = american_to_decimal(american)
    return 1.0 / dec if dec and dec > 0 else None

def devig_two_way(p_home: float, p_away: float):
    """Remove the vig for a 2-way market by normalizing probabilities."""
    if p_home is None or p_away is None:
        return None, None
    s = p_home + p_away
    if s <= 0:
        return None, None
    return p_home / s, p_away / s

def normalize_name(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip().lower()

@lru_cache(maxsize=1)
def get_x_api_key() -> str:
    # Public config JSON used by the Pinnacle site; contains the guest API key
    # Example used widely online: json['api']['haywire']['apiKey']
    url = "https://www.pinnacle.com/config/app.json"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    # Known path; keep a fallback scan in case they move it around
    key = None
    try:
        key = data["api"]["haywire"]["apiKey"]
    except Exception:
        pass
    if not key:
        # fallback: scan for a likely-looking API key string
        def scan(obj):
            if isinstance(obj, dict):
                for k,v in obj.items():
                    res = scan(v)
                    if res:
                        return res
            elif isinstance(obj, list):
                for v in obj:
                    res = scan(v)
                    if res:
                        return res
            elif isinstance(obj, str):
                if len(obj) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", obj):
                    return obj
            return None
        key = scan(data)
    if not key:
        raise RuntimeError("Couldn't locate X-API-Key in app.json")
    return key

def make_headers():
    return {
        "X-API-Key": get_x_api_key(),
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.pinnacle.com/",
        "Accept": "application/json"
    }

def fetch_json(url: str):
    r = requests.get(url, headers=make_headers(), timeout=15)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=300)
def list_sports():
    # Known public endpoint observed by many devs
    url = "https://guest.api.arcadia.pinnacle.com/0.1/sports"
    return fetch_json(url)

@st.cache_data(ttl=300)
def list_leagues(sport_id: int):
    url = f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{sport_id}/leagues?all=false"
    return fetch_json(url)

@st.cache_data(ttl=30)
def get_league_matchups(league_id: int):
    url = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/matchups"
    return fetch_json(url)

@st.cache_data(ttl=30)
def get_league_straight_markets(league_id: int):
    url = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/markets/straight"
    return fetch_json(url)

def find_sport_id_by_name(name_substring: str):
    name_substring = normalize_name(name_substring)
    for s in list_sports():
        if name_substring in normalize_name(s.get("name","")):
            return s.get("id"), s.get("name")
    return None, None

def find_league_id_by_name(sport_id: int, name_substring: str):
    name_substring = normalize_name(name_substring)
    for lg in list_leagues(sport_id):
        if name_substring in normalize_name(lg.get("name","")):
            return lg.get("id"), lg.get("name")
    return None, None

def build_team_index(matchups_json):
    """
    Build a mapping of team name -> list of entries with matchupId, alignment ('home'/'away'), participantId, and opponent name.
    """
    idx = {}
    for m in matchups_json:
        mid = m.get("id")
        participants = m.get("participants", [])
        sides = [p for p in participants if p.get("type") == "matchup"]
        if len(sides) < 2:
            continue
        # identify home/away
        home = next((p for p in sides if normalize_name(p.get("alignment")) == "home"), None)
        away = next((p for p in sides if normalize_name(p.get("alignment")) == "away"), None)
        if not home or not away:
            # Sometimes alignment may be missing; fallback to first two entries
            home, away = sides[0], sides[1]
        def add_entry(team_p, opp_p, align):
            key = normalize_name(team_p.get("name",""))
            entry = {
                "matchupId": mid,
                "alignment": align,
                "participantId": team_p.get("id"),
                "team": team_p.get("name"),
                "opponent": opp_p.get("name"),
            }
            idx.setdefault(key, []).append(entry)
        add_entry(home, away, "home")
        add_entry(away, home, "away")
    return idx

def lookup_team(team_query: str, team_index: dict):
    q = normalize_name(team_query)
    # exact first
    if q in team_index:
        return team_index[q][0], team_index[q]  # chosen, options
    # partial fallback
    for k, entries in team_index.items():
        if q and q in k:
            return entries[0], entries
    return None, []

def extract_moneyline_prices(markets_json, matchup_id: int):
    """Return dict {'home': american_price, 'away': american_price} for a given matchup_id."""
    for m in markets_json:
        if m.get("type") != "moneyline":
            continue
        if m.get("matchupId") != matchup_id:
            continue
        prices = m.get("prices", [])
        out = {}
        for pr in prices:
            des = normalize_name(pr.get("designation",""))
            if des in ("home","away"):
                out[des] = pr.get("price")
        if out:
            return out
    return {}

def compute_fair_for_selection(selection_entry, markets_json):
    mid = selection_entry["matchupId"]
    side = selection_entry["alignment"]  # 'home' or 'away'
    prices = extract_moneyline_prices(markets_json, mid)
    if not prices or side not in prices:
        return None
    # implied probabilities from market (vig-included)
    p_home_raw = implied_prob_from_american(prices.get("home"))
    p_away_raw = implied_prob_from_american(prices.get("away"))
    if p_home_raw is None or p_away_raw is None:
        return None
    p_home_fair, p_away_fair = devig_two_way(p_home_raw, p_away_raw)
    fair_prob = p_home_fair if side == "home" else p_away_fair
    return {
        "matchupId": mid,
        "side": side,
        "team": selection_entry["team"],
        "opponent": selection_entry["opponent"],
        "price_home": prices.get("home"),
        "price_away": prices.get("away"),
        "selected_american": prices.get(side),
        "selected_decimal": american_to_decimal(prices.get(side)),
        "fair_prob": fair_prob,
        "fair_decimal": (1.0 / fair_prob) if fair_prob and fair_prob > 0 else None,
    }

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Pinnacle Parlay FAIR Value (No-Vig)", page_icon="🎯", layout="centered")
st.title("🎯 Parlay FAIR Value (no‑vig) from Pinnacle public JSON")
st.caption("Multiplicative method: strip the vig for each leg, then multiply fair probabilities.")

with st.expander("First time here? (click to expand)"):
    st.markdown("""
**What this does**
- Fetches Pinnacle's public (guest) JSON endpoints.
- Finds moneyline prices for your teams.
- Removes the vig in each 2‑way market.
- Multiplies the fair probabilities to get a no‑vig parlay price.

**Notes**
- This is **read‑only** and doesn't place bets.
- Endpoints and field names can change; if something breaks, try again later.
- Be respectful: keep request counts low.
""")

# Sport & league pickers (pre-filled to MLB)
sports_data = list_sports()
sport_names = [s.get("name") for s in sports_data]
default_sport = "Baseball"
sport_choice = st.selectbox("Sport", sport_names, index=(sport_names.index(default_sport) if default_sport in sport_names else 0))
sport_id, sport_resolved = find_sport_id_by_name(sport_choice)

leagues_data = list_leagues(sport_id) if sport_id else []
league_names = [l.get("name") for l in leagues_data]
# Try to default to MLB
default_league_idx = 0
for i, nm in enumerate(league_names):
    if "mlb" in normalize_name(nm) or "major league baseball" in normalize_name(nm):
        default_league_idx = i
        break
league_choice = st.selectbox("League", league_names, index=default_league_idx if league_names else 0)
league_id, league_resolved = (None, None)
if sport_id and league_choice:
    league_id, league_resolved = find_league_id_by_name(sport_id, league_choice)

st.divider()

example = "New York Mets, Arizona Diamondbacks, Philadelphia Phillies"
teams_text = st.text_area("Enter teams for your parlay (comma or newline separated)", value=example, height=100)
raw_items = [t.strip() for t in re.split(r'[,\n]+', teams_text) if t.strip()]
unique_items = []
seen = set()
for t in raw_items:
    key = t.lower()
    if key not in seen:
        unique_items.append(t)
        seen.add(key)

run = st.button("Calculate FAIR value")

if run:
    if not league_id:
        st.error("Couldn't resolve the league. Try selecting another sport or league.")
        st.stop()
    with st.spinner("Fetching odds..."):
        matchups = get_league_matchups(league_id)
        markets = get_league_straight_markets(league_id)

    team_index = build_team_index(matchups)
    results = []
    unmatched = []

    for q in unique_items:
        chosen, options = lookup_team(q, team_index)
        if not chosen:
            unmatched.append(q)
            continue
        res = compute_fair_for_selection(chosen, markets)
        if res is None:
            unmatched.append(q + " (odds not found)")
        else:
            res["query"] = q
            results.append(res)

    if unmatched:
        st.warning("Couldn't match these entries:\n- " + "\n- ".join(unmatched))

    if not results:
        st.stop()

    # Display individual legs
    st.subheader("Individual legs")
    for r in results:
        st.markdown(
            f"**{r['team']}** vs **{r['opponent']}**  "
            f"({r['side']}) — Market ML: {r['selected_american']:+}, "
            f"Implied: {implied_prob_from_american(r['selected_american']):.3f}, "
            f"**Fair prob**: {r['fair_prob']:.3f}  → **Fair decimal**: {r['fair_decimal']:.3f} "
            f"(Fair American: {decimal_to_american(r['fair_decimal']):+})"
        )

    # Compute parlay fair value (multiplicative)
    fair_prob_parlay = 1.0
    fair_decimal_parlay = 1.0
    for r in results:
        fair_prob_parlay *= r["fair_prob"]
        fair_decimal_parlay *= r["fair_decimal"]
    st.divider()
    st.subheader("Parlay FAIR value (multiplicative, no‑vig)")
    st.metric(
        label="Fair decimal odds",
        value=f"{fair_decimal_parlay:.3f}",
        delta=f"Fair American {decimal_to_american(fair_decimal_parlay):+}"
    )
    st.caption(f"Fair parlay probability ≈ {fair_prob_parlay:.4f}")

    st.info("Tip: Compare this to the book's posted parlay price to see the overround.")
