"""Standalone, dependency-free HTML dashboards for counterfactual experiments."""

import json
from pathlib import Path


STYLE = """
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#17212b;background:#f4f7f8}
*{box-sizing:border-box}body{margin:0}main{max-width:1180px;margin:auto;padding:32px 24px 60px}
h1{font-size:30px;letter-spacing:-.03em;margin:0 0 8px}h2{font-size:18px;margin:0 0 12px}
p{line-height:1.5}.muted{color:#596773}.intro{max-width:820px;margin:0 0 24px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 26px}.chip{background:#e5edf0;border:1px solid #d5e1e5;border-radius:999px;padding:7px 11px;font-size:13px}
.grid{display:grid;grid-template-columns:1fr;gap:18px}.card{background:white;border:1px solid #dbe4e7;border-radius:16px;box-shadow:0 3px 14px #243b4a0b;padding:22px;margin-bottom:18px}
.card.full{grid-column:1/-1}.controls{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
select{font:inherit;padding:9px 12px;border:1px solid #b9cbd1;border-radius:9px;background:white}
svg{width:100%;height:auto;overflow:visible}.axis{stroke:#b9cbd1;stroke-width:1}.gridline{stroke:#e4eaed;stroke-width:1}.label{fill:#50606c;font-size:12px}.dot{cursor:pointer;stroke:white;stroke-width:1.5}.dot:hover{stroke:#17212b;stroke-width:2.5}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:#50606c;margin-top:8px}.swatch{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.stat{background:#f3f7f8;border-radius:12px;padding:14px}.stat b{display:block;font-size:22px;margin:5px 0}.stat small{color:#596773}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #e7edef}th{color:#50606c;font-weight:600}tr:hover{background:#f4f8f9}.scroll{overflow:auto;max-height:430px}
.note{font-size:13px;color:#43535e;background:#eaf1f4;padding:12px 15px;border-radius:11px;margin-top:20px}.explain{border-left:4px solid #187a72;background:#edf7f5;padding:14px 16px;margin:20px 0;border-radius:0 10px 10px 0;line-height:1.5}.good{color:#187a72}.bad{color:#b85659}
.metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.metric-grid .card{margin:0}
@media(max-width:850px){.grid,.metric-grid{display:block}.stats{grid-template-columns:1fr 1fr}main{padding:20px 14px}}
"""


