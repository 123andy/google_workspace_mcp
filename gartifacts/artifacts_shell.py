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

from gartifacts.artifacts_config import brand_color, brand_name, icon

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

_NOTICE = (
    "<p><strong>Never enter passwords or secrets here.</strong></p>"
    "<p>This content was created by people (or by AI) and has not been reviewed.</p>"
    "<p>Don't sign in to any site here. A login box in this area could be a fake.</p>"
    "<p>Only the bar at the top is part of the viewer.</p>"
)

_NOTICE_COOKIE = "artifacts_notice"
_NOTICE_DAYS = 30

_CSS = """
html,body{height:100%%;margin:0}
body{display:flex;flex-direction:column;font:14px system-ui,sans-serif}
.hdr{display:flex;align-items:center;gap:10px;padding:6px 14px;color:#fff;background:%(color)s}
.logo{height:26px;width:32px;box-sizing:border-box;padding:3px;border-radius:6px;
background:#fff;object-fit:contain}
.brand{font-weight:600;white-space:nowrap;min-width:0;overflow:hidden;text-overflow:ellipsis}
.title{opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.warn{margin-left:auto;display:flex;align-items:center;gap:6px;white-space:nowrap}
.hdr button,.hdr a.btn{font:inherit;color:#fff;background:transparent;
border:1px solid rgba(255,255,255,.7);border-radius:4px;padding:3px 10px;
text-decoration:none;cursor:pointer}
.hdr .btn{display:inline-flex;align-items:center;gap:6px}
.hdr button:hover,.hdr a.btn:hover{background:rgba(255,255,255,.15)}
button.info{display:inline-flex;align-items:center;gap:6px;border:0;padding:3px 4px;
font-weight:700;letter-spacing:.04em}
#top{position:relative}
#pop{position:absolute;top:100%%;right:14px;max-width:420px;padding:12px 14px;z-index:2;
color:#222;background:#fff;border:1px solid #888;border-radius:6px;
box-shadow:0 4px 14px rgba(0,0,0,.25)}
#pop[hidden],#ack[hidden]{display:none}
#ack{position:fixed;inset:0;z-index:10;display:flex;align-items:center;
justify-content:center;padding:16px;background:rgba(0,0,0,.55)}
#ack .card{max-width:440px;max-height:100%%;overflow:auto;padding:18px 20px;color:#222;
background:#fff;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
#ack h2{margin:0 0 10px;font-size:18px}#ack p{margin:0 0 8px}
#ack-ok{margin-top:8px;padding:8px 16px;font:inherit;font-weight:600;color:#fff;
background:%(color)s;border:0;border-radius:4px;cursor:pointer}
#pop p{margin:0 0 8px}#pop p:last-child{margin:0}
iframe{flex:1;border:0;width:100%%}
@media(max-width:640px){
.hdr{gap:8px;padding:6px 10px}
.lbl{display:none}
.hdr .btn{padding:5px 8px}
#pop{left:10px;right:10px;max-width:none}}
@media(max-width:480px){.title{display:none}}
@media print{.hdr,#pop,#ack{display:none}html,body{height:auto}body{display:block}
iframe{height:var(--h,100vh)}}
"""

_SCRIPT = """
(function(){
var f=document.getElementById('f'),rl=document.getElementById('reload'),
pop=document.getElementById('pop'),info=document.getElementById('info'),
base=%(base)s;
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
if(document.cookie.split('; ').indexOf(C)<0){
 var top_=document.getElementById('top');top_.inert=true;f.inert=true;ack.hidden=false;
 document.getElementById('ack-ok').focus();
 document.getElementById('ack-ok').addEventListener('click',function(){
  document.cookie=C+'; Max-Age=%(age)d; Path=/; SameSite=Lax'
   +(location.protocol==='https:'?'; Secure':'');
  ack.hidden=true;top_.inert=false;f.inert=false})}
document.getElementById('pdf').addEventListener('click',function(){window.print()});
function toggle(show){pop.hidden=!show;info.setAttribute('aria-expanded',String(show))}
info.addEventListener('click',function(e){e.stopPropagation();toggle(pop.hidden)});
document.addEventListener('click',function(e){if(!pop.contains(e.target))toggle(false)});
document.addEventListener('keydown',function(e){if(e.key==='Escape')toggle(false)});
})();
"""


def shell_page(
    title: str,
    content_url: str,
    content_origin: str,
    reload_url: str,
    base_path: str,
    headers: dict,
) -> HTMLResponse:
    """The page around the sandboxed frame: header bar, warning, Reload and PDF."""
    safe_title = html.escape(title)
    name = html.escape(brand_name())
    nonce = secrets.token_urlsafe(16)
    has_icon = icon() is not None
    logo = f'<img class=logo src="{ICON_ROUTE}" alt="">' if has_icon else ""
    favicon = f'<link rel=icon href="{ICON_ROUTE}">' if has_icon else ""
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{safe_title} - {name}</title>{favicon}"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<style>{_CSS % {'color': brand_color()}}</style></head><body>"
        f"<div id=top><header class=hdr>{logo}<span class=brand>{name}</span>"
        f'<span class=title title="{safe_title}">{safe_title}</span>'
        "<span class=warn><button id=info class=info type=button "
        'aria-label="Caution: about this content" aria-expanded=false '
        f"aria-controls=pop>{_ICON_WARN}CAUTION</button></span>"
        f'<a id=reload class=btn href="{html.escape(reload_url, quote=True)}" '
        f'aria-label="Reload" title="Reload">{_ICON_RELOAD}<span class=lbl>Reload</span></a>'
        f'<button id=pdf class=btn type=button aria-label="Save as PDF" title="Save as PDF">'
        f"{_ICON_PDF}<span class=lbl>PDF</span></button></header>"
        '<div id=pop role=dialog aria-label="About this content" hidden>'
        + _NOTICE
        + "</div></div>"
        "<div id=ack role=dialog aria-modal=true aria-labelledby=ack-t hidden>"
        "<div class=card><h2 id=ack-t>Before you continue</h2>"
        + _NOTICE
        + "<button id=ack-ok type=button>I understand</button></div></div>"
        f'<iframe id=f sandbox="allow-scripts" title="{safe_title}" '
        "referrerpolicy=no-referrer "
        f'src="{html.escape(content_url, quote=True)}"></iframe>'
        f'<script nonce="{nonce}">{_SCRIPT % {"base": json.dumps(base_path), "cookie": json.dumps(_NOTICE_COOKIE), "age": _NOTICE_DAYS * 86400}}</script>'
        "</body></html>"
    )
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        f"img-src 'self'; frame-src {content_origin}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return HTMLResponse(body, headers={**headers, "Content-Security-Policy": csp})
