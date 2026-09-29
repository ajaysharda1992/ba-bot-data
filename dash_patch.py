
# BA Capital dashboard patch: liquidity TP/SL ledger + sweep ledger auto-merge + scan timestamps
# Safe to run multiple times - exits cleanly if already applied.
f = '/tmp/ba-dashboard/index.html'
src = open(f).read()

if 'liqHistTbl' in src and 'MERGE bot history' in src:
    print('Already applied - nothing to do.')
    raise SystemExit

def rep(old, new, label):
    global src
    n = src.count(old)
    assert n == 1, label + ' found ' + str(n) + ' times'
    src = src.replace(old, new)
    print('ok:', label)

# --- A: LIQ_HIST state var ---
rep('LIVE_PRICES={};', 'LIVE_PRICES={}; let LIQ_HIST=[];', 'A liq state')

# --- B: liquidity ledger functions ---
liq_fns = """function loadLiqHist(){ try{ LIQ_HIST=JSON.parse(localStorage.getItem('ba_liqhist'))||[]; }catch(e){ LIQ_HIST=[]; } }
function saveLiqHist(){ try{ localStorage.setItem('ba_liqhist', JSON.stringify(LIQ_HIST.slice(0,150))); }catch(e){} }
function updateLiqLedger(results){
  loadLiqHist();
  for(const r of (results||[])){
    if(r.state!=='ACTIVE') continue;
    if(LIQ_HIST.some(x=>x.sym===r.sym && x.dir===r.dir && Math.abs(x.entry-r.entry)<1e-9)) continue;
    LIQ_HIST.unshift({sym:r.sym, dir:r.dir, entry:r.entry, sl:r.sl, tp:r.tp, t:Date.now(), status:'RUNNING', resultR:0});
  }
  checkLiqLedgerLive();
  saveLiqHist();
  try{ applyViewLiqHist(); }catch(e){}
}
function checkLiqLedgerLive(){
  let ch=false;
  for(const x of LIQ_HIST){
    if(x.status!=='RUNNING') continue;
    const px=LIVE_PRICES[x.sym]; if(!px) continue;
    const LONG=x.dir==='LONG';
    if((LONG&&px>=x.tp)||(!LONG&&px<=x.tp)){ x.status='TP'; x.resultR=+((Math.abs(x.tp-x.entry)/Math.abs(x.entry-x.sl)).toFixed(2)); ch=true; continue; }
    if((LONG&&px<=x.sl)||(!LONG&&px>=x.sl)){ x.status='SL'; x.resultR=-1; ch=true; }
  }
  if(ch){ saveLiqHist(); try{ applyViewLiqHist(); }catch(e){} }
}
function applyViewLiqHist(){
  const tbl=document.getElementById('liqHistTbl'); if(!tbl) return;
  const rows=document.getElementById('liqHistRows');
  const st=document.getElementById('liqHistStatus');
  if(!LIQ_HIST.length){ tbl.style.display='none'; if(st) st.textContent='Liquidity ledger - every triggered setup gets logged here, results tracked live'; return; }
  const tp=LIQ_HIST.filter(x=>x.status==='TP').length, sl=LIQ_HIST.filter(x=>x.status==='SL').length;
  const sumR=LIQ_HIST.reduce((a,x)=>a+(x.resultR||0),0);
  const win=(tp+sl)>0?Math.round(tp/(tp+sl)*100):0;
  if(st) st.innerHTML='Ledger: <b>'+LIQ_HIST.length+'</b> triggered &middot; <b style=\\"color:#2ecc71\\">'+tp+' TP</b> &middot; <b style=\\"color:#E5484D\\">'+sl+' SL</b>'+((tp+sl)>0?' &middot; win <b>'+win+'%</b>':'')+' &middot; total <b style=\\"color:'+(sumR>=0?'#2ecc71':'#E5484D')+'\\">'+(sumR>=0?'+':'')+sumR.toFixed(2)+'R</b>';
  rows.innerHTML=LIQ_HIST.slice(0,50).map(x=>{
    const LONG=x.dir==='LONG';
    let res;
    if(x.status==='TP') res='<span style=\\"color:#2ecc71;font-weight:800;\\">TP +'+(+x.resultR).toFixed(2)+'R</span>';
    else if(x.status==='SL') res='<span style=\\"color:#E5484D;font-weight:800;\\">SL -1R</span>';
    else res='<span style=\\"color:#C9A227;\\">running</span>';
    const tm=new Date(x.t).toLocaleString(\\"en-IN\\",{timeZone:\\"Asia/Kolkata\\",day:\\"2-digit\\",month:\\"short\\",hour:\\"2-digit\\",minute:\\"2-digit\\",hour12:false});
    return '<tr><td class=\\"coin\\">'+x.sym+'</td><td class=\\"'+(LONG?'bull':'bear')+'\\">'+(LONG?'LONG':'SHORT')+'</td><td class=\\"num\\">'+tm+'</td><td style=\\"white-space:nowrap;\\">'+res+'</td></tr>';
  }).join('');
  tbl.style.display='table';
}
function exportLiqHist(){
  const ok=(typeof downloadJSON==='function')?downloadJSON('ba_liquidity_ledger.json', LIQ_HIST):false;
  const msg=document.getElementById('liqHistExpMsg');
  if(ok){ if(msg) msg.textContent='downloaded'; }
  else{ try{ navigator.clipboard.writeText(JSON.stringify(LIQ_HIST)); if(msg) msg.textContent='copied'; }catch(e){} }
}
function resetLiqHist(){ if(confirm('Clear liquidity ledger?')){ LIQ_HIST=[]; saveLiqHist(); applyViewLiqHist(); } }

async function runLiq(auto){"""
rep("async function runLiq(auto){", liq_fns, 'B liq functions')