ACTION_SCRIPT = """
const data=__DATA__;
const $=id=>document.getElementById(id);
const fmt=x=>Number.isFinite(x)?x.toFixed(x>=100?1:2):'—';
const days=data.sampled_turns;
const select=$('day');
days.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`Day ${r.day} · ${r.offered.join(', ')}`;select.append(o)});
function el(tag,attrs={}){const x=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>x.setAttribute(k,v));return x}
function actionName(a){return `wear ${a.wear.map(i=>i+1).join('+')} · toss ${a.discard.length?a.discard.map(i=>i+1).join(','):'none'}`}
function delta(v,b,prefix=''){const d=v-b,cls=d<0?'good':d>0?'bad':'';return `<span class="${cls}">${prefix}${d>0?'+':''}${fmt(d)}</span>`}
function render(){
 const r=days[Number(select.value)||0];if(!r){$('context').textContent='No sampled hand had two socks; try an earlier horizon or higher budget.';return;}
 $('context').textContent=`${r.choices_evaluated} actions · ${r.effective_horizon}-day look-ahead · drawer ${r.drawer_size} · household spent before choice $${fmt(r.spent_so_far)}`;
 if(!r.choices.length){
  $('scatter').replaceChildren();$('frontier').innerHTML='';$('actions').innerHTML='';
  $('picked').textContent='Sockless day: no legal choice';
  $('picked-note').textContent=r.sockless_reason;
  $('stats').innerHTML=`<div class="stat"><small>Offered socks</small><b>${r.offered.length}</b><small>At least 2 are required</small></div><div class="stat"><small>Immediate embarrassment</small><b>65,536</b><small>Fixed sockless penalty</small></div><div class="stat"><small>Action count</small><b>0</b><small>The player is not consulted</small></div>`;
  return;
 }
 const all=r.choices, base=r.baseline_action;
 const svg=$('scatter');svg.replaceChildren();const W=680,H=410,m={l:72,r:24,t:23,b:62};
 const xs=all.map(a=>a.mean.spend),ys=all.map(a=>a.mean.household_embarrassment);
 const xmin=Math.min(0,...xs),xmax=Math.max(...xs)+1,ymin=Math.min(0,...ys),ymax=Math.max(...ys)+1;
 const px=v=>m.l+(v-xmin)/(xmax-xmin)*(W-m.l-m.r);
 const py=v=>H-m.b-(v-ymin)/(ymax-ymin)*(H-m.t-m.b);
 for(let i=0;i<=4;i++){
  const xv=xmin+(xmax-xmin)*i/4,yv=ymin+(ymax-ymin)*i/4;
  svg.append(el('line',{x1:px(xv),y1:m.t,x2:px(xv),y2:H-m.b,class:'gridline'}));
  svg.append(el('line',{x1:m.l,y1:py(yv),x2:W-m.r,y2:py(yv),class:'gridline'}));
  const xt=el('text',{x:px(xv),y:H-m.b+21,'text-anchor':'middle',class:'label'});xt.textContent=fmt(xv);svg.append(xt);
  const yt=el('text',{x:m.l-9,y:py(yv)+4,'text-anchor':'end',class:'label'});yt.textContent=fmt(yv);svg.append(yt)
 }
 const xlabel=el('text',{x:W/2,y:H-8,'text-anchor':'middle',class:'label'});xlabel.textContent='Additional household six-pack spending during look-ahead ($)';svg.append(xlabel);
 const ylabel=el('text',{x:15,y:H/2,transform:`rotate(-90 15 ${H/2})`,'text-anchor':'middle',class:'label'});ylabel.textContent='Household embarrassment during look-ahead';svg.append(ylabel);
 svg.append(el('line',{x1:m.l,y1:H-m.b,x2:W-m.r,y2:H-m.b,class:'axis'}));
 svg.append(el('line',{x1:m.l,y1:m.t,x2:m.l,y2:H-m.b,class:'axis'}));
 const colors=['#187a72','#2b71ae','#b37b20','#b85659'];
 all.forEach((a,index)=>{const isBase=a.wear.join()==base.wear.join()&&a.discard.join()==base.discard.join();
  const angle=(index*2.399963)%6.283,rad=isBase?0:Math.min(5,1+Math.floor(index/8));
  const c=el('circle',{cx:px(a.mean.spend)+Math.cos(angle)*rad,cy:py(a.mean.household_embarrassment)+Math.sin(angle)*rad,r:isBase?8:5.5,fill:colors[Math.min(a.discard.length,3)],class:'dot',opacity:isBase?1:.72});
  if(isBase)c.setAttribute('stroke','#17212b');
  const title=el('title');title.textContent=`${actionName(a)}\nEmbarrassment ${fmt(a.mean.household_embarrassment)} ± ${fmt(a.standard_error.household_embarrassment)} SE\nSpend $${fmt(a.mean.spend)} ± ${fmt(a.standard_error.spend)} SE\nSockless ${fmt(a.mean.sockless_days)}`;c.append(title);
  c.addEventListener('click',()=>show(a,base));svg.append(c)
 });
 $('frontier').innerHTML=r.frontier.map(a=>`<tr><td>${actionName(a)}</td><td>${fmt(a.immediate_embarrassment)}</td><td>${fmt(a.mean.household_embarrassment)}</td><td>$${fmt(a.mean.spend)}</td><td>${fmt(a.mean.sockless_days)}</td></tr>`).join('');
 $('actions').innerHTML=all.map(a=>`<tr><td>${actionName(a)}</td><td>${fmt(a.immediate_embarrassment)}</td><td>${fmt(a.mean.household_embarrassment)} ± ${fmt(a.standard_error.household_embarrassment)}</td><td>${delta(a.mean.household_embarrassment,base.mean.household_embarrassment)}</td><td>$${fmt(a.mean.spend)} ± ${fmt(a.standard_error.spend)}</td><td>${delta(a.mean.spend,base.mean.spend,'$')}</td><td>${fmt(a.mean.sockless_days)}</td></tr>`).join('');
 show(base,base)
}
function show(a,b){$('picked').textContent=actionName(a);$('picked-note').textContent=`Greedy reference: ${actionName(b)}. Negative changes are improvements. Spending is a household outcome from six-pack purchases, not an action fee.`;
 const keys=[['household_embarrassment','Embarrassment'],['spend','Spending ($)'],['sockless_days','Sockless days']];
 $('stats').innerHTML=keys.map(([key,label])=>`<div class="stat"><small>${label}</small><b>${fmt(a.mean[key])}</b><small>greedy ${fmt(b.mean[key])} · change ${fmt(a.mean[key]-b.mean[key])}</small></div>`).join('')
}
select.addEventListener('change',render);render();
"""


