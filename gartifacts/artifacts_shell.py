"""
The viewer's shell page (header bar plus the sandboxed frame) and the small tracker
script added to HTML served from the content host.

The tracker tells the shell which page the frame is on and how tall it is, so Reload
returns to the same page, the address bar is a shareable link to it, and the PDF
button can print the whole page. The shell trusts the message only when it comes from
its own frame, and uses it only as a same-origin path and a clamped number.
"""

import html
import json
import re
import secrets
from typing import Optional

from starlette.responses import HTMLResponse

from gartifacts.artifacts_config import branding

ICON_ROUTE = "/a/_icon"

_TRACKER = (
    "(function(){try{var T=%s;"
    "function p(){return location.pathname.replace(/^\\/c\\/[^\\/]+/,'')"
    "+location.search+location.hash}"
    "function s(){var d=document.documentElement,b=document.body;"
    "parent.postMessage({a:'artifacts',path:p(),"
    "height:Math.max(d.scrollHeight,b?b.scrollHeight:0)},T)}"
    "addEventListener('load',s);addEventListener('hashchange',s);"
    "addEventListener('popstate',s);"
    "if(window.ResizeObserver){new ResizeObserver(s).observe(document.documentElement)}"
    "['pushState','replaceState'].forEach(function(n){var o=history[n];"
    "history[n]=function(){var r=o.apply(this,arguments);s();return r}});"
    "addEventListener('message',function(e){"
    "if(e.source!==parent||!e.data||e.data.a!=='artifacts-print')return;"
    "parent.postMessage({a:'artifacts-printing'},T);window.print()});"
    "addEventListener('beforeprint',function(){var m=document.createElement('style');"
    "m.id='__artifacts_print';m.textContent='@page{margin:6mm}';"
    "document.head.appendChild(m)});"
    "addEventListener('afterprint',function(){var m=document.getElementById('__artifacts_print');"
    "if(m)m.remove()});"
    "s()}catch(e){}})();"
)

_BODY_CLOSE = re.compile(rb"</body\s*>", re.IGNORECASE)


def inject_tracker(document: bytes, app_origin: Optional[str]) -> bytes:
    """Add the tracker script to an HTML document (before the last ``</body>``)."""
    if not app_origin:
        return document
    tag = ("<script>" + _TRACKER % json.dumps(app_origin) + "</script>").encode()
    matches = list(_BODY_CLOSE.finditer(document))
    if not matches:
        return document + tag
    at = matches[-1].start()
    return document[:at] + tag + document[at:]


_ICON_RELOAD = (
    '<svg class=ic viewBox="0 0 24 24" width=16 height=16 fill=none stroke=currentColor '
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
    '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/></svg>'
)
_ICON_SHARE = (
    '<svg class=ic viewBox="0 0 24 24" width=16 height=16 fill=none stroke=currentColor '
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
    "<circle cx=18 cy=5 r=3 /><circle cx=6 cy=12 r=3 /><circle cx=18 cy=19 r=3 />"
    '<path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>'
)
_ICON_WARN = (
    '<svg viewBox="0 0 24 24" width=18 height=18 aria-hidden=true>'
    '<path d="M12 2.5 22.5 21h-21z" fill=#ffc107 stroke=#ffc107 stroke-width=2 '
    'stroke-linejoin=round /><path d="M12 9.5v5M12 17.5v.01" stroke=#333 '
    "stroke-width=2.2 stroke-linecap=round /></svg>"
)
_ICON_PDF = (
    '<svg class=ic viewBox="0 0 24 24" width=16 height=16 fill=none stroke=currentColor '
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5"/><path d="M9 14h6M9 17h4"/></svg>'
)

# The headline is fixed in code; the bullets below it come from the branding.
_HEADLINE = "<p><strong>Never enter passwords or secrets here.</strong></p>"
_NOTICE_COOKIE = "artifacts_notice"
_COPIED = "Link copied. People also need access to the Drive folder."
_NOT_COPIED = "Could not copy. Copy the address from the address bar."

