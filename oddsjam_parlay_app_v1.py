
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Dict, Optional

import requests
import streamlit as st

API_BASE = "https://api.opticodds.com/api/v3"

# ------------- Odds helpers -------------
def american_to_decimal(a: float | int | None) -> Optional[float]:
    if a is None:
        return None
    a = float(a)
    if a > 0:
        return 1.0 + a/100.0
    elif a < 0:
        return 1.0 - 100.0/a
    return None

def decimal_to_american(d: float | None) -> Optional[int]:
    if not d or d <= 1.0:
        return None
    return round((d-1.0)*100.0) if d >= 2.0 else round(-100.0/(d-1.0))

def implied_prob_from_american(a: float | int | None) -> Optional[float]:
    d = american_to_decimal(a)
    return (1.0/d) if d and d > 0 else None

def devig_two_way(p_a: float | None, p_b: float | None) -> tuple[Optional[float], Optional[float]]:
    if p_a is None or p_b is None: return (None, None)
    s = p_a + p_b
    if s <= 0: return (None, None)
    return (p_a/s, p_b/s)

def parse_boosted_odds(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s: return None
    try:
        if s.startswith(('+','-')) or (s.isdigit() and abs(int(s)) >= 100):
            return american_to_decimal(float(s))
        val = float(s)
        if val >= 100:  # user typed 450 meaning +450
            return american_to_decimal(val)
        return val
    except Exception:
        return None

def kelly_fraction(p: float, dec_odds: float) -> Optional[float]:
    if p is None or dec_odds is None or dec_odds <= 1.0:
        return None
    b = dec_odds - 1.0
    q = 1.0 - p
    return (b*p - q) / b

# ------------- API helpers -------------
def api_headers() -> Dict[str, str]:
    key = st.secrets.get("ODDSJAM_API_KEY") or st.secrets.get("OPTICODDS_API_KEY")
    if not key:
        st.error("Missing API key. Add ODDSJAM_API_KEY to your Streamlit secrets.")
        st.stop()
    return {"X-Api-Key": key}

@st.cache_data(ttl=300, show_spinner=False)
def list_leagues() -> List[Dict]:
    r = requests.get(f"{API_BASE}/leagues?sport=baseball", headers=api_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])

@st.cache_data(ttl=120, show_spinner=False)
def list_active_fixtures(league_id: str) -> List[Dict]:
    # We only need upcoming/active fixtures
    r = requests.get(f"{API_BASE}/fixtures/active?league={league_id}", headers=api_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])

@st.cache_data(ttl=60, show_spinner=False)
def get_fixture_odds_moneyline(fixture_id: str, sportsbook: str = "Pinnacle") -> Dict[str, int | None]:
    # Odds endpoint supports: sportsbook (name or id), fixture_id, market
    url = f"{API_BASE}/fixtures/odds"
    params = {
        "fixture_id": fixture_id,
        "sportsbook": sportsbook,
        "market": "Moneyline",
        "odds_format": "AMERICAN"
    }
    r = requests.get(url, params=params, headers=api_headers(), timeout=20)
    r.raise_for_status()
    js = r.json()
    # Find home/away prices from response
    home = None; away = None; home_name=None; away_name=None
    if js and js.get("data"):
        entry = js["data"][0]
        home_name = entry.get("home_team_display")
        away_name = entry.get("away_team_display")
        for odd in entry.get("odds", []):
            if (odd.get("market") or "").lower() == "moneyline" and odd.get("sportsbook") == sportsbook:
                # The selection matches a team
                sel = (odd.get("selection") or "").strip()
                price = odd.get("price")
                if sel == home_name:
                    home = price
                elif sel == away_name:
                    away = price
    return {"home": home, "away": away, "home_name": home_name, "away_name": away_name}

# ------------- UI -------------
st.set_page_config(page_title="MLB Parlay (OddsJam) — Fair Odds + Kelly", page_icon="⚾")
st.title("⚾ MLB Parlay — Fair Odds + Kelly (via OddsJam API)")

with st.expander("Setup (first time)", expanded=False):
    st.markdown("""
1. In Streamlit Cloud → **Settings → Secrets**, add a line like:
```
ODDSJAM_API_KEY = "your-oddsjam-or-opticodds-api-key"
```
2. Re-run the app.
    """)