# --- C: hook ledger update into runLiq ---
rep("    lastLiq=results;",
    "    lastLiq=results;\n    try{ updateLiqLedger(results); }catch(e){ console.error('liq ledger', e); }",
    'C runLiq hook')

# --- D: live resolution interval ---
rep("setInterval(()=>{ if(BOT_DATA && document.getElementById('botArea').style.display==='block') refreshBotMarks(); }, 60000);",
    "setInterval(()=>{ if(BOT_DATA && document.getElementById('botArea').style.display==='block') refreshBotMarks(); }, 60000);\nsetInterval(()=>{ try{ loadLiqHist(); checkLiqLedgerLive(); }catch(e){} }, 30000);",
    'D interval')

# --- E: ledger UI panel under the liq table ---
rep("""      <tbody id="liqRows"></tbody>
    </table></div>""",
    """      <tbody id="liqRows"></tbody>
    </table></div>
    <div style="margin-top:26px;">
      <div class="panel-title" style="font-size:13px;">Liquidity Ledger (triggered setups)</div>
      <div id="liqHistStatus" style="text-align:center;color:#8B94A7;padding:10px;font-size:13px;">Logs every triggered setup, results tracked live</div>
      <div style="text-align:center;margin:2px 0 8px;"><button class="btn2" onclick="exportLiqHist()" style="font-size:11px;padding:6px 16px;">Download / Copy</button> <button class="btn2" onclick="resetLiqHist()" style="font-size:11px;padding:6px 12px;">Reset</button> <span id="liqHistExpMsg" style="font-size:11px;color:#8B94A7;margin-left:8px;"></span></div>
      <div class="tblwrap"><table id="liqHistTbl" style="display:none;">
        <thead><tr><th>Coin</th><th>Dir</th><th>Triggered</th><th>Result</th></tr></thead>
        <tbody id="liqHistRows"></tbody>
      </table></div>
    </div>""",
    'E liq ledger UI')

# --- F: render ledger on tab open ---
rep("  if(t==='liq' && !liqScanning && Date.now()-(window.LIQ_LAST||0)>10*60000) runLiq(false);",
    "  if(t==='liq' && !liqScanning && Date.now()-(window.LIQ_LAST||0)>10*60000) runLiq(false);\n  loadLiqHist(); try{ applyViewLiqHist(); }catch(e){}",
    'F showTab')

# --- G: seed-once -> merge every load ---
old_seed = """  if(!SIG_HIST.length && hist.length){
    SIG_HIST = hist.slice(-100).reverse().map(h=>({
      sym:(h.sym||'').replace('USDT',''), dir:h.dir, entry:h.entry, sl:h.sl, tp:h.tp,
      t:Date.now()-3600000, status:(h.resultR>0?'TP':(h.resultR<0?'SL':'TIME')),
      resultR:h.resultR||0, grade:h.grade||'', volX:h.volX??null, buyPct:h.buyPct??null,
      sweep:'(from bot)', trend:'', corr:null }));
    saveSigHist();
    try{ applyViewSigHist(); }catch(e){}
  }"""
new_seed = """  try{ // MERGE bot history into local ledger (add missing trades, keep existing)
    const have=new Set(SIG_HIST.map(r=>r.sym+'|'+r.dir+'|'+r.entry+'|'+r.resultR));
    let added=0;
    hist.slice(-100).forEach(h=>{
      const key=(h.sym||'').replace('USDT','')+'|'+h.dir+'|'+h.entry+'|'+h.resultR;
      if(!have.has(key)){
        SIG_HIST.push({sym:(h.sym||'').replace('USDT',''), dir:h.dir, entry:h.entry, sl:h.sl, tp:h.tp,
          t:Date.now()-3600000, status:(h.resultR>0?'TP':(h.resultR<0?'SL':'TIME')),
          resultR:h.resultR||0, grade:h.grade||'', volX:h.volX??null, buyPct:h.buyPct??null,
          sweep:'(from bot)', trend:'', corr:null});
        added++;
      }
    });
    if(added){ SIG_HIST.sort((a,b)=>b.t-a.t); saveSigHist(); }
    try{ applyViewSigHist(); }catch(e){}
  }catch(e){}"""
rep(old_seed, new_seed, 'G sig merge')

# --- H: honest timestamps on VPS bot scan section ---
rep("document.getElementById('botSigStatus').textContent='VPS bot detected '+ss.length+' signal(s) at the last 4H close · '+ss[0].t+' IST';",
    "document.getElementById('botSigStatus').textContent='VPS bot: '+ss.length+' signal(s) from '+ss[0].t+' IST · bot file updated '+(d.updated||'').slice(11,16)+' IST';",
    'H status1')
rep("document.getElementById('botSigStatus').textContent='No signals at the last 4H close (or bot updating) — the VPS bot scans every 4H and publishes here';",
    "document.getElementById('botSigStatus').textContent='No signals at the last 4H close · bot file updated '+(d.updated||'').slice(11,16)+' IST · scans every 4H';",
    'H status2')

open(f, 'w').write(src)
print('ALL SECTIONS PATCHED OK')