_CSS = """
html,body{height:100%%;margin:0}
body{display:flex;flex-direction:column;font:14px system-ui,sans-serif}
.hdr{display:flex;align-items:center;gap:10px;padding:6px 14px;color:#fff;background:%(color)s}
.logo{height:28px;width:34px;object-fit:contain;filter:brightness(0) invert(1)}
.hdr button.brandbtn{display:inline-flex;align-items:center;gap:10px;min-width:0;padding:0;
border:0;background:none}
.brand{font-weight:600;white-space:nowrap;min-width:0;overflow:hidden;text-overflow:ellipsis}
.title{opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;
color:inherit;text-decoration:none}
a.title:hover{opacity:1;text-decoration:underline}
.warn{margin-left:auto;display:flex;align-items:center;gap:6px;white-space:nowrap}
.hdr button,.hdr a.btn{font:inherit;color:#fff;background:transparent;
border:1px solid rgba(255,255,255,.7);border-radius:4px;padding:3px 10px;
text-decoration:none;cursor:pointer}
.hdr .btn{display:inline-flex;align-items:center;gap:6px}
.hdr button:hover,.hdr a.btn:hover{background:rgba(255,255,255,.15)}
.hdr button.brandbtn:hover{background:none}
button.info{display:inline-flex;align-items:center;gap:6px;border:0;padding:3px 4px;
font-weight:700;letter-spacing:.04em}
#top{position:relative}
#pop{position:absolute;top:100%%;right:14px;max-width:420px;padding:12px 14px;z-index:2;
color:#222;background:#fff;border:1px solid #888;border-radius:6px;
box-shadow:0 4px 14px rgba(0,0,0,.25)}
#toast{position:absolute;top:100%%;left:50%%;transform:translateX(-50%%);z-index:3;
margin-top:8px;padding:8px 14px;max-width:90%%;color:#fff;background:#222;border-radius:6px;
box-shadow:0 4px 14px rgba(0,0,0,.35)}
#pop[hidden],#ack[hidden],#about[hidden],#toast[hidden]{display:none}
#ack,#about{position:fixed;inset:0;z-index:10;display:flex;align-items:center;
justify-content:center;padding:16px;background:rgba(0,0,0,.78)}
.card{max-width:440px;max-height:100%%;overflow:auto;padding:0;color:#222;
background:#fff;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.card .mh{display:flex;align-items:center;gap:10px;padding:10px 20px;color:#fff;
background:%(color)s;font-weight:600}
.card .mb{padding:16px 20px 18px}
.card h2{margin:0 0 10px;font-size:18px}.card p{margin:0 0 8px}
.card button.ok{margin-top:8px;padding:8px 16px;font:inherit;font-weight:600;color:#fff;
background:%(color)s;border:0;border-radius:4px;cursor:pointer}
#pop p{margin:0 0 8px}#pop p:last-child,#pop ul:last-child{margin:0}
#pop ul,.card ul{margin:0 0 8px;padding-left:20px}
#pop li,.card li{margin-bottom:4px}
button.linkbtn{padding:0;font:inherit;color:#1a4fa0;background:none;border:0;
text-decoration:underline;cursor:pointer}
iframe{flex:1;border:0;width:100%%}
@media(max-width:640px){
.hdr{gap:8px;padding:6px 10px}
.lbl{display:none}
.hdr .btn{padding:5px 8px}
#pop{left:10px;right:10px;max-width:none}}
@media(max-width:480px){.title{display:none}}
@page{margin:6mm}
@media print{.hdr,#pop,#ack,#about,#toast{display:none}html,body{height:auto}body{display:block}
iframe{height:var(--h,100vh)}}
"""

_SCRIPT = """
(function(){
var f=document.getElementById('f'),rl=document.getElementById('reload'),
pop=document.getElementById('pop'),info=document.getElementById('info'),
top_=document.getElementById('top'),base=%(base)s;
function modal(el,open){el.hidden=!open;top_.inert=open;f.inert=open}
addEventListener('message',function(e){
 if(e.source!==f.contentWindow)return;
 var d=e.data;if(!d||d.a!=='artifacts')return;
 var p=d.path;
 if(typeof p==='string'&&p.length<2048&&/^\\/[^\\s\\\\]*$/.test(p)){
  var u=base+p;try{history.replaceState(null,'',u);rl.href=u}catch(x){}}
 var h=d.height;
 if(typeof h==='number'&&isFinite(h)&&h>=0)
  f.style.setProperty('--h',Math.ceil(Math.min(h,200000))+'px');
});
var ack=document.getElementById('ack'),C=%(cookie)s+'=1';
if(ack&&document.cookie.split('; ').indexOf(C)<0){
 modal(ack,true);
 document.getElementById('ack-ok').focus();
 document.getElementById('ack-ok').addEventListener('click',function(){
  document.cookie=C+'; Max-Age=%(age)d; Path=/; SameSite=Lax'
   +(location.protocol==='https:'?'; Secure':'');
  modal(ack,false)})}
var about=document.getElementById('about');
function openAbout(){toggle(false);modal(about,true);document.getElementById('about-ok').focus()}
function closeAbout(){modal(about,false)}
document.getElementById('about-open').addEventListener('click',openAbout);
document.getElementById('about-link').addEventListener('click',openAbout);
document.getElementById('about-ok').addEventListener('click',closeAbout);
about.addEventListener('click',function(e){if(e.target===about)closeAbout()});
var toast=document.getElementById('toast'),tt;
function say(m){toast.textContent=m;toast.hidden=false;clearTimeout(tt);
 tt=setTimeout(function(){toast.hidden=true},4500)}
document.getElementById('share').addEventListener('click',function(){
 var u=location.href;
 function ok(){say(%(copied)s)}function bad(){say(%(notcopied)s)}
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(u).then(ok,bad)}
 else{try{var t=document.createElement('textarea');t.value=u;
  t.style.cssText='position:fixed;opacity:0';document.body.appendChild(t);t.select();
  var r=document.execCommand('copy');document.body.removeChild(t);r?ok():bad()}catch(x){bad()}}});
var printing=false;
addEventListener('message',function(e){if(e.source===f.contentWindow&&e.data&&e.data.a==='artifacts-printing')printing=true});
document.getElementById('pdf').addEventListener('click',function(){
 printing=false;
 try{f.contentWindow.postMessage({a:'artifacts-print'},'*')}catch(x){}
 setTimeout(function(){if(!printing)window.print()},400)});
function toggle(show){pop.hidden=!show;info.setAttribute('aria-expanded',String(show))}
info.addEventListener('click',function(e){e.stopPropagation();toggle(pop.hidden)});
document.addEventListener('click',function(e){if(!pop.contains(e.target))toggle(false)});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){toggle(false);
 if(!about.hidden)closeAbout()}});
})();
"""


