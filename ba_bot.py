#!/usr/bin/env python3
"""
BA CAPITAL — 4H Sweep Bot
Phase 1: PAPER TRADING (simulated fills on live Binance futures data)
Phase 2: live mode (CoinDCX) — stub included, keep PAPER=True until validated.

Runs at :01 UTC of every 4H boundary hour (01/05/09/13/17/21:31 IST).
Engine = the exact validated rules from the BA Capital dashboard.
"""

import json, time, math, os, sys, atexit
from datetime import datetime, timezone, timedelta
import urllib.request

# ==================== CONFIG ====================
PAPER        = True          # True = simulate. False = live (requires API keys + live order code)
RISK_PCT     = 5.0           # % of account risked per trade (PAPER ONLY)
LIVE_RISK_PCT = 2.5          # founder decision: 2.5% of $400 live equity = $10 risk per trade
LIVE_MAX_RISK_USDT = 10.0    # HARD CAP: risk per trade never exceeds $10, even as equity compounds
MODE = "LIVE"                # LIVE = real orders (needs LIVE_ARMED file) | SHADOW = measure only | PAPER = pure sim
LIVE_LEVERAGE = 25           # isolated margin; liquidation ~4% away vs widest allowed stop 2.5% (QML_MAX_RISK_PCT)
                             # 25x chosen so $10 risk on tight 15m stops fits a ~$235 wallet: margin = notional/25
CDCX_KEY_FILE = "/root/.cdcx_key"; CDCX_SECRET_FILE = "/root/.cdcx_secret"
TG_TOKEN_FILE = "/root/.tg_token"; TG_CHAT_FILE = "/root/.tg_chat"
LIVE_ARMED = "/root/LIVE_ARMED"   # safety: file must exist for MODE=LIVE to place orders
MAX_BASIS_PCT = 0.75         # skip trade if |Binance entry vs CoinDCX price| exceeds this
MIN_COINDCX_VOL = 1500000.0  # min CoinDCX 24h quote volume (USDT) - kills micro-cap books only
MIN_NOTIONAL_USDT = 12.0     # skip if position notional below exchange minimum
QML_SCORE_V2_MIN = 6         # FOUNDER SCORE v2 (2026-10-06, scale /16), floor relaxed 7->6 on 2026-10-07:
                             # replay band data shows the 6-band is positive (+0.14R avg) - churn, not poison -
                             # and the candidate sort guarantees a 6 only takes a slot when nothing better
                             # exists (elites can never be crowded out). Shadow twin measures the 6-band
                             # live; revert this number if it degrades. Bands: <6 skip - 6-8 valid -
                             # 9-10 strong - 11+ A+ (counter-trend still needs 11+).
LIVE_MAX_POS = 5             # live: max total concurrent positions (paper keeps 8)
LIVE_CONCURRENT = 5        # margin split into this many slots - concurrent trades coexist, each smaller
SIM_MGMT = "T1"              # SIM trade management: "NONE" | "H" (bank half at +1R) | "T1" (lock SL at +1R).
                             # Replay evidence 2026-10-04 (272 trades, chop): NONE +0.089R | H +0.149R | T1 +0.187R.
                             # T1 = current champion. LIVE enable waits for the CoinDCX bracket-edit endpoint probe.
LIVE_ENTRY = "LIMIT"         # "LIMIT" = rest a limit at the QML level - PROVEN stable on CoinDCX all day.
                             # "MARKET" entries showed instant-close bracket glitches (MAGMA, 1000PEPE) - do not use until diagnosed.
LIVE_MAX_DIR = 3             # live: max same-direction positions - worst one-sided batch = 3R
SWEEP_MODE = "PAPER"         # sweep book STAYS paper even while QML is live (founder requirement)
SWEEP_REST_SLOTS = ("13:31", "17:31")   # IST scan slots that STAND DOWN (audit 2026-10-07, 300 trades:
                             # 12-18 window won 6% / -58.6R while 9:31 slot won 71%). Publish-only there.
QML_MODE   = "LIVE"           # REAL ORDERS MODE (set to "SIM" to return to simulation) | "SIM" = full live-stack simulation (same gates,
                             # same $ sizing, same slots/expiry - fills simulated on closed candles, [SIM] tagged on Telegram)
                             # | "PAPER" = plain paper book (no execution gates)
SIM_STATE = {"equity": 400.0, "day": 0.0, "cur_day": ""}   # simulated live bank - mirrors LIVE_STATE rules
LIVE_VENUE = "BINANCE"       # "BINANCE" = USDT-M futures execution | "CDCX" = CoinDCX (dormant fallback)
LIVE_WALLET_USDT = 126.0     # YOUR Binance USDT-M futures wallet balance (USDT) - founder confirmed
                             # 126.0 on 2026-10-07 (fresh start: ledger wiped, old history archived).
                             # Drives the margin-slot math (wallet x 0.95 / 5 slots). UPDATE THIS
                             # whenever the real wallet changes materially.
# self-healing exchange-rule caches (persist across restarts where noted)
_BLOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocked_syms.json")
try:
    _BLOCKED = set(json.load(open(_BLOCK_FILE)))     # instruments CoinDCX reported "not active"
except Exception:
    _BLOCKED = set()
_QSTEP = {}                                          # sym -> quantity step learned from exchange errors
_TICK = {}                                          # sym -> price tick (decimals) learned from exchange errors
_LVCAP = {}                                        # sym -> max leverage for this size tier, learned from exchange errors
def _committed_margin():
    """USDT locked in all open live positions right now (recomputed from the books)."""
    tot = 0.0
    for bk in (BOOK_SWEEP, BOOK_QML):
        for p in bk['positions']:
            if p.get("live") or p.get("sim"):
                tot += float(p.get("margin_req", 0) or 0)
    return tot

_SKIP_SEEN = {}                                       # sym -> last alerted skip CLASS (dedup: alert once per reason until it changes)
def skip_alert(sym, cls, msg):
    """Telegram dedup: one alert per coin per skip-class. Re-alerts only when the
    reason CHANGES (basis -> listed -> thin) or after a successful trade resets it."""
    if _SKIP_SEEN.get(sym) == cls: return
    _SKIP_SEEN[sym] = cls
    tg(msg)

# ==================== PHASE A ANALYTICS (measurement only - ZERO trading-behaviour change) ====================
_AFILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml_analytics.jsonl")   # append-only event log
_SFILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml_shadow.json")      # open shadow records
_SHFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml_shadow_hist.jsonl")# closed shadow records
_FUNNEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml_funnel.json")
REPORT = {}
FUNNEL = {}
try: FUNNEL = json.load(open(_FUNNEL_FILE))
except Exception: FUNNEL = {}
SHADOW = []
try: SHADOW = json.load(open(_SFILE))
except Exception: SHADOW = []
_HIST_KEYS = []

