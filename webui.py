#!/usr/bin/env python3
"""
Local web UI for clipper.   python webui.py   ->  http://127.0.0.1:8765

Standard library only (plus clipper.py, ffmpeg and yt-dlp). Runs on your own
machine and only listens on 127.0.0.1.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import clipper

OUT_DIR = os.path.abspath("clips")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "clipper_uploads")
MEDIA = {}                      # media id -> file path (uploads / local files shown in the player)
JOBS = {}                       # job id -> state dict
JOB_LOCK = threading.Lock()     # one ffmpeg/yt-dlp job at a time keeps the machine responsive
PORT = 8765


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def new_job():
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "status": "queued", "log": [], "outputs": [], "ranges": [], "error": None}
    return JOBS[jid]


def run_job(job, fn):
    def work():
        with JOB_LOCK:
            job["status"] = "running"
            old = clipper.log
            clipper.log = lambda m: job["log"].append(str(m))
            try:
                fn(job)
                job["status"] = "done"
            except clipper.ClipperError as e:
                job["error"], job["status"] = str(e), "error"
            except Exception as e:                       # never let a thread die silently
                job["error"], job["status"] = f"{type(e).__name__}: {e}", "error"
            finally:
                clipper.log = old
    threading.Thread(target=work, daemon=True).start()


def make_args(opts):
    vertical = opts.get("vertical") or None
    if vertical not in (None, "crop", "blur"):
        raise clipper.ClipperError("bad vertical mode")
    quality = int(opts.get("quality") or 720)
    return SimpleNamespace(out=OUT_DIR, vertical=vertical, quality=quality,
                           crf=20, fast=bool(opts.get("fast")))


def resolve_source(src):
    src = (src or "").strip().strip('"')
    if not src:
        raise clipper.ClipperError("enter a YouTube link or a file path")
    if clipper.is_url(src):
        return src
    path = os.path.abspath(os.path.expanduser(src))
    if not os.path.isfile(path):
        raise clipper.ClipperError(f"file not found: {path}")
    return path


def media_id_for(path):
    for k, v in MEDIA.items():
        if v == path:
            return k
    k = uuid.uuid4().hex[:10]
    MEDIA[k] = path
    return k


YT_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})")


def api_load(body):
    src = resolve_source(body.get("source"))
    if clipper.is_url(src):
        info = clipper.youtube_info(src)
        m = YT_ID.search(src) or YT_ID.search(info.get("webpage_url", ""))
        return {"kind": "youtube", "source": src, "title": info.get("title", ""),
                "duration": info.get("duration") or 0, "videoId": m.group(1) if m else None,
                "hasHeatmap": bool(info.get("heatmap"))}
    clipper.need("ffprobe")
    title = os.path.basename(src)
    if os.path.dirname(src) == UPLOAD_DIR:
        title = re.sub(r"^[0-9a-f]{8}_", "", title)      # drop the random upload prefix
    return {"kind": "file", "source": src, "title": title,
            "duration": clipper.local_duration(src), "mediaUrl": f"/media/{media_id_for(src)}"}


def api_clip(body):
    src = resolve_source(body.get("source"))
    args = make_args(body)
    ranges = []
    for r in body.get("ranges", []):
        s, e = clipper.parse_time(str(r.get("start", ""))), clipper.parse_time(str(r.get("end", "")))
        if e <= s:
            raise clipper.ClipperError(f"clip '{r.get('label') or r.get('start')}': end must be after start")
        ranges.append((s, e, str(r.get("label", "")).strip()))
    if not ranges:
        raise clipper.ClipperError("add at least one clip")
    job = new_job()

    def fn(j):
        title = ""
        if clipper.is_url(src):
            title = clipper.youtube_info(src).get("title", "")
        j["log"].append(f"Cutting {len(ranges)} clip(s)...")
        for i, (s, e, label) in enumerate(ranges, 1):
            out = clipper.make_clip(src, s, e, label, i, title, args)
            j["outputs"].append({"name": os.path.basename(out), "url": "/clips/" + os.path.basename(out)})
    run_job(job, fn)
    return {"job": job["id"]}


def api_auto(body):
    src = resolve_source(body.get("source"))
    count = max(1, min(20, int(body.get("count") or 3)))
    length = max(3, min(600, int(body.get("length") or 30)))
    job = new_job()

    def fn(j):
        j["log"].append("Looking for highlights...")
        _, method, spans = clipper.find_highlights(src, length, count, 10, make_args({}))
        j["log"].append(f"Found {len(spans)} using {method}.")
        j["ranges"] = [{"start": s, "end": e, "label": f"highlight {i}"} for i, (s, e) in enumerate(spans, 1)]
    run_job(job, fn)
    return {"job": job["id"]}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "clipper"

    def log_message(self, *a):
        pass

    # --- safety: only answer requests that came from this page on this machine
    def _allowed(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, download=False):
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end, code = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            return self._send(200, job) if job else self._send(404, {"error": "no such job"})
        if path.startswith("/media/"):
            p = MEDIA.get(path.rsplit("/", 1)[1])
            return self._file(p) if p else self._send(404, {"error": "not found"})
        if path.startswith("/clips/"):
            name = os.path.basename(path)
            return self._file(os.path.join(OUT_DIR, name), download="dl=1" in self.path)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                return self._upload()
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            fn = {"/api/load": api_load, "/api/clip": api_clip, "/api/auto": api_auto}.get(path)
            if not fn:
                return self._send(404, {"error": "not found"})
            self._send(200, fn(body))
        except clipper.ClipperError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _upload(self):
        name = os.path.basename(unquote(self.headers.get("X-Filename") or "upload.mp4"))
        name = re.sub(r"[^\w\-. ]+", "_", name) or "upload.mp4"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, uuid.uuid4().hex[:8] + "_" + name)
        left = int(self.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        self._send(200, api_load({"source": dest}))


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clipper</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--text:#e8eaf0;--mute:#8b93a7;--acc:#6ee7b7;--acc2:#38bdf8;--bad:#f87171}
@media (prefers-color-scheme:light){:root{--bg:#f4f5f8;--card:#fff;--line:#dfe2ea;--text:#171a21;--mute:#5d667a;--acc:#059669;--acc2:#0284c7;--bad:#dc2626}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.card h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0 0 12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit;min-width:0}
input:focus,select:focus{outline:2px solid var(--acc2);outline-offset:0}
button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 13px;font:inherit;cursor:pointer}
button:hover{border-color:var(--acc2)}button:disabled{opacity:.5;cursor:default}
button.pri{background:var(--acc);color:#04130c;border-color:var(--acc);font-weight:600}
button.sm{padding:6px 9px;font-size:13px}
.grow{flex:1;min-width:200px}
.drop{border:2px dashed var(--line);border-radius:10px;padding:14px;text-align:center;color:var(--mute);margin-top:10px}
.drop.over{border-color:var(--acc);color:var(--text)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:860px){.grid{grid-template-columns:1fr}}
.player{background:#000;border-radius:10px;overflow:hidden;aspect-ratio:16/9;position:relative}
.player video,.player iframe,.player #yt{width:100%;height:100%;border:0;display:block}
.nov{display:flex;align-items:center;justify-content:center;height:100%;color:#777;font-size:14px;text-align:center;padding:12px}
.clip{display:grid;grid-template-columns:auto 72px 72px minmax(60px,1fr) auto;gap:6px;align-items:center;margin-bottom:8px}
.clip input{width:100%}.clip .n{color:var(--mute);width:18px;text-align:right}
.clip .acts{display:flex;gap:4px}
.msg{margin:8px 0 0;font-size:13px;color:var(--mute)}.err{color:var(--bad)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;margin:10px 0 0;max-height:150px;overflow:auto;font-size:12.5px;white-space:pre-wrap}
.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;margin-top:12px}
.res{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--bg)}
.res video{width:100%;display:block;background:#000;max-height:300px}
.res div{padding:8px 10px;font-size:12.5px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.res a{color:var(--acc2);text-decoration:none;white-space:nowrap}.res span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.title{margin:10px 0 0;font-weight:600}
label.k{color:var(--mute);font-size:13px}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;font-size:12px}
</style></head><body><main>
<h1>Clipper</h1><p class="sub">Local clipping tool. Everything runs on this computer.</p>

<div class="card"><h2>1 · Source</h2>
  <div class="row"><input id="src" class="grow" placeholder="YouTube link, or full path of a video file on this computer" autocomplete="off">
  <button class="pri" id="load">Load</button></div>
  <div class="drop" id="drop">…or drop a video file here / <a href="#" id="pick">choose a file</a><input type="file" id="file" accept="video/*,audio/*" hidden></div>
  <div class="msg" id="loadmsg"></div><div class="title" id="vtitle"></div>
</div>

<div class="grid">
  <div class="card"><h2>2 · Preview &amp; mark</h2>
    <div class="player" id="player"><div class="nov">Load a video to preview it here</div></div>
    <div class="row" style="margin-top:10px">
      <button id="markin" disabled>Mark start <kbd>I</kbd></button>
      <button id="markout" disabled>Mark end <kbd>O</kbd></button>
      <span class="msg" id="now" style="margin:0"></span>
    </div>
  </div>

  <div class="card"><h2>3 · Clips</h2>
    <div id="clips"></div>
    <div class="row"><button class="sm" id="addrow">+ Add clip</button><button class="sm" id="paste">Paste timestamps</button><button class="sm" id="clear">Clear</button></div>
    <div class="row" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">
      <button class="sm" id="auto" disabled>✨ Auto-find highlights</button>
      <label class="k">count <input id="acount" type="number" value="3" min="1" max="20" style="width:56px"></label>
      <label class="k">secs <input id="alen" type="number" value="30" min="3" max="600" style="width:64px"></label>
    </div>
    <div class="msg" id="automsg"></div>
  </div>
</div>

<div class="card"><h2>4 · Export</h2>
  <div class="row">
    <label class="k">Format <select id="vert"><option value="">Original</option><option value="crop">Vertical 9:16 (crop)</option><option value="blur">Vertical 9:16 (blur background)</option></select></label>
    <label class="k" id="qwrap">YouTube quality <select id="qual"><option>480</option><option selected>720</option><option>1080</option><option>1440</option><option>2160</option></select></label>
    <label class="k" id="fastwrap"><input type="checkbox" id="fast"> fast cut (instant, snaps to keyframes)</label>
    <span class="grow"></span><button class="pri" id="cut" disabled>Cut clips</button>
  </div>
  <div class="msg" id="cutmsg"></div><pre id="log" hidden></pre>
  <div class="results" id="results"></div>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
let S={kind:null,source:null,duration:0,yt:null,vid:null};
let rows=[];

const fmt=t=>{t=Math.max(0,t);const h=Math.floor(t/3600),m=Math.floor(t%3600/60);let s=t%60;
  s=(Math.abs(s-Math.round(s))<0.05)?String(Math.round(s)):s.toFixed(1);if(s.split('.')[0].length<2)s='0'+s;
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+s;};
const api=async(url,body)=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();if(!r.ok)throw new Error(j.error||'request failed');return j;};

/* ---------- rows ---------- */
function render(){
  const box=$('clips');box.innerHTML='';
  if(!rows.length){box.innerHTML='<div class="msg" style="margin:0 0 10px">No clips yet. Use Mark start / Mark end while the video plays, or add one by hand.</div>';}
  rows.forEach((r,i)=>{
    const d=document.createElement('div');d.className='clip';
    d.innerHTML='<span class="n">'+(i+1)+'</span><input placeholder="start" value="'+r.start+'"><input placeholder="end" value="'+r.end+'"><input placeholder="label" value="'+r.label.replace(/"/g,'&quot;')+'"><div class="acts"><button class="sm" title="Play from start">▶</button><button class="sm" title="Remove">✕</button></div>';
    const [a,b,c]=d.querySelectorAll('input');
    a.oninput=()=>r.start=a.value;b.oninput=()=>r.end=b.value;c.oninput=()=>r.label=c.value;
    const [play,del]=d.querySelectorAll('button');
    play.onclick=()=>seek(parseT(r.start));del.onclick=()=>{rows.splice(i,1);render();};
    box.appendChild(d);
  });
  $('cut').disabled=!(S.source&&rows.length);
}
function parseT(s){s=String(s).trim().toLowerCase();let m=s.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?$/);
  if(m&&/[hms]/.test(s))return (+m[1]||0)*3600+(+m[2]||0)*60+(+m[3]||0);
  const p=s.split(':').map(Number);if(!p.length||p.some(isNaN))return NaN;return p.reduce((t,x)=>t*60+x,0);}
const addRow=(start='',end='',label='')=>{rows.push({start:String(start),end:String(end),label});render();};
$('addrow').onclick=()=>addRow();
$('clear').onclick=()=>{rows=[];render();};
$('paste').onclick=()=>{
  const t=prompt('One clip per line, e.g.\n1:30-2:10 funny moment\n5:00-5:45');if(!t)return;
  let bad=0;t.split('\n').forEach(l=>{l=l.split('#')[0].trim();if(!l)return;
    const m=l.match(/^(\S+?)\s*(?:-|–|to|\s)\s*(\S+?)(?:\s+(.*))?$/);
    if(m&&!isNaN(parseT(m[1]))&&!isNaN(parseT(m[2])))rows.push({start:m[1],end:m[2],label:(m[3]||'').trim()});else bad++;});
  render();if(bad)alert(bad+' line(s) could not be read and were skipped.');};

/* ---------- player ---------- */
const now=()=>S.kind==='file'&&S.vid?S.vid.currentTime:(S.yt&&S.yt.getCurrentTime?S.yt.getCurrentTime():0);
function seek(t){if(isNaN(t))return;if(S.kind==='file'&&S.vid){S.vid.currentTime=t;S.vid.play();}else if(S.yt&&S.yt.seekTo){S.yt.seekTo(t,true);S.yt.playVideo();}}
function markIn(){if(!S.source)return;addRow(fmt(now()),'','');}
function markOut(){if(!S.source)return;if(!rows.length)return;
  let r=[...rows].reverse().find(x=>!x.end)||rows[rows.length-1];r.end=fmt(now());render();}
$('markin').onclick=markIn;$('markout').onclick=markOut;
document.addEventListener('keydown',e=>{if(/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName))return;
  if(e.key==='i'||e.key==='I')markIn();if(e.key==='o'||e.key==='O')markOut();});
setInterval(()=>{if(S.source&&!(document.hidden))$('now').textContent='at '+fmt(now());},300);

let ytReady=null;
function loadYT(){if(ytReady)return ytReady;ytReady=new Promise((res,rej)=>{
  window.onYouTubeIframeAPIReady=()=>res();const s=document.createElement('script');
  s.src='https://www.youtube.com/iframe_api';s.onerror=()=>rej();document.head.appendChild(s);
  setTimeout(()=>rej(),8000);});return ytReady;}

async function showPlayer(info){
  const p=$('player');p.innerHTML='';S.yt=null;S.vid=null;
  if(info.kind==='file'){
    const v=document.createElement('video');v.controls=true;v.src=info.mediaUrl;v.preload='metadata';p.appendChild(v);S.vid=v;
  }else if(info.videoId){
    p.innerHTML='<div id="yt"></div>';
    try{await loadYT();S.yt=new YT.Player('yt',{videoId:info.videoId,playerVars:{rel:0,modestbranding:1}});}
    catch(e){p.innerHTML='<div class="nov">Could not load the YouTube preview (offline?).<br>You can still type timestamps and cut clips.</div>';}
  }else p.innerHTML='<div class="nov">No preview for this link. You can still type timestamps.</div>';
}

/* ---------- load ---------- */
async function afterLoad(info){
  S.kind=info.kind;S.source=info.source;S.duration=info.duration;
  $('vtitle').textContent=(info.title||'')+(info.duration?'  ·  '+fmt(info.duration):'');
  $('loadmsg').textContent=info.kind==='youtube'&&info.hasHeatmap?'This video has a "Most replayed" graph, so auto-highlights will be quick.':'';
  $('markin').disabled=$('markout').disabled=$('auto').disabled=false;
  $('qwrap').style.opacity=info.kind==='youtube'?1:.4;$('fastwrap').style.opacity=info.kind==='youtube'?.4:1;
  await showPlayer(info);render();
}
async function doLoad(){
  const v=$('src').value.trim();if(!v)return;
  $('load').disabled=true;$('loadmsg').className='msg';$('loadmsg').textContent='Loading…';
  try{await afterLoad(await api('/api/load',{source:v}));}
  catch(e){$('loadmsg').className='msg err';$('loadmsg').textContent=e.message;}
  $('load').disabled=false;
}
$('load').onclick=doLoad;$('src').onkeydown=e=>{if(e.key==='Enter')doLoad();};
async function upload(f){
  $('loadmsg').className='msg';$('loadmsg').textContent='Uploading '+f.name+'… (stays on this computer)';
  try{const r=await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name)},body:f});
    const j=await r.json();if(!r.ok)throw new Error(j.error);$('src').value=j.source;await afterLoad(j);}
  catch(e){$('loadmsg').className='msg err';$('loadmsg').textContent=e.message;}
}
$('pick').onclick=e=>{e.preventDefault();$('file').click();};
$('file').onchange=e=>{if(e.target.files[0])upload(e.target.files[0]);};
['dragenter','dragover'].forEach(n=>$('drop').addEventListener(n,e=>{e.preventDefault();$('drop').classList.add('over');}));
['dragleave','drop'].forEach(n=>$('drop').addEventListener(n,e=>{e.preventDefault();$('drop').classList.remove('over');}));
$('drop').addEventListener('drop',e=>{if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0]);});

/* ---------- jobs ---------- */
async function poll(id,onTick){
  while(true){
    const j=await (await fetch('/api/jobs/'+id)).json();onTick(j);
    if(j.status==='done'||j.status==='error')return j;
    await new Promise(r=>setTimeout(r,800));
  }
}
$('auto').onclick=async()=>{
  $('auto').disabled=true;$('automsg').className='msg';$('automsg').textContent='Analysing…';
  try{
    const {job}=await api('/api/auto',{source:S.source,count:$('acount').value,length:$('alen').value});
    const j=await poll(job,j=>{if(j.log.length)$('automsg').textContent=j.log[j.log.length-1];});
    if(j.status==='error')throw new Error(j.error);
    j.ranges.forEach(r=>addRow(fmt(r.start),fmt(r.end),r.label));
    $('automsg').textContent=j.log[j.log.length-1]+' Added to the list. Adjust, then cut.';
  }catch(e){$('automsg').className='msg err';$('automsg').textContent=e.message;}
  $('auto').disabled=false;
};
$('cut').onclick=async()=>{
  const bad=rows.findIndex(r=>isNaN(parseT(r.start))||isNaN(parseT(r.end))||parseT(r.end)<=parseT(r.start));
  if(bad>=0){$('cutmsg').className='msg err';$('cutmsg').textContent='Clip '+(bad+1)+': check the start/end times (end must be after start).';return;}
  $('cut').disabled=true;$('results').innerHTML='';$('log').hidden=false;$('log').textContent='';
  $('cutmsg').className='msg';$('cutmsg').textContent='Working…';
  try{
    const {job}=await api('/api/clip',{source:S.source,ranges:rows,vertical:$('vert').value,quality:$('qual').value,fast:$('fast').checked});
    const j=await poll(job,j=>{$('log').textContent=j.log.join('\n');$('log').scrollTop=1e9;
      $('results').innerHTML=j.outputs.map(o=>'<div class="res"><video controls preload="metadata" src="'+o.url+'"></video><div><span title="'+o.name+'">'+o.name+'</span><a href="'+o.url+'?dl=1">Download</a></div></div>').join('');});
    if(j.status==='error')throw new Error(j.error);
    $('cutmsg').textContent='Done. '+j.outputs.length+' clip(s) saved in the "clips" folder next to the tool.';
  }catch(e){$('cutmsg').className='msg err';$('cutmsg').textContent=e.message;}
  $('cut').disabled=!rows.length;
};
render();
</script></body></html>
"""


def main():
    global PORT, OUT_DIR
    p = argparse.ArgumentParser(description="Clipper web UI (local)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("-o", "--out", default="clips", help="folder for finished clips (default: clips)")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    a = p.parse_args()
    PORT, OUT_DIR = a.port, os.path.abspath(a.out)
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"port {PORT} is busy. Try: python webui.py --port 8800")
    url = f"http://127.0.0.1:{PORT}"
    print(f"Clipper is running at {url}   (Ctrl+C to stop)\nClips are saved in: {OUT_DIR}")
    if not a.no_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
