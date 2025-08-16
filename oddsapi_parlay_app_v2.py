
import json
import re
import unicodedata
import requests
import streamlit as st

API_BASE = "https://api.the-odds-api.com/v4"

SPORT_CHOICES = {
    "MLB (Baseball)": "baseball_mlb",
    "NBA (Basketball)": "basketball_nba",
    "NHL (Hockey)": "icehockey_nhl",
    "NFL (Football)": "americanfootball_nfl",
    "NCAAF (Football)": "americanfootball_ncaaf",
    "NCAAB (Basketball)": "basketball_ncaab",
    "EPL (Soccer)": "soccer_epl",
    "La Liga (Soccer)": "soccer_spain_la_liga",
    "Serie A (Soccer)": "soccer_italy_serie_a",
    "Bundesliga (Soccer)": "soccer_germany_bundesliga",
    "Ligue 1 (Soccer)": "soccer_france_ligue_one",
}

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
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def token_set(s: str):
    toks = normalize_name(s).split()
    stop = {"the","fc","cf","sc","ac","club","mlb","nba","nfl","nhl","ncaa","ncaab","ncaaf","soccer","basketball","football","hockey"}
    return {t for t in toks if len(t) >= 2 and t not in stop}

def token_overlap(a: str, b: str):
    ta = token_set(a); tb = token_set(b)
    if not ta or not tb: return 0.0
    inter = len(ta & tb)
    return inter / max(len(ta), len(tb))

def map_outcomes_to_home_away(home_team: str, away_team: str, outcomes: list):
    """Map bookmaker outcome names to home/away via token overlap; return dict with 'home' and 'away' prices if found."""
    if not outcomes or len(outcomes) < 2:
        return {}
    scores = []
    for o in outcomes:
        name = o.get("name","")
        price = o.get("price")
        s_home = token_overlap(name, home_team)
        s_away = token_overlap(name, away_team)
        scores.append((s_home, s_away, name, price))
    home_choice = max(scores, key=lambda t: t[0])
    away_choice = max(scores, key=lambda t: t[1])
    if home_choice[2] == away_choice[2]:
        sorted_by_away = sorted(scores, key=lambda t: t[1], reverse=True)
        for cand in sorted_by_away:
            if cand[2] != home_choice[2]:
                away_choice = cand
                break
    if home_choice[0] <= 0 and away_choice[1] <= 0:
        return {}
    return {"home": home_choice[3], "away": away_choice[3]}

# ---------------- UI ----------------
st.set_page_config(page_title="Parlay Fair Odds + Kelly (Multi-sport)", page_icon="🎯")
st.title("🎯 Parlay — Fair Odds + Kelly (Multi‑Sport via The Odds API)")

with st.expander("Setup", expanded=True):
    user_key = st.text_input("The Odds API key", type="password", help="Paste your API key here. Or add THE_ODDS_API_KEY in Secrets.")
    st.caption("Get a key at https://the-odds-api.com (free tier available).")

c1, c2 = st.columns([1,1])
with c1:
    sport_label = st.selectbox("Sport/League", list(SPORT_CHOICES.keys()), index=0)
    sport_key = SPORT_CHOICES[sport_label]
with c2:
    odds_format = st.selectbox("Odds format", ["american","decimal"], index=0)

c3, c4, c5 = st.columns([1,1,1])
with c3:
    regions = st.multiselect("Regions", ["us","us2","uk","eu","au"], default=["us","us2"])
with c4:
    preferred_books = st.multiselect("Preferred bookmaker(s)", ["Pinnacle","FanDuel","DraftKings","BetMGM","Caesars","PointsBet","bet365"], default=["FanDuel"], help="We try these first; fallback to first available book if missing.")
with c5:
    strict_filter = st.checkbox("Strict server-side filter", value=False, help="If ON, pass your preferred books to the API. Otherwise fetch all and pick client-side.")

search = st.text_input("Filter by team (type part of name)", value="")
show_debug = st.checkbox("Show debug info", value=False)

# Test button
if st.button("Test endpoint"):
    key = resolve_api_key(user_key)
    if not key:
        st.error("No API key found. Enter it above or add THE_ODDS_API_KEY in Secrets.")
    else:
        params = {"apiKey": key, "regions": ",".join(regions) if regions else "us", "markets": "h2h", "oddsFormat": odds_format}
        if strict_filter and preferred_books:
            params["bookmakers"] = ",".join(preferred_books)
        data, err = safe_get(f"{API_BASE}/sports/{sport_key}/odds", params)
        if err:
            st.error(f"{sport_label} odds error: {err}")
        else:
            st.success(f"✅ {sport_label} odds accessible (events: {len(data)})")
            if show_debug:
                st.json((data or [])[:2])

st.divider()
st.subheader("Select your moneyline legs")

key = resolve_api_key(user_key)
if not key:
    st.warning("Enter your API key above to continue.")
    st.stop()

params = {"apiKey": key, "regions": ",".join(regions) if regions else "us", "markets": "h2h", "oddsFormat": odds_format}
if strict_filter and preferred_books:
    params["bookmakers"] = ",".join(preferred_books)
