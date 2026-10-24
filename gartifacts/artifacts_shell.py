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


_CSS = """
html,body{height:100%%;margin:0}
body{display:flex;flex-direction:column;font:14px system-ui,sans-serif}
.hdr{display:flex;align-items:center;gap:10px;padding:6px 14px;color:#fff;background:%(color)s}
.logo{height:26px;width:32px;box-sizing:border-box;padding:3px;border-radius:6px;
background:#fff;object-fit:contain}
.brand{font-weight:600;white-space:nowrap}
.title{opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.warn{margin-left:auto;display:flex;align-items:center;gap:6px;font-weight:600;
white-space:nowrap}
.hdr button,.hdr a.btn{font:inherit;color:#fff;background:transparent;
border:1px solid rgba(255,255,255,.7);border-radius:4px;padding:3px 10px;
text-decoration:none;cursor:pointer}
.hdr button:hover,.hdr a.btn:hover{background:rgba(255,255,255,.15)}
.hdr button.info{width:22px;height:22px;padding:0;border-radius:50%%;font-weight:700;
line-height:1}
#pop{position:absolute;top:44px;right:14px;max-width:420px;padding:12px 14px;z-index:2;
color:#222;background:#fff;border:1px solid #888;border-radius:6px;
box-shadow:0 4px 14px rgba(0,0,0,.25)}
#pop[hidden]{display:none}
#pop p{margin:0 0 8px}#pop p:last-child{margin:0}
iframe{flex:1;border:0;width:100%%}
@media print{.hdr,#pop{display:none}html,body{height:auto}body{display:block}
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
        f"<header class=hdr>{logo}<span class=brand>{name}</span>"
        f'<span class=title title="{safe_title}">{safe_title}</span>'
        "<span class=warn>Never enter passwords or secrets "
        '<button id=info class=info type=button aria-label="About this content" '
        "aria-expanded=false aria-controls=pop>i</button></span>"
        f'<a id=reload class=btn href="{html.escape(reload_url, quote=True)}">Reload</a>'
        "<button id=pdf type=button>PDF</button></header>"
        '<div id=pop role=dialog aria-label="About this content" hidden>'
        "<p><strong>This content was created by people (or by AI) and has not been "
        "reviewed.</strong></p>"
        "<p>Be especially careful about what you type here. Never sign in to any site "
        "inside this frame, and never enter a password, key or personal "
        "information: a login box in this area could be a fake.</p>"
        "<p>Only the bar above is part of the viewer.</p></div>"
        f'<iframe id=f sandbox="allow-scripts" title="{safe_title}" '
        "referrerpolicy=no-referrer "
        f'src="{html.escape(content_url, quote=True)}"></iframe>'
        f'<script nonce="{nonce}">{_SCRIPT % {"base": json.dumps(base_path)}}</script>'
        "</body></html>"
    )
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        f"img-src 'self'; frame-src {content_origin}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return HTMLResponse(body, headers={**headers, "Content-Security-Policy": csp})
