#!/usr/bin/env python3
"""
Setup / control panel for the phone bot - the phone version of setup_panel.py.

    python panel.py            then open  http://localhost:8080  in the phone browser

Tabs: Positions | Detection | Control | Test      (same as the PC panel)
The top bar is the "overlay": live stats + Pause/Resume (replaces F6) + Stop.
Tip: put the browser and the game in split-screen / a floating window, or just
switch back and forth - the bot keeps running in Termux.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot

PORT = int(os.environ.get("PANEL_PORT", "8080"))

SNAP = {"img": None, "ready": False, "msg": ""}
CAP = {"key": None, "msg": ""}
TEST = {"det": "-", "click": "-", "wall": "-"}
LOCK = threading.Lock()


# --------------------------------------------------------------------------
#  State
# --------------------------------------------------------------------------

def state():
    cfg = bot.cfg
    groups = []
    for g, keys in bot.GROUPS:
        groups.append([g, [[k, bot.LABELS[k], cfg["positions"].get(k)] for k in keys]])
    templates = [[k, v[0], bot.has_template(k)] for k, v in bot.TEMPLATES.items()]
    return {
        "groups": groups,
        "templates": templates,
        "settings": cfg["settings"],
        "serial": cfg["adb_serial"],
        "stats": bot.STATS,
        "paused": bot.PAUSE.is_set(),
        "logs": list(bot.LOGS)[-120:],
        "cap": CAP,
        "snap": {"ready": SNAP["ready"], "msg": SNAP["msg"]},
        "test": TEST,
    }


def bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


# --------------------------------------------------------------------------
#  Actions
# --------------------------------------------------------------------------

def r_settings(d):
    s = bot.cfg["settings"]
    try:
        s["attacks"] = max(1, int(d.get("attacks", s["attacks"])))
        s["troop_count"] = max(4, int(d.get("troop_count", s["troop_count"])))
        s["spell_count"] = max(1, int(d.get("spell_count", s["spell_count"])))
        s["wall_every"] = max(1, int(d.get("wall_every", s["wall_every"])))
        s["wall_enabled"] = bool(d.get("wall_enabled", s["wall_enabled"]))
    except (TypeError, ValueError):
        return {"ok": False, "error": "numbers only"}
    if d.get("serial"):
        bot.cfg["adb_serial"] = str(d["serial"]).strip()
    bot.save()
    return {"ok": True}


def _setpos_job(key):
    CAP.update(key=key, msg=f'Tap "{bot.LABELS[key]}" in the game now...')
    try:
        info = bot.touch_info()
        x, y = bot.get_tap(info)
        bot.cfg["positions"][key] = [x, y]
        bot.save()
        CAP.update(key=None, msg=f"OK {key} = ({x}, {y})")
    except bot.BotError as e:
        CAP.update(key=None, msg=f"X {e}")
    except Exception as e:
        CAP.update(key=None, msg=f"X error: {e}")


def r_setpos(d):
    key = d.get("key")
    if key not in bot.LABELS:
        return {"ok": False, "error": "unknown key"}
    if "x" in d and "y" in d:
        bot.cfg["positions"][key] = [int(d["x"]), int(d["y"])]
        bot.save()
        CAP.update(key=None, msg=f"OK {key} = ({int(d['x'])}, {int(d['y'])})")
        return {"ok": True}
    if CAP["key"]:
        return {"ok": False, "error": "already waiting for a tap"}
    bg(_setpos_job, key)
    return {"ok": True}


def _snap_job(delay):
    SNAP.update(ready=False)
    for i in range(delay, 0, -1):
        SNAP["msg"] = f"Switch to the game now - screenshot in {i}s..."
        time.sleep(1)
    try:
        SNAP["img"] = bot.shot()
        SNAP.update(ready=True, msg="Screenshot taken - drag a box around the item")
    except bot.BotError as e:
        SNAP.update(img=None, ready=False, msg=f"X {e}")


def r_snap(d):
    bg(_snap_job, max(0, min(30, int(d.get("delay", 8)))))
    return {"ok": True}


def r_capture(d):
    name = d.get("name")
    if name not in bot.TEMPLATES or SNAP["img"] is None:
        return {"ok": False, "error": "take a screenshot first"}
    x1, y1, x2, y2 = (int(d[k]) for k in ("x1", "y1", "x2", "y2"))
    w, h = SNAP["img"].size
    x1, x2 = sorted((max(0, x1), min(w, x2)))
    y1, y2 = sorted((max(0, y1), min(h, y2)))
    if x2 - x1 < 5 or y2 - y1 < 5:
        return {"ok": False, "error": "box too small"}
    os.makedirs(bot.IMG_DIR, exist_ok=True)
    SNAP["img"].crop((x1, y1, x2, y2)).save(f"{bot.IMG_DIR}/{name}.png")
    SNAP.update(ready=False, msg=f"OK captured {name}.png")
    return {"ok": True}


def _delayed(slot, delay, fn):
    for i in range(delay, 0, -1):
        TEST[slot] = f"Switch to the game - scanning in {i}s..."
        time.sleep(1)
    try:
        TEST[slot] = fn()
    except bot.BotError as e:
        TEST[slot] = f"X {e}"
    except Exception as e:
        TEST[slot] = f"X error: {e}"


def _match_msg(key):
    p, conf, thr = bot.best_match(key)
    if p:
        return f"OK {key}: FOUND at ({p[0]}, {p[1]}) - {conf * 100:.1f}% confidence"
    return f"X {key}: NOT FOUND - best match {conf * 100:.1f}% (needs {thr * 100:.0f}%)"


def r_test_template(d):
    key = d.get("key")
    if key not in bot.TEMPLATES:
        return {"ok": False, "error": "unknown template"}
    bg(_delayed, "det", 5, lambda: _match_msg(key))
    return {"ok": True}


def r_test_wall(d):
    bg(_delayed, "wall", 5, lambda: _match_msg("wall_text"))
    return {"ok": True}


def r_test_click(d):
    key = d.get("key")
    p = bot.pos(key) if key in bot.LABELS else None
    if not p:
        TEST["click"] = f"X Position '{key}' is not set"
        return {"ok": True}

    def go():
        bot.tap(p, jitter=0)
        return f"OK Tapped {key} at ({p[0]}, {p[1]})"
    bg(_delayed, "click", 3, go)
    return {"ok": True}


def _bot_job():
    bot.log("Bot started")
    bot.log(f"Target: {bot.cfg['settings']['attacks']} attacks")
    if bot.cfg["settings"]["wall_enabled"]:
        bot.log(f"Wall upgrade every {bot.cfg['settings']['wall_every']} attacks")
    bot.log("Switch to the game - starting in 5s")
    try:
        bot.sleep(5)
        bot.run_attacks()
    except bot.Stopped:
        bot.STATS["running"] = False
        bot.log("Bot stopped.")
    except Exception as e:
        bot.STATS["running"] = False
        bot.log(f"Bot error: {e}")


def r_start(d):
    if bot.STATS["running"]:
        return {"ok": False, "error": "already running"}
    m = bot.missing_required()
    if m:
        return {"ok": False, "error": "Set these positions first: " + ", ".join(m)}
    bot.start_flags()
    bg(_bot_job)
    return {"ok": True}


def r_stop(d):
    if bot.STATS["running"]:
        bot.STOP.set()
        bot.PAUSE.clear()
        bot.log("Stopping bot...")
    return {"ok": True}


def r_pause(d):
    if not bot.STATS["running"]:
        return {"ok": True}
    if bot.PAUSE.is_set():
        bot.PAUSE.clear()
        bot.log("Resumed")
    else:
        bot.PAUSE.set()
        bot.log("Paused")
    return {"ok": True}


def r_reset(d):
    bot.cfg["positions"] = {}
    for k in bot.TEMPLATES:
        try:
            os.remove(f"{bot.IMG_DIR}/{k}.png")
        except OSError:
            pass
    bot.save()
    return {"ok": True}


ROUTES = {
    "/api/settings": r_settings, "/api/setpos": r_setpos, "/api/snap": r_snap,
    "/api/capture": r_capture, "/api/test_template": r_test_template,
    "/api/test_wall": r_test_wall, "/api/test_click": r_test_click,
    "/api/start": r_start, "/api/stop": r_stop, "/api/pause": r_pause,
    "/api/reset": r_reset,
}


# --------------------------------------------------------------------------
#  HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        p = self.path.split("?")[0]
        if p == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, state())
        if p == "/api/snap.png":
            if SNAP["img"] is None:
                return self._send(404, {"error": "no screenshot"})
            buf = bot.BytesIO()
            SNAP["img"].save(buf, "PNG")
            return self._send(200, buf.getvalue(), "image/png")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "json only"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        fn = ROUTES.get(self.path.split("?")[0])
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            with LOCK:
                res = fn(data)
        except bot.BotError as e:
            res = {"ok": False, "error": str(e)}
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._send(200, res)


def serve(port=PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv


def main():
    os.makedirs(bot.IMG_DIR, exist_ok=True)
    try:
        bot.load()
    except bot.BotError:
        with open(bot.CFG_FILE, "w") as f:
            json.dump(bot.DEFAULT, f, indent=2)
        bot.load()
        print("config.json created")
    srv = serve()
    print(f"Panel running -> open http://localhost:{PORT} in your browser (Ctrl+C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bot.STOP.set()
        print("bye")


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CoC Bot - Setup Panel</title>
<style>
:root{--bg:#14161c;--card:#1d2029;--line:#2c3040;--tx:#e6e8ee;--mut:#8b90a0;--ok:#35d07f;--bad:#ff5d5d;--acc:#4c8dff;--warn:#ffc94d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding-bottom:40px}
#bar{position:sticky;top:0;z-index:5;background:#10131a;border-bottom:1px solid var(--line);padding:8px 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#stats{font:700 13px ui-monospace,monospace;color:var(--ok);flex:1;min-width:180px}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 12px;font-size:14px}
button.g{background:#2a2f3f}button.r{background:#c93a3a}button.y{background:#b58a14}button:disabled{opacity:.4}
#tabs{display:flex;gap:4px;padding:8px 8px 0}#tabs button{flex:1;background:#232736;border-radius:8px 8px 0 0}#tabs button.on{background:var(--acc)}
.pane{display:none;padding:10px}.pane.on{display:block}
fieldset{border:1px solid var(--line);border-radius:10px;margin:0 0 10px;padding:8px 10px;background:var(--card)}
legend{color:var(--mut);padding:0 6px}
.row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #252a38}.row:last-child{border:0}
.row .n{flex:1}.row .v{width:96px;text-align:center;font-size:13px}.ok{color:var(--ok)}.no{color:var(--bad)}
.row button{padding:6px 10px}input[type=number],input[type=text]{background:#10131a;border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:7px;width:90px}
input[type=text]{width:100%}label{display:flex;align-items:center;gap:8px;margin:7px 0}label span{flex:1}
#log{background:#0d0f14;border-radius:8px;padding:8px;height:230px;overflow:auto;font:12px ui-monospace,monospace;white-space:pre-wrap;color:#c8ccd8}
.res{margin-top:8px;color:var(--mut);word-break:break-word}
#status{padding:6px 12px;color:var(--warn);min-height:26px}
#ov{display:none;position:fixed;inset:0;background:#000d;z-index:20;flex-direction:column}
#ov.on{display:flex}#ovh{padding:8px;display:flex;gap:8px;align-items:center}#ovh span{flex:1;color:var(--warn)}
#ovb{flex:1;overflow:auto}#wrap{position:relative;display:inline-block;touch-action:none;line-height:0}
#wrap img{width:100%;display:block;-webkit-user-drag:none}#box{position:absolute;border:2px dashed #ff3b3b;background:#ff3b3b22;pointer-events:none}
</style></head><body>
<div id="bar"><div id="stats">Attacks: 0/0 | 50%: 0 | Left: 0</div>
<button id="bpause" class="y" onclick="act('pause')" disabled>Pause</button>
<button id="bstop" class="r" onclick="act('stop')" disabled>Stop</button></div>
<div id="status"></div>
<div id="tabs"></div>

<div class="pane" id="p0"><div id="pos"></div></div>

<div class="pane" id="p1"><div id="tpl"></div>
<div class="res">Tap a capture button, then switch to the game within a few seconds. Come back here and drag a box around the item.</div></div>

<div class="pane" id="p2">
<fieldset><legend>Attack Settings</legend>
<label><span>Number of Attacks</span><input id="s_attacks" type="number" min="1"></label>
<label><span>Troop Count (min 4)</span><input id="s_troop_count" type="number" min="4"></label>
<label><span>Spell Count</span><input id="s_spell_count" type="number" min="1"></label>
<label><span>Enable Wall Upgrade</span><input id="s_wall_enabled" type="checkbox"></label>
<label><span>Check Walls Every (attacks)</span><input id="s_wall_every" type="number" min="1"></label>
<label><span>ADB address</span><input id="s_serial" type="text" style="width:150px"></label>
<button class="g" onclick="saveSettings()">Save settings</button></fieldset>
<div style="display:flex;gap:8px;margin-bottom:10px"><button id="bstart" onclick="startBot()">Start</button>
<button class="g" onclick="if(confirm('Clear ALL positions and templates?'))act('reset')">Reset All</button></div>
<fieldset><legend>Log</legend><div id="log"></div></fieldset></div>

<div class="pane" id="p3">
<fieldset><legend>Test Image Detection</legend>
<label><span><select id="t_tpl"></select></span><button onclick="runTest('test_template',{key:$('#t_tpl').value})">Run Test</button></label>
<div class="res" id="r_det">-</div></fieldset>
<fieldset><legend>Test Position Click</legend>
<label><span><select id="t_pos"></select></span><button onclick="runTest('test_click',{key:$('#t_pos').value})">Click Position</button></label>
<div class="res" id="r_click">-</div></fieldset>
<fieldset><legend>Test Wall Detection</legend>
<button onclick="runTest('test_wall',{})">Find "Wall" on Screen</button><div class="res" id="r_wall">-</div></fieldset>
<div class="res">Tests wait a few seconds so you can switch to the game first.</div></div>

<div id="ov"><div id="ovh"><span id="ovmsg"></span><button class="g" onclick="closeOv()">Cancel</button></div>
<div id="ovb"><div id="wrap"><img id="snapimg"><div id="box" style="display:none"></div></div></div></div>

<script>
const $=s=>document.querySelector(s);let S=null,last={},capName=null,ovOpen=false,inited=false;
async function api(p,b){try{const r=await fetch('/api/'+p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});return await r.json()}catch(e){return{ok:false,error:'panel not reachable'}}}
const TABS=['Positions','Detection','Control','Test'];
TABS.forEach((t,i)=>{const b=document.createElement('button');b.textContent=t;b.onclick=()=>tab(i);b.id='tb'+i;$('#tabs').appendChild(b)});
function tab(i){TABS.forEach((_,j)=>{$('#p'+j).classList.toggle('on',i==j);$('#tb'+j).classList.toggle('on',i==j)});localStorage.tab=i}
tab(+localStorage.tab||0);
function status(m){$('#status').textContent=m||''}
async function act(p){const r=await api(p,{});if(r.error)status('X '+r.error)}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function renderPos(){let h='';S.groups.forEach(([g,rows])=>{h+='<fieldset><legend>'+esc(g)+'</legend>';rows.forEach(([k,l,p])=>{
const set=p&&p.length==2&&(p[0]||p[1]);h+='<div class="row"><div class="n">'+esc(l)+'</div><div class="v '+(set?'ok':'no')+'">'+(set?'('+p[0]+', '+p[1]+')':'not set')+'</div>'+
'<button onclick="setPos(\''+k+'\')">Set</button><button class="g" onclick="manual(\''+k+'\')">&#9998;</button></div>'});h+='</fieldset>'});$('#pos').innerHTML=h}
async function setPos(k){const r=await api('setpos',{key:k});if(r.error)status('X '+r.error)}
async function manual(k){const v=prompt('x y (example: 540 300)');if(!v)return;const m=v.match(/(\d+)\D+(\d+)/);if(!m)return status('X use: x y');await api('setpos',{key:k,x:+m[1],y:+m[2]})}
function renderTpl(){let h='<fieldset><legend>Templates</legend>';S.templates.forEach(([k,l,ok])=>{h+='<div class="row"><div class="n">'+esc(l)+'</div><div class="v '+(ok?'ok':'no')+'">'+(ok?'captured':'not set')+'</div>'+
'<button onclick="snap(\''+k+'\')">&#128247;</button><button class="g" onclick="runTest(\'test_template\',{key:\''+k+'\'});tab(3)">Test</button></div>'});$('#tpl').innerHTML=h+'</fieldset>'}
async function snap(k){capName=k;await api('snap',{delay:8})}
function openOv(){if(ovOpen)return;ovOpen=true;$('#snapimg').src='/api/snap.png?'+Date.now();$('#ovmsg').textContent='Drag a box around the item';$('#box').style.display='none';$('#ov').classList.add('on')}
function closeOv(){ovOpen=false;capName=null;$('#ov').classList.remove('on')}
(function(){const w=$('#wrap');let sx,sy,drag=false;
function pt(e){const r=$('#snapimg').getBoundingClientRect();return[Math.max(0,Math.min(r.width,e.clientX-r.left)),Math.max(0,Math.min(r.height,e.clientY-r.top))]}
w.addEventListener('pointerdown',e=>{[sx,sy]=pt(e);drag=true;w.setPointerCapture(e.pointerId)});
w.addEventListener('pointermove',e=>{if(!drag)return;const[x,y]=pt(e),b=$('#box');b.style.display='block';b.style.left=Math.min(sx,x)+'px';b.style.top=Math.min(sy,y)+'px';b.style.width=Math.abs(x-sx)+'px';b.style.height=Math.abs(y-sy)+'px'});
w.addEventListener('pointerup',async e=>{if(!drag)return;drag=false;const[x,y]=pt(e),im=$('#snapimg'),k=im.naturalWidth/im.clientWidth;
if(Math.abs(x-sx)<8||Math.abs(y-sy)<8)return;
const r=await api('capture',{name:capName,x1:Math.round(Math.min(sx,x)*k),y1:Math.round(Math.min(sy,y)*k),x2:Math.round(Math.max(sx,x)*k),y2:Math.round(Math.max(sy,y)*k)});
if(r.ok){closeOv()}else status('X '+r.error)})})();
async function saveSettings(){const r=await api('settings',{attacks:$('#s_attacks').value,troop_count:$('#s_troop_count').value,spell_count:$('#s_spell_count').value,wall_enabled:$('#s_wall_enabled').checked,wall_every:$('#s_wall_every').value,serial:$('#s_serial').value});status(r.ok?'OK settings saved':'X '+r.error)}
async function startBot(){await saveSettings();const r=await api('start',{});if(r.error)status('X '+r.error);else status('Bot starting - switch to the game')}
async function runTest(p,b){const r=await api(p,b);if(r.error)status('X '+r.error)}
function fillSettings(){const s=S.settings;$('#s_attacks').value=s.attacks;$('#s_troop_count').value=s.troop_count;$('#s_spell_count').value=s.spell_count;$('#s_wall_enabled').checked=s.wall_enabled;$('#s_wall_every').value=s.wall_every;$('#s_serial').value=S.serial;
$('#t_tpl').innerHTML=S.templates.map(t=>'<option value="'+t[0]+'">'+esc(t[1])+'</option>').join('');
$('#t_pos').innerHTML=S.groups.flatMap(g=>g[1]).map(r=>'<option value="'+r[0]+'">'+esc(r[0])+'</option>').join('')}
async function tick(){const s=await api('state');if(!s||!s.groups){status('X panel not reachable');return}S=s;
if(!inited){fillSettings();inited=true}
const j1=JSON.stringify(s.groups),j2=JSON.stringify(s.templates);if(j1!=last.j1){renderPos();last.j1=j1}if(j2!=last.j2){renderTpl();last.j2=j2}
const st=s.stats;$('#stats').textContent='Attacks: '+st.done+'/'+st.total+' | 50%: '+st.ok+' | Left: '+Math.max(0,st.total-st.done)+(s.paused?'  PAUSED':'');
$('#bpause').disabled=!st.running;$('#bpause').textContent=s.paused?'Resume':'Pause';$('#bstop').disabled=!st.running;$('#bstart').disabled=st.running;
const lg=$('#log'),atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;lg.textContent=s.logs.join('\n');if(atEnd)lg.scrollTop=lg.scrollHeight;
if(s.cap.key)status(s.cap.msg);else if(s.cap.msg&&!ovOpen&&!s.snap.msg)status(s.cap.msg);
if(capName&&!s.snap.ready&&s.snap.msg)status(s.snap.msg);
if(capName&&s.snap.ready&&!ovOpen){openOv();status('')}
$('#r_det').textContent=s.test.det;$('#r_click').textContent=s.test.click;$('#r_wall').textContent=s.test.wall}
setInterval(tick,1000);tick();
</script></body></html>
"""

if __name__ == "__main__":
    main()
