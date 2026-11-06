#!/usr/bin/env python3
"""
Builds a password-protected dashboard page (site/index.html) from the saved,
encrypted history. The page holds only encrypted data; it is unlocked in the
viewer's browser with the DASH_PASSWORD secret (PBKDF2-SHA256 + AES-GCM).

Usage:
  python dashboard.py            # writes site/index.html
"""
import base64
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from common import load_config, load_state

TZ = ZoneInfo("America/New_York")
ITERATIONS = 250_000
OUT_DIR = "site"


def encrypt(data: bytes, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, data, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"salt": b64(salt), "iv": b64(iv), "ct": b64(ct), "iter": ITERATIONS}


def cat_name(c: str) -> str:
    return "Police" if c in ("Police/Traffic", "Police") else (c or "Other")


def build_data() -> dict:
    cfg = load_config()
    brief = cfg.get("brief", {})
    hist = load_state("history.json", {"days": {}})
    disp = load_state("dispatches.json", {"incidents": {}}).get("incidents", {})
    headings = {
        "spotlight": "⭐ Spotlight", "flag": "🚩 Flagged", "meetings": "🏛️ Meetings",
        "prep": "🧭 Preparedness Watch", "recalls": "⚠️ Serious Recalls", "newsletters": "🗞️ Newsletters",
        "police": "🚓 Police & Emergency", "local": "📰 Local News", "county": "🗺️ Region",
    }
    headings.update({k: v for k, v in brief.get("headings", {}).items() if k in headings})
    return {
        "updated": datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p"),
        "title": brief.get("subtitle", ""),
        "headings": headings,
        "order": ["flag", "local", "county", "meetings", "newsletters", "prep", "recalls", "police"],
        "days": hist.get("days", {}),
        "latest": hist.get("latest", {}),
        "dispatch": sorted(({**c, "category": cat_name(c.get("category", ""))} for c in disp.values()),
                           key=lambda c: c.get("time", ""), reverse=True),
        "hidden_911": [c for c in ["Police", "EMS", "Fire", "Other"]
                       if c not in {cat_name(x) for x in cfg.get("dispatch", {}).get("show_categories", ["Police", "EMS", "Fire", "Other"])}],
    }


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Daily Brief</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d1f23;--mute:#6b7280;--line:#e5e7eb;--accent:#0b57d0;--flag:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--card:#1c1e22;--ink:#e8eaed;--mute:#9aa0a6;--line:#2c2f34;--accent:#8ab4f8;--flag:#f28b82}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}.sub{color:var(--mute);font-size:12px;margin:2px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:10px 0}
summary{cursor:pointer;padding:12px 14px;font-weight:600;list-style:none;display:flex;justify-content:space-between;align-items:center}
summary::-webkit-details-marker{display:none}summary:after{content:"▸";color:var(--mute);transition:transform .15s}
details[open]>summary:after{transform:rotate(90deg)}
.count{color:var(--mute);font-weight:400;font-size:13px;margin-left:6px}
.body{padding:0 14px 10px}.sub details{margin:6px 0;border-radius:8px}.sub summary{padding:8px 10px;font-size:14px}
.item{padding:7px 0;border-top:1px solid var(--line)}.item:first-child{border-top:0}
.item a{color:var(--accent);text-decoration:none}.meta{color:var(--mute);font-size:12px}
.flag{color:var(--flag);font-size:12px;font-weight:600}.snip{font-size:13px;color:var(--mute)}
.bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}
.bar button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:16px;padding:6px 12px;font-size:13px}
.bar button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
input[type=search],input[type=password]{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:15px}
.wx{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.wx div{font-size:13px}.wx b{display:block;font-size:14px}
.big{font-size:18px}.empty{color:var(--mute);font-size:13px;padding:4px 0}
#lock{max-width:360px;margin:15vh auto;text-align:center}#lock button{margin-top:10px;padding:10px 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;font-size:15px}
#err{color:var(--flag);font-size:13px;min-height:18px;margin-top:8px}
</style></head><body>
<div id="lock" class="wrap"><h1>Daily Brief</h1><p class="sub">Enter password</p>
<input id="pw" type="password" autocomplete="current-password">
<label class="sub" style="display:block;margin-top:8px"><input id="rem" type="checkbox" checked> Remember on this device</label>
<button id="go">Unlock</button><div id="err"></div></div>
<div id="app" class="wrap" hidden></div>
<script>
const ENC=__ENC__;
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function unlock(pw){
  const k0=await crypto.subtle.importKey("raw",new TextEncoder().encode(pw),"PBKDF2",false,["deriveKey"]);
  const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b64(ENC.salt),iterations:ENC.iter,hash:"SHA-256"},k0,{name:"AES-GCM",length:256},false,["decrypt"]);
  const pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:b64(ENC.iv)},key,b64(ENC.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
const store={get(){try{return localStorage.getItem("db_pw")}catch(e){return null}},set(v){try{localStorage.setItem("db_pw",v)}catch(e){}},del(){try{localStorage.removeItem("db_pw")}catch(e){}}};
async function tryPw(pw,remember){
  try{const D=await unlock(pw);if(remember)store.set(pw);document.getElementById("lock").hidden=true;render(D);}
  catch(e){store.del();document.getElementById("err").textContent="Wrong password";}
}
document.getElementById("go").onclick=()=>tryPw(document.getElementById("pw").value,document.getElementById("rem").checked);
document.getElementById("pw").onkeydown=e=>{if(e.key==="Enter")document.getElementById("go").click()};
const saved=store.get();if(saved)tryPw(saved,true);

const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let D,range=7,q="",openKey=decodeURIComponent((location.hash||"").slice(1));
function render(data){D=data;const app=document.getElementById("app");app.hidden=false;draw();}
function daysBack(n){const out=[];const t=new Date();for(let i=0;i<n;i++){const d=new Date(t);d.setDate(t.getDate()-i);out.push(d.toISOString().slice(0,10))}return out}
function localDay(d){const z=new Date(d.getTime()-d.getTimezoneOffset()*6e4);return z.toISOString().slice(0,10)}
function draw(){
  const app=document.getElementById("app");
  const want=new Set((()=>{const t=new Date(),o=[];for(let i=0;i<range;i++){const d=new Date(t);d.setDate(t.getDate()-i);o.push(localDay(d))}return o})());
  const match=x=>!q||(x.title+" "+(x.source||"")+" "+(x.snippet||"")).toLowerCase().includes(q);
  let h=`<h1>Daily Brief</h1><div class="sub">${esc(D.title)} · updated ${esc(D.updated)}</div>
  <div class="bar">${[[1,"Today"],[7,"7 days"],[30,"30 days"]].map(([n,l])=>`<button data-r="${n}" class="${range===n?"on":""}">${l}</button>`).join("")}</div>
  <input type="search" id="q" placeholder="Search everything…" value="${esc(q)}">`;
  const section=key=>{
    const rows=[];for(const [day,items] of Object.entries(D.days||{})){if(!want.has(day))continue;for(const it of items){if(it.section===key&&match(it))rows.push({...it,day})}}
    rows.sort((a,b)=>b.day.localeCompare(a.day));
    if(!rows.length)return "";
    return `<details id="${key}" ${key==="flag"||key==="spotlight"||q||openKey===key?"open":""}><summary>${esc(D.headings[key]||key)}<span class="count">${rows.length}</span></summary><div class="body">`+
      rows.map(it=>`<div class="item">${it.flags&&it.flags.length?`<div class="flag">🚩 ${esc(it.flags.join(", "))}</div>`:""}<a href="${esc(it.url)}">${esc(it.title)}</a><div class="meta">${esc(it.source)} · ${esc(it.when||it.day)}</div>${it.snippet&&it.flags&&it.flags.length?`<div class="snip">${esc(it.snippet)}</div>`:""}</div>`).join("")+
      `</div></details>`};
  // 1. Spotlight always first
  const sp=section("spotlight");
  h+=sp||`<div class="card" id="spotlight"><b>${esc(D.headings.spotlight||"Spotlight")}</b><div class="empty">Nothing new in this range.</div></div>`;
  // 2. Key points, 3. Weather
  const L=D.latest||{};
  if(L.key_points&&L.key_points.length&&!q){h+=`<div class="card"><b>Key points</b> <span class="meta">(${esc(L.date)})</span>`+
    L.key_points.map(([ic,t,u])=>`<div class="item">${ic} ${u?`<a href="${esc(u)}">${esc(t)}</a>`:esc(t)}</div>`).join("")+`</div>`}
  const W=L.weather;
  if(W&&W.days&&W.days.length&&!q){h+=`<div class="card" id="weather"><b>🌤️ Weather${W.label?" — "+esc(W.label):""}</b>`+
    (W.alerts||[]).map(a=>`<div class="flag">⚠️ ${esc(a.event)}</div>`).join("")+
    `<div class="wx" style="margin-top:6px">`+W.days.map(d=>{const m=d.day||d.night;return `<div><b>${esc(d.name)}</b><span class="big">${d.day?d.day.temp+"°":"—"} / ${d.night?d.night.temp+"°":"—"}</span><br>${esc(m.text)}</div>`}).join("")+`</div></div>`}
  // 4. News → meetings/legal → preparedness → recalls → police
  for(const key of D.order)h+=section(key);
  // 5. 911 last — range-aware, tap-to-expand by category
  const cut=new Date(Date.now()-(range===1?24:range*24)*3600e3);
  const calls=(D.dispatch||[]).filter(c=>{const t=new Date(c.time.replace(" ","T"));return t>=cut&&!(D.hidden_911||[]).includes(c.category)&&(!q||(c.type+" "+c.location).toLowerCase().includes(q))});
  const cats=["Police","Fire","EMS","Other"],icon={"Police":"🚓","Fire":"🚒","EMS":"🚑","Other":"📟"};
  h+=`<details id="dispatch" ${(q&&calls.length)||openKey==="dispatch"?"open":""}><summary>🚨 911 calls ${range===1?"(last 24 hrs)":"(last "+range+" days)"}<span class="count">${calls.length}</span></summary><div class="body sub">`;
  for(const c of cats){const cs=calls.filter(x=>x.category===c);if(!cs.length)continue;
    h+=`<details><summary>${icon[c]} ${c}<span class="count">${cs.length}</span></summary><div class="body">`+
    cs.map(x=>`<div class="item"><span class="meta">${esc(x.time.slice(5,16))}</span> ${x.map?`<a href="${esc(x.map)}">`:""}${esc(x.type)} — ${esc(x.location)}${x.map?"</a>":""}${x.station?`<div class="meta">${esc(x.station)}</div>`:""}</div>`).join("")+`</div></details>`}
  if(!calls.length)h+=`<div class="empty">No calls in this range.</div>`;
  h+=`</div></details>`;
  h+=`<p class="sub" style="margin-top:18px"><a href="#" id="lockout">Lock this device</a></p>`;
  app.innerHTML=h;
  if(openKey){const el=document.getElementById(openKey);if(el)setTimeout(()=>el.scrollIntoView({block:"start"}),50);openKey=""}
  app.querySelectorAll("[data-r]").forEach(b=>b.onclick=()=>{range=+b.dataset.r;draw()});
  const qi=document.getElementById("q");qi.oninput=()=>{q=qi.value.trim().toLowerCase();const pos=qi.selectionStart;draw();const n=document.getElementById("q");n.focus();n.setSelectionRange(pos,pos)};
  document.getElementById("lockout").onclick=e=>{e.preventDefault();store.del();location.reload()};
}
</script></body></html>"""


def main():
    pw = os.environ.get("DASH_PASSWORD", "").strip()
    if not pw:
        sys.exit("Missing DASH_PASSWORD secret.")
    enc = encrypt(json.dumps(build_data(), ensure_ascii=False).encode(), pw)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE.replace("__ENC__", json.dumps(enc)))
    print("Dashboard built.")


if __name__ == "__main__":
    main()
