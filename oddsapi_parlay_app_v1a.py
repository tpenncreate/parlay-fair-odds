
import json
import re
import unicodedata
import requests
import streamlit as st

API_BASE = "https://api.the-odds-api.com/v4"

# ---------------- Core helpers ----------------
def american_to_decimal(a):
    if a is None: return None
    a = float(a)
    return 1.0 + a/100.0 if a > 0 else 1.0 - 100.0/a

def decimal_to_american(d):
    if not d or d <= 1.0: return None
    return round((d-1.0)*100.0) if d >= 2.0 else round(-100.0/(d-1.0))

def implied_prob_from_american(a):
    d = american_to_decimal(a)
    return (1.0/d) if d and d > 0 else None

def devig_two_way(p_a, p_b):
    if p_a is None or p_b is None: return (None, None)
    s = p_a + p_b
    if s <= 0: return (None, None)
    return (p_a/s, p_b/s)

def parse_boosted_odds(s):
    s = (s or "").strip()
    if not s: return None
    try:
        if s.startswith(('+','-')) or (s.isdigit() and abs(int(s)) >= 100):
            return american_to_decimal(float(s))
        val = float(s)
        if val >= 100:  # user meant +450
            return american_to_decimal(val)
        return val
    except Exception:
        return None

def kelly_fraction(p, dec_odds):
    if p is None or dec_odds is None or dec_odds <= 1.0: return None
    b = dec_odds - 1.0
    q = 1.0 - p
    return (b*p - q) / b

def resolve_api_key(user_key_input: str | None):
    if user_key_input and user_key_input.strip():
        return user_key_input.strip()
    return st.secrets.get("THE_ODDS_API_KEY") or st.secrets.get("ODDS_API_KEY")

def safe_get(url: str, params: dict):
    try:
        r = requests.get(url, params=params, timeout=25)
        if r.status_code != 200:
            try:
                body = r.json()
            except Exception:
                body = r.text[:500]
            return None, {"status": r.status_code, "body": body}
        return r.json(), None
    except requests.RequestException as e:
        return None, {"error": str(e)}

# -------------- Name matching helpers --------------
def normalize_name(s: str):
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def token_set(s: str):
    toks = normalize_name(s).split()
    # drop very short/common tokens
    stop = {"the","fc","cf","sc","ac","club","mlb","baseball"}
    return {t for t in toks if len(t) >= 2 and t not in stop}

def token_overlap(a: str, b: str):
    ta = token_set(a); tb = token_set(b)
    if not ta or not tb: return 0.0
    inter = len(ta & tb)
    return inter / max(len(ta), len(tb))

def map_outcomes_to_home_away(home_team: str, away_team: str, outcomes: list):
    """
    Try to map bookmaker outcome names to home/away using token overlap.
    Returns dict {"home": price, "away": price} if both found, else {}
    """
    if not outcomes or len(outcomes) < 2:
        return {}
    # Build scores for each outcome against home and away names
    scores = []
    for o in outcomes:
        name = o.get("name","")
        price = o.get("price")
        s_home = token_overlap(name, home_team)
        s_away = token_overlap(name, away_team)
        scores.append((s_home, s_away, name, price))
    # Choose best for home and best for away (must be distinct outcomes)
    home_choice = max(scores, key=lambda t: t[0])
    away_choice = max(scores, key=lambda t: t[1])
    if home_choice[2] == away_choice[2]:
        # If they tie to the same outcome, pick the second-best for away
        sorted_by_away = sorted(scores, key=lambda t: t[1], reverse=True)
        for cand in sorted_by_away:
            if cand[2] != home_choice[2]:
                away_choice = cand
                break
    # Require some minimal overlap to avoid mismaps
    if home_choice[0] <= 0 and away_choice[1] <= 0:
        return {}
    return {"home": home_choice[3], "away": away_choice[3]}

# ---------------- UI ----------------
st.set_page_config(page_title="MLB Parlay — The Odds API (robust ML)", page_icon="⚾")
st.title("⚾ MLB Parlay — Fair Odds + Kelly (The Odds API)")

with st.expander("Setup (first time)", expanded=True):
    user_key = st.text_input("The Odds API key", type="password", help="Paste your API key here to test quickly. Or add THE_ODDS_API_KEY in Secrets.")
    st.caption("Get a key at https://the-odds-api.com — free tier available.")

col1, col2, col3 = st.columns([1,1,1])
with col1:
    regions = st.multiselect("Regions", ["us","us2","uk","eu","au"], default=["us","us2"], help="Which bookmakers' regions to include.")
with col2:
    bookmakers = st.multiselect("Bookmakers filter (optional)", ["Pinnacle","FanDuel","DraftKings","BetMGM","Caesars","PointsBet","bet365"], default=["Pinnacle"], help="If empty, the API returns all books in these regions.")
with col3:
    odds_format = st.selectbox("Odds format", ["american","decimal"], index=0)

show_debug = st.checkbox("Show debug info", value=False)