COMPARE_SCRIPT = """
const data=__DATA__;
const $=id=>document.getElementById(id),fmt=x=>x.toFixed(x>=100?1:2);
const rows=data.runs;
function el(tag,attrs={}){const x=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>x.setAttribute(k,v));return x}
function renderMetric(id,key,title){
 const svg=$(id),W=340,H=320,m={l:52,r:12,t:25,b:55};svg.replaceChildren();
 const vals=rows.flatMap(r=>[r.a[key],r.b[key]]),ymax=Math.max(1,...vals)*1.17;
 const py=v=>H-m.b-v/ymax*(H-m.t-m.b),colors=['#187a72','#b85659'],xs=[105,255];
 for(let i=0;i<=4;i++){const v=ymax*i/4,y=py(v);svg.append(el('line',{x1:m.l,y1:y,x2:W-m.r,y2:y,class:'gridline'}));const t=el('text',{x:m.l-8,y:y+4,'text-anchor':'end',class:'label'});t.textContent=fmt(v);svg.append(t)}
 ['a','b'].forEach((name,index)=>{
  const values=rows.map(r=>r[name][key]),mean=values.reduce((x,y)=>x+y,0)/values.length;
  values.forEach((v,j)=>{const jitter=((j*37)%23)-11,c=el('circle',{cx:xs[index]+jitter,cy:py(v),r:4,fill:colors[index],opacity:.55,class:'dot'});const tip=el('title');tip.textContent=`seed ${rows[j].seed}: ${fmt(v)}`;c.append(tip);svg.append(c)});
  svg.append(el('line',{x1:xs[index]-25,y1:py(mean),x2:xs[index]+25,y2:py(mean),stroke:colors[index],'stroke-width':3}));
  const t=el('text',{x:xs[index],y:py(mean)-8,'text-anchor':'middle',fill:colors[index],'font-size':13,'font-weight':700});t.textContent=fmt(mean);svg.append(t);
  const label=el('text',{x:xs[index],y:H-18,'text-anchor':'middle',class:'label'});label.textContent=data.labels[index];svg.append(label)
 });
}
renderMetric('emb','daily_embarrassment','Embarrassment / person / day');
renderMetric('spend','annual_spend','Household spend divided by people');
renderMetric('sockless','annual_sockless','Sockless days / person / 360 days');
const keys=[['daily_embarrassment','Embarrassment / person / day'],['annual_spend','Household spend / people / 360 days'],['annual_sockless','Sockless days / person / 360 days']];
const mean=(side,key)=>rows.reduce((v,r)=>v+r[side][key],0)/rows.length;
$('summary').innerHTML=keys.map(([key,name])=>`<tr><td>${name}</td><td>${fmt(mean('a',key))}</td><td>${fmt(mean('b',key))}</td><td>${fmt(mean('b',key)-mean('a',key))}</td></tr>`).join('');
"""