def _bullets(items) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def shell_page(
    title: str,
    content_url: str,
    content_origin: str,
    reload_url: str,
    base_path: str,
    headers: dict,
) -> HTMLResponse:
    """The page around the sandboxed frame: header bar, notices, Reload, Share and PDF."""
    look = branding()
    safe_title = html.escape(title)
    name = html.escape(look.name)
    nonce = secrets.token_urlsafe(16)
    logo = f'<img class=logo src="{ICON_ROUTE}" alt="">' if look.icon else ""
    favicon = f'<link rel=icon href="{ICON_ROUTE}">' if look.icon else ""
    notice = _HEADLINE + _bullets(look.notice)
    about_label = html.escape(f"About {look.name}", quote=True)
    ack = (
        "<div id=ack role=dialog aria-modal=true aria-labelledby=ack-t hidden>"
        f"<div class=card><div class=mh>{logo}<span>{name}</span></div><div class=mb>"
        f"<h2 id=ack-t>Before you continue</h2>{notice}"
        "<button id=ack-ok class=ok type=button>I understand</button></div></div></div>"
        if look.notice_days > 0
        else ""
    )
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{safe_title} - {name}</title>{favicon}"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<style>{_CSS % {'color': look.color}}</style></head><body>"
        "<div id=top><header class=hdr>"
        f'<button id=about-open class=brandbtn type=button aria-label="{about_label}" '
        f'title="{about_label}">{logo}<span class=brand>{name}</span></button>'
        f'<a class=title href="{html.escape(base_path, quote=True)}/" '
        f'title="Go to the first page of {safe_title}">{safe_title}</a>'
        "<span class=warn><button id=info class=info type=button "
        'aria-label="Caution: about this content" aria-expanded=false '
        f"aria-controls=pop>{_ICON_WARN}CAUTION</button></span>"
        '<button id=share class=btn type=button aria-label="Copy link" title="Copy link">'
        f"{_ICON_SHARE}<span class=lbl>Share</span></button>"
        f'<a id=reload class=btn href="{html.escape(reload_url, quote=True)}" '
        f'aria-label="Reload" title="Reload">{_ICON_RELOAD}<span class=lbl>Reload</span></a>'
        '<button id=pdf class=btn type=button aria-label="Save as PDF" title="Save as PDF">'
        f"{_ICON_PDF}<span class=lbl>PDF</span></button></header>"
        f'<div id=pop role=dialog aria-label="About this content" hidden>{notice}'
        f"<p><button id=about-link class=linkbtn type=button>{about_label}</button></p></div>"
        "<div id=toast role=status aria-live=polite hidden></div></div>"
        f"{ack}"
        "<div id=about role=dialog aria-modal=true aria-labelledby=about-t hidden>"
        f"<div class=card><div class=mh>{logo}<span>{name}</span></div><div class=mb>"
        f"<h2 id=about-t>{about_label}</h2>{_bullets(look.about)}"
        "<button id=about-ok class=ok type=button>Close</button></div></div></div>"
        f'<iframe id=f sandbox="allow-scripts allow-modals" title="{safe_title}" '
        "referrerpolicy=no-referrer "
        f'src="{html.escape(content_url, quote=True)}"></iframe>'
        f'<script nonce="{nonce}">{_script(base_path, look.notice_days)}</script>'
        "</body></html>"
    )
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        f"img-src 'self'; frame-src {content_origin}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return HTMLResponse(body, headers={**headers, "Content-Security-Policy": csp})


def _script(base_path: str, notice_days: int) -> str:
    return _SCRIPT % {
        "base": json.dumps(base_path),
        "cookie": json.dumps(_NOTICE_COOKIE),
        "age": max(notice_days, 0) * 86400,
        "copied": json.dumps(_COPIED),
        "notcopied": json.dumps(_NOT_COPIED),
    }