# Select MLB league
leagues = list_leagues()
mlb = next((l for l in leagues if (l.get("id") == "mlb") or ("major league baseball" in (l.get("name","").lower()))), None)
league_names = [f"{l.get('name')} (id={l.get('id')})" for l in leagues]
idx = league_names.index(f"{mlb.get('name')} (id={mlb.get('id')})") if mlb else 0
chosen = st.selectbox("League", league_names, index=idx if idx is not None and idx >= 0 and idx < len(league_names) else 0)
league_id = chosen.split("id=")[1].split(")")[0]

fixtures = list_active_fixtures(league_id)
if not fixtures:
    st.warning("No active fixtures returned for this league right now. If this seems wrong, try again in a minute or double-check your API key/plan.")
    st.stop()

# Build a list of team picks
options = []
fixture_lookup = {}
for fx in fixtures:
    fid = fx.get("id")
    home = fx.get("home_team_display")
    away = fx.get("away_team_display")
    start = fx.get("start_date")
    label_home = f"{home} vs {away} — pick {home} (home) — {start} — id:{fid}::home"
    label_away = f"{home} vs {away} — pick {away} (away) — {start} — id:{fid}::away"
    options.append(label_home)
    options.append(label_away)
    fixture_lookup[label_home] = (fid, "home")
    fixture_lookup[label_away] = (fid, "away")

picks = st.multiselect("Choose ML legs (you can pick Home or Away from each matchup)", options=options)

boosted_input = st.text_input("Boosted final odds (e.g., +450 or 5.50)", value="+450")

if st.button("Calculate FAIR value & Kelly"):
    if not picks:
        st.warning("Pick at least one leg.")
        st.stop()

    legs = []
    parlay_prob = 1.0
    parlay_dec = 1.0

    for pick in picks:
        fid, side = fixture_lookup[pick]
        prices = get_fixture_odds_moneyline(fid, "Pinnacle")
        if not prices["home"] or not prices["away"]:
            st.error(f"Couldn't get Pinnacle ML for fixture {fid}. Try another fixture or sportsbook.")
            st.stop()
        p_home = implied_prob_from_american(prices["home"])
        p_away = implied_prob_from_american(prices["away"])
        f_home, f_away = devig_two_way(p_home, p_away)
        fair_prob = f_home if side == "home" else f_away
        fair_dec = 1.0 / fair_prob if fair_prob else None
        legs.append({
            "fixture": fid,
            "home": prices["home_name"],
            "away": prices["away_name"],
            "price_home": prices["home"],
            "price_away": prices["away"],
            "side": side,
            "fair_prob": fair_prob,
            "fair_dec": fair_dec,
            "fair_amer": decimal_to_american(fair_dec) if fair_dec else None
        })
        parlay_prob *= fair_prob
        parlay_dec *= fair_dec

    st.subheader("Legs")
    for lg in legs:
        team = lg["home"] if lg["side"]=="home" else lg["away"]
        opp  = lg["away"] if lg["side"]=="home" else lg["home"]
        st.markdown(
            f"- **{team}** vs **{opp}** ({lg['side']}) — Pinnacle ML: "
            f"Home {lg['price_home']:+}, Away {lg['price_away']:+} → "
            f"**Fair prob** {lg['fair_prob']:.3f} (**Fair dec** {lg['fair_dec']:.3f}, **Fair Amer** {lg['fair_amer']:+})"
        )

    st.divider()
    st.subheader("Parlay FAIR value")
    st.metric("Fair decimal odds", f"{parlay_dec:.3f}", delta=f"Fair American {decimal_to_american(parlay_dec):+}")
    st.caption(f"Fair parlay probability ≈ {parlay_prob:.4f}")

    offered_dec = parse_boosted_odds(boosted_input)
    if not offered_dec:
        st.error("Couldn't parse boosted odds. Enter like +450 or 5.50")
        st.stop()

    k_full = kelly_fraction(parlay_prob, offered_dec)
    if k_full is None:
        st.error("Kelly fraction could not be computed (check boosted odds).")
        st.stop()

    k_quarter = max(0.0, k_full * 0.25)
    b = offered_dec - 1.0
    ev_per_dollar = parlay_prob * b - (1.0 - parlay_prob)

    st.divider()
    st.subheader("Kelly staking")
    st.write(f"**Offered (boosted) decimal odds:** {offered_dec:.3f} (American {decimal_to_american(offered_dec):+})")
    st.write(f"**Fair win probability (parlay):** {parlay_prob:.4%}")
    st.write(f"**Full Kelly fraction:** {k_full:.3%}")
    st.success(f"**Quarter‑Kelly stake:** {k_quarter:.3%} of bankroll")
    st.caption(f"Expected value per $1 staked at offered odds: {ev_per_dollar:.4f}")