def a_log(ev):
    """Append one analytics event (jsonl). Never raises."""
    try:
        ev["t"] = str(datetime.now(IST))[:19]
        with open(_AFILE, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass

def funnel_add(key, n=1):
    FUNNEL[key] = FUNNEL.get(key, 0) + n

def funnel_save():
    try: json.dump(FUNNEL, open(_FUNNEL_FILE, "w"))
    except Exception: pass

def _hist_keys():
    if _HIST_KEYS: return _HIST_KEYS
    try:
        for ln in open(_SHFILE).read().splitlines()[-3000:]:
            try: _HIST_KEYS.append(json.loads(ln)["key"])
            except Exception: pass
    except Exception: pass
    return _HIST_KEYS

def shadow_open(sig, regime, corr, basis):
    """Shadow twin (#29): paper-record EVERY tradable-quality signal so CoinDCX gate
    rejections still get outcomes. Models: D=ideal P1, A=limit at P1, B=basis-adjusted P1,
    C=market at signal. Assumed-fill model - labeled, never presented as live truth."""
    global SHADOW
    key = sig["sym"] + "|" + sig["dir"] + "|" + str(round(sig["qm"], 10))
    if any(r["key"] == key for r in SHADOW) or key in _hist_keys(): return
    risk = abs(sig["qm"] - sig["sl"])
    if risk <= 0: return
    lg = sig["dir"] == "LONG"
    tp2 = min(sig["p2"], sig["qm"] + 2*risk) if lg else max(sig["p2"], sig["qm"] - 2*risk)
    p1b = sig["qm"] * (1 + (basis or 0)/100.0)
    def _m(fill_px, filled_now):
        return dict(filled=filled_now, fill_px=fill_px, mfe_r=-9.0, mae_r=9.0, exit=None, r=None, bars=0)
    SHADOW.append(dict(key=key, sym=sig["sym"], dir=sig["dir"], p1=sig["qm"], sl=sig["sl"], tp=tp2, risk=risk,
        score=sig["score"], regime=regime, corr=round(corr or 0.0, 2), basis=basis, sig_px=sig["last"],
        age_bars=sig.get("age_bars"), dist_atr=sig.get("dist_atr"), rr=sig.get("rr"), rel15=sig.get("rel15"), pct24=sig.get("pct24"),
        volX=sig.get("volX"), sweep_atr=sig.get("sweep_atr"),
        candle_ts=sig.get("_candle_ts"), t=str(datetime.now(IST))[:19], last_bar=0, bars_total=0,
        opp=dict(mfe_r=-9.0, mae_r=9.0),
        models={"D": _m(sig["qm"], True), "A": _m(sig["qm"], False), "L": _m(sig["qm"], False),
                "B": _m(round(p1b, 10), False), "C": _m(sig["last"], True)}))
    funnel_add("shadow_opened")
    a_log(dict(ev="shadow_open", key=key, sym=sig["sym"], dir=sig["dir"], score=sig["score"], basis=basis))
    try: json.dump(SHADOW, open(_SFILE, "w"))
    except Exception: pass

def _shadow_step(kmap):
    """Advance every open shadow record by one closed bar. Call once per scan with kmap."""
    global SHADOW
    done = []
    for rec in SHADOW:
        k = kmap.get(rec["sym"])
        if not k or len(k) < 3: continue
        bar = k[-2]
        bo = int(float(bar[0]))
        if bo == rec["last_bar"]: continue
        rec["last_bar"] = bo; rec["bars_total"] = rec.get("bars_total", 0) + 1
        h, l, c = float(bar[2]), float(bar[3]), float(bar[4])
        lg = rec["dir"] == "LONG"; risk = rec["risk"]; p1 = rec["p1"]
        # opportunity cost of the SIGNAL itself (independent of any fill model)
        rec["opp"]["mfe_r"] = max(rec["opp"]["mfe_r"], ((h - p1) if lg else (p1 - l)) / risk)
        rec["opp"]["mae_r"] = min(rec["opp"]["mae_r"], ((l - p1) if lg else (p1 - h)) / risk)
        all_done = True
        for name, m in rec["models"].items():
            if m["exit"]: continue
            fp = m["fill_px"]
            if not m["filled"]:
                touch = (l <= fp) if lg else (h >= fp)     # ASSUMED fill: level traded through
                if touch: m["filled"] = True
                elif rec["bars_total"] >= 32:
                    m["exit"] = "NOT_FILLED"; continue
                else: all_done = False; continue
            m["bars"] += 1
            m["mfe_r"] = max(m["mfe_r"], ((h - fp) if lg else (fp - l)) / risk)
            m["mae_r"] = min(m["mae_r"], ((l - fp) if lg else (fp - h)) / risk)
            if name == "L":
                # LADDER (paper's validated disposition): 50% off at +1R & SL->BE,
                # 25% off at +1.5R & trail locks +1R, runner to TP. Pessimistic bar order.
                st = m.setdefault("ladder", dict(frac=1.0, banked=0.0, sl=rec["sl"], t1=False, t2=False))
                _adv = ((h - fp) if lg else (fp - l)) / risk
                if ((l <= st["sl"]) if lg else (h >= st["sl"])):
                    _wick = (st["sl"] - l) if lg else (h - st["sl"])
                    _fill = (st["sl"] - SLIP_WICK_FRAC_DN*_wick) if lg else (st["sl"] + SLIP_WICK_FRAC_UP*_wick)
                    st["banked"] += st["frac"] * (((_fill - fp) if lg else (fp - _fill)) / risk)
                    m["exit"] = "LADDER_SL"; m["r"] = round(st["banked"], 2); continue
                if not st["t1"] and _adv >= 1.0:
                    st["banked"] += 0.5 * 1.0; st["frac"] = round(st["frac"] - 0.5, 4); st["t1"] = True; st["sl"] = fp
                if not st["t2"] and _adv >= 1.5:
                    st["banked"] += 0.25 * 1.5; st["frac"] = round(st["frac"] - 0.25, 4); st["t2"] = True
                    st["sl"] = fp + (risk if lg else -risk)
                if st["frac"] > 0 and ((h >= rec["tp"]) if lg else (l <= rec["tp"])):
                    st["banked"] += st["frac"] * (((rec["tp"] - fp) if lg else (fp - rec["tp"])) / risk)
                    m["exit"] = "LADDER_TP"; m["r"] = round(st["banked"], 2); continue
                if m["bars"] >= 32:
                    st["banked"] += st["frac"] * (((c - fp) if lg else (fp - c)) / risk)
                    m["exit"] = "LADDER_TS"; m["r"] = round(st["banked"], 2); continue
                all_done = False
                continue
            hit_sl = (l <= rec["sl"]) if lg else (h >= rec["sl"])
            hit_tp = (h >= rec["tp"]) if lg else (l <= rec["tp"])
            if hit_sl and hit_tp: m["ambiguous"] = True   # one candle spanned both - intrabar order unknowable at 15m
            if hit_sl:
                _wick = (rec["sl"] - l) if lg else (h - rec["sl"])
                _fill = (rec["sl"] - SLIP_WICK_FRAC_DN*_wick) if lg else (rec["sl"] + SLIP_WICK_FRAC_UP*_wick)
                m["exit"] = "SL"; m["exit_px"] = round(_fill, 10)
                m["r"] = round(max(min((((_fill - fp) if lg else (fp - _fill)) / risk), 2.0), -2.0), 2)
            elif hit_tp: m["exit"] = "TP";        m["r"] = round(((rec["tp"] - fp) if lg else (fp - rec["tp"])) / risk, 2)
            elif m["bars"] >= 32: m["exit"] = "TIME_STOP"; m["r"] = round(((c - fp) if lg else (fp - c)) / risk, 2)
            else: all_done = False
        if all_done or rec["bars_total"] >= 48: done.append(rec)
    for rec in done:
        SHADOW.remove(rec)
        _HIST_KEYS.append(rec["key"])
        try:
            with open(_SHFILE, "a") as f: f.write(json.dumps(rec) + "\n")
        except Exception: pass
        funnel_add("shadow_closed")
    if done:
        try: json.dump(SHADOW, open(_SFILE, "w"))
        except Exception: pass

def daily_report(funnel):
    """Compile yesterday's funnel + shadow-model stats into REPORT (dashboard + tg)."""
    rows = []
    try: rows = open(_SHFILE).read().splitlines()[-2000:]
    except Exception: pass
    stats = {}
    for ln in rows:
        try: rec = json.loads(ln)
        except Exception: continue
        for m, d in rec.get("models", {}).items():
            if d.get("exit") not in ("SL", "TP", "TIME_STOP", "LADDER_SL", "LADDER_TP", "LADDER_TS"): continue
            s = stats.setdefault(m, dict(n=0, w=0, rsum=0.0, sub=0, fills=0))
            s["sub"] += 1
            if d.get("filled"): s["fills"] += 1
            if d.get("r") is not None:
                s["n"] += 1; s["rsum"] += d["r"]; s["w"] += 1 if d["r"] > 0 else 0
    rep = dict(date=str(datetime.now(IST))[:10], funnel=dict(funnel), models={})
    msg = "QML DAILY  models: D=idealP1 A=limitP1 L=ladder B=basisAdj C=mkt@sig\n"
    for m in "DABCL":
        s = stats.get(m)
        if not s or not s["n"]: continue
        avg = s["rsum"]/s["n"]; fill = 100.0*s["fills"]/max(1, s["sub"])
        rep["models"][m] = dict(n=s["n"], win_pct=round(100.0*s["w"]/s["n"], 1), avg_r=round(avg, 2), fill_pct=round(fill, 0))
        msg += f"{m}: n={s['n']} win={100*s['w']/s['n']:.0f}% avgR={avg:+.2f} fill={fill:.0f}%\n"
    fn = {k: v for k, v in funnel.items() if not k.startswith("_")}
    msg += "funnel: " + ", ".join(f"{k}:{v}" for k, v in sorted(fn.items())[:14])
    tg(msg[:900])
    REPORT.clear(); REPORT.update(rep)
    funnel_save()
LIVE_CAPITAL_INR = 200.0     # REAL money base for live risk sizing, in USDT (the exchange currency)
                             # NOTE: legacy field name says INR, but qty math uses it as USDT.
                             # Wallet is ~235 USDT; 200 leaves headroom. Do NOT put 20000 here.
LIVE_TP_R = 2.0              # live TP at entry +/- 2.0R (min with P2) - the strategy's true full target (was paper's TP_R=2.0)
FILL_TIMEOUT_SCANS = 6       # cancel unfilled live bracket after 6 scans (~90 min) - advisor review 2026-10-07:
                             # valid retests regularly take >45 min; the 1.5-ATR stale guard and the
                             # TP-consumed check already kill genuinely dead orders, so the timer was
                             # redundant strictness. Wider window = more fills on slow retests.
TP_R         = 2.0           # final full close
PART1_R      = 1.0           # close 50% here, SL -> breakeven
PART2_R      = 1.5           # close 25% here, SL trails to lock +1R
PART1_FRAC   = 0.50
PART2_FRAC   = 0.25
TRAIL_LOCK_R = 1.0           # locked profit once 1.5R part taken
MAX_POSITIONS= 6             # max concurrent sweep trades
QML_MAX_POSITIONS = 8        # max concurrent QML (15m) trades
QML_RISK_PCT = 5.0           # risk per QML trade
QML_REGIME_TF = "4h"         # BTC regime timeframe for QML gate (was 1h: slower flips, fewer whipsaws)
QML_CORR_GATE = 0.55         # only coins this correlated with BTC obey the regime (was 0.45: more coins trade freely)
QML_MIN_SCORE = 5            # shadow twin records score>=5 RETESTs on the new /16 scale (was 4 of 8)
QML_ZONE_ATR  = 0.15         # retest zone = P1 +/- 0.15*ATR(14)
QML_MAJOR_LEG_ATR = 2.0      # FOUNDER STRUCTURE ENGINE (2026-10-07): a swing only counts as MAJOR if
                             # the leg from the previous major swing moved >= 2 x ATR. Internal wiggles
                             # are invisible to structure. Self-scales per coin (ATR-based).
QML_TREND_EMA_BAND = 0.003   # FOUNDER FIX 2026-10-07 (EMA follow-up, bos2_replay): 18/20 shorts were
                             # taken ABOVE the coin's own 15m EMA - fighting the coin trend. Those 18 cost
                             # -3.1R. Gate: SHORT only when price < EMA20(15m)x(1-band), LONG only when
                             # above EMAx(1+band). BTC regime is blind in chop; the coin's own trend is not.
QML_BOS_MIN_CLOSES = 2       # FOUNDER FIX 2026-10-06 (TIA + ARB losses within 10 min): ONE close beyond
                             # P2 confirms nothing - micro-dips close below once and instantly reclaim,
                             # and the bot shorts the pullback into strength. BOS now requires N
                             # CONSECUTIVE candle closes beyond P2 before the structure counts as
                             # broken. Entry mechanics unchanged: retest limit at P1, all other gates.
QML_ARMED_MAX_BARS = 96      # ARMED expires 24h after BOS if no retest
QML_ARMED_MAX_DIST_ATR = 3.0 # ARMED expires if price runs >3 ATR from P1
QML_MAX_RISK_PCT = 0.025     # SL distance vs price above this = invalid structure (not scored)
QML_MIN_RISK_ATR = 0.50      # LIVE EVIDENCE 2026-10-06 (TAO/DOGE/CRV batch): 3 shorts stopped in <7 min
                             # with stops only ~0.2% from entry = ~0.3 ATR - inside noise. A stop closer
                             # than half an ATR is not a structure stop, it is a lottery ticket. Reject.
QML_MIN_SWEEP_ATR = 0.25     # P3 must clear P1 by >= 0.25 ATR. A sweep of a few ticks is not a liquidity
                             # grab - and it fakes a giant R:R (0.2% stop vs 4% TP) that games the
                             # rr>=2 score point and the rr>=1 gate.
CLOSE_STOP_HARD_ATR = 1.0    # CLOSE-BASED STOP (post-mortem 2026-10-06): die only on candle CLOSE beyond
                             # P3, not on the hunt wick. Evidence: 14/20 live stops then reached TP anyway,
                             # 12/20 were wick-only; close-stop sim +0.69R avg vs -0.05R actual. This buffer
                             # = the catastrophe line: exchange hard stop 1 ATR beyond P3 for real explosions
                             # (news bombs / bot offline). Wick into it = killed; close beyond soft level = exit.
ABS_MOM_BLOCK = 3.0          # LIVE EVIDENCE 2026-10-06 (ZEC): absolute 1h momentum filter - no basket
                             # comparison, flat is flat. Block SHORT when the coin's OWN 1h gain >= +3%,
                             # mirror LONG at -3%. The relative gate (REL_MOM_BLOCK) goes blind during a
                             # market-wide pump (everything green = nobody "leads" the basket). Opening
                             # bid pending replay evidence - tune from the gate_abs_mom funnel count.
QML_MAX_DIR = 6              # max open positions in one direction (blocks 7th+ same-way entry; existing book untouched)
DAILY_LOSS_LIMIT_R = -3.0    # stop trading for the day after this much R lost
COOLDOWN_H   = 6.0           # after ANY close on a sym+dir, block re-entry for N hours (kills restart/re-entry storms)
# ---- PHASE D FIXES (evidence: 2026-10-03 morning, -4.65R correlated adverse-fill batch) ----
EXEC_STALE_ATR = 1.5         # FIX 1: cancel a resting limit when price has run this many ATR from the level -
                             # an order left behind only fills on the adverse retrace (ICP/SUPER/WLD -1R lesson)
LIVE_MAX_CORR_DIR = 2        # FIX 2: max same-direction positions among BTC-correlated coins
                             # (corr >= QML_CORR_GATE) - 5 correlated shorts = one bet, died as one (-5R lesson)
QML_SCORE_V2_COUNTER = 11    # FOUNDER SCORE v2: counter-regime trades need A+ (11+) on the /16 scale.
                             # Evidence 2026-10-06: score-7/8-of-8 counter-trend shorts (ZEC/NIL) died too.
COUNTER_TREND_MIN_SCORE = QML_SCORE_V2_COUNTER  # legacy alias - FIX 3: counter-regime trades
SLIP_WICK_FRAC = 0.3
SLIP_WICK_FRAC_DN = 0.35     # SL exits hit by down-moves slip more (dumps move faster than pumps - literature)
SLIP_WICK_FRAC_UP = 0.25     # SL exits hit by up-moves slip less
LIQ_MIN_QVOL = 25_000_000    # Binance 24h quote-volume floor at signal
REL_MOM_BLOCK = 2.0          # %-points: block SHORT when the coin's 45m (3-candle) return beats the alt-basket median
                             # by this much (replay 2026-10-06: 2-5% bucket = 53% fast-stops, -0.24R; 5-10% = 100%/-2R).
                             # Mirror-blocks LONGs at -2%. The <0% bucket (81% win, +0.57R) stays fully open.    # Binance 24h quote-volume floor at signal (evidence 2026-10-04 replay:
                             # score6+ trades on <$25M books avg -0.25R via stop-gun wicks; >=$25M: +0.25R)         # stop-outs fill at SL +/- this fraction of the wick that ran beyond SL.
                             # Evidence: CRV/MARSCOIN/XMR all wicked 1.3-2.6R past SL before stopping -
                             # clean -1.0R fills are fiction in fast markets. 0.0 = ideal fills (old behavior).
                             # must score this high, else blocked (5 counter-trend shorts in a rally lesson)
MIN_VOLX     = 1.2           # sweep signals below this volume ratio are published but NOT traded
ACCOUNT_INR  = 200000.0      # paper starting equity (2 lakh INR)
DATA_URL     = "https://fapi.binance.com/fapi/v1/klines?symbol={}&interval=4h&limit=100"
LEDGER_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ba_bot_trades.json")
IST = timezone(timedelta(hours=5, minutes=30))
_BASE = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(_BASE, "ba_bot.lock")
HALT_FILE = os.path.join(_BASE, "BOT_HALT")
TG_SOFT_HALT = os.path.join(_BASE, "TG_SOFT_HALT")   # Telegram /stop: pause NEW entries, keep managing

# ---- STARTUP CONFIG VALIDATOR (2026-10-06): a half-merged deploy must NEVER boot "successfully".
# The LIQ_MIN_QVOL incident taught us a lost config line only surfaces mid-scan as a NameError
# that kills the trading loop silently. This check refuses to boot if any known config name,
# function or trading-critical constant is missing or has the wrong type.
def config_sanity():
    """Refuse to boot unless every trading-critical name exists with a sane type."""
    required = {
        # execution / risk
        "PAPER": bool, "MODE": str, "QML_MODE": str, "SWEEP_MODE": str, "LIVE_VENUE": str,
        "LIVE_ARMED": str, "RISK_PCT": (int, float), "LIVE_RISK_PCT": (int, float),
        "LIVE_MAX_RISK_USDT": (int, float), "LIVE_LEVERAGE": int,
        "LIVE_WALLET_USDT": (int, float), "LIVE_CAPITAL_INR": (int, float),
        "LIVE_TP_R": (int, float), "FILL_TIMEOUT_SCANS": int, "LIVE_ENTRY": str,
        # QML gates
        "QML_MAX_POSITIONS": int, "QML_RISK_PCT": (int, float), "QML_REGIME_TF": str,
        "QML_CORR_GATE": (int, float), "QML_MIN_SCORE": int, "QML_SCORE_V2_MIN": int, "QML_SCORE_V2_COUNTER": int,
        "QML_BOS_MIN_CLOSES": int, "QML_TREND_EMA_BAND": (int, float),
        "QML_MAJOR_LEG_ATR": (int, float), "StructTracker": object,
        "LIQ_SIM": object, "LIQ_SIM_RISK": (int, float), "LIQ_SIM_MAXPOS": int,
        "liqsim_manage": object, "detect_liq15": object,
        "SWEEP_SIM": object, "SWEEP_SIM_RISK": (int, float), "SWEEP_SIM_MAXPOS": int,
        "sweepsim_enter": object, "sweepsim_manage": object, "SWEEP_REST_SLOTS": object,
        "QML_ZONE_ATR": (int, float), "QML_ARMED_MAX_BARS": int,
        "QML_ARMED_MAX_DIST_ATR": (int, float), "QML_MAX_RISK_PCT": (int, float),
        "QML_MAX_DIR": int, 
        # execution fixes / gates (the ones that went missing before)
        "EXEC_STALE_ATR": (int, float), "LIQ_MIN_QVOL": (int, float),
        "QML_MIN_RISK_ATR": (int, float), "QML_MIN_SWEEP_ATR": (int, float),
        "ABS_MOM_BLOCK": (int, float), "CLOSE_STOP_HARD_ATR": (int, float),
        "REL_MOM_BLOCK": (int, float), "LIVE_MAX_POS": int, "LIVE_MAX_DIR": int,
        "LIVE_MAX_CORR_DIR": int, "MIN_COINDCX_VOL": (int, float),
        "MIN_NOTIONAL_USDT": (int, float), "MAX_BASIS_PCT": (int, float),
        # lifecycle
        "DAILY_LOSS_LIMIT_R": (int, float), "COOLDOWN_H": (int, float),
        "SLIP_WICK_FRAC": (int, float), "SLIP_WICK_FRAC_DN": (int, float),
        "SLIP_WICK_FRAC_UP": (int, float), "MIN_VOLX": (int, float),
        "ACCOUNT_INR": (int, float), "TP_R": (int, float),
        "PART1_R": (int, float), "PART2_R": (int, float),
        "PART1_FRAC": (int, float), "PART2_FRAC": (int, float),
        "TRAIL_LOCK_R": (int, float), "MAX_POSITIONS": int,
        # live bridge functions (a broken merge can drop whole defs)
        "b_filters": object, "b_round": object, "b_qty": object, "b_leverage": object,
        "b_margin_isolated": object, "b_single_asset_mode": object,
        "b_position": object, "b_place_bracket": object, "b_modify_sl": object,
        "b_close_market": object, "b_trades_pnl": object,
        "bridge_open": object, "bridge_close": object, "bridge_part": object,
        "exec_guard": object, "binance_qvol": object, "qml_regime": object,
        "detect_qm15": object, "open_trade": object, "manage_book": object,
        "qml_scan": object, "scan_once": object, "load_pairs": object,
        "tg": object, "save_state": object, "upload_github": object,
    }
    bad = []
    for name, typ in required.items():
        if name not in globals():
            bad.append(f"{name} MISSING")
        elif typ is not object and not isinstance(globals()[name], typ):
            bad.append(f"{name} wrong type ({type(globals()[name]).__name__})")
    # value sanity: these must never be zero/negative or the whole book mis-sizes
    for name in ("LIVE_MAX_RISK_USDT", "LIQ_MIN_QVOL", "LIVE_WALLET_USDT", "LIVE_LEVERAGE",
                 "LIVE_MIN_SCORE", "COUNTER_TREND_MIN_SCORE", "EXEC_STALE_ATR"):
        v = globals().get(name)
        if isinstance(v, (int, float)) and v <= 0:
            bad.append(f"{name}={v} (must be > 0)")
    if bad:
        msg = "CONFIG SANITY FAILED - refusing to boot:\n  " + "\n  ".join(bad)
        print("FATAL " + msg)
        try:
            tg("🛑 " + msg[:400])
        except Exception:
            pass
        sys.exit(1)
    print(f"  config sanity: OK ({len(required)} trading-critical names verified)")

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            old = int(open(LOCK_FILE).read().strip())
            if os.path.exists(f"/proc/{old}"):
                print(f"FATAL: another bot instance is already running (PID {old}). Exiting - no duplicate possible.")
                sys.exit(1)
            print("  stale lock from dead process - removing")
        except SystemExit:
            raise
        except Exception:
            pass
        try: os.remove(LOCK_FILE)
        except Exception: pass
    open(LOCK_FILE, "w").write(str(os.getpid()))

def release_lock():
    try: os.remove(LOCK_FILE)
    except Exception: pass

def halt_requested():
    return os.path.exists(HALT_FILE)

def self_audit():
    for name, bk in (("SWEEP", BOOK_SWEEP), ("QML", BOOK_QML)):
        expected = ACCOUNT_INR + sum(x.get('pnl', 0) or 0 for x in bk['history'])
        actual = bk['equity']
        if abs(actual - expected) > 1:
            print(f"  !! {name} self-audit: equity {actual:,.0f} != history {expected:,.0f} - auto-corrected")
            bk['equity'] = expected
        else:
            print(f"  self-audit {name}: OK  Rs.{actual:,.0f}  ({len(bk['history'])} trades)")

# ---- GitHub bridge (dashboard reads this) ----
# 1) github.com -> New repository -> name it exactly: ba-bot-data  (PUBLIC)
# 2) Settings -> Developer settings -> Personal access tokens -> Tokens (classic)
#    -> Generate -> tick 'repo' -> copy the token below
GH_USER = "ajaysharda1992"
GH_REPO = "ba-bot-data"
GH_TOKEN = ""
try:
    _tf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gh_token")
    if os.path.exists(_tf):
        GH_TOKEN = open(_tf).read().strip()
except Exception:
    pass
if not GH_TOKEN:
    print("  !! .gh_token file not found - GitHub uploads disabled")
import threading
def _ssl_ctx():
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx

def upload_github():
    if not (GH_USER and GH_TOKEN): return
    import base64
    try:
        ctx = _ssl_ctx()
        content = base64.b64encode(open(LEDGER_FILE,'rb').read()).decode()
        url = f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/bot_trades.json"
        req = urllib.request.Request(url, headers={"Authorization":"Bearer "+GH_TOKEN,"User-Agent":"ba-bot"})
        sha = None
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r: sha = json.loads(r.read())["sha"]
        except Exception: pass
        data = json.dumps({"message":"bot update","content":content,"sha":sha}).encode()
        req2 = urllib.request.Request(url, data=data, method="PUT",
              headers={"Authorization":"Bearer "+GH_TOKEN,"User-Agent":"ba-bot","Content-Type":"application/json"})
        with urllib.request.urlopen(req2, timeout=20, context=ctx) as r:
            print("  -> uploaded to GitHub (dashboard updated)")
    except Exception as e:
        print("  ! GitHub upload failed:", e)
        time.sleep(3)
        try:
            with urllib.request.urlopen(req2, timeout=20, context=ctx) as r:
                print("  -> uploaded to GitHub on retry (dashboard updated)")
        except Exception as e2:
            print("  ! GitHub upload retry failed:", e2)
def uploader_loop():
    while True:
        time.sleep(1800)   # every 30 minutes
        upload_github()
threading.Thread(target=uploader_loop, daemon=True).start()

# ==================== LIVE / SHADOW EXECUTION BRIDGE ====================
import hashlib, hmac as _hmac

def _fload(p):
    try: return open(p).read().strip()
    except Exception: return ""
CDCX_KEY   = _fload(CDCX_KEY_FILE)
CDCX_SECRET= _fload(CDCX_SECRET_FILE)
BIN_KEY    = _fload("/root/.binance_key")
BIN_SECRET = _fload("/root/.binance_secret")
TG_TOKEN   = _fload(TG_TOKEN_FILE)
TG_CHAT    = _fload(TG_CHAT_FILE)
CDCX_BASE  = "https://api.coindcx.com"

def tg(msg):
    """Fire-and-forget Telegram alert."""
    if not (TG_TOKEN and TG_CHAT): return
    try:
        body = json.dumps({"chat_id": TG_CHAT, "text": msg[:900]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
              data=body, headers={"Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("  ! tg alert failed:", e)

TG_OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_offset")

def _tg_cmd(cmd):
    """Execute a Telegram control command. Only the owner's chat is honored."""
    try:
        if cmd in ("/stop", "stop", "/pause", "pause"):
            open(TG_SOFT_HALT, "w").write(str(datetime.now(IST)))
            tg("\u23f8 BOT PAUSED - no NEW entries. Open positions still managed with SL/TP. Send /start to resume.")
        elif cmd in ("/start", "start", "/resume", "resume"):
            if os.path.exists(TG_SOFT_HALT): os.remove(TG_SOFT_HALT)
            tg("\u25b6 BOT RESUMED - trading normally.")
        elif cmd in ("/kill", "kill"):
            open(HALT_FILE, "w").write("tg")
            tg("\U0001f6d1 BOT KILLED - process exits within ~60s. Restart needs the VPS terminal: bash /root/restart_bot.sh")
        elif cmd in ("/status", "status"):
            d = json.load(open(LEDGER_FILE)) if os.path.exists(LEDGER_FILE) else {}
            sim, live = d.get("sim", {}), d.get("live", {})
            pos = len(d.get("qml", {}).get("open", []))
            mode = "PAUSED (/stop)" if os.path.exists(TG_SOFT_HALT) else ("HALTED" if os.path.exists(HALT_FILE) else "RUNNING")
            tg(f"\U0001f4ca STATUS: {mode} | qml[{QML_MODE}] sim ${float(sim.get('equity',0)):.0f} (day {float(sim.get('day',0)):+.2f}R) | live ${float(live.get('equity',0)):.0f} | open {pos}")
    except Exception as e:
        print("  ! tg cmd failed:", e)

def _fill_watch_loop():
    """Close the fill->bracket gap: poll live-pending Binance orders every 20s and attach
    the bracket the moment one fills (the 15-min scan cadence left positions naked and
    caused -2021 'would immediately trigger' rejections)."""
    while True:
        time.sleep(20)
        if halt_requested(): return
        try:
            for pos in BOOK_QML['positions']:
                if not (pos.get("live") and pos.get("live_pending")): continue
                od = bget("/fapi/v1/order", symbol=pos["sym"], orderId=pos.get("bin_order_id"))
                if od.get("status") == "FILLED":
                    pos["live_pending"] = False
                    pos["fill_ts"] = int(time.time())
                    try:
                        b_place_bracket(pos)
                        tg(f"LIVE FILLED {pos['sym']} {pos['dir']} @ {pos['entry']} - SL/TP bracket placed")
                    except Exception as e:
                        tg(f"LIVE FILLED {pos['sym']} but BRACKET FAILED: {str(e)[:120]} - CLOSING POSITION (no naked trades)")
                        try: b_close_market(pos)
                        except Exception: pass
                    funnel_add("orders_filled")
                    a_log(dict(ev="fill", sym=pos["sym"], dir=pos["dir"], entry=pos["entry"],
                               sl=pos["sl"], tp=pos["tp"], fast=True))
        except Exception:
            pass

def tg_command_loop():
    """Remote control: poll Telegram for commands from the owner chat only."""
    offset = 0
    try: offset = int(open(TG_OFFSET_FILE).read().strip() or 0)
    except Exception: pass
    while True:
        time.sleep(15)
        if not (TG_TOKEN and TG_CHAT): continue
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset+1}&timeout=15"
            with urllib.request.urlopen(url, timeout=20) as r:
                ups = json.loads(r.read()).get("result", [])
            for u in ups:
                offset = u["update_id"]
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id", "")) != str(TG_CHAT).strip(): continue
                cmd = (m.get("text") or "").strip().lower()
                if cmd: _tg_cmd(cmd)
            try: open(TG_OFFSET_FILE, "w").write(str(offset))
            except Exception: pass
        except Exception:
            pass

def _cdcx_sign(body_str):
    return _hmac.new(CDCX_SECRET.encode(), body_str.encode(), hashlib.sha256).hexdigest()

def cdcx_post(path, payload, timeout=15):
    """Signed POST to CoinDCX. Raises on error."""
    payload = dict(payload); payload["timestamp"] = int(time.time()*1000)
    body = json.dumps(payload, separators=(',', ':'))   # compact form - CoinDCX signs canonical compact JSON
    req = urllib.request.Request(CDCX_BASE + path, data=body.encode(),
        headers={"Content-Type":"application/json","X-AUTH-APIKEY":CDCX_KEY,"X-AUTH-SIGNATURE":_cdcx_sign(body),
                 "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 "Accept":"application/json","Origin":"https://coindcx.com","Referer":"https://coindcx.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def cdcx_balance():
    """USDT balance (spot wallet via documented endpoint; futures wallet path is finalized in Phase C probe)."""
    try:
        d = cdcx_post("/exchange/v1/users/balances", {})
        for b in (d if isinstance(d, list) else [d]):
            if isinstance(b, dict) and b.get("currency") == "USDT":
                return float(b.get("balance", 0) or 0)
        return d
    except Exception as e:
        return f"ERR {e}"

SHADOW_LEDGER = "/root/shadow_ledger.jsonl"

def shadow_log(rec):
    try:
        with open(SHADOW_LEDGER, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print("  ! shadow ledger write failed:", e)

_TICKER_CACHE = {"t": 0, "data": None}
def cdcx_price(sym):
    """CoinDCX spot ticker price (public /exchange/ticker, verified working).
    Market format in list is 'BTCUSDT'. Cached 30s. Returns float or None (symbol not listed)."""
    try:
        now = time.time()
        if not _TICKER_CACHE["data"] or now - _TICKER_CACHE["t"] > 30:
            with urllib.request.urlopen(urllib.request.Request("https://api.coindcx.com/exchange/ticker",
                  headers={"User-Agent":"Mozilla/5.0"}), timeout=10) as r:
                _TICKER_CACHE["data"] = json.loads(r.read().decode())
            _TICKER_CACHE["t"] = now
        for row in (_TICKER_CACHE["data"] if isinstance(_TICKER_CACHE["data"], list) else []):
            if isinstance(row, dict) and row.get("market") == sym:
                return float(row.get("last_price"))
        return None
    except Exception:
        return None

def cdcx_market(sym):
    """(price, 24h quote volume USDT) from cached ticker, or (None, 0)."""
    p = cdcx_price(sym)
    try:
        for row in (_TICKER_CACHE["data"] if isinstance(_TICKER_CACHE.get("data"), list) else []):
            if isinstance(row, dict) and row.get("market") == sym:
                vol = float(row.get("volume", 0) or 0)
                return p, (vol * p if p else 0.0)
    except Exception: pass
    return p, 0.0

_BVOL = {"t": 0, "d": {}}
_QVOL = {"t": 0, "d": {}}
def binance_qvol(sym):
    """Binance USDT-M 24h quote volume (all-symbols ticker, cached 10 min)."""
    try:
        if time.time() - _QVOL["t"] > 600 or sym not in _QVOL["d"]:
            with urllib.request.urlopen(urllib.request.Request(
                    "https://fapi.binance.com/fapi/v1/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
                for t in json.loads(r.read()):
                    _QVOL["d"][t.get("symbol")] = float(t.get("quoteVolume") or 0)
            _QVOL["t"] = time.time()
        return _QVOL["d"].get(sym, 0.0)
    except Exception:
        return _QVOL["d"].get(sym, 0.0)   # on fetch failure keep last known; 0 if never fetched

def _binance_quote_vol(sym):
    try:
        if time.time() - _BVOL["t"] > 600:
            with urllib.request.urlopen(urllib.request.Request(
                "https://fapi.binance.com/fapi/v1/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
                for x in json.loads(r.read().decode()):
                    _BVOL["d"][x.get("symbol")] = float(x.get("quoteVolume", 0) or 0)
            _BVOL["t"] = time.time()
        return _BVOL["d"].get(sym, 0.0)
    except Exception:
        return 9e18   # on fetch failure, do not block trading

def exec_guard(sym, entry):
    """Venue-aware entry guard."""
    if LIVE_VENUE == "BINANCE":
        f = b_filters(sym)
        if not f:
            return False, "no Binance USDT-perp market"
        qv = _binance_quote_vol(sym)
        if qv < LIQ_MIN_QVOL:
            return False, f"thin book (24h ${qv/1e6:.0f}M < ${LIQ_MIN_QVOL/1e6:.0f}M)"
        if sym in _BLOCKED:
            return False, "previously rejected on Binance"
        return True, f"binance vol ${qv/1e6:.0f}M"
    if not (CDCX_KEY and CDCX_SECRET): return True, ""
    if PAIRS_LIVE and sym not in PAIRS:
        return False, "not listed on CoinDCX USDT futures"
    if sym in _BLOCKED:
        return False, "not listed on CoinDCX USDT futures (previously rejected)"
    p, qv = cdcx_market(sym)
    if p is not None:
        basis = (p - entry) / entry * 100
        if abs(basis) > MAX_BASIS_PCT: return False, f"basis {basis:+.2f}% > {MAX_BASIS_PCT}%"
        if qv < MIN_COINDCX_VOL: return False, f"thin book (24h vol ${qv:,.0f}, cdcx)"
        return True, f"basis {basis:+.2f}%"
    qv = _binance_quote_vol(sym)
    if qv < MIN_COINDCX_VOL: return False, f"thin book (24h vol ${qv:,.0f}, binance proxy)"
    return True, "basis n/a (futures-only listing)"
def _live_ok():
    return MODE=="LIVE" and ((LIVE_VENUE=="BINANCE" and BIN_KEY and BIN_SECRET) or (LIVE_VENUE=="CDCX" and CDCX_KEY and CDCX_SECRET)) and os.path.exists(LIVE_ARMED)

def bridge_open(pos, strategy):
    """Place the live bracket order. Returns True ONLY if the exchange accepted it.
    PAPER -> True (nothing to do). SHADOW -> logged, True.
    LIVE  -> True (order resting/filled) or False (rejected / not armed).
    If False, the caller MUST drop the position (ghost-position guard)."""
    bm = pos.get("bmode", "PAPER")
    if bm == "PAPER": return True
    msg = f"[{bm}] OPEN {pos['sym']} {pos['dir']} entry={pos['entry']} sl={pos['sl']} tp={pos['tp']} risk={pos['risk_inr']}"
    if bm == "SHADOW":
        sp = cdcx_price(pos["sym"])
        basis = round((sp-pos["entry"])/pos["entry"]*100, 3) if sp else None
        shadow_log(dict(t=str(datetime.now(IST))[:19], event="open", sym=pos["sym"], dir=pos["dir"],
                        entry=pos["entry"], cdcx=sp, basis_pct=basis, sl=pos["sl"], tp=pos["tp"],
                        risk_inr=pos["risk_inr"], strategy=strategy))
        print("  SHADOW-INTENT", msg, "| cdcx:", sp, "| basis%:", basis)
        tg(msg + f" | cdcx={sp} basis={basis}%")
        return True
    if bm == "SIM":
        import uuid as _uuid
        # TP parity with live: cap at LIVE_TP_R from entry (live path applies this in its
        # sizing loop; SIM skipped it - CRV 2026-10-03 proved the divergence matters)
        _r0 = abs(pos["entry"] - pos["sl"]) or 1e-9
        if pos["dir"] == "LONG":
            pos["tp"] = round(min(pos["tp"], pos["entry"] + LIVE_TP_R*_r0), 10)
        else:
            pos["tp"] = round(max(pos["tp"], pos["entry"] - LIVE_TP_R*_r0), 10)
        pos["margin_req"] = round(pos["qty"] * pos["entry"] / LIVE_LEVERAGE, 2)
        pos["lev"] = LIVE_LEVERAGE
        pos["live_pending"] = True; pos["pending_scans"] = 0
        pos["live_oid"] = "sim-" + _uuid.uuid4().hex[:12]
        funnel_add("orders_submitted")
        a_log(dict(ev="order", sim=True, sym=pos["sym"], dir=pos["dir"], p1=pos["entry"],
                   order_px=pos["entry"], qty=pos["qty"], lev=LIVE_LEVERAGE,
                   basis_at_order=None, signal_age_s=None, ack_ms=0))
        tg(f"[SIM] OPEN {pos['sym']} {pos['dir']} qty={pos['qty']} @ {pos['entry']} sl={pos['sl']} tp={pos['tp']} (sim bank ${SIM_STATE['equity']:.0f})")
        print(f"  SIM-OPEN  {pos['sym']} {pos['dir']} @ {pos['entry']} (no exchange call)")
        return True
    if not _live_ok():
        print("  ! LIVE requested but not armed (keys/LIVE_ARMED) - skipped"); return False
    import uuid as _uuid, urllib.error as _ue
    try:
        if LIVE_VENUE != "BINANCE":
            tg(f"LIVE SKIP {pos['sym']}: venue {LIVE_VENUE} not wired in this build"); return False
        f = b_filters(pos["sym"])
        if not f:
            tg(f"LIVE SKIP {pos['sym']}: no Binance USDT-perp market"); return False
        risk_usdt = float(pos["risk_inr"])
        _e = b_round(pos["sym"], pos["entry"]); _s = b_round(pos["sym"], pos["sl"])
        _dist = abs(_e - _s)
        if _dist <= 0 or _dist < pos["risk"] * 0.20:
            tg(f"LIVE SKIP {pos['sym']} {pos['dir']}: stop collapses at tick"); return False
        qty = b_qty(pos["sym"], risk_usdt / _dist)
        if qty < f.get("minQty", 0):
            tg(f"LIVE SKIP {pos['sym']}: qty below min"); return False
        tp_live = (min(pos["tp"], pos["entry"] + LIVE_TP_R*_dist) if pos["dir"]=="LONG"
                   else max(pos["tp"], pos["entry"] - LIVE_TP_R*_dist))
        _t = b_round(pos["sym"], tp_live)
        lev = LIVE_LEVERAGE
        b_margin_isolated(pos["sym"])
        b_leverage(pos["sym"], lev)
        wallet = LIVE_WALLET_USDT * 0.95
        slot = wallet / max(1, LIVE_CONCURRENT)
        avail = max(0.0, wallet - _committed_margin())
        cap = min(slot, avail)
        notional = qty * _e
        if notional / lev > cap:
            qty = b_qty(pos["sym"], cap * lev / _e)
            notional = qty * _e
            if qty < f.get("minQty", 0):
                tg(f"LIVE SKIP {pos['sym']} {pos['dir']}: no free margin slot"); return False
        if notional < f.get("minNotional", 5):
            tg(f"LIVE SKIP {pos['sym']}: below min notional"); return False
        cid = "ba-" + _uuid.uuid4().hex[:20]
        # -2027 (CAPUSDT 2026-10-06): Binance caps notional per leverage tier; small coins
        # can't carry 25x size. Step leverage DOWN and re-size instead of failing the trade.
        res = None
        for _try_lev in (LIVE_LEVERAGE, 10, 5):
            try:
                if _try_lev != lev:
                    b_leverage(pos["sym"], _try_lev)
                    lev = _try_lev
                    if notional / lev > cap:
                        qty = b_qty(pos["sym"], cap * lev / _e)
                        notional = qty * _e
                        if qty < f.get("minQty", 0):
                            raise RuntimeError("below min qty at reduced leverage")
                res = bpost("/fapi/v1/order", {"symbol": pos["sym"], "side": "BUY" if pos["dir"]=="LONG" else "SELL",
                             "type": "LIMIT", "timeInForce": "GTC", "quantity": qty, "price": _e,
                             "newClientOrderId": cid})
                break
            except Exception as _e2027:
                if "-2027" in str(_e2027) or "maximum allowable position" in str(_e2027):
                    continue
                raise
        if res is None:
            tg(f"LIVE SKIP {pos['sym']} {pos['dir']}: no leverage tier fits 25/10/5x (position cap)")
            return False
        funnel_add("orders_submitted")
        a_log(dict(ev="order", sym=pos["sym"], dir=pos["dir"], p1=pos["entry"], order_px=_e, qty=qty,
                   lev=lev, signal_age_s=None, ack_ms=None))
        pos.update(entry=_e, sl=_s, tp=_t, risk=_dist, qty=qty, lev=lev,
                   live_oid=str(res.get("orderId")), client_order_id=cid, bin_order_id=res.get("orderId"),
                   margin_req=round(notional/lev, 2), open_ms=int(time.time()*1000))
        try:   # verify the margin fix took (was silently cross 20x)
            _pr = bget("/fapi/v2/positionRisk", symbol=pos["sym"])
            _p0 = _pr[0] if isinstance(_pr, list) and _pr else {}
            if str(_p0.get("marginType", "")).lower() != "isolated" or int(float(_p0.get("leverage", 0) or 0)) != lev:
                tg(f"!! {pos['sym']}: margin verify FAILED - marginType={_p0.get('marginType')} lev={_p0.get('leverage')} (want ISOLATED {lev}x). Position is open - fix in app.")
        except Exception:
            pass
        pos["live_pending"] = True; pos["pending_scans"] = 0
        tg(f"LIVE OPEN {pos['sym']} {pos['dir']} qty={qty} @ {_e} sl={_s} tp={_t} {lev}x id={res.get('orderId')}")
        print(f"  LIVE-OPEN (binance limit) id={res.get('orderId')} qty={qty} @ {_e}")
        return True
    except Exception as e:
        tg(f"LIVE OPEN FAILED {pos['sym']} {pos['dir']}: {str(e)[:180]}")
        print("  ! LIVE-OPEN failed:", e)
        return False
def bridge_part(bk, pos, frac, r):
    bm = pos.get("bmode", "PAPER")
    if bm == "PAPER": return
    msg = f"[{bm}] PARTIAL {pos['sym']} close {frac} at {r:+.2f}R (left {pos['left']})"
    if bm == "SHADOW": print("  SHADOW", msg); return
    if not _live_ok(): return
    print(f"  LIVE partial {frac} skipped (v1: bracket holds full size, SL trails via edit_sl)")

def bridge_close(bk, pos):
    bm = pos.get("bmode", "PAPER")
    if bm == "PAPER": return
    msg = f"[{bm}] CLOSED {pos['sym']} {pos['dir']} {pos.get('resultR','?'):+.2f}R (Rs.{pos.get('pnl',0):,.0f}) [{ ' , '.join(pos.get('parts',[])) }]"
    if bm == "SHADOW":
        sp = cdcx_price(pos["sym"])
        exit_px = pos.get("exit")
        slip = round((sp-exit_px)/exit_px*100, 3) if (sp and exit_px) else None
        shadow_log(dict(t=str(datetime.now(IST))[:19], event="close", sym=pos["sym"], dir=pos["dir"],
                        resultR=pos.get("resultR"), paper_exit=exit_px, cdcx=sp, slip_pct=slip))
        print("  SHADOW", msg, "| cdcx:", sp, "| slip%:", slip)
        tg(msg + f" | cdcx={sp} slip={slip}%")
        return
    if not _live_ok(): return
    try:
        closed = b_close_market(pos)
        tg(f"LIVE CLOSE {pos['sym']} -> market-closed {closed} on Binance")
        tg(msg)
    except Exception as e:
        tg(f"LIVE CLOSE FAILED {pos['sym']}: {e}")

LIVE_STATE = {"equity": 200.0, "day": 0.0, "cur_day": ""}

# ---- CoinDCX OFFICIAL futures flow (/exchange/v1, HMAC classic key - PROVEN by probe) ----
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

def xpost(path, payload, timeout=15):
    payload = dict(payload); payload["timestamp"] = int(time.time()*1000)
    body = json.dumps(payload, separators=(',', ':'))
    req = urllib.request.Request("https://api.coindcx.com" + path, data=body.encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "X-AUTH-APIKEY": CDCX_KEY, "X-AUTH-SIGNATURE": _cdcx_sign(body),
        "User-Agent": _UA, "Origin": "https://coindcx.com", "Referer": "https://coindcx.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def cdcx_cancel_pair_all(pair):
    """VERIFIED against CoinDCX (probe 2026-10-04 + LYN phantom 2026-10-05).
    Goal = NO resting orders for this pair. Semantics:
    - orders found & all cancelled          -> (True, n/n cancelled)
    - orders found & cancel failed          -> (False, retry next scan)   <- only true failure
    - nothing found / list errored / dead pair -> (True, nothing to protect)
      (a phantom position on a dead pair is unprotectable - the correct action is to DROP it)"""
    try:
        st, r = xpost("/exchange/v1/derivatives/futures/orders", {"pair": pair})
        items = r if isinstance(r, list) else []
    except Exception as e:
        return True, f"list error: {str(e)[:60]}"
    resting = [o for o in items if (o.get("status") or "").lower() in
               ("untriggered", "open", "pending", "accepted", "trigger pending") and o.get("id")]
    if not resting:
        return True, "0 resting"
    n = 0
    for o in resting:
        try:
            s2, _ = xpost("/exchange/v1/derivatives/futures/orders/cancel", {"id": o["id"], "order_id": o["id"]})
            if s2 == 200: n += 1
        except Exception:
            pass
    return n == len(resting), f"{n}/{len(resting)} cancelled"

# ==================== BINANCE USDT-M LIVE BRIDGE ====================
BIN_BASE = "https://fapi.binance.com"

def bsign(qs):
    return _hmac.new(BIN_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()

def breq(method, path, params=None, timeout=15):
    params = {k: v for k, v in dict(params or {}).items() if v is not None}
    params["timestamp"] = int(time.time()*1000); params["recvWindow"] = 5000
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    qs += "&signature=" + bsign(qs)
    req = urllib.request.Request(BIN_BASE + path + "?" + qs, method=method,
        headers={"X-MBX-APIKEY": BIN_KEY, "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:250]}")

def bget(p, payload=None, **kw):
    q = dict(payload or {}); q.update(kw); return breq("GET", p, q)
def bpost(p, payload=None, **kw):
    q = dict(payload or {}); q.update(kw); return breq("POST", p, q)
def bdel(p, payload=None, **kw):
    q = dict(payload or {}); q.update(kw); return breq("DELETE", p, q)

_BINFO = {"t": 0, "d": {}}
def b_filters(sym):
    """Exact exchange rules: tick, qty step, min qty, min notional. Cached 6h."""
    try:
        if time.time() - _BINFO["t"] > 6*3600 or sym not in _BINFO["d"]:
            with urllib.request.urlopen(BIN_BASE + "/fapi/v1/exchangeInfo", timeout=25) as r:
                for s in json.loads(r.read()).get("symbols", []):
                    if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
                        continue
                    f = {}
                    for fl in s.get("filters", []):
                        if fl["filterType"] == "PRICE_FILTER": f["tick"] = float(fl["tickSize"])
                        if fl["filterType"] == "LOT_SIZE":     f["step"] = float(fl["stepSize"]); f["minQty"] = float(fl["minQty"])
                        if fl["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"): f["minNotional"] = float(fl.get("notional") or fl.get("minNotional") or 5)
                    _BINFO["d"][s["symbol"]] = f
            _BINFO["t"] = time.time()
        return _BINFO["d"].get(sym)
    except Exception:
        return _BINFO["d"].get(sym)

def b_round(sym, px):
    f = b_filters(sym) or {"tick": 0.0001}
    t = f["tick"]
    dec = max(0, int(round(-math.log10(t)))) if t < 1 else 0
    return round(math.floor(px / t) * t, dec)

def b_qty(sym, q):
    f = b_filters(sym) or {"step": 0.01}
    return round(math.floor(q / f["step"]) * f["step"], 6)

def b_leverage(sym, lev):
    try:
        bpost("/fapi/v1/leverage", symbol=sym, leverage=lev)
    except Exception as e:
        import re as _re
        m = _re.search(r'[Ll]everage\s+(\d+)', str(e))
        if m:
            try: bpost("/fapi/v1/leverage", symbol=sym, leverage=int(m.group(1)))
            except Exception: pass   # keep account default if even the cap fails

def b_single_asset_mode():
    """ROOT CAUSE 2026-10-06 (error -4168 on every marginType call): the ACCOUNT was in
    Multi-Assets mode, which rejects ALL isolated margin account-wide. Flip to Single-Asset
    so per-symbol isolated works. Requires flat account (Binance rule); retry next boot."""
    try:
        bpost("/fapi/v1/multiAssetsMargin", multiAssetsMargin="false")
        print("  account switched to Single-Asset mode - isolated margin now available")
        return True
    except Exception as e:
        s = str(e)
        if "false" in s or "already" in s.lower():
            return True
        print("  ! multiAssetsMargin(false) failed (open positions/orders?):", s[:120])
        return False

def b_margin_isolated(sym):
    """LIVE BUG 2026-10-06 (app showed 'Cross 20x' on bot positions): /fapi/v1/leverage sets the number
    but NOT the margin mode - marginType is a separate setting and was never called, so every position
    silently ran the account default (cross). Isolated first, always. Binance returns 'No need to change
    margin type' when already isolated - that error is success."""
    try:
        bpost("/fapi/v1/marginType", symbol=sym, marginType="ISOLATED")
    except Exception as e:
        if "No need to change" in str(e):
            return
        if "-4168" in str(e) or "Multi-Assets" in str(e):
            # account-level root cause: flip once, retry the symbol
            if b_single_asset_mode():
                try:
                    bpost("/fapi/v1/marginType", symbol=sym, marginType="ISOLATED")
                    return
                except Exception as e2:
                    print(f"  ! marginType({sym}) retry failed:", str(e2)[:90])
                    return
        print(f"  ! marginType({sym}) failed:", str(e)[:90])

def b_position(sym):
    try:
        for p in bget("/fapi/v2/positionRisk", symbol=sym):
            if p.get("symbol") == sym:
                return float(p.get("positionAmt", 0) or 0), float(p.get("entryPrice", 0) or 0)
    except Exception:
        pass
    return 0.0, 0.0

def _b_algo_leg(pos, otype, trig):
    side = "SELL" if pos["dir"] == "LONG" else "BUY"
    return bpost("/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": pos["sym"],
        "side": side, "type": otype, "triggerPrice": b_round(pos["sym"], trig),
        "quantity": pos["qty"], "reduceOnly": "true", "workingType": "CONTRACT_PRICE"})

def _b_verify_leg(pos, key, otype, trig):
    """Binance's algo service occasionally drops legs from queries - verify and recreate."""
    oid = pos.get(key)
    if not oid: return
    try:
        d = bget("/fapi/v1/algoOrder", symbol=pos["sym"], algoId=oid)
        if d.get("algoStatus") != "NEW":
            raise RuntimeError("leg not active")
    except Exception:
        try:
            r = _b_algo_leg(pos, otype, trig)
            pos[key] = r.get("algoId")
        except Exception as e:
            tg(f"!! BRACKET LEG MISSING {pos['sym']} ({otype}) and recreate failed: {str(e)[:90]} - check app NOW")

def b_place_bracket(pos):
    """Attach SL + TP as algo conditional orders (verified: /fapi/v1/order rejects STOP/TP with
    -4120; the algoOrder service is home for legs). -2021 self-heal: if a leg's trigger is ALREADY
    reached at placement time, the condition is true NOW - execute it as a market exit (the stop or
    target would have fired anyway) instead of leaving the position naked (UNI 2026-10-06).
    CLOSE-BASED STOP 2026-10-06: the SL leg rests at the HARD level (soft SL + 1 ATR) - catastrophe
    protection only. The real stop is software: exit on candle CLOSE beyond the soft level."""
    _buf = CLOSE_STOP_HARD_ATR * (float(pos.get("atr") or 0) or abs(pos["sl"]-pos["entry"]))
    _hard = (pos["sl"] - _buf) if pos["dir"] == "LONG" else (pos["sl"] + _buf)
    placed = {}
    for otype, trig, key in (("STOP_MARKET", _hard, "sl_oid"), ("TAKE_PROFIT_MARKET", pos["tp"], "tp_oid")):
        try:
            r = _b_algo_leg(pos, otype, trig)
            pos[key] = r.get("algoId"); placed[key] = True
        except Exception as e:
            if "immediately trigger" in str(e):
                _kind = "SL" if otype == "STOP_MARKET" else "TP"
                tg(f"LIVE {pos['sym']} {pos['dir']}: {_kind} level already reached at bracket time - closing at market (was: {str(e)[:60]})")
                b_close_market(pos)
                raise RuntimeError(f"{_kind} immediately triggered - closed at market")
            raise
    _b_verify_leg(pos, "sl_oid", "STOP_MARKET", pos["sl"])
    _b_verify_leg(pos, "tp_oid", "TAKE_PROFIT_MARKET", pos["tp"])

def b_modify_sl(pos, new_stop):
    """T1 HARVEST LOCK - verified on the live account: algo legs have no PUT modify, so the
    lock = cancel the old SL leg + recreate at the lock price (2 native calls, ~300ms).
    Raises on failure so the manager retries next scan."""
    side = "SELL" if pos["dir"] == "LONG" else "BUY"
    try:
        bdel("/fapi/v1/algoOrder", symbol=pos["sym"], algoId=pos["sl_oid"])
    except Exception:
        pass
    r = _b_algo_leg(pos, "STOP_MARKET", new_stop)
    if not r.get("algoId"):
        raise RuntimeError("recreate returned no algoId")
    pos["sl_oid"] = r.get("algoId")

def b_cancel_order(sym, oid):
    """2026-10-06: a MANUAL cancel in the app made the bot retry forever - Binance returns
    'order does not exist' and the old code read that as failure, spamming cancel FAILED
    every scan. Already-gone IS the goal. Treat as success."""
    try:
        return bdel("/fapi/v1/order", symbol=sym, orderId=oid)
    except Exception as e:
        if "does not exist" in str(e) or "Unknown order" in str(e):
            return {"status": "CANCELLED"}
        raise

def b_close_market(pos):
    for oid in (pos.get("sl_oid"), pos.get("tp_oid")):
        if oid:
            try: bdel("/fapi/v1/algoOrder", symbol=pos["sym"], algoId=oid)
            except Exception: pass
    amt, _ = b_position(pos["sym"])
    if abs(amt) > 0:
        bpost("/fapi/v1/order", symbol=pos["sym"], side="SELL" if amt > 0 else "BUY",
              type="MARKET", quantity=b_qty(pos["sym"], abs(amt)), reduceOnly="true")
    return abs(amt)

def b_trades_pnl(sym, since_ms):
    try:
        since = int(since_ms) if since_ms else int(time.time()*1000) - 7*86400000   # fallback: last 7 days
        tot = 0.0
        for t in bget("/fapi/v1/userTrades", symbol=sym, startTime=since, limit=100):
            tot += float(t.get("realizedPnl") or 0)
        return tot
    except Exception:
        return None

def futures_positions():
    try:
        d = xpost("/exchange/v1/derivatives/futures/positions", {})
        return d if isinstance(d, list) else []
    except Exception:
        return []

# ---- legacy /api/v1 JWT auth (kept for reference; not used by LIVE bridge) ----
AUTH_FILE = "/root/.cdcx_jwt"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_TX = {"token": None, "t": 0}

def _authtoken():
    t = _fload(AUTH_FILE).strip()
    if len(t) >= 2 and t[0] in '"\'' and t[-1] == t[0]:
        t = t[1:-1].strip()
    return t

def mint_tx_token():
    import uuid
    at = _authtoken()
    print("  jwt file: len=%d head=%s tail=%s" % (len(at), at[:10], at[-8:]))
    body = json.dumps({"purpose": "chart_sync"}, separators=(',', ':'))
    qs = ("?token=" + uuid.uuid4().hex + "&correlationId=" + str(uuid.uuid4()) +
          "&application=coindcx-charts-production&x=" + str(uuid.uuid4()) +
          "&deviceId=" + (_devtoken() or uuid.uuid4().hex))
    req = urllib.request.Request("https://api.coindcx.com/api/v1/users/transaction_token" + qs,
        data=body.encode(), headers={"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": "Bearer " + _authtoken(), "User-Agent": _UA,
            "Origin": "https://coindcx.com", "Referer": "https://coindcx.com/"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tx = r.headers.get("X-Transaction-Token")
            r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: body = raw.decode()
        except Exception: body = repr(raw[:200])
        raise RuntimeError(f"tx mint failed {e.code}: {body[:250]}")
    if not tx: raise RuntimeError("X-Transaction-Token missing in response")
    return tx

def get_tx(force=False):
    if not force and _TX["token"] and time.time() - _TX["t"] < 300:
        return _TX["token"]
    _TX["token"] = mint_tx_token(); _TX["t"] = time.time()
    return _TX["token"]

DEVICE_FILE = "/root/.cdcx_device"
def _devtoken():
    return _fload(DEVICE_FILE)

def api_v1(path, payload, use_auth=False, query=True):
    import uuid
    if query and path.startswith("/api/v1/derivatives"):
        path = (path + "?margin_currency_short_name=USDT&application=coindcx-charts-production&x=" +
                str(uuid.uuid4()) + "&correlationId=" + str(uuid.uuid4()) +
                "&token=" + uuid.uuid4().hex + "&deviceId=" + (_devtoken() or uuid.uuid4().hex))
    """POST to the new /api/v1 platform. Auto-refreshes transaction token on 401 once."""
    body = json.dumps(payload, separators=(',', ':'))
    def call(auth_header):
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "Authorization": auth_header, "User-Agent": _UA,
             "Origin": "https://coindcx.com", "Referer": "https://coindcx.com/"}
        if _devtoken(): h["X-Device-Token"] = _devtoken()
        req = urllib.request.Request("https://api.coindcx.com" + path, data=body.encode(), headers=h)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    auth = ("Bearer " + _authtoken()) if use_auth else get_tx()
    try:
        return call(auth)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            if use_auth: raise
            return call(get_tx(force=True))
        raise

def margin_probe():
    """OFFICIAL CREATE TEST: HMAC + /exchange/v1/derivatives/futures/orders/create + exact order payload.
    If this works: official keys, no tokens, no copy-paste. Leaves position open for app verification."""
    print("=== OFFICIAL CREATE TEST (/orders/create + HMAC + exact payload) ===")
    if not (CDCX_KEY and CDCX_SECRET): print("keys missing"); return
    import urllib.error, uuid, re
    px, _ = cdcx_market("SOLUSDT")
    print("SOL ref:", px)
    if not px: return
    cid = "ba-" + uuid.uuid4().hex[:20]

    def create(path):
        o = {"fromOrderForm": False, "leverage": LIVE_LEVERAGE, "margin_currency_short_name": "USDT",
             "notification": "email_notification", "order_type": "market_order", "pair": "B-SOL_USDT",
             "position_margin_type": "isolated", "side": "buy", "client_order_id": cid,
             "stop_loss_price": round(px*0.96, 2), "take_profit_price": round(px*1.04, 2),
             "total_quantity": 0.09}   # ~$10.65: above $6 minimum AND divisible by 0.01
        body = json.dumps({"timestamp": int(time.time()*1000), "order": o}, separators=(',', ':'))
        req = urllib.request.Request("https://api.coindcx.com" + path, data=body.encode(), headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-AUTH-APIKEY": CDCX_KEY, "X-AUTH-SIGNATURE": _cdcx_sign(body),
            "User-Agent": _UA, "Origin": "https://coindcx.com", "Referer": "https://coindcx.com/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()

    for path in ("/exchange/v1/derivatives/futures/orders/create",):
        try:
            st, resp = create(path)
            fresh = False
            m = re.search(r'"created_at":\s*"?([0-9]+)', resp)
            if m:
                try: fresh = abs(time.time() - int(m.group(1))) < 300
                except Exception: pass
            print(f"[OK {path}] {st}")
            print("  RAW:", resp[:350])
            print(f"  client_order_id {cid}:", "FOUND" if cid in resp else "NOT FOUND", "| created_at fresh:", fresh)
            print("  >>> CHECK THE APP NOW: small SOL position with SL/TP? Close manually, report (a)(b)(c).")
            tg("official create test order placed - verify app")
            return
        except urllib.error.HTTPError as e:
            print(f"[{e.code} {path}]", e.read().decode()[:150])
        except Exception as e:
            print(f"[ERR {path}]", str(e)[:120])
    print("both paths failed - paste output")

def startup_report():
    print(f"  sweep[{SWEEP_MODE}] qml[{QML_MODE}] live equity Rs.{LIVE_STATE['equity']:,.0f}  keys={'yes' if (BIN_KEY if LIVE_VENUE=='BINANCE' else CDCX_KEY) else 'NO'}  tg={'yes' if TG_TOKEN else 'NO'}")
    tg(f"BA bot started - SWEEP=PAPER (Rs.{BOOK_SWEEP['equity']:,.0f} paper) | QML=LIVE (Rs.{LIVE_STATE['equity']:,.0f} real; paper book Rs.{BOOK_QML['equity']:,.0f} frozen)")

def probe():
    print("=== COINDCX / TELEGRAM PROBE ===")
    print("1. creds:", "key OK" if CDCX_KEY else "KEY MISSING", "|", "secret OK" if CDCX_SECRET else "SECRET MISSING", "|", "tg token OK" if TG_TOKEN else "tg MISSING", "|", "chat", TG_CHAT or "MISSING")
    if CDCX_KEY and CDCX_SECRET:
        try:
            b = cdcx_balance(); print("2. futures wallet balance:", b)
        except Exception as e: print("2. balance FAILED:", e)
    p = cdcx_price("BTCUSDT")
    print("2b. cdcx price (BTC):", p)
    if TG_TOKEN and TG_CHAT:
        try: tg("BA bot probe: alerts working."); print("3. telegram alert sent - check your chat")
        except Exception as e: print("3. tg FAILED:", e)
    print("NOTE: futures order endpoints are used only in LIVE mode; verify with a min-size probe order in Phase C.")

# ==================== DATA ====================
def get_klines(symbol):
    try:
        with urllib.request.urlopen(DATA_URL.format(symbol), timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! {symbol} fetch failed: {e}")
        return None

def pearson(a, b):
    n = min(len(a), len(b))
    if n < 10: return 0.0
    ma, mb = sum(a)/n, sum(b)/n
    num = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    da  = sum((a[i]-ma)**2 for i in range(n))
    db  = sum((b[i]-mb)**2 for i in range(n))
    return num/math.sqrt(da*db) if da>0 and db>0 else 0.0

def ema20(closes, upto):
    m = 2/21; e = closes[0]
    for i in range(1, upto+1): e = (closes[i]-e)*m + e
    return e

# ==================== ENGINE ====================
PAIRS = []  # loaded below

def detect_signal(sym, k, btc_k):
    """Replicates dashboard fetchPair: sweep + engulf + EMA20 gate + BTC-corr gate. Returns dict or None."""
    if not k or len(k) < 60 or not btc_k or len(btc_k) < 60: return None
    prev, cur = k[-3], k[-2]              # last closed vs previous (last element = forming)
    pO,pH,pL,pC = float(prev[1]),float(prev[2]),float(prev[3]),float(prev[4])
    cO,cH,cL,cC = float(cur[1]),float(cur[2]),float(cur[3]),float(cur[4])
    closes = [float(x[4]) for x in k]
    ema = ema20(closes, len(k)-2)
    bodyTop, bodyBot = max(pO,pC), min(pO,pC)
    LONG  = cL < pL and cC > bodyTop and cC > ema*1.005
    SHORT = cH > pH and cC < bodyBot and cC < ema*0.995
    if not (LONG or SHORT): return None
    # BTC correlation gate
    M = min(len(k), len(btc_k))
    cr = [ (float(k[i+1][4])-float(k[i][4]))/float(k[i][4]) for i in range(M-60, M-1) ]
    br = [ (float(btc_k[i+1][4])-float(btc_k[i][4]))/float(btc_k[i][4]) for i in range(M-60, M-1) ]
    corr = pearson(cr, br)
    bcloses = [float(x[4]) for x in btc_k]
    bema = ema20(bcloses, len(btc_k)-2)
    blast = bcloses[len(btc_k)-2]
    bUp, bDown = blast > bema*1.005, blast < bema*0.995
    if abs(corr) >= 0.45:
        if (LONG and bDown) or (SHORT and bUp): return None
    # TRAP filter (ledger logs A+/B only)
    vol = float(cur[5]); tb = float(cur[9])
    buyPct = tb/vol*100 if vol > 0 else 50
    if (LONG and buyPct < 45) or (SHORT and buyPct > 55): return None
    vavg = sum(float(x[5]) for x in k[-22:-2]) / 20
    volX = vol/vavg if vavg > 0 else 0
    # grade
    agree = (LONG and buyPct >= 55) or (SHORT and buyPct <= 45)
    grade = "A+" if (agree and volX >= 1.5) else "B"
    entry, sl = cC, (cL if LONG else cH)
    risk = abs(entry-sl)
    if risk <= 0 or risk/entry < 0.002: return None
    tp = entry + TP_R*risk if LONG else entry - TP_R*risk
    return dict(sym=sym, dir="LONG" if LONG else "SHORT", entry=entry, sl=sl, tp=tp,
                grade=grade, volX=round(volX,2), buyPct=round(buyPct,1), t=cur[6])

# ==================== PAPER EXECUTION (scale-out + trailing, per-strategy books) ====================
BOOK_SWEEP = dict(positions=[], history=[], equity=ACCOUNT_INR, max_pos=MAX_POSITIONS, tf_bars=6, day_r=0.0, cur_day=None)
SWEEP_SIGNALS = []   # last scan's detected signals (published to GitHub for the dashboard)

def load_state():
    if os.path.exists(LEDGER_FILE):
        try:
            d = json.load(open(LEDGER_FILE))
            BOOK_SWEEP['history'] = d.get("history", [])
            BOOK_SWEEP['equity']  = d.get("equity", ACCOUNT_INR)
            BOOK_SWEEP['positions'] = d.get("open", [])
            qm = d.get("qml", {})
            BOOK_QML['history'] = qm.get("history", [])
            BOOK_QML['equity']  = qm.get("equity", ACCOUNT_INR)
            BOOK_QML['positions'] = qm.get("open", [])
            BOOK_SWEEP['day_r'] = d.get("day_r", 0.0)
            _cd = d.get("cur_day", "")
            if _cd:
                try: BOOK_SWEEP['cur_day'] = datetime.strptime(_cd, "%Y-%m-%d").date()
                except Exception: pass
            BOOK_QML['day_r'] = d.get("qml_day_r", 0.0)
            _cd2 = d.get("qml_cur_day", "")
            if _cd2:
                try: BOOK_QML['cur_day'] = datetime.strptime(_cd2, "%Y-%m-%d").date()
                except Exception: pass
            liqsim_load(d)
            sweepsim_load(d)
            _sm = d.get('sim', {})
            if isinstance(_sm, dict):
                SIM_STATE['equity'] = float(_sm.get('equity', 400.0))
                SIM_STATE['day'] = float(_sm.get('day', 0.0))
                SIM_STATE['cur_day'] = str(_sm.get('cur_day', ''))
            _lv = d.get("live", {})
            if isinstance(_lv, dict):
                LIVE_STATE['equity'] = float(_lv.get('equity', LIVE_CAPITAL_INR))
                LIVE_STATE['day'] = float(_lv.get('day', 0.0))
                LIVE_STATE['cur_day'] = str(_lv.get('cur_day', ''))
                _stk = _lv.get('streak')   # circuit-breaker memory MUST survive restarts (ZEC 2026-10-06)
                if isinstance(_stk, list):
                    LIVE_STATE['streak'] = [(float(a), float(b)) for a, b in _stk
                        if isinstance(a, (int, float)) and isinstance(b, (int, float))][-4:]
        except Exception: pass

def save_state():
    json.dump(dict(history=BOOK_SWEEP['history'][-300:], open=BOOK_SWEEP['positions'], equity=BOOK_SWEEP['equity'],
                   sweep_signals=SWEEP_SIGNALS[:12],
                   qml=dict(history=BOOK_QML['history'][-300:], open=BOOK_QML['positions'],
                            equity=BOOK_QML['equity'], signals=QML_SIGNALS[:12]),
                   market=dict(rows=MKT_DATA[:150], t=str(datetime.now(IST))[:16]),
                   liq=dict(setups=LIQ_DATA[:20], t=str(datetime.now(IST))[:16]),
                   day_r=BOOK_SWEEP['day_r'], cur_day=str(BOOK_SWEEP['cur_day'] or ''),
                   qml_day_r=BOOK_QML['day_r'], qml_cur_day=str(BOOK_QML['cur_day'] or ''),
                   live=dict(LIVE_STATE), sim=dict(SIM_STATE),
                   report=dict(REPORT), funnel=dict(FUNNEL),
                   pairs=list(PAIRS)[:600], pairs_live=PAIRS_LIVE,
                   liq_shadow=dict(LIQ_SHADOW_STATS),
                   liqsim=dict(equity=LIQ_SIM['equity'], positions=LIQ_SIM['positions'],
                               history=LIQ_SIM['history'][-300:], day=LIQ_SIM['day'],
                               cur_day=LIQ_SIM['cur_day']),
                   sweepsim=dict(equity=SWEEP_SIM['equity'], positions=SWEEP_SIM['positions'],
                               history=SWEEP_SIM['history'][-300:], day=SWEEP_SIM['day'],
                               cur_day=SWEEP_SIM['cur_day']),
                   updated=str(datetime.now(IST))),
              open(LEDGER_FILE, "w"), indent=1)
    if GH_TOKEN: upload_github()

def close_part(bk, pos, frac, r):
    pnl = r * frac * pos["risk_inr"]
    tgt = LIVE_STATE if pos.get("live") else bk
    tgt['equity'] += pnl; tgt['day'] = tgt.get('day', 0.0) + r * frac; tgt['day_r'] = tgt.get('day_r', 0.0) + r * frac
    pos["r_acc"] += r * frac
    pos["left"] = round(pos["left"] - frac, 4)
    pos["pnl_sum"] += pnl
    pos["parts"].append(f"+{r}R x {frac}")
    bridge_part(bk, pos, frac, r)

def close_rest(bk, pos, r, exit_px):
    frac = pos["left"]
    if frac > 0:
        pnl = r * frac * pos["risk_inr"]
        tgt = LIVE_STATE if pos.get("live") else bk
        tgt['equity'] += pnl; tgt['day'] = tgt.get('day', 0.0) + r * frac; tgt['day_r'] = tgt.get('day_r', 0.0) + r * frac
        pos["r_acc"] += r * frac
        pos["pnl_sum"] += pnl
        pos["parts"].append(f"{r:+.2f}R x {frac}")
        pos["left"] = 0.0
    pos["exit"] = exit_px
    pos["resultR"] = round(pos["r_acc"], 2)
    pos["pnl"] = round(pos["pnl_sum"], 0)
    pos["exitTime"] = str(datetime.now(IST))[:16]
    bk['history'].append(pos)
    bridge_close(bk, pos)

def on_cooldown(bk, sym, d):
    """True if this sym+dir closed any trade within COOLDOWN_H hours (win or loss)."""
    now = datetime.now(IST)
    for h in bk['history'][-40:]:
        if h.get('sym')==sym and h.get('dir')==d and h.get('exitTime'):
            try:
                t = datetime.strptime(h['exitTime'], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                if (now - t).total_seconds() < COOLDOWN_H*3600: return True
            except Exception: pass
    return False

def _sim_manage(bk, pos, kmap):
    """SIM-mode lifecycle: expiry rules, simulated limit fill, SL/TP/time-stop exits.
    ASSUMED FILL MODEL - labeled, never presented as live truth (same honesty standard
    as the shadow twin). Exits priced at bracket levels; SL checked before TP when one
    candle spans both (pessimistic)."""
    k = kmap.get(pos["sym"])
    if not k or len(k) < 3: return
    bar = k[-2]
    hh, ll, cc = float(bar[2]), float(bar[3]), float(bar[4])
    lg = pos["dir"] == "LONG"
    if pos.get("live_pending"):
        pos["pending_scans"] = pos.get("pending_scans", 0) + 1
        _tp_hit = False
        try:
            _ts0 = datetime.strptime(pos.get("t_open", ""), "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp() * 1000
            for _b in k[:-1]:
                if int(float(_b[0])) < _ts0: continue
                if (lg and float(_b[2]) >= pos["tp"]) or ((not lg) and float(_b[3]) <= pos["tp"]):
                    _tp_hit = True; break
        except Exception:
            pass
        _atr = pos.get("atr") or (sum(float(k[x][2])-float(k[x][3]) for x in range(len(k)-16, len(k)-2))/14)
        _away = bool(_atr) and abs(cc - pos["entry"]) > EXEC_STALE_ATR * float(_atr)
        if _tp_hit or _away:
            _reason = "tp_already_hit" if _tp_hit else "left_level"
            bk['positions'].remove(pos)
            funnel_add("orders_cancelled"); funnel_add("gate_expired_at_rest")
            a_log(dict(ev="cancel", sim=True, sym=pos["sym"], dir=pos["dir"], cancelled=True,
                       cls="SIGNAL_EXPIRED", reason=_reason))
            _msg = "TP already traded - move consumed" if _tp_hit else "price left the level"
            tg(f"[SIM] EXPIRED {pos['sym']} {pos['dir']}: {_msg} - order cancelled")
            print(f"  [SIM] EXPIRED {pos['sym']} ({_reason})")
            return
        touched = (ll <= pos["entry"]) if lg else (hh >= pos["entry"])
        if touched:
            pos["live_pending"] = False
            funnel_add("orders_filled")
            a_log(dict(ev="fill", sim=True, sym=pos["sym"], dir=pos["dir"], entry=pos["entry"], sl=pos["sl"], tp=pos["tp"]))
            tg(f"[SIM] FILLED {pos['sym']} {pos['dir']} @ {pos['entry']}")
        elif pos["pending_scans"] >= FILL_TIMEOUT_SCANS:
            bk['positions'].remove(pos)
            funnel_add("orders_cancelled")
            a_log(dict(ev="cancel", sim=True, sym=pos["sym"], dir=pos["dir"], cancelled=True, cls="TIMEOUT"))
            tg(f"[SIM] TIMEOUT unfilled {pos['sym']}: cancelled")
        return
    risk = pos["risk"]
    exited, exit_px = None, None
    # MANAGEMENT LAYER (evidence-backed, SIM-tested): H banks half at +1R; T1 locks SL at +1R. Runner keeps the original bracket.
    _adv_r = ((hh - pos["entry"]) if lg else (pos["entry"] - ll)) / risk
    if SIM_MGMT in ("H", "T1") and pos.get("mgmt_state") is None and _adv_r >= 1.0:
        if SIM_MGMT == "H":
            pos["mgmt_state"] = "banked"; pos["mgmt_frac"] = 0.5
            _bank = round(0.5 * pos["risk_inr"], 2)
            SIM_STATE['equity'] += _bank; SIM_STATE['day'] += 0.5
            bk['equity'] += _bank; bk['day_r'] = bk.get('day_r', 0.0) + 0.5
            pos["pnl_parts"] = pos.get("pnl_parts", 0.0) + _bank
            pos["parts"].append(f"H banked +0.5R (${pos['risk_inr']/2:.0f})")
            tg(f"[SIM] BANK {pos['sym']} {pos['dir']} half at +1R - runner continues")
        else:
            pos["mgmt_state"] = "locked"
            pos["lock_px"] = pos["entry"] + risk if lg else pos["entry"] - risk
            tg(f"[SIM] LOCK {pos['sym']} {pos['dir']} SL->+1R ({round(pos['lock_px'],8)}) - runner continues")
    if pos.get("mgmt_state") == "locked":
        _lp = pos["lock_px"]
        _hit_lock = (ll <= _lp) if lg else (hh >= _lp)
        _hit_tp2 = (hh >= pos["tp"]) if lg else (ll <= pos["tp"])
        if _hit_lock and _hit_tp2: pos["ambiguous"] = True
        if _hit_lock:
            pos["bars"] = pos.get("bars", 0) + 1
            _final_close(pos, bk, "LOCK1R", _lp, lg, risk)
            return
    # CLOSE-BASED STOP mirror: wick into the level tolerated; die on CLOSE beyond; hard wick stop 1 ATR beyond
    _bufS = CLOSE_STOP_HARD_ATR * (float(pos.get("atr") or 0) or risk)
    _hardS = (pos["sl"] - _bufS) if lg else (pos["sl"] + _bufS)
    _softS = pos.get("lock_px", pos["sl"])
    _h_hard = (lg and ll <= _hardS) or ((not lg) and hh >= _hardS)
    _h_sl = (lg and _cc < _softS) or ((not lg) and _cc > _softS)
    _h_tp = (lg and hh >= pos["tp"]) or ((not lg) and ll <= pos["tp"])
    if _h_sl and _h_tp: pos["ambiguous"] = True
    if _h_hard:
        _wick = (_hardS - ll) if lg else (hh - _hardS)
        exit_px = (_hardS - SLIP_WICK_FRAC_DN*_wick) if lg else (_hardS + SLIP_WICK_FRAC_UP*_wick)
        pos["slip"] = round(abs(exit_px - _hardS), 10)
        exited = "SL_HARD"
    elif _h_sl:
        exit_px = _cc
        pos["slip"] = 0.0
        exited = "CLOSE_STOP"
    elif _h_tp: exited, exit_px = "TP", pos["tp"]
    pos["bars"] = pos.get("bars", 0) + 1
    if not exited and pos["bars"] >= bk['tf_bars']: exited, exit_px = "TIME_STOP", cc
    if exited:
        _final_close(pos, bk, exited, exit_px, lg, risk)
        return

def _final_close(pos, bk, exited, exit_px, lg, risk):
    frac = pos.get("mgmt_frac", 1.0)
    r = round(max(min((((exit_px - pos["entry"]) if lg else (pos["entry"] - exit_px)) / risk), 2.0), -2.0) * frac, 2)
    pnl = round(r * pos["risk_inr"] + pos.get("pnl_parts", 0.0), 2)
    SIM_STATE['equity'] += pnl; SIM_STATE['day'] += r
    bk['equity'] += pnl; bk['day_r'] = bk.get('day_r', 0.0) + r
    pos["resultR"] = r; pos["pnl"] = pnl; pos["exit"] = exit_px
    pos["exit_kind"] = exited; pos["exitTime"] = str(datetime.now(IST))[:16]
    bk['history'].append(pos); bk['positions'].remove(pos)
    funnel_add("exits_time_stop" if exited == "TIME_STOP" else ("exits_win" if r > 0 else "exits_loss"))
    a_log(dict(ev="exit", sim=True, sym=pos["sym"], dir=pos["dir"], kind=exited, r=r, pnl=pnl,
               exit_px=exit_px, slip=pos.get("slip", 0.0)))
    _amb = " (AMBIGUOUS: candle spanned SL+TP, priced pessimistically)" if pos.get("ambiguous") else ""
    tg(f"[SIM] CLOSED {pos['sym']} {pos['dir']} [{exited}] {r:+.2f}R (${pnl:+.2f}){_amb} - sim bank ${SIM_STATE['equity']:.0f} (day {SIM_STATE['day']:+.2f}R)")
    print(f"  [SIM] CLOSED {pos['sym']} {pos['dir']} {exited} {r:+.2f}R")
    save_state()

# ---- LIQUIDITY SETUP SHADOW (LIQUIDITY-tab setups, SIM-tracked like QML shadow) ----
_LSH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "liq_shadow.json")
_LSH_HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "liq_shadow_hist.jsonl")
LIQ_SHADOW = []
LIQ_SHADOW_STATS = {}
try: LIQ_SHADOW = json.load(open(_LSH_FILE))
except Exception: LIQ_SHADOW = []

def liq_shadow_open(s):
    key = s["sym"] + "|" + s["dir"] + "|" + str(round(s["entry"], 10))
    if any(r["key"] == key for r in LIQ_SHADOW): return
    risk = abs(s["entry"] - s["sl"]) or 1e-9
    LIQ_SHADOW.append(dict(key=key, sym=s["sym"], dir=s["dir"], entry=s["entry"], sl=s["sl"], tp=s["tp"],
                           risk=risk, rr=round(abs(s["tp"] - s["entry"]) / risk, 2), t=str(datetime.now(IST))[:19],
                           last_bar=0, bars=0, mfe_r=-9.0, mae_r=9.0))
    funnel_add("liq_shadow_opened")
    try: json.dump(LIQ_SHADOW, open(_LSH_FILE, "w"))
    except Exception: pass

def liq_shadow_step(kmap):
    """Advance open liquidity-shadow records one closed bar. ASSUMED FILL MODEL (labeled):
    ACTIVE state = the midpoint limit filled. Same slippage + ambiguity conventions as QML shadow."""
    done = []
    for rec in LIQ_SHADOW:
        k = kmap.get(rec["sym"])
        if not k or len(k) < 3: continue
        bar = k[-2]
        bo = int(float(bar[0]))
        if bo == rec["last_bar"]: continue
        rec["last_bar"] = bo; rec["bars"] += 1
        h, l, c = float(bar[2]), float(bar[3]), float(bar[4])
        lg = rec["dir"] == "LONG"
        rec["mfe_r"] = max(rec["mfe_r"], ((h - rec["entry"]) if lg else (rec["entry"] - l)) / rec["risk"])
        rec["mae_r"] = min(rec["mae_r"], ((l - rec["entry"]) if lg else (rec["entry"] - h)) / rec["risk"])
        hsl = (l <= rec["sl"]) if lg else (h >= rec["sl"])
        htp = (h >= rec["tp"]) if lg else (l <= rec["tp"])
        if hsl and htp: rec["ambiguous"] = True
        exit, r, exit_px = None, None, None
        if hsl:
            wick = (rec["sl"] - l) if lg else (h - rec["sl"])
            fill = (rec["sl"] - SLIP_WICK_FRAC_DN * wick) if lg else (rec["sl"] + SLIP_WICK_FRAC_UP * wick)
            exit, exit_px = "SL", fill
            r = ((fill - rec["entry"]) if lg else (rec["entry"] - fill)) / rec["risk"]
        elif htp:
            exit, exit_px, r = "TP", rec["tp"], abs(rec["tp"] - rec["entry"]) / rec["risk"]
        elif rec["bars"] >= 32:
            exit, exit_px = "TIME_STOP", c
            r = ((c - rec["entry"]) if lg else (rec["entry"] - c)) / rec["risk"]
        if exit:
            rec.update(exit=exit, r=round(max(min(r, 3.0), -2.0), 2), exit_px=exit_px,
                       exitTime=str(datetime.now(IST))[:16])
            done.append(rec)
    for rec in done:
        LIQ_SHADOW.remove(rec)
        try:
            with open(_LSH_HIST, "a") as f: f.write(json.dumps(rec) + "\n")
        except Exception: pass
    if done:
        try: json.dump(LIQ_SHADOW, open(_LSH_FILE, "w"))
        except Exception: pass
        try: hist = [json.loads(x) for x in open(_LSH_HIST).read().splitlines()[-500:]]
        except Exception: hist = []
        closed = [h for h in hist if h.get("r") is not None]
        w = sum(1 for h in closed if h["r"] > 0)
        LIQ_SHADOW_STATS.clear()
        LIQ_SHADOW_STATS.update(dict(n=len(closed), wins=w,
                                     avg_r=round(sum(h["r"] for h in closed) / max(1, len(closed)), 2),
                                     total_r=round(sum(h["r"] for h in closed), 1), open=len(LIQ_SHADOW)))

def manage_book(bk, kmap):
    for pos in bk['positions'][:]:
        if pos.get("sim"):
            _sim_manage(bk, pos, kmap); continue
        if pos.get("live") and pos.get("live_pending"):
            pos["pending_scans"] = pos.get("pending_scans", 0) + 1
            # execution-time expiry (TP consumed / price left level) - Binance cancel
            k0 = kmap.get(pos["sym"])
            if k0 and len(k0) > 20:
                try:
                    _lg = pos["dir"] == "LONG"
                    _cur = float(k0[-2][4])
                    _tp_hit = False
                    _ts0 = datetime.strptime(pos.get("t_open", ""), "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp() * 1000
                    for _b in k0[:-1]:
                        if int(float(_b[0])) < _ts0: continue
                        if (_lg and float(_b[2]) >= pos["tp"]) or ((not _lg) and float(_b[3]) <= pos["tp"]):
                            _tp_hit = True; break
                    _expire = "tp_already_hit" if _tp_hit else None
                    if not _expire:
                        _atr = pos.get("atr") or (sum(float(k0[x][2])-float(k0[x][3]) for x in range(len(k0)-16, len(k0)-2))/14)
                        if _atr and abs(_cur - pos["entry"]) > EXEC_STALE_ATR * float(_atr):
                            _expire = "left_level"
                    if _expire:
                        _amt, _ep = b_position(pos["sym"])
                        if abs(_amt) < 1e-9:
                            try:
                                b_cancel_order(pos["sym"], pos["bin_order_id"]); cancelled = True
                            except Exception:
                                cancelled = False
                            funnel_add("orders_cancelled"); funnel_add("gate_expired_at_rest")
                            a_log(dict(ev="cancel", sym=pos["sym"], dir=pos["dir"], cancelled=cancelled, cls="SIGNAL_EXPIRED", reason=_expire))
                            if cancelled:
                                bk['positions'].remove(pos)
                                tg(f"LIVE EXPIRED {pos['sym']} {pos['dir']}: {_expire} - entry cancelled")
                                save_state()
                            else:
                                tg(f"LIVE EXPIRED {pos['sym']}: cancel FAILED - retry next scan")
                            continue
                except Exception:
                    pass
            # fill detection -> attach bracket
            try:
                _od = bget("/fapi/v1/order", symbol=pos["sym"], orderId=pos.get("bin_order_id"))
                if _od.get("status") == "FILLED":
                    pos["live_pending"] = False
                    pos["fill_ts"] = int(time.time())
                    try:
                        b_place_bracket(pos)
                        tg(f"LIVE FILLED {pos['sym']} {pos['dir']} @ {pos['entry']} - SL/TP bracket placed")
                    except Exception as e:
                        tg(f"LIVE FILLED {pos['sym']} but BRACKET FAILED: {str(e)[:120]} - CLOSING POSITION (no naked trades)")
                        try: b_close_market(pos)
                        except Exception: pass
                    funnel_add("orders_filled")
                    a_log(dict(ev="fill", sym=pos["sym"], dir=pos["dir"], entry=pos["entry"], sl=pos["sl"], tp=pos["tp"]))
            except Exception:
                pass
            if pos.get("live_pending") and pos["pending_scans"] >= FILL_TIMEOUT_SCANS:
                try:
                    b_cancel_order(pos["sym"], pos["bin_order_id"]); cancelled = True
                except Exception:
                    cancelled = False
                funnel_add("orders_cancelled")
                a_log(dict(ev="cancel", sym=pos["sym"], dir=pos["dir"], cancelled=cancelled, cls="TIMEOUT"))
                bk['positions'].remove(pos)
                note = "cancelled" if cancelled else "COULD NOT CANCEL - check Binance app"
                tg(f"LIVE TIMEOUT unfilled {pos['sym']}: {note}")
            continue
        k = kmap.get(pos["sym"])
        if not k or len(k) < 2: continue
        if pos.get("live"):
            try:
                amt, epx = b_position(pos["sym"])
                if abs(amt) > 1e-9 and not pos.get("sl_oid") and not pos.get("tp_oid"):
                    # NAKED KILLER: a live position without bracket legs is never tolerated
                    tg(f"LIVE NAKED {pos['sym']} {pos['dir']}: no SL/TP attached - closing at market (no naked trades)")
                    b_close_market(pos)
                    continue
                if abs(amt) < 1e-9:
                    # position closed on the exchange (SL/TP fired) -> cancel the SURVIVING
                    # conditional leg before booking (Binance does not auto-cancel it)
                    for _oid in (pos.get("sl_oid"), pos.get("tp_oid")):
                        if _oid:
                            try: bdel("/fapi/v1/algoOrder", symbol=pos["sym"], algoId=_oid)
                            except Exception: pass
                    pnl = b_trades_pnl(pos["sym"], pos.get("open_ms", 0))
                    if pnl is None: pnl = 0.0
                    r = pnl / pos["risk_inr"]
                    _dur = (int(time.time()) - int(pos.get("fill_ts") or pos.get("open_ms") or time.time())) / 60.0
                    _st = LIVE_STATE.setdefault("streak", [])
                    _st.append((_dur, r)); del _st[:-4]
                    LIVE_STATE['equity'] += pnl; LIVE_STATE['day'] += r
                    pos["resultR"] = round(r, 2); pos["pnl"] = round(pnl, 2); pos["exit"] = epx
                    pos["exit_kind"] = "SYNC"; pos["exitTime"] = str(datetime.now(IST))[:16]
                    bk['history'].append(pos); bk['positions'].remove(pos)
                    funnel_add("exits_win" if r > 0 else "exits_loss")
                    a_log(dict(ev="exit", sym=pos["sym"], dir=pos["dir"], kind="SYNC", r=round(r,2), pnl=round(pnl,2)))
                    tg(f"LIVE {pos['sym']} {pos['dir']} closed on Binance (bracket/manual): {r:+.2f}R (${pnl:+.2f}) equity ${LIVE_STATE['equity']:.0f}")
                    continue
                # === T1 HARVEST LOCK - automatic on Binance (the +1R SL move CoinDCX cannot do) ===
                _c = kmap.get(pos["sym"])
                if _c and len(_c) > 2 and not pos.get("t1_locked"):
                    _hh, _ll = float(_c[-2][2]), float(_c[-2][3])
                    _lg = pos["dir"] == "LONG"
                    _rnow = ((_hh - pos["entry"]) if _lg else (pos["entry"] - _ll)) / pos["risk"]
                    if _rnow >= 1.0:
                        _lock = pos["entry"] + pos["risk"] if _lg else pos["entry"] - pos["risk"]
                        try:
                            _lbuf = CLOSE_STOP_HARD_ATR * (float(pos.get("atr") or 0) or pos["risk"])
                            b_modify_sl(pos, (_lock - _lbuf) if pos["dir"]=="LONG" else (_lock + _lbuf))
                            pos["t1_locked"] = True; pos["lock_px"] = _lock
                            tg(f"LIVE LOCK {pos['sym']} {pos['dir']} SL moved to +1R ({round(_lock,6)}) - runner to TP")
                            print(f"  LIVE LOCK {pos['sym']} @ +1R")
                        except Exception as e:
                            if "immediately trigger" in str(e):
                                tg(f"LIVE {pos['sym']} {pos['dir']}: lock level already reached - closing at market")
                                b_close_market(pos)
                            else:
                                try:
                                    b_place_bracket(pos)
                                    tg(f"LIVE {pos['sym']}: T1 lock failed ({str(e)[:60]}) - original bracket restored")
                                except Exception:
                                    tg(f"LIVE {pos['sym']}: T1 lock AND bracket restore failed - CLOSING (no naked trades)")
                                    b_close_market(pos)
                # === CLOSE-BASED STOP (2026-10-06): candle CLOSE beyond the soft level = exit.
                # Wick into the level = tolerated (the hunt). Hard exchange stop 1 ATR beyond
                # covers explosions. After T1 lock the soft level is the lock price.
                _c2 = kmap.get(pos["sym"])
                if _c2 and len(_c2) > 2:
                    _soft = pos.get("lock_px") if pos.get("t1_locked") else pos["sl"]
                    _cc = float(_c2[-2][4])
                    if ((_cc > _soft) if pos["dir"]=="LONG" else (_cc < _soft)):
                        _amt2, _epx2 = b_position(pos["sym"])
                        b_close_market(pos)
                        r = round(max(min(((_epx2 - pos["entry"]) if pos["dir"]=="LONG" else (pos["entry"] - _epx2)) / pos["risk"], TP_R), -2.0), 2)
                        pnl = b_trades_pnl(pos["sym"], pos.get("open_ms", 0)) or round(r * pos["risk_inr"], 2)
                        r = round(pnl / pos["risk_inr"], 2) if pos["risk_inr"] else r
                        LIVE_STATE['equity'] += pnl; LIVE_STATE['day'] += r
                        pos["resultR"] = r; pos["pnl"] = round(pnl, 2); pos["exit"] = _epx2; pos["exit_kind"] = "CLOSE_STOP"
                        pos["exitTime"] = str(datetime.now(IST))[:16]
                        bk['history'].append(pos); bk['positions'].remove(pos)
                        funnel_add("exits_loss" if r < 0 else "exits_win")
                        a_log(dict(ev="exit", sym=pos["sym"], dir=pos["dir"], kind="CLOSE_STOP", r=r, pnl=round(pnl,2)))
                        tg(f"LIVE CLOSE-STOP {pos['sym']} {pos['dir']} {r:+.2f}R (${pnl:+.2f}) - candle closed beyond level, out. Equity ${LIVE_STATE['equity']:.0f}")
                        continue
                # time stop (32 bars) - market close
                pos["bars"] = pos.get("bars", 0) + 1
                if pos["bars"] >= bk['tf_bars']:
                    _cc = float(_c[-2][4]) if _c else pos["entry"]
                    r = round(max(min(((_cc - pos["entry"]) if pos["dir"]=="LONG" else (pos["entry"] - _cc)) / pos["risk"], TP_R), -2.0), 2)
                    b_close_market(pos)
                    pnl = round(r * pos["risk_inr"], 2)
                    LIVE_STATE['equity'] += pnl; LIVE_STATE['day'] += r
                    pos["resultR"] = r; pos["pnl"] = pnl; pos["exit"] = _cc; pos["exit_kind"] = "TIME_STOP"
                    pos["exitTime"] = str(datetime.now(IST))[:16]
                    bk['history'].append(pos); bk['positions'].remove(pos)
                    funnel_add("exits_time_stop")
                    a_log(dict(ev="exit", sym=pos["sym"], dir=pos["dir"], kind="TIME_STOP", r=r, pnl=pnl))
                    tg(f"LIVE TIME-STOP {pos['sym']} {pos['dir']} {r:+.2f}R (${pnl:+.2f}) - market closed, equity ${LIVE_STATE['equity']:.0f}")
                    continue
            except Exception:
                pass
            continue
        c = k[-2]
        hh, ll = float(c[2]), float(c[3])
        LONG = pos["dir"] == "LONG"
        r_mult = ((hh - pos["entry"]) / pos["risk"]) if LONG else ((pos["entry"] - ll) / pos["risk"])
        if not pos.get("live") and not pos["p1"] and r_mult >= PART1_R:
            close_part(bk, pos, PART1_FRAC, PART1_R)
            pos["p1"] = True; pos["be"] = True
        if not pos.get("live") and pos["p1"] and not pos["p2"] and r_mult >= PART2_R:
            close_part(bk, pos, PART2_FRAC, PART2_R)
            pos["p2"] = True; pos["trail"] = True
        eff_sl = pos["sl"]
        if pos.get("trail"): eff_sl = pos["entry"] + TRAIL_LOCK_R*pos["risk"]* (1 if LONG else -1)
        elif pos.get("be"):  eff_sl = pos["entry"]
        # LIVE v1: exchange bracket SL is static (no trail edits); trail arrives in v1.1 with verified endpoint
        done = False
        if LONG and ll <= eff_sl:
            r = (eff_sl - pos["entry"]) / pos["risk"]
            close_rest(bk, pos, round(r,2), eff_sl); done = True; pos["exit_kind"] = "SL"
        elif not LONG and hh >= eff_sl:
            r = (pos["entry"] - eff_sl) / pos["risk"]
            close_rest(bk, pos, round(r,2), eff_sl); done = True; pos["exit_kind"] = "SL"
        if not done and r_mult >= TP_R:
            close_rest(bk, pos, TP_R, pos["tp"]); done = True; pos["exit_kind"] = "TP"
        pos["bars"] += 1
        if not done and pos["bars"] >= bk['tf_bars']:
            r = ((float(c[4]) - pos["entry"]) / pos["risk"]) if LONG else ((pos["entry"] - float(c[4])) / pos["risk"])
            close_rest(bk, pos, round(max(min(r, TP_R), -1.0),2), float(c[4])); done = True; pos["exit_kind"] = "TIME_STOP"
        if done and pos.get("live"):
            funnel_add("exits_time_stop" if pos.get("exit_kind") == "TIME_STOP" else ("exits_win" if (pos.get("resultR") or 0) > 0 else "exits_loss"))
            a_log(dict(ev="exit", sym=pos["sym"], dir=pos["dir"], kind=pos.get("exit_kind"), r=pos.get("resultR"), pnl=pos.get("pnl")))
        if done:
            bk['positions'].remove(pos)
            print(f"  CLOSED({pos.get('strategy','sweep')}) {pos['sym']} {pos['dir']}  {pos['resultR']:+.2f}R  (Rs.{pos['pnl']:,.0f})  [{' , '.join(pos['parts'])}]  equity=Rs.{bk['equity']:,.0f}")
    save_state()

def open_trade(sig, bk, strategy='sweep', risk_pct=RISK_PCT):
    bm = QML_MODE if strategy == 'qml' else SWEEP_MODE
    pos_live = (bm == "LIVE")
    pos_sim  = (bm == "SIM")
    if pos_live:
        risk_inr = min(LIVE_STATE['equity'] * LIVE_RISK_PCT / 100.0, LIVE_MAX_RISK_USDT)   # sized on REAL capital, HARD-CAPPED at $10
    elif pos_sim:
        risk_inr = SIM_STATE['equity'] * LIVE_RISK_PCT / 100.0    # sim mirrors live: 2.5% of the $400 sim bank
    else:
        risk_inr = bk['equity'] * risk_pct / 100.0
    if bm in ("LIVE", "SIM"):
        ok, why = exec_guard(sig["sym"], sig["entry"])
        if not ok:
            print(f"  GUARD-SKIP {sig['sym']} {sig['dir']} - {why}")
            cls = 'basis' if why.startswith('basis') else ('listed' if why.startswith('not listed') else ('blocklisted' if 'previously rejected' in why else ('thin' if why.startswith('thin') else 'guard')))
            funnel_add('gate_exec_' + cls)
            a_log(dict(ev='reject', sym=sig['sym'], dir=sig['dir'], gate='exec_' + cls, why=why, score=sig.get('score')))
            skip_alert(sig['sym'], cls, f"[{bm}] GUARD-SKIP {sig['sym']} {sig['dir']}: {why}")
            shadow_log(dict(t=str(datetime.now(IST))[:19], event="guard_skip", sym=sig["sym"], dir=sig["dir"], reason=why, strategy=strategy))
            return
    qty = risk_inr / abs(sig["entry"]-sig["sl"])
    pos = dict(sym=sig["sym"], dir=sig["dir"], entry=sig["entry"], sl=sig["sl"], tp=sig["tp"],
               grade=sig.get("grade",""), volX=sig.get("volX",0), strategy=strategy,
               risk=abs(sig["entry"]-sig["sl"]), risk_inr=round(risk_inr,2), qty=round(qty,4),
               t_open=str(datetime.now(IST))[:16], bars=0, _candle_ts=sig.get("_candle_ts"),
               atr=sig.get("atr"), corr=sig.get("corr"), sim=pos_sim,
               p1=False, p2=False, be=False, trail=False,
               r_acc=0.0, left=1.0, pnl_sum=0.0, parts=[], live=pos_live, bmode=bm)
    bk['positions'].append(pos)
    print(f"  OPEN({strategy})  {sig['sym']} {sig['dir']} [{sig.get('grade','')}] entry={sig['entry']} sl={sig['sl']} tp={sig['tp']}  risk={risk_pct}% (Rs.{risk_inr:,.0f})")
    if not bridge_open(pos, strategy):
        # exchange rejected / not armed -> drop the ghost position so manage_book
        # never force-syncs a fake close against LIVE_STATE equity
        bk['positions'].remove(pos)
        print(f"  OPEN-REVERTED({strategy}) {sig['sym']} {sig['dir']} - live order NOT confirmed on exchange; position dropped, no P&L will be booked")


# ==================== QML (15m) STRATEGY ====================
QML_URL = "https://fapi.binance.com/fapi/v1/klines?symbol={}&interval=15m&limit=300"
BOOK_QML = dict(positions=[], history=[], equity=ACCOUNT_INR, max_pos=QML_MAX_POSITIONS, tf_bars=32, day_r=0.0, cur_day=None)
QML_SIGNALS = []

_K15CACHE = {}
def get_klines_15_cached(symbol):
    """Incremental 15m klines: between scans fetch ONLY the new closed candles.
    Cuts scan-fetch from ~90s (150 full fetches) to ~10-20s - the Phase A.5 latency fix.
    Same information set: closed candles only; detection logic untouched."""
    c = _K15CACHE.get(symbol)
    try:
        if c and time.time() - c["t"] < 4*3600:
            url = ("https://fapi.binance.com/fapi/v1/klines?symbol=" + symbol +
                   "&interval=15m&limit=5&startTime=" + str(c["last"] + 1))
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                new = json.loads(r.read().decode())
            if new:
                c["bars"].extend(new[:-1])          # new[:-1] are now-closed; last element is the forming bar
                c["bars"] = c["bars"][-290:]
                if len(new) >= 2: c["last"] = int(float(new[-2][0]))
                c["t"] = time.time()
                return c["bars"] + [new[-1]]
            return c["bars"] + c.get("form", [])
    except Exception:
        pass
    k = get_klines_15(symbol)
    if k and len(k) > 2:
        _K15CACHE[symbol] = {"bars": k[:-1][-290:], "last": int(float(k[-2][0])),
                             "t": time.time(), "form": [k[-1]]}
    return k

def fetch_kmap_parallel(symbols, workers=8):
    from concurrent.futures import ThreadPoolExecutor
    kmap = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for s, k in zip(symbols, ex.map(get_klines_15_cached, symbols)):
            if k: kmap[s] = k
    return kmap

def get_klines_15(symbol):
    try:
        with urllib.request.urlopen(QML_URL.format(symbol), timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def qml_regime():
    try:
        with urllib.request.urlopen("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval="+QML_REGIME_TF+"&limit=100", timeout=15) as r:
            kh = json.loads(r.read().decode())
        closes=[float(x[4]) for x in kh]
        m=2/21; e=closes[0]
        for i in range(1,len(closes)-1): e=(closes[i]-e)*m+e
        last=closes[len(closes)-2]
        band=0.005 if QML_REGIME_TF=="4h" else 0.003   # 4h gate uses +/-0.5% like the sweep bot
        return 'LONG' if last>e*(1+band) else ('SHORT' if last<e*(1-band) else 'BOTH')
    except Exception:
        return 'BOTH'
def swings15(k):
    s=[]
    for i in range(2,len(k)-2):
        l=float(k[i][3]); h=float(k[i][2])
        if l<=float(k[i-1][3]) and l<=float(k[i-2][3]) and l<=float(k[i+1][3]) and l<=float(k[i+2][3]): s.append({'i':i,'t':'L','p':l})
        if h>=float(k[i-1][2]) and h>=float(k[i-2][2]) and h>=float(k[i+1][2]) and h>=float(k[i+2][2]): s.append({'i':i,'t':'H','p':h})
    return s

class StructTracker:
    """FOUNDER STRUCTURE ENGINE (shadow v1, 2026-10-07) - the notebook spec, mechanically:
    major-swing zigzag (legs >= QML_MAJOR_LEG_ATR x ATR) -> structure state -> protected level.
    PROTECTION MIGRATION (founder's rule): a HL only becomes protected once the rally out of it
    breaks the HH it came from - only then do breakout longs' stops sit below it. Until that
    confirmation the previous confirmed HL stays protected. Mirror for shorts.
    Break = QML_BOS_MIN_CLOSES consecutive closes beyond the protected level.
    SHADOW-ONLY: computes and logs decisions; never blocks a trade (spec: detection != authorization).
    Point-in-time: rebuilt from closed candles each scan; no state carried across scans."""
    def __init__(self, k):
        closed = k[:-1]
        if len(closed) < 40: raise ValueError("short history")
        self.atr = sum(float(closed[i][2])-float(closed[i][3]) for i in range(len(closed)-15, len(closed)-1))/14
        if self.atr <= 0: raise ValueError("bad atr")
        T = QML_MAJOR_LEG_ATR * self.atr
        # zigzag on wicks: alternate H/L pivots, min leg size T (running extremes tracked per side)
        piv = []                      # (bar_index, price, 'H'/'L')
        d = None
        e_hi = float(closed[0][2]); i_hi = 0
        e_lo = float(closed[0][3]); i_lo = 0
        for i in range(1, len(closed)):
            h, l = float(closed[i][2]), float(closed[i][3])
            if d is None:
                if h - e_lo >= T:    piv.append((i_lo, e_lo, 'L')); d = 'up';   e_hi, i_hi = h, i
                elif e_hi - l >= T:  piv.append((i_hi, e_hi, 'H')); d = 'down'; e_lo, i_lo = l, i
                else:
                    if h > e_hi: e_hi, i_hi = h, i
                    if l < e_lo: e_lo, i_lo = l, i
            elif d == 'up':
                if h > e_hi: e_hi, i_hi = h, i
                if e_hi - l >= T: piv.append((i_hi, e_hi, 'H')); d = 'down'; e_lo, i_lo = l, i
            else:
                if l < e_lo: e_lo, i_lo = l, i
                if h - e_lo >= T: piv.append((i_lo, e_lo, 'L')); d = 'up'; e_hi, i_hi = h, i
        # provisional final pivot: the running extreme IS the current swing until a T-reversal
        # proves otherwise (otherwise intact tops read as "no structure")
        if d == 'up' and (not piv or e_hi > piv[-1][1] + 0.5*T):
            piv.append((i_hi, e_hi, 'H'))
        elif d == 'down' and (not piv or e_lo < piv[-1][1] - 0.5*T):
            piv.append((i_lo, e_lo, 'L'))
        self.pivots = piv
        self.state, self.protected, self.prot_idx = self._classify(piv)
        self.broken = self._break_check(closed)
        if self.broken and self.state in ('BULLISH', 'BEARISH'):
            self.state = 'TRANSITIONAL'
    def _classify(self, piv):
        if len(piv) < 3: return 'NONE', None, -1
        prot = None; prot_i = -1; state = 'NONE'
        last = piv[-1]
        if last[2] == 'H':
            # bullish structure if the latest H exceeded the previous H -> the L before it protected
            hs = [p for p in piv if p[2] == 'H']; ls = [p for p in piv if p[2] == 'L']
            if len(hs) >= 2 and len(ls) >= 1 and last[1] > hs[-2][1]:
                cand = [l for l in ls if l[0] < last[0]]
                if cand: prot, prot_i = cand[-1][1], cand[-1][0]; state = 'BULLISH'
        elif last[2] == 'L':
            ls2 = [p for p in piv if p[2] == 'L']; hs2 = [p for p in piv if p[2] == 'H']
            if len(ls2) >= 2 and len(hs2) >= 1 and last[1] < ls2[-2][1]:
                cand = [h for h in hs2 if h[0] < last[0]]
                if cand: prot, prot_i = cand[-1][1], cand[-1][0]; state = 'BEARISH'
        return state, prot, prot_i
    def _break_check(self, closed):
        if self.protected is None: return False
        n = 0
        for b in closed[-6:]:
            c = float(b[4])
            if self.state == 'BEARISH':
                if c > self.protected: n += 1
                else: n = 0
            else:
                if c < self.protected: n += 1
                else: n = 0
        return n >= QML_BOS_MIN_CLOSES
    def decision(self, d):
        """Shadow decision for a QML direction. allow=False = this rule-set would reject."""
        if self.state == 'NONE':
            return dict(state='NONE', allow=True, why='insufficient major structure')
        if d == 'SHORT':
            if self.state == 'BULLISH':
                if self.broken: return dict(state='TRANSITIONAL', allow=True, why='protected HL broken by 2 closes - counter-trend window open')
                return dict(state='BULLISH', allow=False, why='protected HL intact - no counter-trend short (TIA/ARB rule)')
            return dict(state=self.state, allow=True, why='not fighting a bullish structure')
        else:
            if self.state == 'BEARISH':
                if self.broken: return dict(state='TRANSITIONAL', allow=True, why='protected LH broken by 2 closes - counter-trend window open')
                return dict(state='BEARISH', allow=False, why='protected LH intact - no counter-trend long')
            return dict(state=self.state, allow=True, why='not fighting a bearish structure')

def detect_qm15(k, regime='BOTH'):
    """QML v2 - raw structure detection + quality score. No hard gates on BTC/volume/R:R.
    BEARISH: P1=swing high, P2=swing low, P3=swing high>P1 (sweep), P4=CLOSE below P2 (BOS). QML level=P1.
    BULLISH: mirrored (P3 swing low < P1, P4 close above P2).
    Status: ARMED (BOS done, awaiting retest) / RETEST (first touch of P1 zone) / USED (retested already).
    Dead if: close beyond P3 before retest, armed >24h, or price ran >3 ATR from P1."""
    closed=k[:-1]
    if len(closed)<40: return None
    s=swings15(closed)
    if len(s)<4: return None
    atr=sum(float(closed[i][2])-float(closed[i][3]) for i in range(len(closed)-15,len(closed)-1))/14
    if atr<=0: return None
    vavg=sum(float(x[5]) for x in closed[-21:-1])/20
    cur=float(closed[-1][4])
    n=len(s); SLIP=0.001
    def build(bear, i1, i2, i3, p4i):
        P1=s[i1]['p']; P2=s[i2]['p']; P3=s[i3]['p']
        if bear and P2>=P1: return None
        if (not bear) and P2<=P1: return None
        dirn='SHORT' if bear else 'LONG'
        sl=P3*(1+SLIP) if bear else P3*(1-SLIP)
        tp=P2; entry=P1
        risk=abs(entry-sl)
        if risk<=0 or risk/entry>QML_MAX_RISK_PCT: return None
        if risk < QML_MIN_RISK_ATR*atr: return None            # stop inside noise - untradable (2026-10-06)
        if abs(P3-P1) < QML_MIN_SWEEP_ATR*atr: return None     # shallow sweep fakes R:R - untradable
        zlo=entry-QML_ZONE_ATR*atr; zhi=entry+QML_ZONE_ATR*atr
        dead=False; touches=0
        for j in range(p4i+1, len(closed)):
            c=closed[j]; hh=float(c[2]); ll=float(c[3]); cc=float(c[4])
            if (bear and cc>P3) or ((not bear) and cc<P3): dead=True; break
            if hh>=zlo and ll<=zhi: touches+=1
        if dead: return None
        if touches>=2: status='USED'
        elif touches==1: status='RETEST'
        else:
            if len(closed)-1-p4i>QML_ARMED_MAX_BARS: return None
            if abs(cur-entry)>QML_ARMED_MAX_DIST_ATR*atr: return None
            status='ARMED'
        # ---- FOUNDER SCORE v2 (2026-10-06, /16): structure -> location -> confirmation ----
        # OTE = 61.8-78.6% retracement of the P3->P4 leg (P4 = leg extreme). LATCHED +1:
        # once touched before entry, deeper retracement does NOT revoke it.
        p3i=s[i3]['i']
        leg_lo=min(float(closed[j][3]) for j in range(p3i, p4i+1)) if bear else None
        leg_hi=max(float(closed[j][2]) for j in range(p3i, p4i+1)) if (not bear) else None
        if bear:
            ote_lo=leg_lo+0.618*(P3-leg_lo); ote_hi=leg_lo+0.786*(P3-leg_lo)
        else:
            ote_lo=leg_hi-0.786*(leg_hi-P3); ote_hi=leg_hi-0.618*(leg_hi-P3)
        ote=0; rej=0
        first_touch=-1
        for j in range(p4i+1, len(closed)):
            hh=float(closed[j][2]); ll=float(closed[j][3]); cc=float(closed[j][4])
            if hh>=ote_lo and ll<=ote_hi and not ote: ote=1
            if first_touch<0 and hh>=zlo and ll<=zhi:
                first_touch=j
                if (cc<P1) if bear else (cc>P1): rej=2   # closed back beyond P1 = confirmed rejection
        # clean sweep wick: P3 candle closes back beyond P1, wick >=60% of its range
        _o=float(closed[p3i][1]); _h=float(closed[p3i][2]); _l=float(closed[p3i][3]); _c=float(closed[p3i][4])
        swk=0
        if _h>_l:
            if bear and _c<P1 and (_h-max(_o,_c))>=0.6*(_h-_l): swk=1
            if (not bear) and _c>P1 and (min(_o,_c)-_l)>=0.6*(_h-_l): swk=1
        score=2  # sweep beyond P1 + BOS close are definitional
        disp=abs(float(closed[p4i][4])-P3)/atr
        if disp>=0.5: score+=2
        v=max(float(closed[p4i][5]), float(closed[p3i][5]))
        volX=v/vavg if vavg>0 else 0
        if volX>=1.2: score+=1
        if volX>=1.5: score+=1
        if (regime=='SHORT' and bear) or (regime=='LONG' and (not bear)): score+=2
        if status=='RETEST': score+=2
        rr=abs(tp-entry)/risk
        if rr>=2: score+=2
        score+=ote+rej+swk
        return dict(dir=dirn, status=status, qm=entry, p1=P1, p2=P2, p3=P3, p4=float(closed[p4i][4]),
                    sl=sl, tp=tp, zone_lo=zlo, zone_hi=zhi, score=score, disp=round(disp,2),
                    volX=round(volX,2), rr=round(rr,2),
                    grade='A+' if score>=11 else ('A' if score>=9 else 'B'),
                    ote=ote, rejection=rej, sweep_wick=swk,
                    atr=round(atr,10), sweep_atr=round(abs(P3-P1)/atr,3), age_bars=len(closed)-1-p4i,
                    comp=dict(base=2, disp=2 if disp>=0.5 else 0,
                              vol=2 if volX>=1.5 else (1 if volX>=1.2 else 0),
                              regime=2 if ((regime=='SHORT' and bear) or (regime=='LONG' and (not bear))) else 0,
                              retest=2 if status=='RETEST' else 0, rr=2 if rr>=2 else 0,
                              ote=ote, rej=rej, sweep=swk))
    W=30
    for i1 in range(n-1,-1,-1):
        if s[i1]['t']!='H': continue
        P1=s[i1]['p']
        for i2 in range(i1+1, min(n, i1+W)):
            if s[i2]['t']!='L': continue
            P2=s[i2]['p']
            for i3 in range(i2+1, min(n, i2+W)):
                if s[i3]['t']!='H' or s[i3]['p']<=P1: continue
                p4i=-1
                for j in range(s[i3]['i']+QML_BOS_MIN_CLOSES, len(closed)):
                    if all(float(closed[j-m][4])<P2 for m in range(QML_BOS_MIN_CLOSES)): p4i=j; break
                if p4i<0: continue
                r=build(True, i1, i2, i3, p4i)
                if r: return r
    for i1 in range(n-1,-1,-1):
        if s[i1]['t']!='L': continue
        P1=s[i1]['p']
        for i2 in range(i1+1, min(n, i1+W)):
            if s[i2]['t']!='H': continue
            P2=s[i2]['p']
            for i3 in range(i2+1, min(n, i2+W)):
                if s[i3]['t']!='L' or s[i3]['p']>=P1: continue
                p4i=-1
                for j in range(s[i3]['i']+QML_BOS_MIN_CLOSES, len(closed)):
                    if all(float(closed[j-m][4])>P2 for m in range(QML_BOS_MIN_CLOSES)): p4i=j; break
                if p4i<0: continue
                r=build(False, i1, i2, i3, p4i)
                if r: return r
    return None


QML_TOP_N = 150
_qml_cache = {"t": 0, "list": None}
def qml_pairs():
    import json as _json, time as _t, urllib.request as _ur
    if _qml_cache["list"] and _t.time() - _qml_cache["t"] < 6*3600:
        return _qml_cache["list"]
    try:
        req = _ur.Request("https://fapi.binance.com/fapi/v1/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=20) as r:
            data = _json.load(r)
        rows = [d for d in data if str(d.get("symbol", "")).endswith("USDT")]
        rows.sort(key=lambda d: float(d.get("quoteVolume", 0) or 0), reverse=True)
        lst = [d["symbol"] for d in rows[:QML_TOP_N]]
        _qml_cache["list"] = lst
        _qml_cache["t"] = _t.time()
        print("  QML universe: top " + str(len(lst)) + " liquid USDT pairs by 24h volume")
        return lst
    except Exception as e:
        print("  QML universe fetch failed, falling back to full PAIRS list:", e)
        return PAIRS

def qml_scan():
    bk=BOOK_QML
    today=datetime.now(IST).date()
    if bk['cur_day']!=today: bk['cur_day'],bk['day_r']=today,0.0
    if QML_MODE=='LIVE' and LIVE_STATE.get('cur_day')!=str(today):
        LIVE_STATE['cur_day']=str(today); LIVE_STATE['day']=0.0; LIVE_STATE['day_r']=0.0
    if QML_MODE=='SIM' and SIM_STATE.get('cur_day')!=str(today):
        SIM_STATE['cur_day']=str(today); SIM_STATE['day']=0.0
    if FUNNEL.get('_day') != str(today):
        if FUNNEL.get('_day'):
            try: daily_report(FUNNEL)
            except Exception as e: print("  ! daily report failed:", e)
        FUNNEL.clear(); FUNNEL['_day'] = str(today); funnel_save()
    print(f"\n=== QML SCAN {datetime.now(IST):%d %b %H:%M IST} | equity=Rs.{bk['equity']:,.0f} | day {bk['day_r']:+.2f}R ===")
    _dr = LIVE_STATE['day'] if QML_MODE=='LIVE' else (SIM_STATE['day'] if QML_MODE=='SIM' else bk['day_r'])
    qml_blocked = _dr <= DAILY_LOSS_LIMIT_R
    _st = LIVE_STATE.get("streak", [])[-3:]
    if QML_MODE == 'LIVE' and len(_st) == 3 and all(d < 20 for d, r in _st) and all(r < 0 for r in _st):
        # STOP-CLUSTER circuit breaker: the venue is stop-gunning our setups THIS SESSION
        # (alt-pump bloodbath 2026-10-06) - flat books teach nothing; pause 6h automatically
        if not qml_blocked:
            tg("STOP-CLUSTER: 3 straight losses each stopped <20min after fill - pausing NEW entries until the streak clears (execution-feedback breaker)")
        qml_blocked = True
    if qml_blocked:
        print(f"  QML daily loss limit ({bk['day_r']:+.2f}R) - PUBLISHING signals only, no new entries")
    regime=qml_regime()
    print(f"  BTC 1H regime: {regime}")
    kmap = fetch_kmap_parallel(qml_pairs())
    # alt-basket median 15m return (relative-momentum gate input) - computed from the same klines
    _BASKET = ["ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","TRXUSDT","DOTUSDT","LTCUSDT","NEARUSDT","UNIUSDT","ATOMUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","SEIUSDT","TIAUSDT","APTUSDT","FILUSDT","AAVEUSDT","LDOUSDT","ARUSDT","GRTUSDT","MKRUSDT","ALGOUSDT","VETUSDT","ICPUSDT","HBARUSDT"]
    _brets = []
    for _s in _BASKET:
        _k = kmap.get(_s)
        if _k and len(_k) > 5:
            _a = float(_k[-5][4]); _b = float(_k[-2][4])   # 45m horizon (3 candles), not 1 - a pump that
            if _a > 0: _brets.append((_b - _a) / _a * 100) # pauses one candle still has momentum (TAO/DOGE)
    _basket_med15 = sorted(_brets)[len(_brets)//2] if _brets else 0.0
    manage_book(bk, kmap)
    # LIQ SIM on the 15-min cycle (founder: live-order realism) - idempotent watermark
    try:
        for _s in [p["sym"] for p in LIQ_SIM["positions"] if p["sym"] not in kmap]:
            _k = get_klines_15(_s)
            if _k: kmap[_s] = _k
        if LIQ_SIM["positions"] or LIQ_DATA:
            liqsim_manage(kmap)
    except Exception:
        pass
    QML_SIGNALS.clear()
    # correlation vs BTC (15m returns, ~30h window) for regime-gate exemptions
    bmap={}
    btc_k=kmap.get("BTCUSDT")
    if btc_k and len(btc_k)>40:
        for s,k in kmap.items():
            if len(k)<40: continue
            M=min(len(k),len(btc_k))
            a=max(1,M-121)
            cr=[(float(k[i+1][4])-float(k[i][4]))/float(k[i][4]) for i in range(a,M-1)]
            br=[(float(btc_k[i+1][4])-float(btc_k[i][4]))/float(btc_k[i][4]) for i in range(a,M-1)]
            bmap[s]=pearson(cr,br)
    for s,k in kmap.items():
        if s=="BTCUSDT": continue
        q=detect_qm15(k, regime)
        if not q: continue
        if q['status']=='USED': continue
        q['sym']=s
        q['last']=float(k[-2][4])   # Binance futures price NOW - the honest fill reference for market entries
        if q['status']=='RETEST':
            # STALE-RETEST guard: a retest is only fresh while price is NEAR the QML level.
            # Without this, a touch hours ago kept status=RETEST forever while price ran
            # away - the bot then tried to enter 10%+ from market (surfaced as absurd
            # "basis +12%" guard skips). ARMED had a 3-ATR expiry; RETEST had none. Now fixed.
            cur_px = float(k[-2][4])
            atr_now = sum(float(k[i][2])-float(k[i][3]) for i in range(len(k)-16, len(k)-2))/14
            if atr_now > 0 and abs(cur_px - q['qm']) > 1.5 * atr_now:
                q['status'] = 'STALE'   # publish for visibility, but never tradable
        funnel_add('raw_qml'); funnel_add('st_'+q['status'])
        # SHADOW structural decision (founder engine v1): log only, never blocks (spec: detection != authorization)
        if q['status'] in ('RETEST', 'ARMED'):
            try:
                _st = StructTracker(k)
                _sd = _st.decision(q['dir'])
                _cl2 = [float(x[4]) for x in k[:-1]]; _e = _cl2[0]
                for _x2 in _cl2[1:]: _e = (_x2 - _e) * 2/21 + _e
                a_log(dict(ev='struct', sym=s, dir=q['dir'], qstatus=q['status'], score=q['score'],
                           state=_sd['state'], allow=_sd['allow'], why=_sd['why'],
                           protected=round(_st.protected or 0.0, 10), broken=_st.broken,
                           ema_dist=round((float(k[-2][4])-_e)/_e*100, 2) if _e else None,
                           key=s+"|"+q['dir']+"|"+str(round(q['qm'], 10))))
                funnel_add('struct_allow' if _sd['allow'] else 'struct_block')
            except Exception:
                pass
        if q['score']>=4: funnel_add('score_ge4')
        if q['score']>=5: funnel_add('score_ge5')
        if q['score']>=6: funnel_add('score_ge6')
        a_log(dict(ev='detect', sym=s, dir=q['dir'], status=q['status'], score=q['score'], comp=q.get('comp'),
                   p1=q['qm'], p2=q['p2'], p3=q['p3'], rr=q['rr'], volX=q['volX'], disp=q['disp'],
                   sweep_atr=q.get('sweep_atr'), age_bars=q.get('age_bars'), regime=regime,
                   corr=round(bmap.get(s,0.0),2), px=q['last'], candle_ts=int(k[-2][6]),
                   dist_atr=(round(abs(q['last']-q['qm'])/q['atr'],2) if q.get('atr') else None)))
        _r15 = ((float(k[-2][4]) - float(k[-5][4])) / float(k[-5][4]) * 100) if len(k) > 5 and float(k[-5][4]) else 0.0   # 45m (3-candle) momentum, was 1 candle
        _a1h = ((float(k[-2][4]) - float(k[-6][4])) / float(k[-6][4]) * 100) if len(k) > 6 and float(k[-6][4]) else 0.0    # absolute 1h move - flat-is-flat filter
        _cl = [float(x[4]) for x in k[:-1]]                 # closed candles only
        _e = _cl[0]
        for _x in _cl[1:]: _e = (_x - _e) * 2/21 + _e       # EMA20(15m) point-in-time
        _last = float(k[-2][4])
        _trend_ok = (_last > _e*(1+QML_TREND_EMA_BAND)) if q['dir']=='LONG' else (_last < _e*(1-QML_TREND_EMA_BAND))
        _pct24 = round((float(k[-2][4]) / float(k[-98][4]) - 1) * 100, 2) if len(k) > 98 and float(k[-98][4]) else None
        QML_SIGNALS.append(dict(sym=s,dir=q['dir'],status=q['status'],qm=q['qm'],sl=q['sl'],tp=q['tp'], rel15=round(_r15 - _basket_med15, 2), abs1h=round(_a1h, 2), pct24=_pct24, trend_ok=_trend_ok,
            grade=q['grade'],score=q['score'],volX=q['volX'],disp=q['disp'],rr=q['rr'],
            p1=q['p1'],p2=q['p2'],p3=q['p3'],zone_lo=q['zone_lo'],zone_hi=q['zone_hi'],
            comp=q.get('comp'), atr=q.get('atr'), sweep_atr=q.get('sweep_atr'), age_bars=q.get('age_bars'),
            last=q['last'], _candle_ts=int(k[-2][6]),
            t=str(datetime.now(IST))[:16]))
    QML_SIGNALS.sort(key=lambda x: (0 if x['status']=='RETEST' else 1, -x['score']))
    del QML_SIGNALS[12:]
    for sig in QML_SIGNALS[:5]:
        print(f"  QML {sig['sym']} {sig['dir']} [{sig['grade']}] {sig['status']} score={sig['score']}/8 qm={sig['qm']}")
    if len(bk['positions'])>=bk['max_pos']:
        print(f"  QML max positions ({bk['max_pos']}) reached."); save_state(); return
    def rej(sig, gate, why):
        funnel_add('gate_'+gate)
        a_log(dict(ev='reject', sym=sig['sym'], dir=sig['dir'], gate=gate, why=why, score=sig['score'],
                   p1=sig['qm'], rr=sig['rr'], dist_atr=(round(abs(sig['last']-sig['qm'])/sig['atr'],2) if sig.get('atr') else None)))
    for sig in QML_SIGNALS:
        if sig['status']!='RETEST': continue
        if sig['score'] < QML_SCORE_V2_MIN:
            rej(sig,'score',f"score {sig['score']}/{16}<{QML_SCORE_V2_MIN}"); continue
        if MODE=="LIVE" and regime in ('LONG','SHORT') and \
           ((regime=='LONG' and sig['dir']=='SHORT') or (regime=='SHORT' and sig['dir']=='LONG')) and \
           sig['score'] < QML_SCORE_V2_COUNTER:
            rej(sig,'countertrend',f"{sig['dir']} vs BTC regime {regime}, score {sig['score']}<{QML_SCORE_V2_COUNTER} (A+ required)"); continue
        if any(p['sym']==sig['sym'] for p in bk['positions']):
            rej(sig,'existing_position','sym already open'); continue
        _maxp = LIVE_MAX_POS if MODE=="LIVE" else bk['max_pos']
        if len(bk['positions']) >= _maxp:
            rej(sig,'book_full',f"{_maxp} live max"); print(f"  QML SKIP {sig['sym']} {sig['dir']} - book full ({_maxp} live max)"); continue
        _maxd = LIVE_MAX_DIR if MODE=="LIVE" else QML_MAX_DIR
        _dirn=sum(1 for p in bk['positions'] if p['dir']==sig['dir'])
        if _dirn>=_maxd:
            rej(sig,'direction_cap',f"{_maxd} {sig['dir']} open"); print(f"  QML SKIP {sig['sym']} {sig['dir']} - direction cap"); continue
        _dc = sum(1 for p in bk['positions'] if p['dir']==sig['dir'] and abs(float(p.get('corr',0) or 0))>=QML_CORR_GATE)
        if MODE=="LIVE" and _dc >= LIVE_MAX_CORR_DIR:
            rej(sig,'corr_dir_cap',f"{_dc} correlated {sig['dir']} positions open (corr>={QML_CORR_GATE})"); continue
        if sig.get('rr',9)<1.0:
            rej(sig,'rr_low',f"rr {sig['rr']}<1"); print(f"  QML PUBLISH-ONLY {sig['sym']} {sig['dir']} - R:R below 1"); continue
        if os.path.exists(TG_SOFT_HALT):
            rej(sig,'tg_pause','paused via Telegram /stop'); continue
        _qv = binance_qvol(sig['sym'])
        if _qv < LIQ_MIN_QVOL:
            rej(sig,'liq_vol',f"24h vol ${(_qv or 0)/1e6:.0f}M < ${LIQ_MIN_QVOL/1e6:.0f}M floor"); continue
        _rel = sig.get('rel15')
        if _rel is not None and ((sig['dir']=='SHORT' and _rel >= REL_MOM_BLOCK) or (sig['dir']=='LONG' and _rel <= -REL_MOM_BLOCK)):
            rej(sig,'rel_mom',f"coin 45m rel momentum {_rel:+.1f}% vs basket (block at ±{REL_MOM_BLOCK}%)"); continue
        _a1 = sig.get('abs1h')
        if _a1 is not None and ((sig['dir']=='SHORT' and _a1 >= ABS_MOM_BLOCK) or (sig['dir']=='LONG' and _a1 <= -ABS_MOM_BLOCK)):
            rej(sig,'abs_mom',f"coin 1h move {_a1:+.1f}% absolute (block at ±{ABS_MOM_BLOCK}%, no basket)"); continue
        if sig.get('trend_ok') is False:
            rej(sig,'trend',f"{sig['dir']} vs the coin's own 15m trend (EMA gate) - bos2_replay: 18/20 above-EMA shorts = -3.1R"); continue
        if qml_blocked:
            rej(sig,'daily_breaker','-3R reached'); print(f"  QML PUBLISH-ONLY {sig['sym']} {sig['dir']} - daily limit"); continue
        if on_cooldown(bk, sig['sym'], sig['dir']):
            rej(sig,'cooldown',f"{COOLDOWN_H}h"); print(f"  QML SKIP {sig['sym']} {sig['dir']} — cooldown"); continue
        q=dict(sym=sig['sym'],dir=sig['dir'],grade=sig['grade'],entry=sig['qm'],sl=sig['sl'],tp=sig['tp'],volX=sig['volX'],last=sig.get('last'),_candle_ts=sig.get('_candle_ts'),atr=sig.get('atr'),corr=bmap.get(sig['sym']))
        if len(bk['positions'])<bk['max_pos']: open_trade(q, bk, 'qml', QML_RISK_PCT)
    # SHADOW TWIN: paper-record every score>=4 RETEST (before/after live gates) + advance one bar
    for sig in QML_SIGNALS:
        if sig['status']=='RETEST' and sig['score']>=QML_MIN_SCORE:
            _p,_qv = cdcx_market(sig['sym'])
            _b = round((_p-sig['last'])/sig['last']*100,3) if _p else None
            shadow_open(sig, regime, bmap.get(sig['sym']), _b)
    _shadow_step(kmap)
    funnel_save()
    save_state()

def next_15m_time():
    # every 30 min — safe on the VPS dedicated IP
    now=datetime.now(timezone.utc)
    for m in (1, 16, 31, 46):        # 1 min after candle close (was :03 - the 'move gone' latency fix)
        t=now.replace(minute=m,second=0,microsecond=0)
        if t>now: return t
    t=(now+timedelta(hours=1)).replace(minute=1,second=0,microsecond=0)
    return t

def qml_loop():
    while True:
        if halt_requested(): print("BOT_HALT present - QML loop stopping"); return
        t=next_15m_time()
        while datetime.now(timezone.utc)<t: time.sleep(30)
        try: qml_scan()
        except Exception as e: print("  ! qml scan error:", e)


# ==================== LIQUIDITY + MARKET PUBLISHERS (hourly, VPS-only) ====================
LIQ_DATA = []
MKT_DATA = []

def detect_liq15(k):
    """LIQUIDITY SCANNER v2 (2026-10-07) - the advisor stack merged into one detector:
    ERL raid (impulse takes out prior 20-bar high/low) -> entry at the PREMIUM-half FVG
    (not the midpoint - 'don't trade all FVGs': premium/discount + indecision quality +
    no runaway/rejection-collision) -> SL beyond the raid extreme -> TP at 2R. 1:2 fixed."""
    closed=k[:-1]
    if len(closed)<80: return None
    atr=sum(float(closed[i][2])-float(closed[i][3]) for i in range(len(closed)-15,len(closed)-1))/14
    vavg=sum(float(x[5]) for x in closed[-21:-1])/20
    if atr<=0 or vavg<=0: return None
    def body(bi,c): return abs(float(closed[bi][4])-float(closed[bi][1]))
    def rng(bi): return float(closed[bi][2])-float(closed[bi][3])
    for i in range(len(closed)-2, max(60,len(closed)-75)-1, -1):
        for st in (i-4,i-3,i-2):
            if st<22: continue
            legs=closed[st:i+1]
            if len(legs)<3: continue
            if max(float(x[2])-float(x[3]) for x in legs)<1.4*atr: continue
            if sum(float(x[5]) for x in legs)/len(legs) < 1.5*vavg: continue
            p20_hi=max(float(closed[j][2]) for j in range(st-20,st))
            p20_lo=min(float(closed[j][3]) for j in range(st-20,st))
            eq_g=(float(closed[st][1])+float(closed[st][4]))/2
            # ---- DOWN raid (SHORT): sweeps p20_lo ----
            hi0=float(closed[st][2]); lo0=min(float(closed[i-1][3]),float(closed[i][3]))
            chg0=(hi0-lo0)/hi0*100
            if 2<=chg0<=10 and lo0<p20_lo:
                if hi0-max(float(x[2]) for x in legs) > (hi0-lo0)*0.35: continue
                eq=(hi0+lo0)/2
                # premium-half FVGs: 3-candle gap, mid >= eq, inside origin, quality filters
                best=None
                for j in range(st+1, i):
                    glo=float(closed[j+1][2]); ghi=float(closed[j-1][3])
                    if not (glo < ghi): continue
                    if ghi-glo < 0.25*atr: continue
                    mid=(glo+ghi)/2
                    if mid < eq: continue            # premium only (no cheap sells)
                    if ghi > hi0: continue           # rejection-collision: above origin wall
                    if all(body(x,0) >= 0.55*max(rng(x),1e-12) for x in (j-1,j,j+1)): continue  # runaway
                    inde = body(j,0) <= 0.35*max(rng(j),1e-12)
                    score = (1 if inde else 0, ghi)   # prefer indecision, then nearest origin
                    if best is None or score > best[0]: best=(score, glo, ghi)
                if not best: continue
                _, glo, ghi = best
                entry=(glo+ghi)/2; sl=lo0*0.9985; risk=entry-sl
                if risk<=0: continue
                tp=entry-2.0*risk
                state='PENDING'
                for j in range(i+1,len(closed)):
                    if float(closed[j][3])<lo0*0.9985: state='BROKEN'; break
                    if float(closed[j][2])>=entry: state='ACTIVE'; break
                if state=='BROKEN': continue
                return dict(sym='',dir='SHORT',state=state,chg=round(chg0,2),nc=len(legs),
                            volX=round((sum(float(x[5]) for x in legs)/len(legs))/vavg,2),
                            entry=round(entry,10),fvg_lo=round(glo,10),fvg_hi=round(ghi,10),
                            eq=round(eq,10),atr=round(atr,10),sl=sl,tp=tp,rr=2.0,
                            _candle_ts=int(closed[-1][6]),_leg_end=i)
            # ---- UP raid (LONG): sweeps p20_hi ----
            lo0u=float(closed[st][3]); hi0u=max(float(closed[i-1][2]),float(closed[i][2]))
            chgu=(hi0u-lo0u)/lo0u*100
            if 2<=chgu<=10 and hi0u>p20_hi:
                if min(float(x[3]) for x in legs)-lo0u > (hi0u-lo0u)*0.35: continue
                eq2=(lo0u+hi0u)/2
                best=None
                for j in range(st+1, i):
                    ghi=float(closed[j+1][3]); glo=float(closed[j-1][2])
                    if not (glo < ghi): continue
                    if ghi-glo < 0.25*atr: continue
                    mid=(glo+ghi)/2
                    if mid > eq2: continue            # discount only (no expensive buys)
                    if glo < lo0u: continue           # rejection-collision: below origin wall
                    if all(body(x,0) >= 0.55*max(rng(x),1e-12) for x in (j-1,j,j+1)): continue
                    inde = body(j,0) <= 0.35*max(rng(j),1e-12)
                    score = (1 if inde else 0, -glo)
                    if best is None or score > best[0]: best=(score, glo, ghi)
                if not best: continue
                _, glo, ghi = best
                entry=(glo+ghi)/2; sl=hi0u*1.0015; risk=sl-entry
                if risk<=0: continue
                tp=entry+2.0*risk
                state='PENDING'
                for j in range(i+1,len(closed)):
                    if float(closed[j][2])>hi0u*1.0015: state='BROKEN'; break
                    if float(closed[j][3])<=entry: state='ACTIVE'; break
                if state=='BROKEN': continue
                return dict(sym='',dir='LONG',state=state,chg=round(chgu,2),nc=len(legs),
                            volX=round((sum(float(x[5]) for x in legs)/len(legs))/vavg,2),
                            entry=round(entry,10),fvg_lo=round(glo,10),fvg_hi=round(ghi,10),
                            eq=round(eq2,10),atr=round(atr,10),sl=sl,tp=tp,rr=2.0,
                            _candle_ts=int(closed[-1][6]),_leg_end=i)
    return None


# ---- SWEEP SIM ($500 live-mirror: A+ gated entries, 1:2 TP, NO scale-out/lock - straight runner) ----
SWEEP_SIM = {"equity": 500.0, "positions": [], "history": [], "day": 0.0, "cur_day": ""}
SWEEP_SIM_RISK = 2.5
SWEEP_SIM_MAXPOS = 5

def sweepsim_load(d):
    s = d.get("sweepsim")
    if isinstance(s, dict):
        SWEEP_SIM["equity"] = float(s.get("equity", 500.0))
        SWEEP_SIM["positions"] = s.get("positions", [])
        SWEEP_SIM["history"] = s.get("history", [])
        SWEEP_SIM["day"] = float(s.get("day", 0.0))
        SWEEP_SIM["cur_day"] = str(s.get("cur_day", ""))

def sweepsim_enter(sig, s, k):
    if len(SWEEP_SIM["positions"]) >= SWEEP_SIM_MAXPOS: return
    if any(p["sym"] == s for p in SWEEP_SIM["positions"]): return
    try:
        atr = sum(float(k[i][2])-float(k[i][3]) for i in range(len(k)-16, len(k)-2))/14
    except Exception:
        atr = abs(sig["entry"]-sig["sl"])
    lg = sig["dir"] == "LONG"
    entry = float(sig["entry"]); sl = float(sig["sl"])
    tp = entry + 2.0*(entry-sl) if lg else entry - 2.0*(sl-entry)
    risk = round(SWEEP_SIM["equity"] * SWEEP_SIM_RISK / 100.0, 2)
    SWEEP_SIM["positions"].append(dict(sym=s, dir=sig["dir"], entry=entry, sl=sl, tp=round(tp, 10),
        sl_hard=(sl - atr) if lg else (sl + atr), risk_usdt=risk, ts=int(sig["t"]),
        bars=0, t_open=str(datetime.now(IST))[:16]))
    a_log(dict(ev="sweepsim_open", sym=s, dir=sig["dir"], grade=sig.get("grade"), entry=entry, tp=tp, risk=risk))

def sweepsim_manage(kmap):
    """4H-bar exits mirroring the live exit stack: close-based stop + hard 1-ATR stop +
    TP at 2R + 6-bar time stop. Deliberately NO scale-out locking (founder spec)."""
    global SWEEP_SIM
    today = str(datetime.now(IST).date())
    if SWEEP_SIM["cur_day"] != today:
        SWEEP_SIM["cur_day"] = today; SWEEP_SIM["day"] = 0.0
    for pos in SWEEP_SIM["positions"][:]:
        k = kmap.get(pos["sym"])
        if not k: continue
        new_bars = [b for b in k[:-1] if int(b[6]) > pos["ts"]]
        if not new_bars:
            continue
        pos["bars"] += len(new_bars); pos["ts"] = int(new_bars[-1][6])
        lg = pos["dir"] == "LONG"; risk = pos["risk_usdt"]; done = None
        for b in new_bars:
            h, l, c = float(b[2]), float(b[3]), float(b[4])
            hard = pos["sl_hard"]
            hit_hard = (l <= hard) if lg else (h >= hard)
            soft = (c < pos["sl"]) if lg else (c > pos["sl"])
            hit_tp = (h >= pos["tp"]) if lg else (l <= pos["tp"])
            if hit_hard:
                wick = (hard - l) if lg else (h - hard)
                fill = (hard - SLIP_WICK_FRAC_DN*wick) if lg else (hard + SLIP_WICK_FRAC_UP*wick)
                r = ((fill - pos["entry"]) if lg else (pos["entry"] - fill)) / abs(pos["entry"] - pos["sl"])
                done = ("SL_HARD", round(max(min(r, 3), -3), 2)); break
            if hit_tp and soft: done = ("AMBIGUOUS", -1.0); break
            if soft:
                r = ((c - pos["entry"]) if lg else (pos["entry"] - c)) / abs(pos["entry"] - pos["sl"])
                done = ("CLOSE_STOP", round(max(min(r, 3), -3), 2)); break
            if hit_tp: done = ("TP", 2.0); break
        if done is None and pos["bars"] >= 6:
            c = float(new_bars[-1][4])
            r = ((c - pos["entry"]) if lg else (pos["entry"] - c)) / abs(pos["entry"] - pos["sl"])
            done = ("TIME_STOP", round(max(min(r, 3), -3), 2))
        if done:
            kind, r = done
            pnl = round(r * risk, 2)
            SWEEP_SIM["equity"] += pnl; SWEEP_SIM["day"] += r
            pos.update(resultR=r, pnl=pnl, exit_kind=kind, exitTime=str(datetime.now(IST))[:16])
            SWEEP_SIM["history"].append(pos)
            SWEEP_SIM["positions"].remove(pos)
            a_log(dict(ev="sweepsim_exit", sym=pos["sym"], dir=pos["dir"], kind=kind, r=r, pnl=pnl,
                       equity=round(SWEEP_SIM["equity"], 2)))

# ---- LIQ SIM v2 ($500 live-mirror paper book: premium-FVG limit entries, 1:2 TP, NO 1R lock) ----
LIQ_SIM = {"equity": 500.0, "positions": [], "history": [], "day": 0.0, "cur_day": ""}
LIQ_SIM_RISK = 2.5          # % of sim equity per trade (mirrors LIVE_RISK_PCT)
LIQ_SIM_MAXPOS = 5

def liqsim_load(d):
    s = d.get("liqsim")
    if isinstance(s, dict):
        LIQ_SIM["equity"] = float(s.get("equity", 500.0))
        LIQ_SIM["positions"] = s.get("positions", [])
        LIQ_SIM["history"] = s.get("history", [])
        LIQ_SIM["day"] = float(s.get("day", 0.0))
        LIQ_SIM["cur_day"] = str(s.get("cur_day", ""))

def liqsim_manage(kmap):
    """Advance the $500 SIM book one hourly scan. Fill/exits on CLOSED 15m bars:
    close-based soft stop (wick tolerated) + hard stop 1 ATR beyond + TP at 2R + 32-bar time stop.
    Deliberately NO +1R lock - founder wants the raw 1:2 runner behavior measured."""
    global LIQ_SIM
    today = str(datetime.now(IST).date())
    if LIQ_SIM["cur_day"] != today:
        LIQ_SIM["cur_day"] = today; LIQ_SIM["day"] = 0.0
    for pos in LIQ_SIM["positions"][:]:
        k = kmap.get(pos["sym"])
        if not k: continue
        new_bars = [b for b in k[:-1] if int(b[6]) > pos["ts"]]
        if not pos.get("filled"):
            for b in new_bars:
                if float(b[3]) <= pos["entry"] <= float(b[2]):
                    pos["filled"] = True
                    pos["fill_ts"] = int(b[6])
                    break
            if not pos.get("filled"):
                pos["wait"] = pos.get("wait", 0) + len(new_bars)
                if pos["wait"] >= 32:
                    LIQ_SIM["positions"].remove(pos)
                else:
                    pos["ts"] = int(new_bars[-1][6]) if new_bars else pos["ts"]
                continue
        lg = pos["dir"] == "LONG"
        risk = pos["risk_usdt"]
        done = None
        bars_seen = 0
        for b in [x for x in new_bars if int(x[6]) > pos.get("fill_ts", pos["ts"])]:
            bars_seen += 1
            h, l, c = float(b[2]), float(b[3]), float(b[4])
            hard = pos["sl_hard"]
            hit_hard = (l <= hard) if lg else (h >= hard)
            soft_breach = (c < pos["sl"]) if lg else (c > pos["sl"])
            hit_tp = (h >= pos["tp"]) if lg else (l <= pos["tp"])
            if hit_hard:
                wick = (hard - l) if lg else (h - hard)
                fill = (hard - SLIP_WICK_FRAC_DN*wick) if lg else (hard + SLIP_WICK_FRAC_UP*wick)
                r = ((fill - pos["entry"]) if lg else (pos["entry"] - fill)) / abs(pos["entry"] - pos["sl"])
                done = ("SL_HARD", round(max(min(r, 3), -3), 2)); break
            if hit_tp and soft_breach:
                done = ("AMBIGUOUS", -1.0); break
            if soft_breach:
                r = ((c - pos["entry"]) if lg else (pos["entry"] - c)) / abs(pos["entry"] - pos["sl"])
                done = ("CLOSE_STOP", round(max(min(r, 3), -3), 2)); break
            if hit_tp:
                done = ("TP", 2.0); break
        if done is None and bars_seen >= 32:
            c = float(new_bars[-1][4])
            r = ((c - pos["entry"]) if lg else (pos["entry"] - c)) / abs(pos["entry"] - pos["sl"])
            done = ("TIME_STOP", round(max(min(r, 3), -3), 2))
        if done:
            kind, r = done
            pnl = round(r * risk, 2)
            LIQ_SIM["equity"] += pnl; LIQ_SIM["day"] += r
            pos.update(resultR=r, pnl=pnl, exit_kind=kind,
                       exitTime=str(datetime.now(IST))[:16])
            LIQ_SIM["history"].append(pos)
            LIQ_SIM["positions"].remove(pos)
            a_log(dict(ev="liqsim_exit", sym=pos["sym"], dir=pos["dir"], kind=kind, r=r, pnl=pnl,
                       equity=round(LIQ_SIM["equity"], 2)))
    # new entries from fresh setups (resting limit at the premium FVG, like live)
    open_syms = {p["sym"] for p in LIQ_SIM["positions"]}
    for s in LIQ_DATA:
        if s["state"] == "BROKEN" or s["sym"] in open_syms: continue
        if len(LIQ_SIM["positions"]) >= LIQ_SIM_MAXPOS: break
        risk = round(LIQ_SIM["equity"] * LIQ_SIM_RISK / 100.0, 2)
        lg = s["dir"] == "LONG"
        atr = float(s.get("atr") or abs(s["entry"] - s["sl"]))
        LIQ_SIM["positions"].append(dict(
            sym=s["sym"], dir=s["dir"], entry=s["entry"], sl=s["sl"], tp=s["tp"],
            sl_hard=(s["sl"] - atr) if lg else (s["sl"] + atr),
            risk_usdt=risk, ts=s.get("_candle_ts", int(time.time()*1000)),
            filled=False, wait=0, t_open=str(datetime.now(IST))[:16]))
        open_syms.add(s["sym"])

def liq_publish_scan():
    print(f"\n=== LIQ PUBLISH {datetime.now(IST):%H:%M IST} ===")
    regime=qml_regime()
    out=[]
    kmap = {}
    for s in PAIRS:
        k=get_klines_15(s)
        if not k: continue
        kmap[s] = k
        r=detect_liq15(k)
        if not r: continue
        r['sym']=s
        if regime!='BOTH' and ((regime=='LONG' and r['dir']!='LONG') or (regime=='SHORT' and r['dir']!='SHORT')): continue
        out.append(r)
        if r.get('state') == 'ACTIVE':
            liq_shadow_open(r)
    out.sort(key=lambda x: (0 if x['state']=='ACTIVE' else 1, -x['rr']))
    LIQ_DATA.clear(); LIQ_DATA.extend(out[:20])
    liq_shadow_step(kmap)
    print(f"  published {len(LIQ_DATA)} liquidity setups | liq-shadow tracked: {len(LIQ_SHADOW)} open, {LIQ_SHADOW_STATS.get('n',0)} closed")
    save_state()

def market_publish_scan():
    print(f"\n=== MARKET PUBLISH {datetime.now(IST):%H:%M IST} ===")
    tmap={}
    try:
        with urllib.request.urlopen("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20) as r:
            for t in json.loads(r.read().decode()):
                tmap[t['symbol']]=t
    except Exception as e:
        print("  ticker fetch failed:", e); return
    rows=[]
    for s in PAIRS:
        k=get_klines_15(s)
        t=tmap.get(s)
        if not k or not t: continue
        closed=k[:-1]
        if len(closed)<20: continue
        def bucket(n):
            seg=closed[-n:]
            v=sum(float(x[5]) for x in seg); tb=sum(float(x[9]) for x in seg)
            return (tb/v*100) if v>0 else 50
        chg1h=(float(closed[-1][4])-float(closed[-5][4]))/float(closed[-5][4])*100 if len(closed)>5 else 0
        rows.append(dict(sym=s.replace('USDT',''), price=float(t['lastPrice']), pct24=float(t['priceChangePercent']),
                         qvol=float(t['quoteVolume']), chg1h=chg1h,
                         f30=bucket(2), f60=bucket(4), f120=bucket(8), f240=bucket(16)))
    rows.sort(key=lambda x: -x['pct24'])
    MKT_DATA.clear(); MKT_DATA.extend(rows[:150])
    print(f"  published {len(MKT_DATA)} market rows")
    save_state()

def hourly_loop(job, minute):
    while True:
        if halt_requested(): print("BOT_HALT present - publisher stopping"); return
        now=datetime.now(timezone.utc)
        t=now.replace(minute=minute,second=0,microsecond=0)
        if t<=now: t=(now+timedelta(hours=1)).replace(minute=minute,second=0,microsecond=0)
        while datetime.now(timezone.utc)<t: time.sleep(45)
        try: job()
        except Exception as e: print("  ! publish error:", e)

# ==================== MAIN LOOP ====================
PAIRS_LIVE = False   # True only when the list came from the CoinDCX API this session

def load_pairs():
    """Fetch the REAL CoinDCX USDT-futures instrument list. The old version sent unencoded
    '[]' in the URL, failed silently, and fell back to 16 hardcoded coins - which disabled
    the listing guard and got good coins wrongly rejected + blocklisted. Never again."""
    global PAIRS, PAIRS_LIVE
    PAIRS_LIVE = False
    urls = [
        "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name%5B%5D=USDT",
        "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name=USDT",
        "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments",
    ]
    for u in urls:
        try:
            # browser headers REQUIRED - default python UA gets 403 from Cloudflare
            # (this was the root cause of the silent 16-pair fallback from day one)
            req = urllib.request.Request(u, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "application/json",
                "Origin": "https://coindcx.com",
                "Referer": "https://coindcx.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                lst = json.loads(r.read().decode())
            if isinstance(lst, list) and lst:
                PAIRS = sorted({str(x).replace("B-", "").replace("_USDT", "") + "USDT" for x in lst if "USDT" in str(x)})
                PAIRS_LIVE = True
                print(f"Loaded {len(PAIRS)} USDT-futures pairs from CoinDCX API (live list)")
                if len(PAIRS) < 40:
                    tg(f"!! CoinDCX API returned only {len(PAIRS)} pairs - endpoint may have changed, verify")
                return
        except Exception as e:
            print(f"  pair fetch failed ({u[-45:]}): {e}")
    PAIRS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","TRXUSDT","DOTUSDT","LTCUSDT","NEARUSDT","UNIUSDT","ATOMUSDT","ARBUSDT"]
    print("!! FALLBACK: 16 hardcoded pairs - listing guard relaxed, expect some rejects")
    tg("!! CoinDCX pair-list fetch FAILED - bot on 16-pair fallback, many coins will be skipped")

def reconcile_blocklist():
    """Unblock any coin that now appears on the freshly-loaded CoinDCX list.
    Fixes stale entries from the era when the bot ran on the broken 16-pair fallback."""
    if not PAIRS_LIVE or not _BLOCKED: return
    unblocked = sorted(s for s in _BLOCKED if s in PAIRS)
    if unblocked:
        for s in unblocked: _BLOCKED.discard(s)
        try: json.dump(sorted(_BLOCKED), open(_BLOCK_FILE, "w"))
        except Exception: pass
        tail = '...' if len(unblocked) > 6 else ''
        tg(f"BLOCKLIST UPDATE: {len(unblocked)} coins now listed on CoinDCX and unblocked: {', '.join(unblocked[:6])}{tail}")
        print(f"  blocklist reconciled: unblocked {len(unblocked)} coins")

def next_run_time():
    """Next :01 UTC of hours 0,4,8,12,16,20 (as a datetime)."""
    now = datetime.now(timezone.utc)
    for h in (0,4,8,12,16,20):
        t = now.replace(hour=h, minute=1, second=0, microsecond=0)
        if t > now: return t
    return (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)

def scan_once():
    bk = BOOK_SWEEP
    today = datetime.now(IST).date()
    if bk['cur_day'] != today: bk['cur_day'], bk['day_r'] = today, 0.0
    print(f"\n=== SWEEP SCAN {datetime.now(IST):%d %b %H:%M IST} | equity=Rs.{bk['equity']:,.0f} | day P&L={bk['day_r']:+.2f}R ===")
    daily_blocked = bk['day_r'] <= DAILY_LOSS_LIMIT_R
    if daily_blocked:
        print(f"  Daily loss limit ({bk['day_r']:+.2f}R) - PUBLISHING signals only, no new entries today")
    btc_k = get_klines("BTCUSDT")
    kmap = {}
    for s in PAIRS:
        k = get_klines(s)
        if k: kmap[s] = k
    manage_book(bk, kmap)   # ALWAYS manage open trades first, even on limit days
    sweepsim_manage(kmap)   # $500 SWEEP SIM: same 4H cadence, live-mirror exits
    _sw_regime = qml_regime()          # BTC 4H regime (audit: aligned 86% win vs counter 12%)
    _sw_slot = datetime.now(IST).strftime("%H:%M")
    _maxp = LIVE_MAX_POS if MODE=="LIVE" else bk['max_pos']
    book_full = len(bk['positions']) >= _maxp
    if book_full:
        print(f"  Max positions ({_maxp}) reached - publishing signals only")
    SWEEP_SIGNALS.clear()
    for s, k in kmap.items():
        if s == "BTCUSDT": continue
        if any(p["sym"] == s for p in bk['positions']): continue
        sig = detect_signal(s, k, btc_k)
        if sig:
            print(f"  SIGNAL {s} {sig['dir']} grade={sig['grade']} volX={sig['volX']} buy={sig['buyPct']}%")
            risk = abs(sig['entry']-sig['sl'])
            SWEEP_SIGNALS.append(dict(sym=s, dir=sig['dir'], grade=sig['grade'],
                entry=sig['entry'], sl=sig['sl'],
                tp=sig['entry']+2*risk if sig['dir']=='LONG' else sig['entry']-2*risk,
                volX=sig['volX'], buyPct=sig['buyPct'], t=str(datetime.now(IST))[:16]))
            if daily_blocked or book_full:
                print(f"  PUBLISH-ONLY {s} {sig['dir']} [{sig['grade']}] - daily limit / book full, not traded")
            else:
                if sig['grade'] != 'A+':
                    print(f"  SKIP {s} {sig['dir']} — grade B (audit 300 trades: B = 37% win / -65R; publish-only)")
                elif (_sw_regime == 'LONG' and sig['dir'] == 'SHORT') or (_sw_regime == 'SHORT' and sig['dir'] == 'LONG'):
                    print(f"  SKIP {s} {sig['dir']} — counter-BTC-regime {_sw_regime} (audit: 12% win vs 86% aligned)")
                elif _sw_slot in SWEEP_REST_SLOTS:
                    print(f"  SKIP {s} {sig['dir']} — rest slot {_sw_slot} (audit: 13:31/17:31 scans won 6%)")
                elif sig['volX'] < MIN_VOLX:
                    print(f"  SKIP {s} {sig['dir']} — volX {sig['volX']} below {MIN_VOLX} (published, not traded)")
                elif on_cooldown(bk, s, sig['dir']):
                    print(f"  SKIP {s} {sig['dir']} — re-entry cooldown {COOLDOWN_H}h after previous close")
                else:
                    open_trade(sig, bk, 'sweep')
                    sweepsim_enter(sig, s, k)
    save_state()

if __name__ == "__main__" and "--margin-probe" in sys.argv:
    margin_probe(); raise SystemExit(0)

if __name__ == "__main__" and "--probe" in sys.argv:
    probe(); raise SystemExit(0)

if __name__ == "__main__":
    acquire_lock()
    atexit.register(release_lock)
    config_sanity()          # refuse to boot on a half-merged file (LIQ_MIN_QVOL lesson)
    if LIVE_VENUE == "BINANCE" and (BIN_KEY and BIN_SECRET):
        b_single_asset_mode()   # -4168 root cause: Multi-Assets mode blocks all isolated margin
    if halt_requested():
        print("BOT_HALT file present - bot will not start. Remove it to resume:  rm -f BOT_HALT"); release_lock(); sys.exit(0)
    print("BA CAPITAL 4H Sweep Bot — PAPER MODE" if PAPER else "LIVE MODE")
    print(f"risk={RISK_PCT}%/trade  scale-out @1R(50%)/1.5R(25%)/2R(25%)  trail lock +{TRAIL_LOCK_R}R  max={MAX_POSITIONS} pos  daily limit={DAILY_LOSS_LIMIT_R}R")
    print(f"re-entry cooldown={COOLDOWN_H}h after any close  |  min sweep volX={MIN_VOLX}")
    load_state(); load_pairs(); reconcile_blocklist()
    self_audit()
    startup_report()
    if not PAPER and RISK_PCT > 2.0:
        print("REFUSING TO RUN LIVE with paper risk settings. Set RISK_PCT <= 2.0 first."); release_lock(); sys.exit(1)
    threading.Thread(target=qml_loop, daemon=True).start()
    threading.Thread(target=tg_command_loop, daemon=True).start()
    if QML_MODE == "LIVE" and LIVE_VENUE == "BINANCE":
        threading.Thread(target=_fill_watch_loop, daemon=True).start()
    threading.Thread(target=hourly_loop, args=(liq_publish_scan,14), daemon=True).start()
    threading.Thread(target=hourly_loop, args=(market_publish_scan,44), daemon=True).start()
    print("QML bot (15m) started — 8 max positions, scans every 15 min, own Rs.2,00,000 book")
    while True:
        try:
            scan_once()
        except Exception as e:
            print("  ! scan error (continuing):", e)
        t = next_run_time()
        print(f"  next scan at {t.astimezone(IST):%d %b %H:%M} IST — sleeping. (Ctrl+C to stop)")
        while datetime.now(timezone.utc) < t:
            time.sleep(60)   # short naps: resumes correctly even after laptop sleep
            if halt_requested():
                print("BOT_HALT detected - bot stopping cleanly"); release_lock(); sys.exit(0)