# Test button
if st.button("Test MLB odds endpoint"):
    key = resolve_api_key(user_key)
    if not key:
        st.error("No API key found. Enter it above or add THE_ODDS_API_KEY in Secrets.")
    else:
        params = {
            "apiKey": key,
            "regions": ",".join(regions) if regions else "us",
            "markets": "h2h",
            "oddsFormat": odds_format,
            "bookmakers": ",".join(bookmakers) if bookmakers else None
        }
        data, err = safe_get(f"{API_BASE}/sports/baseball_mlb/odds", {k:v for k,v in params.items() if v})
        if err:
            st.error(f"MLB odds error: {err}")
        else:
            st.success(f"✅ MLB odds accessible (events: {len(data)})")
            if show_debug:
                st.json((data or [])[:2])

st.divider()
st.subheader("Build your moneyline parlay")

key = resolve_api_key(user_key)
if not key:
    st.warning("Enter your API key above to continue.")
    st.stop()

# Pull MLB h2h odds
params = {
    "apiKey": key,
    "regions": ",".join(regions) if regions else "us",
    "markets": "h2h",
    "oddsFormat": odds_format,
    "bookmakers": ",".join(bookmakers) if bookmakers else None
}
data, err = safe_get(f"{API_BASE}/sports/baseball_mlb/odds", {k:v for k,v in params.items() if v})
if err or not data:
    st.error(f"MLB odds error: {err}")
    st.stop()

# Build selectable options
options = []
lookup = {}
skipped = []

for ev in data:
    eid = ev.get("id")
    home = ev.get("home_team")
    away = ev.get("away_team")
    start = ev.get("commence_time")

    # Find a bookmaker market with two outcomes; try to map to home/away
    mapped = {}
    chosen_bk = None
    for bk in ev.get("bookmakers", []):
        bkname = bk.get("title")
        if bookmakers and bkname not in bookmakers:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != "h2h": 
                continue
            outs = mk.get("outcomes", [])
            if len(outs) < 2: 
                continue
            if odds_format == "decimal":
                # outcomes price field may be decimal already; use as-is
                m = map_outcomes_to_home_away(home, away, outs)
            else:
                m = map_outcomes_to_home_away(home, away, outs)
            if "home" in m and "away" in m and m["home"] is not None and m["away"] is not None:
                mapped = m
                chosen_bk = bkname
                break
        if mapped: 
            break

    if not mapped:
        skipped.append({"event": f"{home} vs {away}", "reason": "Couldn't map bookmaker outcome names to home/away", "available_books": [b.get('title') for b in ev.get('bookmakers',[])]})
        continue

    label_home = f"{home} vs {away} — pick {home} (home) — {start} — {chosen_bk} — id:{eid}::home"
    label_away = f"{home} vs {away} — pick {away} (away) — {start} — {chosen_bk} — id:{eid}::away"
    options.append(label_home); lookup[label_home] = (eid, "home", mapped["home"], mapped["away"], home, away)
    options.append(label_away); lookup[label_away] = (eid, "away", mapped["home"], mapped["away"], home, away)

if show_debug:
    st.write(f"Events: {len(data)}, selectable options: {len(options)}")
    if skipped:
        st.warning("Some events were skipped due to name mapping. Here are a few examples:")
        st.json(skipped[:5])

if not options:
    st.error("No selectable MLB moneyline legs were built. Try unchecking the bookmaker filter (so it can use any book), or add more regions like 'uk' or 'eu'. Also try enabling 'Show debug info' to see why events were skipped.")
    st.stop()

picks = st.multiselect("Choose ML legs", options=options)
boosted_input = st.text_input("Boosted final odds (e.g., +450 or 5.50)", value="+450")

if st.button("Calculate FAIR value & Kelly"):
    if not picks:
        st.warning("Pick at least one leg.")
        st.stop()

    legs = []
    parlay_prob = 1.0
    parlay_dec = 1.0

    for pick in picks:
        eid, side, home_line, away_line, home, away = lookup[pick]
        if odds_format == "decimal":
            p_home = 1.0 / float(home_line)
            p_away = 1.0 / float(away_line)
        else:
            p_home = implied_prob_from_american(home_line)
            p_away = implied_prob_from_american(away_line)

        f_home, f_away = devig_two_way(p_home, p_away)
        fair_prob = f_home if side == "home" else f_away
        fair_dec = 1.0 / fair_prob if fair_prob else None
        legs.append({
            "event": eid,
            "home": home,
            "away": away,
            "price_home": home_line,
            "price_away": away_line,
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
        if odds_format == "decimal":
            line_str = f"Home {float(lg['price_home']):.2f}, Away {float(lg['price_away']):.2f} (decimal)"
        else:
            line_str = f"Home {lg['price_home']:+}, Away {lg['price_away']:+} (american)"
        st.markdown(
            f"- **{team}** vs **{opp}** ({lg['side']}) — {line_str} → "
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