data, err = safe_get(f"{API_BASE}/sports/{sport_key}/odds", params)
if err or not data:
    st.error(f"{sport_label} odds error: {err}")
    st.stop()

# Build matches list with preferred-book fallback and team filtering
matches = []
skipped = []

for ev in data:
    eid = ev.get("id")
    home = ev.get("home_team")
    away = ev.get("away_team")
    start = ev.get("commence_time")

    # Client-side choose bookmaker
    per_book = []
    for bk in ev.get("bookmakers", []):
        bkname = bk.get("title")
        outs = None
        for mk in bk.get("markets", []):
            if mk.get("key") == "h2h":
                outs = mk.get("outcomes", [])
                break
        if not outs:
            continue
        m = map_outcomes_to_home_away(home, away, outs)
        if "home" in m and "away" in m and m["home"] is not None and m["away"] is not None:
            per_book.append((bkname, m))

    chosen_bk = None; mapped = {}
    if preferred_books:
        for pref in preferred_books:
            for bkname, m in per_book:
                if bkname == pref:
                    chosen_bk = bkname; mapped = m; break
            if chosen_bk: break
    if not chosen_bk and per_book:
        chosen_bk, mapped = per_book[0]

    if not mapped:
        skipped.append({"event": f"{home} vs {away}", "reason": "No book had both sides mapped", "available_books": [b for b,_ in per_book]})
        continue

    # Search filter
    needle = normalize_name(search)
    if needle:
        if needle not in normalize_name(home) and needle not in normalize_name(away):
            continue

    matches.append({
        "id": eid, "home": home, "away": away, "start": start, "book": chosen_bk,
        "home_line": mapped["home"], "away_line": mapped["away"]
    })

if show_debug:
    st.write(f"Events: {len(data)}, usable matches after filtering: {len(matches)}")
    if skipped:
        st.warning("Some events were skipped (no mapped ML at any preferred/available book). Here are a few examples:")
        st.json(skipped[:5])

if not matches:
    st.error("No usable matchups. Try clearing the team filter, broadening regions, or turning OFF strict filter.")
    st.stop()

# UI: Checkbox per game side (prevent both sides selected)
if "selected" not in st.session_state:
    st.session_state.selected = {}

for m in matches:
    eid = m["id"]; home = m["home"]; away = m["away"]; start = m["start"]
    bk = m["book"]; hl = m["home_line"]; al = m["away_line"]

    with st.container(border=True):
        st.caption(f"{bk} — {start}")
        c1, c2 = st.columns(2)
        with c1:
            key_home = f"{eid}_home"
            sel_home = st.checkbox(f"{home}", key=key_home)
        with c2:
            key_away = f"{eid}_away"
            sel_away = st.checkbox(f"{away}", key=key_away)

        # show lines under names
        if odds_format == "decimal":
            st.write(f"Home {float(hl):.2f} vs Away {float(al):.2f} (decimal) — {away} @ {home}")
        else:
            st.write(f"Home {hl:+} vs Away {al:+} (american) — {away} @ {home}")

        # Prevent both sides
        if sel_home and sel_away:
            st.warning("You selected both sides. Uncheck one.")
        st.session_state.selected[eid] = {"home": sel_home, "away": sel_away, "meta": m}

st.divider()
boosted_input = st.text_input("Boosted final odds for your parlay (e.g., +450 or 5.50)", value="+450")

if st.button("Calculate FAIR value & Kelly"):
    # Build legs from selections
    legs = []
    parlay_prob = 1.0
    parlay_dec = 1.0

    for eid, pick in st.session_state.selected.items():
        meta = pick["meta"]
        home = meta["home"]; away = meta["away"]
        hl = meta["home_line"]; al = meta["away_line"]
        if odds_format == "decimal":
            p_home = 1.0 / float(hl); p_away = 1.0 / float(al)
        else:
            p_home = implied_prob_from_american(hl); p_away = implied_prob_from_american(al)
        f_home, f_away = devig_two_way(p_home, p_away)

        side = None
        if pick["home"] and not pick["away"]:
            side = "home"; fair_prob = f_home
        elif pick["away"] and not pick["home"]:
            side = "away"; fair_prob = f_away
        else:
            # skip if both/none selected
            continue
        fair_dec = 1.0 / fair_prob if fair_prob else None

        legs.append({
            "event": eid, "home": home, "away": away, "book": meta["book"],
            "price_home": hl, "price_away": al, "side": side,
            "fair_prob": fair_prob, "fair_dec": fair_dec, "fair_amer": decimal_to_american(fair_dec) if fair_dec else None
        })
        parlay_prob *= fair_prob
        parlay_dec *= fair_dec

    if not legs:
        st.error("No valid legs (make sure only one side per game is checked).")
        st.stop()

    st.subheader("Legs")
    for lg in legs:
        team = lg["home"] if lg["side"]=="home" else lg["away"]
        opp  = lg["away"] if lg["side"]=="home" else lg["home"]
        if odds_format == "decimal":
            line_str = f"{lg['book']}: Home {float(lg['price_home']):.2f}, Away {float(lg['price_away']):.2f}"
        else:
            line_str = f"{lg['book']}: Home {lg['price_home']:+}, Away {lg['price_away']:+}"
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
