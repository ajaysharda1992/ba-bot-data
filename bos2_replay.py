#!/usr/bin/env python3
"""
2-CLOSE BOS REPLAY (2026-10-06) - the founder's question before deploy:
  Of every SHORT we actually took on Binance, how many would this rule have saved,
  and of the survivors, how many would have reached +1R or TP?
Rule: the structure must show 2 CONSECUTIVE 15m candle closes beyond P2 BEFORE our
entry time, else the trade never happens. Survivors are simulated with the new
exit stack (close-based stop + hard stop 1 ATR beyond + +1R lock).
Read-only. Run ANYTIME (works with the old ba_bot.py still in place):
  cd /root && python3 bos2_replay.py
"""
import json, time, urllib.request
from datetime import datetime, timezone, timedelta

LEDGER = "/root/ba_bot_trades.json"
IST = timezone(timedelta(hours=5, minutes=30))
HARD_ATR = 1.0

def fetch(url):
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5)
    return None

def ist_ms(s):
    try: return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()*1000)
    except Exception: return None

def analyze(p):
    sym=p.get("sym"); d=p.get("dir"); lg=(d=="LONG")
    e=float(p["entry"]); sl=float(p["sl"]); tp=float(p["tp"])
    risk=abs(e-sl); rr=abs(e-tp)/risk if risk>0 else 0
    ent_ms=ist_ms(p.get("t_open","")); ex_ms=ist_ms(p.get("exitTime","")) or ent_ms
    if not ent_ms: return None
    k=fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=15m&limit=200"
            f"&endTime={ex_ms+16*3600*1000}")
    if not k or len(k)<30: return None
    bars=[b for b in k if int(b[6])<=ent_ms]          # information available AT entry
    after=[b for b in k if int(b[0])>ent_ms]
    if len(bars)<20 or not after: return None
    atr=sum(float(b[2])-float(b[3]) for b in bars[-15:-1])/14 or risk
    hard=(sl-HARD_ATR*atr) if lg else (sl+HARD_ATR*atr)
    beyond=lambda c: (c>tp) if lg else (c<tp)
    # 1) the founder's rule: 2 consecutive closes beyond P2 BEFORE entry?
    bos2=False
    for i in range(1, len(bars)):
        if beyond(float(bars[i][4])) and beyond(float(bars[i-1][4])): bos2=True; break
    # 2) simulate survivor with the new exit stack (close-based stop, hard wick stop, +1R lock)
    simR=None
    if bos2:
        locked=False
        for b in after:
            h,l,c=float(b[2]),float(b[3]),float(b[4])
            adv=((c-e) if lg else (e-c))/risk
            if adv>=1.0: locked=True
            hit_hard=(l<=hard) if lg else (h>=hard)
            hit_tp=((h>=tp) if lg else (l<=tp))
            closed_beyond=((c>sl) if lg else (c<sl))
            if hit_tp: simR=rr; break
            if hit_hard and not locked: simR=-1.0; break
            if closed_beyond: simR=(1.0 if locked else round(max(min(((c-e) if lg else (e-c))/risk,2),-2),2)); break
        if simR is None: simR=(1.0 if locked else round(max(min(((float(after[-1][4])-e) if lg else (e-float(after[-1][4])))/risk,2),-2),2))
    return dict(sym=sym, dir=d, oldR=round(float(p.get("resultR") or 0),2),
                bos2=bos2, simR=round(simR,2) if simR is not None else None)

def main():
    d=json.load(open(LEDGER))
    hist=[h for h in (d.get("qml",{}).get("history") or []) if h.get("live") or h.get("exit_kind")=="SYNC"]
    seen=set(); rows=[]
    for h in hist:
        key=(h.get("sym"),h.get("dir"),h.get("entry"))
        if key in seen: continue
        seen.add(key)
        a=analyze(h)
        if a: rows.append(a); time.sleep(0.2)
    json.dump(rows, open("/root/bos2_rows.json","w"), indent=1)
    sh=[r for r in rows if r["dir"]=="SHORT"]
    skipped=[r for r in sh if not r["bos2"]]
    surv=[r for r in sh if r["bos2"]]
    tp=[r for r in surv if (r["simR"] or 0)>=2]
    one=[r for r in surv if 1<=(r["simR"] or 0)<2]
    win=[r for r in surv if (r["simR"] or 0)>0]
    print(f"\nSHORTS traded on Binance: {len(sh)}")
    print(f"  saved by 2-close rule (trade never happens): {len(skipped)}  "
          f"[{', '.join(r['sym'] for r in skipped)}]")
    print(f"  survivors: {len(surv)}")
    print(f"    of survivors - hit TP (>=2R): {len(tp)} [{', '.join(r['sym'] for r in tp)}]")
    print(f"                 +1R banked (lock): {len(one)} [{', '.join(r['sym'] for r in one)}]")
    print(f"                 any profit: {len(win)}/{len(surv)} | losses: {len(surv)-len(win)}")
    so=sum(r['oldR'] for r in sh); sn=sum(r['simR'] or 0 for r in surv)
    print(f"\n  total old R on these shorts: {so:+.1f}R")
    print(f"  total new-stack R (survivors only): {sn:+.1f}R  | skipped losses avoided: {len(skipped)} x ~-1R")
    print("saved -> /root/bos2_rows.json (paste to me)")

if __name__=='__main__':
    main()