def write_action_html(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    params = report['parameters']
    chips = ''.join(f'<span class="chip">{name}: {value}</span>' for name, value in params.items())
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sock choices · counterfactuals</title><style>{STYLE}</style><main>
<h1>What does one sock choice change?</h1><p class="intro muted">Every legal action in a sampled hand is compared from the same paused state. Dots show mean outcomes from short randomized futures; greedy governs subsequent roommate choices.</p>
<div class="explain"><strong>How spending works:</strong> nobody pays a daily fee. A discarded or holed sock adds to that color's pending count. Six pending socks of one color trigger a $10 household six-pack if the budget allows it. The horizontal axis is additional household spending observed after today's choice during the look-ahead. An average can be between $10 increments because multiple randomized futures are combined.</div>
<div class="chips">{chips}</div><div class="grid"><section class="card"><h2>Spend versus embarrassment</h2><div class="controls"><label for="day">Sampled turn</label><select id="day"></select><span id="context" class="muted"></span></div><svg id="scatter" viewBox="0 0 680 410" role="img" aria-label="Scatter plot of action spending and embarrassment"></svg><div class="legend"><span><i class="swatch" style="background:#187a72"></i>0 discards</span><span><i class="swatch" style="background:#2b71ae"></i>1</span><span><i class="swatch" style="background:#b37b20"></i>2</span><span><i class="swatch" style="background:#b85659"></i>3+</span><span>Dark outline: greedy action</span></div></section>
<section class="card"><h2 id="picked">Selected action</h2><p id="picked-note" class="muted"></p><div id="stats" class="stats"></div><p class="note">Click a dot to inspect it. Lower is better. Embarrassment and spending are totals accumulated during the look-ahead.</p></section>
<section class="card full"><h2>Pareto actions</h2><p class="muted">No other tested action is better on all three future means: household embarrassment, household spending, and sockless days.</p><div class="scroll"><table><thead><tr><th>Action</th><th>Immediate score</th><th>Future embarrassment</th><th>Future household spend</th><th>Sockless</th></tr></thead><tbody id="frontier"></tbody></table></div></section>
<section class="card full"><h2>All actions, sorted by future embarrassment</h2><p class="muted">Change is relative to greedy for this same hand. Negative is better. ± values are Monte Carlo standard errors.</p><div class="scroll"><table><thead><tr><th>Action</th><th>Immediate score</th><th>Future embarrassment</th><th>Change</th><th>Future household spend</th><th>Change</th><th>Sockless</th></tr></thead><tbody id="actions"></tbody></table></div></section></div>
<p class="note">Oracle drawer and pending-discard diagnostics are for analysis only. Error estimates and held-out seeds are needed before turning an apparent pattern into a player rule.</p></main><script>{ACTION_SCRIPT.replace('__DATA__', json.dumps(report).replace('</', '<\\/'))}</script></html>'''
    path.write_text(html, encoding='utf-8')
    return path


def write_comparison_html(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    p = report['parameters']
    chips = ''.join(f'<span class="chip">{name}: {value}</span>' for name, value in p.items())
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sock experiment · {report['kind']}</title><style>{STYLE}</style><main>
<h1>{report['title']}</h1><p class="intro muted">Each dot is one seed; horizontal strokes show across-seed means. Embarrassment is per person per day. Spending and sockless days are scaled to 360 days.</p>
<div class="explain"><strong>Spending is an outcome:</strong> there is no daily or per-person fee. The simulator spends $10 only when six discarded or holed socks of one color trigger a household replacement pack. To compare household sizes, this chart divides the resulting household total by roommate count. That normalization does not change how the budget is charged.</div>
<div class="chips">{chips}</div><div class="metric-grid"><section class="card"><h2>Embarrassment per person per day</h2><svg id="emb" viewBox="0 0 340 320" role="img" aria-label="Embarrassment comparison"></svg></section><section class="card"><h2>Household spend / people / 360 days</h2><svg id="spend" viewBox="0 0 340 320" role="img" aria-label="Spending comparison"></svg></section><section class="card"><h2>Sockless days per person / 360 days</h2><svg id="sockless" viewBox="0 0 340 320" role="img" aria-label="Sockless comparison"></svg></section></div>
<section class="card"><h2>Across-seed means</h2><table><thead><tr><th>Metric</th><th>{report['labels'][0]}</th><th>{report['labels'][1]}</th><th>Second minus first</th></tr></thead><tbody id="summary"></tbody></table></section>
<p class="note">{report['note']}</p></main><script>{COMPARE_SCRIPT.replace('__DATA__', json.dumps(report).replace('</', '<\\/'))}</script></html>'''
    path.write_text(html, encoding='utf-8')
    return path


def write_index_html(path: str | Path = 'results/index.html') -> Path:
    """Write a small hub so daily decisions are not confused with comparisons."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sock simulation dashboards</title><style>{STYLE}
.links{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}.link{{display:block;text-decoration:none;color:inherit;background:white;border:1px solid #dbe4e7;border-radius:16px;padding:24px;box-shadow:0 3px 14px #243b4a0b}}.link:hover{{border-color:#187a72;transform:translateY(-1px)}}.link strong{{display:block;font-size:19px;margin-bottom:8px}}.tag{{display:inline-block;font-size:12px;font-weight:700;color:#187a72;background:#e7f4f2;border-radius:999px;padding:5px 9px;margin-bottom:14px}}@media(max-width:700px){{.links{{grid-template-columns:1fr}}}}
</style><main><h1>Sock simulation dashboards</h1><p class="intro muted">Daily decision analysis and experiment-level comparisons answer different questions. Start with a daily dashboard to inspect the offered hand and legal choices.</p>
<div class="explain"><strong>Looking for everyday choices?</strong> Open the daily-choice dashboard. Use its day selector to inspect what was offered, which two socks could be worn, which leftovers could be discarded, and the estimated consequence of every legal action.</div>
<div class="links">
<a class="link" href="dataset_overview.html"><span class="tag">FULL DATASET</span><strong>1.63M-rollout overview</strong><span class="muted">Coverage, policies, budgets, roommate counts, and matched discard effects</span></a>
<a class="link" href="strategy_rules.html"><span class="tag">CONDITIONAL RULES</span><strong>Budget, stock, roommates, and adversarial choices</strong><span class="muted">Held-out evidence for defending against greedy, discard timing, and drawer distribution</span></a>
<a class="link" href="socks_choices.html"><span class="tag">DAILY CHOICES</span><strong>Four-sock decisions</strong><span class="muted">Every day · 24 legal actions per full hand · immediate and future outcomes</span></a>
<a class="link" href="socks_compare_pooling.html"><span class="tag">COMPARISON</span><strong>Pooled versus separate</strong><span class="muted">Matched socks and budget per person across multiple seeds</span></a>
</div></main></html>'''
    path.write_text(html, encoding='utf-8')
    return path
