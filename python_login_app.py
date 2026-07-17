"""Local controller for an authorized Buyin browser session."""

from pathlib import Path
import shutil
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from threading import Lock

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape

from flask import Flask, g, jsonify, make_response, redirect, render_template_string, request, session
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from auth_store import AuthStore
from history_store import HistoryStore

LOGIN_URL = "https://buyin.jinritemai.com/mpa/account/login"
PROFILE_DIR = Path(__file__).parent / "data" / "buyin-browser-profile"
DATA_DIR = Path(__file__).parent / "data"
AUTH_MARKER = DATA_DIR / "buyin-authenticated.marker"
MATERIAL_LIST_PATH = "/pc/selection/common/material_list"
REQUEST_SPEC_FILE = DATA_DIR / "material_list_request.json"
SKIPPED_FILE = DATA_DIR / "selection_skipped.json"
NETWORK_CAPTURE_FILE = DATA_DIR / "network_capture.jsonl"
MATERIAL_PAGES_FILE = DATA_DIR / "material_list_pages.json"
HISTORY_DATABASE = DATA_DIR / "history.db"
AUTH_DATABASE = DATA_DIR / "auth.db"
SESSION_SECRET_FILE = DATA_DIR / ".session-secret"
CAPTURE_NETWORK = os.getenv("CAPTURE_NETWORK") == "1"
USE_DIRECT_MATERIAL = os.getenv("BUYIN_DIRECT_MATERIAL", "1") == "1"
HEADLESS_BROWSER = os.getenv("BUYIN_HEADLESS", "1") != "0"
MATERIAL_RESPONSE_TIMEOUT_MS = int(os.getenv("MATERIAL_RESPONSE_TIMEOUT_MS", "15000"))
DETAIL_PAGE_TIMEOUT_MS = int(os.getenv("DETAIL_PAGE_TIMEOUT_MS", "15000"))
DETAIL_SETTLE_MS = int(os.getenv("DETAIL_SETTLE_MS", "4000"))
CONTACT_HOVER_SETTLE_MS = int(os.getenv("CONTACT_HOVER_SETTLE_MS", "1000"))
CONTACT_RESPONSE_TIMEOUT_MS = int(os.getenv("CONTACT_RESPONSE_TIMEOUT_MS", "2000"))
CONTACT_REQUEST_GAP_MS = int(os.getenv("CONTACT_REQUEST_GAP_MS", "1200"))
DETAIL_CONCURRENCY = max(1, int(os.getenv("DETAIL_CONCURRENCY", "1")))
last_contact_request_at = 0.0


class ContactRateLimitedError(RuntimeError):
    """Raised when Buyin refuses a contact request because it is too frequent."""


def _is_rate_limited_payload(payload: object, status: int | None = None) -> bool:
    if status == 429:
        return True
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if str(code) in {"11001", "429"}:
        return True
    messages = [payload.get("msg"), payload.get("message"), payload.get("error")]
    data = payload.get("data")
    if isinstance(data, dict):
        messages.extend([data.get("msg"), data.get("message"), data.get("error")])
    return any("频繁" in str(message) for message in messages if message)

history_store = HistoryStore(HISTORY_DATABASE)
history_store.migrate_json_once(DATA_DIR / "selection_results.json")
auth_store = AuthStore(AUTH_DATABASE)


def find_browser() -> str | None:
    """Prefer an installed browser so bundled Chromium is not required."""
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((item for item in candidates if item and Path(item).exists()), None)


def mark_authenticated(current_page: Page) -> None:
    if "buyin.jinritemai.com" in current_page.url and "/account/login" not in current_page.url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        AUTH_MARKER.touch()


def sanitized_url(url: str) -> str:
    parts = urlsplit(url)
    safe_keys = {"cursor", "contact_type"}
    query = [(key, value if key in safe_keys else "<redacted>") for key, value in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def capture_network_response(response) -> None:
    path = urlsplit(response.url).path
    if path not in {MATERIAL_LIST_PATH, "/square_pc_api/common/contact"}:
        return
    try:
        body = response.body().decode("utf-8", errors="replace")
        request = response.request
        record = {
            "url": sanitized_url(response.url),
            "path": path,
            "status": response.status,
            "method": request.method,
            "request_body": request.post_data,
            "response_body": json.loads(body),
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with NETWORK_CAPTURE_FILE.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[network capture failed] {exc}")

def _load_session_secret() -> str:
    configured = os.getenv("APP_SESSION_SECRET")
    if configured:
        return configured
    if SESSION_SECRET_FILE.exists():
        return SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret


app = Flask(__name__)
app.secret_key = _load_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)
state_lock = Lock()
auth_state_lock = Lock()
playwright = None
context: BrowserContext | None = None
page: Page | None = None
browser_headless = False
high_sales_filter_applied = False


def _current_user() -> dict | None:
    username = session.get("username")
    session_id = session.get("auth_session_id")
    if not username or not session_id:
        return None
    user = auth_store.get_user(str(username))
    if user is None:
        session.clear()
        return None
    if user["role"] == "admin":
        lease = auth_store.get_admin_session()
        if lease is None or lease["session_id"] != session_id:
            session.clear()
            return None
    return user


def _unauthorized_response():
    payload = {"ok": False, "authenticated": False, "auth_required": True, "error": "请先登录"}
    if request.path.startswith("/api/") or request.path in {"/status", "/login-qr"}:
        return jsonify(payload), 401
    return redirect("/login")


def _forbidden_response(message: str = "没有权限执行此操作"):
    if request.path.startswith("/api/") or request.path in {
        "/status", "/login-qr", "/open-login", "/go-selection", "/collect-selection",
    }:
        return jsonify(ok=False, error=message), 403
    return make_response(message, 403)


def _require_admin():
    user = getattr(g, "current_user", None)
    if not user or user["role"] != "admin":
        return _forbidden_response("仅管理员可以执行此操作")
    return None


def _claim_admin_session(session_id: str) -> None:
    with auth_state_lock:
        current = auth_store.get_admin_session()
        if current and current["session_id"] != session_id and current["task_active"]:
            raise RuntimeError("管理员正在执行采集任务，当前不能顶替登录")
        auth_store.claim_admin_session(session_id)


def _begin_admin_task(session_id: str) -> None:
    with auth_state_lock:
        current = auth_store.get_admin_session()
        if current is None or current["session_id"] != session_id:
            raise RuntimeError("管理员登录状态已失效，请重新登录")
        if current["task_active"]:
            raise RuntimeError("管理员已有采集任务正在执行")
        auth_store.set_admin_task(session_id, True)


def _end_admin_task(session_id: str) -> None:
    with auth_state_lock:
        current = auth_store.get_admin_session()
        if current and current["session_id"] == session_id:
            auth_store.set_admin_task(session_id, False)


@app.before_request
def enforce_application_login():
    if request.path in {"/login", "/auth/login", "/auth/logout"}:
        return None
    user = _current_user()
    if user is None:
        return _unauthorized_response()
    g.current_user = user
    return None


PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>选品采集控制台</title>
<style>
:root{--navy:#172235;--blue:#2563eb;--bg:#f3f5f8;--line:#e5e7eb;--text:#1f2937;--muted:#6b7280;--green:#16805c;--red:#c2413b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}.shell{min-height:100vh;display:flex}.side{width:232px;background:var(--navy);color:#dbe5f4;padding:22px 14px;flex:none}.brand{font-size:18px;font-weight:700;color:#fff;padding:0 12px 26px}.brand small{display:block;color:#91a1b8;font-size:11px;font-weight:400;margin-top:4px}.nav-title{padding:12px;font-size:11px;color:#8191a8}.nav-item{display:block;padding:10px 12px;border-radius:5px;color:#c5d2e4;text-decoration:none;margin:3px 0}.nav-item.active,.nav-item:hover{background:#26364e;color:#fff}.main{flex:1;min-width:0}.top{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 34px}.top h1{font-size:18px;margin:0}.user{color:var(--muted);font-size:13px}.content{max-width:1400px;margin:0 auto;padding:28px 34px}.crumb{font-size:13px;color:var(--muted);margin-bottom:20px}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:22px}.card{background:#fff;border:1px solid var(--line);border-radius:6px;padding:18px 20px}.card-label{color:var(--muted);font-size:13px}.card-value{font-size:26px;font-weight:700;margin-top:7px}.card-sub{font-size:12px;color:var(--muted);margin-top:4px}.panel{background:#fff;border:1px solid var(--line);border-radius:6px;margin-bottom:20px}.panel-head{display:flex;justify-content:space-between;align-items:center;padding:17px 20px;border-bottom:1px solid var(--line)}.panel-head h2{font-size:16px;margin:0}.panel-body{padding:20px}.actions{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.field{display:flex;flex-direction:column;gap:6px;min-width:180px}.field label{font-size:12px;color:var(--muted)}.field input{height:38px;padding:0 11px;border:1px solid #cfd5df;border-radius:4px;font:inherit}.btn{height:38px;border:1px solid #cbd5e1;background:#fff;color:var(--text);padding:0 15px;border-radius:4px;cursor:pointer;font:inherit}.btn:hover{background:#f8fafc}.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue)}.btn.danger{color:var(--red)}.btn:disabled{opacity:.55;cursor:wait}.status{display:flex;align-items:center;gap:9px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af}.dot.online{background:#20a36b}.status-url{margin-top:10px;color:var(--muted);word-break:break-all;font-size:12px}.note{color:var(--muted);margin:0 0 17px}.modal-backdrop{position:fixed;inset:0;background:rgba(15,23,42,.35);display:none;align-items:center;justify-content:center;padding:20px}.modal-backdrop.show{display:flex}.modal{background:#fff;width:min(420px,100%);border-radius:7px;box-shadow:0 16px 48px rgba(15,23,42,.22);padding:24px}.modal h3{margin:0 0 10px;font-size:17px}.modal p{margin:0;color:var(--muted);white-space:pre-wrap}.modal-foot{text-align:right;margin-top:22px}
@media(max-width:800px){.side{width:70px;padding:18px 8px}.brand{font-size:0;padding:0 10px 24px}.brand:before{content:"AI";font-size:18px}.brand small,.nav-title{display:none}.nav-item{font-size:0;text-align:center}.nav-item:before{content:"●";font-size:15px}.top{padding:0 18px}.content{padding:20px 16px}.cards{grid-template-columns:1fr}.panel-head{align-items:flex-start;gap:10px;flex-direction:column}}
</style></head><body><div class="shell"><aside class="side"><div class="brand">选品采集<small>Buyin Data Console</small></div><div class="nav-title">工作台</div><a class="nav-item active" href="/">采集控制台</a><a class="nav-item" href="/results">历史数据</a></aside><main class="main"><header class="top"><h1>选品采集控制台</h1><span class="user">本地业务工具</span></header><section class="content"><div class="crumb">工作台 / 采集控制台</div><div class="cards"><div class="card"><div class="card-label">浏览器会话</div><div class="card-value" id="sessionValue">未连接</div><div class="card-sub" id="sessionSub">等待状态更新</div></div><div class="card"><div class="card-label">历史记录</div><div class="card-value" id="historyValue">-</div><div class="card-sub">SQLite 持久化记录</div></div><div class="card"><div class="card-label">当前页面</div><div class="card-value" id="pageValue">-</div><div class="card-sub" id="pageSub">暂无页面信息</div></div></div><div class="panel"><div class="panel-head"><h2>采集任务</h2><span class="status" id="statusText"><i class="dot" id="statusDot"></i>检查连接状态</span></div><div class="panel-body"><p class="note">先打开登录页完成授权，再进入选品页面。采集结果会追加保存到历史数据库。</p><div class="actions"><div class="field"><label for="collectLimit">本次采集数量</label><input id="collectLimit" type="number" min="1" max="10000" value="90"></div><button class="btn primary" id="collectBtn" onclick="collectSelection()">开始采集</button><button class="btn" onclick="openLogin()">打开登录页</button><button class="btn" onclick="goSelection()">进入选品页</button><button class="btn" onclick="refreshStatus()">刷新状态</button><button class="btn danger" onclick="closeBrowser()">关闭浏览器</button></div><div class="status-url" id="statusUrl"></div></div></div><div class="panel"><div class="panel-head"><h2>快捷入口</h2></div><div class="panel-body"><a class="btn" href="/results">查看历史数据</a></div></div></section></main></div><div class="modal-backdrop" id="modal"><div class="modal"><h3 id="modalTitle">操作结果</h3><p id="modalMessage"></p><div class="modal-foot"><button class="btn primary" onclick="closeModal()">知道了</button></div></div></div>
<script>
const $=id=>document.getElementById(id);
function showModal(title,message){$('modalTitle').textContent=title;$('modalMessage').textContent=message;$('modal').classList.add('show')}
function closeModal(){$('modal').classList.remove('show')}
async function action(url,options,title){try{const response=await fetch(url,options);const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'操作失败');showModal(title,data.message||'操作已完成');return data}catch(error){showModal(title+'失败',error.message);return null}finally{await refreshStatus()}}
async function refreshStatus(){const status=await fetch('/status').then(r=>r.json());$('sessionValue').textContent=status.open?'已连接':'未连接';$('sessionSub').textContent=status.open?'浏览器会话正常':'请打开登录页';$('pageValue').textContent=status.open?(status.title||'已打开'):'-';$('pageSub').textContent=status.open?'当前页面':'暂无页面信息';$('statusDot').classList.toggle('online',!!status.open);$('statusText').innerHTML='<i class="dot '+(status.open?'online':'')+'"></i>'+(status.open?'浏览器已连接':'浏览器未连接');$('statusUrl').textContent=status.url||''}
async function refreshHistory(){const data=await fetch('/api/history').then(r=>r.json());$('historyValue').textContent=(data.count||0).toLocaleString()}
async function openLogin(){startLoginQr();const result=await action('/open-login',{method:'POST'},'Login QR');if(!result)stopLoginQr();else refreshLoginQr()}
async function goSelection(){await action('/go-selection',{method:'POST'},'进入选品页')}
async function collectSelection(){const button=$('collectBtn');const limit=Number($('collectLimit').value);if(!Number.isInteger(limit)||limit<1||limit>10000){showModal('参数错误','采集数量必须在 1 到 10000 之间');return}button.disabled=true;button.textContent='采集中...';try{await action('/collect-selection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit})},'采集任务');await refreshHistory()}finally{button.disabled=false;button.textContent='开始采集'}}
async function closeBrowser(){await action('/close',{method:'POST'},'关闭浏览器')}
async function refreshStatusAction(){await refreshStatus();showModal('刷新状态','浏览器状态已更新')}
document.querySelector('[onclick="refreshStatus()"]')?.addEventListener('click',refreshStatusAction)
refreshStatus();refreshHistory();setInterval(refreshStatus,5000);
</script></body></html>
"""

PAGE = PAGE.replace(
    '</header><section class="content">',
    '<button class="btn danger header-action" id="logoutBtn" data-requires-auth disabled onclick="logoutSession()">退出登录</button></header><section class="content">',
    1,
)
PAGE = PAGE.replace(
    '<div class="panel"><div class="panel-head"><h2>',
    '<aside class="operation-guide"><div class="guide-kicker">操作流程</div><h2>按顺序完成操作</h2><ol class="operation-steps"><li id="stepLogin"><span>1</span><div><strong>打开登录页</strong><p>扫码完成 Buyin 账号登录。</p></div></li><li id="stepSelection"><span>2</span><div><strong>进入选品页</strong><p>登录成功后进入商品选品页面。</p></div></li><li id="stepCollect"><span>3</span><div><strong>开始采集</strong><p>设置数量并开始采集，结果会保存到历史数据。</p></div></li></ol><p class="guide-state" id="guideState">请先打开登录页并完成扫码。</p></aside><div class="panel"><div class="panel-head"><h2>',
    1,
)
PAGE = PAGE.replace(
    '<input id="collectLimit"',
    '<input data-requires-auth disabled id="collectLimit"',
    1,
).replace(
    '<button class="btn primary" id="collectBtn"',
    '<button class="btn primary" data-requires-auth disabled id="collectBtn"',
    1,
).replace(
    '<button class="btn" onclick="openLogin()">',
    '<button class="btn" id="openLoginBtn" onclick="openLogin()">',
    1,
).replace(
    '<button class="btn" onclick="goSelection()">',
    '<button class="btn" data-requires-auth disabled onclick="goSelection()">',
    1,
).replace(
    '<button class="btn" onclick="refreshStatus()">',
    '<button class="btn" id="refreshStatusBtn" onclick="refreshStatus()">',
    1,
).replace(
    '<button class="btn danger" onclick="closeBrowser()">',
    '<button class="btn danger" data-requires-browser disabled onclick="closeBrowser()">',
    1,
)
PAGE = PAGE.replace(
    '</style>',
    '.header-action{margin-left:auto}.btn:disabled{opacity:.45;cursor:not-allowed;background:#eef1f5;color:#94a3b8;border-color:#d8dee8}.operation-guide{float:right;width:320px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:20px}.operation-guide h2{font-size:17px;margin:3px 0 18px}.guide-kicker{font-size:12px;color:var(--blue);font-weight:600}.operation-steps{list-style:none;padding:0;margin:0}.operation-steps li{display:flex;gap:12px;padding:0 0 18px;position:relative;color:#64748b}.operation-steps li:not(:last-child):after{content:"";position:absolute;left:13px;top:28px;bottom:2px;width:1px;background:#dbe3ef}.operation-steps li>span{width:27px;height:27px;flex:none;display:grid;place-items:center;border-radius:50%;background:#eef2f7;color:#64748b;font-weight:700;font-size:12px}.operation-steps strong{display:block;color:#334155;font-size:13px}.operation-steps p{margin:4px 0 0;font-size:12px;line-height:1.5}.operation-steps li.done>span{background:#dcfce9;color:#16805c}.operation-steps li.active>span{background:#dbeafe;color:var(--blue)}.guide-state{margin:2px 0 0;padding:10px 12px;background:#f8fafc;color:var(--muted);font-size:12px;border-radius:4px}.operation-guide~.panel{margin-right:340px}.content:after{content:"";display:block;clear:both}@media(max-width:900px){.operation-guide{float:none;width:auto;margin-bottom:20px}.operation-guide~.panel{margin-right:0}}@media(max-width:800px){.top{gap:10px}.top h1{font-size:16px}.header-action{padding:0 10px;font-size:12px}} </style>',
    1,
)
PAGE = PAGE.replace(
    'async function refreshStatus()',
    'function updateAuthControls(status){const authenticated=!!status.authenticated;const browserOpen=!!status.open;document.querySelectorAll("[data-requires-auth]").forEach(control=>{control.disabled=!authenticated;control.setAttribute("aria-disabled",String(!authenticated))});document.querySelectorAll("[data-requires-browser]").forEach(control=>{control.disabled=!browserOpen});const selectionPage=authenticated&&/(?:merch-picking|selection)/.test(status.url||"");$("stepLogin").classList.toggle("done",authenticated);$("stepSelection").classList.toggle("done",selectionPage);$("stepSelection").classList.toggle("active",authenticated&&!selectionPage);$("stepCollect").classList.toggle("active",selectionPage);$("guideState").textContent=selectionPage?"已进入选品页，可以开始采集。":authenticated?"登录成功，请进入选品页。":browserOpen?"登录页已打开，请完成扫码登录。":"请先打开登录页并完成扫码。"}async function refreshStatus()',
    1,
)
PAGE = PAGE.replace(
    "$('statusUrl').textContent=status.url||''}",
    "$('statusUrl').textContent=status.url||'';updateAuthControls(status)}",
    1,
)
PAGE = PAGE.replace(
    'async function closeBrowser(){await action(\'/close\',{method:\'POST\'},\'关闭浏览器\')}',
    'async function closeBrowser(){await action(\'/close\',{method:\'POST\'},\'关闭浏览器\')}async function logoutSession(){await action(\'/logout\',{method:\'POST\'},\'退出登录\')}',
    1,
)

PAGE = re.sub(
    r'<button class="btn danger header-action".*?</button>',
    '',
    PAGE,
    count=1,
    flags=re.DOTALL,
)
PAGE = re.sub(
    r'<div class="actions">.*?</div><div class="status-url"',
    '<div class="actions"><div class="action-group"><button class="btn" id="openLoginBtn" onclick="openLogin()">打开登录页</button><button class="btn" data-requires-auth disabled onclick="goSelection()">进入选品页</button></div><div class="action-separator" aria-hidden="true"></div><div class="action-group"><div class="field"><label for="collectLimit">本次采集数量</label><input data-requires-auth disabled id="collectLimit" type="number" min="1" max="10000" value="90"></div><button class="btn primary" data-requires-auth disabled id="collectBtn" onclick="collectSelection()">开始采集</button></div><div class="action-group browser-group"><button class="btn" data-requires-browser disabled id="browserModeBtn" onclick="toggleBrowserMode()">切换前台</button><button class="btn danger" data-requires-auth disabled id="logoutBtn" onclick="logoutSession()">退出百应</button></div></div><div class="status-url"',
    PAGE,
    count=1,
    flags=re.DOTALL,
)
PAGE = PAGE.replace(
    '.header-action{margin-left:auto}',
    '.header-action{margin-left:auto}.actions{align-items:flex-end}.action-group{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}.action-separator{width:24px;height:1px;flex:none}.browser-group{margin-left:auto}.btn.logged-in:disabled{opacity:1;background:#111827;color:#fff;border-color:#111827;cursor:default}@media(max-width:1100px){.browser-group{margin-left:0}}',
    1,
)
PAGE = re.sub(
    r'function updateAuthControls\(status\)\{.*?\}async function refreshStatus\(\)',
    'function updateAuthControls(status){const authenticated=!!status.authenticated;const browserOpen=!!status.open;document.querySelectorAll("[data-requires-auth]").forEach(control=>{control.disabled=!authenticated;control.setAttribute("aria-disabled",String(!authenticated))});document.querySelectorAll("[data-requires-browser]").forEach(control=>{control.disabled=!browserOpen});const loginButton=$("openLoginBtn");loginButton.disabled=authenticated;loginButton.classList.toggle("logged-in",authenticated);loginButton.textContent=authenticated?"已登录":"打开登录页";const browserButton=$("browserModeBtn");browserButton.textContent=status.headless?"切换前台":"切换后台";const selectionPage=authenticated&&/(?:merch-picking|selection)/.test(status.url||"");$("stepLogin").classList.toggle("done",authenticated);$("stepSelection").classList.toggle("done",selectionPage);$("stepSelection").classList.toggle("active",authenticated&&!selectionPage);$("stepCollect").classList.toggle("active",selectionPage);$("guideState").textContent=selectionPage?"已进入选品页，可以开始采集。":authenticated?"登录成功，请进入选品页。":browserOpen?"登录页已打开，请完成扫码登录。":"请先打开登录页并完成扫码。"}async function refreshStatus()',
    PAGE,
    count=1,
    flags=re.DOTALL,
)
PAGE = re.sub(
    r'async function closeBrowser\(\)\{.*?\}async function refreshStatusAction',
    'async function toggleBrowserMode(){await action("/toggle-browser",{method:"POST"},"切换浏览器运行模式")}async function logoutSession(){await action("/logout",{method:"POST"},"退出百应")}async function refreshStatusAction',
    PAGE,
    count=1,
    flags=re.DOTALL,
)

PAGE = re.sub(
    r'async function closeBrowser\(\).*?async function refreshStatusAction',
    'async function toggleBrowserMode(){await action("/toggle-browser",{method:"POST"},"切换浏览器运行模式")}async function logoutSession(){await action("/logout",{method:"POST"},"退出百应")}async function refreshStatusAction',
    PAGE,
    count=1,
    flags=re.DOTALL,
)
PAGE = PAGE.replace(
    '</ol><p class="guide-state"',
    '<li><span>4</span><div><strong>切换浏览器模式</strong><p>按需切换爬虫浏览器前台或后台运行。</p></div></li><li><span>5</span><div><strong>退出百应</strong><p>结束本次账号会话并清理登录状态。</p></div></li></ol><p class="guide-state"',
    1,
)
PAGE = PAGE.replace(
    '<a class="nav-item" href="/results">历史数据</a>',
    '<a class="nav-item" href="/results">历史数据</a><a class="nav-item" href="/admin/accounts">账号管理</a>',
    1,
)
PAGE = PAGE.replace(
    '<span class="user">本地业务工具</span>',
    '<span class="user">管理员：admin　<a href="/auth/logout">退出系统</a></span>',
    1,
)

# The login browser runs on the server. Expose a fresh screenshot so a remote
# user can scan the QR code without requiring a desktop session on the server.
PAGE = PAGE.replace(
    '</style></head>',
    '.login-qr-panel{display:none}.login-qr-panel.show{display:block}.login-qr-wrap{display:flex;gap:20px;align-items:center;flex-wrap:wrap}.login-qr{width:min(360px,100%);height:auto;min-height:180px;object-fit:contain;border:1px solid var(--line);background:#fff}.login-qr-note{color:var(--muted);max-width:360px}.login-qr-note strong{display:block;color:var(--text);margin-bottom:6px}@media(max-width:800px){.login-qr-wrap{display:block}.login-qr{margin-bottom:12px}} </style></head>',
    1,
)
PAGE = PAGE.replace(
    '<div class="panel"><div class="panel-head"><h2>',
    '<div class="panel login-qr-panel" id="loginQrPanel"><div class="panel-head"><h2>扫码登录</h2></div><div class="panel-body login-qr-wrap"><img class="login-qr" id="loginQr" alt="登录二维码"><div class="login-qr-note"><strong>请使用手机扫描二维码</strong><span id="loginQrState">二维码加载中...</span></div></div></div><div class="panel"><div class="panel-head"><h2>',
    1,
)
PAGE = PAGE.replace(
    '</script></body></html>',
    '''
<script>
let loginQrTimer=null;
function stopLoginQr(){
    if(loginQrTimer){clearInterval(loginQrTimer);loginQrTimer=null}
    $('loginQrPanel')?.classList.remove('show');
}
async function refreshLoginQr(){
    const response=await fetch('/status');
    const status=await response.json();
    const panel=$('loginQrPanel');
    if(!panel)return;
    if(status.authenticated){stopLoginQr();return}
    if(status.open&&status.login_page){
        panel.classList.add('show');
        $('loginQr').src='/login-qr?t='+Date.now();
        $('loginQrState').textContent='请在手机上完成扫码，登录成功后二维码会自动消失。';
    }
}
function startLoginQr(){
    stopLoginQr();
    $('loginQrPanel')?.classList.add('show');
    $('loginQrState').textContent='二维码加载中...';
    refreshLoginQr();
    loginQrTimer=setInterval(refreshLoginQr,3000);
}
</script>
</script></body></html>''',
    1,
)
PAGE = PAGE.replace(
    "async function openLogin(){await action('/open-login',{method:'POST'},'閲嶆柊鎵爜鐧诲綍')}",
    "async function openLogin(){startLoginQr();const result=await action('/open-login',{method:'POST'},'閲嶆柊鎵爜鐧诲綍');if(!result)stopLoginQr();else refreshLoginQr()}",
    1,
)

PAGE = re.sub(
    r"\$\('loginQrState'\)\.textContent=.*?\n",
    "$('loginQrState').textContent='Please scan the QR code with your phone. The QR code will disappear after login.';\n",
    PAGE,
    count=1,
)
PAGE = PAGE.replace('</script>\n</script></body></html>', '</script></body></html>')
RESULTS_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>历史数据</title>
<style>:root{--navy:#172235;--blue:#2563eb;--bg:#f3f5f8;--line:#e5e7eb;--muted:#6b7280}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#1f2937;font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}.shell{min-height:100vh;display:flex}.side{width:232px;background:var(--navy);color:#dbe5f4;padding:22px 14px;flex:none}.brand{font-size:18px;font-weight:700;color:#fff;padding:0 12px 26px}.brand small{display:block;color:#91a1b8;font-size:11px;font-weight:400;margin-top:4px}.nav-title{padding:12px;font-size:11px;color:#8191a8}.nav-item{display:block;padding:10px 12px;border-radius:5px;color:#c5d2e4;text-decoration:none;margin:3px 0}.nav-item.active,.nav-item:hover{background:#26364e;color:#fff}.main{flex:1;min-width:0}.top{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 34px}.top h1{font-size:18px;margin:0}.content{max-width:1600px;margin:0 auto;padding:28px 34px}.crumb{font-size:13px;color:var(--muted);margin-bottom:20px}.panel{background:#fff;border:1px solid var(--line);border-radius:6px}.panel-head{display:flex;justify-content:space-between;align-items:center;padding:17px 20px;border-bottom:1px solid var(--line)}.panel-head h2{font-size:16px;margin:0}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.input{height:36px;border:1px solid #cfd5df;border-radius:4px;padding:0 10px;font:inherit}.btn{height:36px;border:1px solid #cbd5e1;background:#fff;padding:0 14px;border-radius:4px;cursor:pointer;font:inherit}.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}.meta{color:var(--muted);font-size:13px}.table-wrap{overflow:auto}table{width:100%;min-width:1040px;border-collapse:collapse}th,td{padding:12px 16px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}th{background:#f8fafc;color:#4b5563;font-weight:600;white-space:nowrap}tbody tr:hover{background:#f8fbff}img{width:56px;height:56px;object-fit:cover;border-radius:4px;background:#f3f4f6}.product{max-width:280px;font-weight:600}.sub{color:var(--muted);font-size:12px;margin-top:3px}.score{font-weight:700;color:#111827}.pager{display:flex;justify-content:space-between;align-items:center;padding:14px 20px}.empty{text-align:center;color:var(--muted);padding:60px}.loading{opacity:.55}
@media(max-width:800px){.side{width:70px;padding:18px 8px}.brand{font-size:0;padding:0 10px 24px}.brand:before{content:"AI";font-size:18px}.brand small,.nav-title{display:none}.nav-item{font-size:0;text-align:center}.nav-item:before{content:"●";font-size:15px}.top{padding:0 18px}.content{padding:20px 16px}.panel-head{align-items:flex-start;gap:10px;flex-direction:column}}
</style></head><body><div class="shell"><aside class="side"><div class="brand">选品采集<small>Buyin Data Console</small></div><div class="nav-title">工作台</div><a class="nav-item" href="/">采集控制台</a><a class="nav-item active" href="/results">历史数据</a></aside><main class="main"><header class="top"><h1>历史数据</h1><span class="meta">SQLite indexed storage</span></header><section class="content"><div class="crumb">工作台 / 历史数据</div><div class="panel"><div class="panel-head"><h2>商品采集记录</h2><div class="toolbar"><input class="input" id="query" placeholder="搜索商品、店铺或 ID"><button class="btn primary" onclick="search()">查询</button><a class="btn" href="/">返回控制台</a></div></div><div class="panel-head"><span class="meta" id="summary">正在加载...</span><span class="meta">按采集时间倒序</span></div><div class="table-wrap"><table><thead><tr><th>商品</th><th>商品名称</th><th>店铺名称</th><th>店铺分</th><th>月销</th><th>商家商品 ID</th><th>采集时间</th></tr></thead><tbody id="rows"></tbody></table><div class="empty" id="empty" hidden>暂无符合条件的历史数据</div></div><div class="pager"><span class="meta" id="pageInfo"></span><div><button class="btn" id="prev" onclick="turn(-1)">上一页</button><button class="btn" id="next" onclick="turn(1)">下一页</button></div></div></div></section></main></div>
<script>let offset=0;const size=100;function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function load(){document.body.classList.add('loading');const q=encodeURIComponent(document.getElementById('query').value.trim());const items=await fetch('/api/results?limit='+size+'&offset='+offset+'&q='+q).then(r=>r.json());const rows=document.getElementById('rows');rows.innerHTML=items.map(item=>'<tr><td>'+(item.image_url?'<img src="'+esc(item.image_url)+'" loading="lazy">':'-')+'</td><td><div class="product">'+esc(item.name||'未命名商品')+'</div><div class="sub">商品 ID：'+esc(item.product_id)+'</div></td><td>'+esc(item.shop_name||'未获取到店铺名称')+'<div class="sub">店铺 ID：'+esc(item.shop_id)+'</div></td><td><span class="score">'+esc(item.shop_score||'-')+'</span></td><td>'+esc(item.month_sale||'-')+'</td><td>'+esc(item.merchant_product_id||'-')+'</td><td>'+esc(item.collected_at||'-').replace('T',' ').slice(0,19)+'</td></tr>').join('');document.getElementById('empty').hidden=items.length>0;document.getElementById('summary').textContent='本页 '+items.length+' 条';document.getElementById('pageInfo').textContent='第 '+(Math.floor(offset/size)+1)+' 页';document.getElementById('prev').disabled=offset===0;document.getElementById('next').disabled=items.length<size;document.body.classList.remove('loading')}function search(){offset=0;load()}function turn(direction){offset=Math.max(0,offset+direction*size);load()}load()</script></body></html>
"""


def _launch_browser(headless: bool) -> None:
    global playwright, context, page, browser_headless
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    options = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": headless,
        "viewport": {"width": 1440, "height": 900},
        "timeout": 60000,
    }
    browser_path = find_browser()
    if browser_path:
        options["executable_path"] = browser_path
    context = playwright.chromium.launch_persistent_context(**options)
    context.on("console", lambda message: print(f"[browser console] {message.type}: {message.text}"))
    context.on("requestfailed", lambda request: print(
        f"[request failed] {request.method} {request.url} :: {request.failure}"
    ))
    context.on("response", lambda response: print(
        f"[response {response.status}] {response.url}"
    ) if response.status >= 400 else None)
    if CAPTURE_NETWORK:
        context.on("response", capture_network_response)
    page = context.pages[0] if context.pages else context.new_page()
    browser_headless = headless


def _close_browser_context() -> None:
    global playwright, context, page, browser_headless
    if context is not None:
        context.close()
    if playwright is not None:
        playwright.stop()
    playwright = context = page = None
    browser_headless = False


def _open_login_url() -> None:
    if page is None:
        raise RuntimeError("The browser page is not available.")
    page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)


def _switch_to_headless_after_login() -> None:
    """Hide the temporary QR browser after the persistent session becomes valid."""
    global page
    if not HEADLESS_BROWSER or browser_headless or page is None or page.is_closed():
        return
    if "buyin.jinritemai.com" not in page.url or "/account/login" in page.url:
        return
    current_url = page.url
    _close_browser_context()
    _launch_browser(headless=True)
    page.goto(current_url, wait_until="commit", timeout=60000)
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    mark_authenticated(page)


def open_login_page(force_visible: bool = False) -> None:
    with state_lock:
        if force_visible:
            if context is not None:
                _close_browser_context()
            _launch_browser(headless=True)
            context.clear_cookies()
            _open_login_url()
            if "/account/login" not in page.url:
                try:
                    page.evaluate("""() => { localStorage.clear(); sessionStorage.clear(); }""")
                except Exception:
                    pass
                context.clear_cookies()
                _open_login_url()
            mark_authenticated(page)
            return
        if context is None:
            _launch_browser(headless=HEADLESS_BROWSER)
        _open_login_url()
        if browser_headless and "/account/login" in page.url:
            _close_browser_context()
            _launch_browser(headless=True)
            _open_login_url()
        mark_authenticated(page)


def open_selection_page() -> None:
    with state_lock:
        if page is None or page.is_closed():
            raise RuntimeError("The browser is not open. Open the login page first.")
        if "buyin.jinritemai.com" not in page.url:
            raise RuntimeError("The current page is not the official Buyin site.")
        if "/account/login" in page.url:
            raise RuntimeError("The saved login session has expired. Click Open login page and scan the QR code.")
        selection = page.get_by_text("选品", exact=True).first
        selection.wait_for(state="visible", timeout=15000)
        selection.click()
        page.wait_for_timeout(3000)
        mark_authenticated(page)


def nested_value(item: object, keys: tuple[str, ...]) -> str | None:
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value)
        for value in item.values():
            found = nested_value(value, keys)
            if found:
                return found
    elif isinstance(item, list):
        for value in item:
            found = nested_value(value, keys)
            if found:
                return found
    return None


def first_visible(locator):
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    raise RuntimeError("The expected visible control was not found.")


def dom_click(locator) -> None:
    locator.evaluate(
        """element => {
            const target = element.closest('button,[role="button"],[class*="select"]') || element;
            target.click();
        }"""
    )


def _remove_high_sales_filter(body: dict) -> dict:
    """Remove the one-shot >=5000 sales filter from a captured request body."""
    cleaned = json.loads(json.dumps(body))
    filters = cleaned.get("filters")
    if isinstance(filters, dict):
        filters.pop("alliance_sales_30d", None)
        if not filters:
            cleaned.pop("filters", None)
    return cleaned


def direct_material_list(current_page: Page, body_override: dict | None = None) -> dict:
    spec = json.loads(REQUEST_SPEC_FILE.read_text(encoding="utf-8"))
    if body_override is None:
        body_override = json.loads(spec.get("body") or "{}")
    body_override = _remove_high_sales_filter(body_override)
    spec = {**spec, "body": json.dumps(body_override, ensure_ascii=False)}
    result = current_page.evaluate(
        """async spec => {
            const headers = {};
            if (spec.content_type) headers['content-type'] = spec.content_type;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), spec.timeout_ms || 15000);
            const response = await fetch(spec.url, {
                method: spec.method,
                headers,
                body: spec.body || undefined,
                credentials: 'include',
                signal: controller.signal
            });
            clearTimeout(timer);
            return {status: response.status, text: await response.text()};
        }""",
        {**spec, "timeout_ms": MATERIAL_RESPONSE_TIMEOUT_MS},
    )
    if result["status"] < 200 or result["status"] >= 300:
        raise RuntimeError(f"Direct material_list request failed: HTTP {result['status']}")
    payload = json.loads(result["text"])
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else result["text"][:300]
        raise RuntimeError(f"material_list returned no data: code={code}, msg={message}")
    return payload


def collect_material_pages(
    current_page: Page,
    first_payload: dict,
    minimum: int = 90,
    excluded_shop_ids: set[str] | None = None,
) -> tuple[list, list, int]:
    """Fetch enough promotions for new shops, skipping shops seen in history."""
    pages = [first_payload]
    promotions = []
    excluded_shop_ids = set(excluded_shop_ids or ())
    seen_shop_ids = set(excluded_shop_ids)
    skipped_existing_shop_ids: set[str] = set()

    def add_new_shop_promotions(payload: dict) -> None:
        for promotion in payload.get("data", {}).get("summary_promotions") or []:
            shop_id = nested_value(promotion, ("shop_id", "shopId"))
            if shop_id and shop_id in seen_shop_ids:
                if shop_id in excluded_shop_ids:
                    skipped_existing_shop_ids.add(shop_id)
                continue
            if shop_id:
                seen_shop_ids.add(shop_id)
            promotions.append(promotion)

    add_new_shop_promotions(first_payload)
    last_payload = first_payload
    cursor = 0
    size = 30
    request_body = None
    if USE_DIRECT_MATERIAL and REQUEST_SPEC_FILE.exists():
        try:
            request_body = json.loads(json.loads(REQUEST_SPEC_FILE.read_text(encoding="utf-8"))["body"])
        except (KeyError, TypeError, json.JSONDecodeError):
            request_body = None

    while len(promotions) < minimum and last_payload.get("data", {}).get("has_more"):
        expected_cursor = cursor + size

        next_payload = None
        if request_body is not None:
            try:
                next_payload = direct_material_list(
                    current_page,
                    {**request_body, "cursor": expected_cursor},
                )
            except Exception as exc:
                print(f"[direct pagination failed] {exc}; falling back to page scroll", flush=True)
                request_body = None

        if next_payload is None:
            def is_expected_page(response) -> bool:
                if MATERIAL_LIST_PATH not in response.url:
                    return False
                try:
                    body = json.loads(response.request.post_data or "{}")
                    return int(body.get("cursor") or 0) == expected_cursor
                except Exception:
                    return False

            try:
                with current_page.expect_response(
                    is_expected_page, timeout=MATERIAL_RESPONSE_TIMEOUT_MS
                ) as response_info:
                    current_page.evaluate(
                        """() => {
                            window.scrollTo(0, document.body.scrollHeight);
                            for (const element of document.querySelectorAll('*')) {
                                const style = getComputedStyle(element);
                                if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                                    element.scrollHeight > element.clientHeight) {
                                    element.scrollTop = element.scrollHeight;
                                }
                            }
                        }"""
                    )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "翻页时未捕获到商品列表请求，请确认选品页网络正常后再重试。"
                ) from exc
            response = response_info.value
            if not response.ok:
                raise RuntimeError(f"商品列表翻页请求失败：HTTP {response.status}")
            next_payload = response.json()
        pages.append(next_payload)
        add_new_shop_promotions(next_payload)
        last_payload = next_payload
        cursor = expected_cursor

    return promotions[:minimum], pages, len(skipped_existing_shop_ids)


def visible_control_in_ancestors(label, pattern):
    container = label
    for _ in range(4):
        container = container.locator("..").first
        candidates = container.get_by_text(pattern)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    raise RuntimeError("The expected control was not found in the filter row.")


def clear_high_sales_filter() -> None:
    """Unselect the temporary >=5000 sales filter after each collection."""
    global high_sales_filter_applied
    with state_lock:
        if page is None or page.is_closed():
            return
        try:
            control = first_visible(page.get_by_text(re.compile(r"(?:\u2265|>=)\s*5000")))
            selected = control.evaluate(
                """element => {
                    let current = element;
                    for (let index = 0; index < 6 && current; index += 1) {
                        if (current.getAttribute('aria-checked') === 'true' ||
                            current.getAttribute('aria-selected') === 'true') return true;
                        if (current.querySelector && current.querySelector('input:checked')) return true;
                        const className = typeof current.className === 'string' ? current.className : '';
                        if (/(?:active|selected|checked)/i.test(className)) return true;
                        current = current.parentElement;
                    }
                    return false;
                }"""
            )
            if high_sales_filter_applied or selected:
                dom_click(control)
                page.wait_for_timeout(500)
            high_sales_filter_applied = False
        except Exception as exc:
            print(f"[filter cleanup skipped] {exc}", flush=True)


def _reveal_contact_field(detail_page: Page, label_pattern: re.Pattern, contact_type: int) -> str | None:
    """Click one contact eye and return its value when the endpoint succeeds."""
    candidates = detail_page.get_by_text(label_pattern)
    label_candidates = None
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if candidate.is_visible():
            label_candidates = candidate
            break
    if label_candidates is None:
        return None

    row = label_candidates.locator("..").first
    containers = (row, row.locator("..").first)
    for container in containers:
        controls = container.locator("button, [role='button']")
        if not controls.count():
            controls = container.locator("[class*='eye'], [class*='icon']")
        candidate = None
        for index in range(controls.count() - 1, -1, -1):
            control = controls.nth(index)
            if control.is_visible():
                candidate = control
                break
        if candidate is None:
            continue
        for _ in range(1):
            global last_contact_request_at
            wait_ms = CONTACT_REQUEST_GAP_MS - int((time.monotonic() - last_contact_request_at) * 1000)
            if wait_ms > 0:
                time.sleep(wait_ms / 1000)
            try:
                with detail_page.expect_response(
                    lambda response: (
                        "/square_pc_api/common/contact" in response.url
                        and f"contact_type={contact_type}" in response.url
                    ),
                    timeout=CONTACT_RESPONSE_TIMEOUT_MS,
                ) as response_info:
                    last_contact_request_at = time.monotonic()
                    candidate.click(force=True)
                response = response_info.value
                try:
                    payload = response.json()
                except Exception as exc:
                    if _is_rate_limited_payload(None, response.status):
                        raise ContactRateLimitedError("操作过于频繁") from exc
                    raise
                if _is_rate_limited_payload(payload, response.status):
                    raise ContactRateLimitedError("操作过于频繁")
                data = payload.get("data") if isinstance(payload, dict) else None
                contact = data.get("contact") if isinstance(data, dict) else None
                if contact:
                    return str(contact)
                break
            except ContactRateLimitedError:
                raise
            except Exception:
                break
    return None


def reveal_public_contacts(detail_page: Page) -> dict[str, str | None]:
    """Read both optional contact fields from the product detail page."""
    detail_page.bring_to_front()
    detail_page.wait_for_timeout(DETAIL_SETTLE_MS)
    contact_candidates = detail_page.get_by_text("\u8054\u7cfb\u5546\u5bb6", exact=True)
    if not contact_candidates.count():
        return {"merchant_product_id": None, "mobile": None}
    contact = first_visible(contact_candidates)
    hover_target = contact
    for _ in range(2):
        hover_target.hover(force=True)
        detail_page.wait_for_timeout(CONTACT_HOVER_SETTLE_MS)
        values = {
            "merchant_product_id": _reveal_contact_field(
                detail_page, re.compile(r"(?:\u5fae\u4fe1\u53f7|merchant_product_id)"), 2
            ),
            "mobile": _reveal_contact_field(
                detail_page, re.compile(r"(?:\u624b\u673a\u53f7|mobile)"), 1
            ),
        }
        if any(values.values()):
            return values
        hover_target = hover_target.locator("..").first
    return {"merchant_product_id": None, "mobile": None}


def has_public_contact(item: dict) -> bool:
    """Return whether a collected shop has at least one usable public contact."""
    return any(
        str(item.get(field) or "").strip()
        for field in ("merchant_product_id", "mobile")
    )


def collect_selection_data(limit: int | None = None) -> dict:
    with state_lock:
        if page is None or page.is_closed():
            raise RuntimeError("The browser is not open.")
        if "buyin.jinritemai.com" not in page.url:
            raise RuntimeError("The current page is not the official Buyin site.")
        if "/account/login" in page.url:
            raise RuntimeError("The saved login session has expired. Click Open login page and scan the QR code.")
        mark_authenticated(page)

        if USE_DIRECT_MATERIAL and REQUEST_SPEC_FILE.exists():
            try:
                payload = direct_material_list(page)
            except Exception as exc:
                print(f"[direct request failed] {exc}; recapturing request")
                REQUEST_SPEC_FILE.unlink(missing_ok=True)
                payload = None
        else:
            payload = None

        if payload is None:
            monthly = first_visible(page.get_by_text(re.compile(r"\u6708\u9500")))
            dom_click(monthly)
            page.wait_for_timeout(500)
            high_sales = first_visible(page.get_by_text(re.compile(r"(?:\u2265|>=)\s*5000")))
            try:
                with page.expect_request(
                    lambda request: MATERIAL_LIST_PATH in request.url,
                    timeout=MATERIAL_RESPONSE_TIMEOUT_MS,
                ) as request_info, page.expect_response(
                    lambda response: MATERIAL_LIST_PATH in response.url,
                    timeout=MATERIAL_RESPONSE_TIMEOUT_MS,
                ) as response_info:
                    dom_click(high_sales)
                    global high_sales_filter_applied
                    high_sales_filter_applied = True
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "未捕获到商品列表请求，请确认选品页已加载完成后再重试。"
                ) from exc
            request = request_info.value
            response = response_info.value
            if not response.ok:
                raise RuntimeError(f"商品列表请求失败：HTTP {response.status}")
            content_type = request.headers.get("content-type")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            REQUEST_SPEC_FILE.write_text(
                json.dumps({
                    "url": request.url,
                    "method": request.method,
                    "content_type": content_type,
                    "body": request.post_data,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("material_list response data is empty")
        target = limit if limit is not None else int(os.getenv("COLLECT_LIMIT", "90"))
        target = max(1, min(target, 10000))
        candidate_target = min(10000, target + max(20, target // 2))
        existing_shop_ids = history_store.list_shop_ids()
        promotions, material_pages, skipped_existing = collect_material_pages(
            page,
            payload,
            candidate_target,
            excluded_shop_ids=existing_shop_ids,
        )
        print(
            f"[collect] target={target} candidates={len(promotions)} "
            f"pages={len(material_pages)} historical_shops={skipped_existing}",
            flush=True,
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "material_list_response.json").write_text(
            json.dumps({
                "code": 0,
                "data": {
                    "total": data.get("total"),
                    "has_more": len(promotions) < (data.get("total") or len(promotions)),
                    "summary_promotions": promotions,
                },
                "page_count": len(material_pages),
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        MATERIAL_PAGES_FILE.write_text(
            json.dumps(material_pages, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results = []
        skipped = 0
        no_contact = 0
        qualified_count = 0
        stopped_rate_limited = False
        skipped_items = []
        collection = history_store.start_collection()
        known_shop_ids = set(existing_shop_ids)
        for batch_start in range(0, len(promotions), DETAIL_CONCURRENCY):
            if qualified_count >= target:
                break

            pending = []
            batch = promotions[batch_start:batch_start + DETAIL_CONCURRENCY]
            for offset, promotion in enumerate(batch):
                index = batch_start + offset + 1
                commodity_id = nested_value(promotion, ("commodity_id", "commodityId"))
                product_id = nested_value(promotion, ("product_id", "productId"))
                shop_id = nested_value(promotion, ("shop_id", "shopId"))
                base_model = promotion.get("base_model") or {}
                product_info = base_model.get("product_info") or {}
                shop_info = base_model.get("shop_info") or {}
                image_urls = (product_info.get("main_img") or {}).get("url_list") or []
                month_sale = (product_info.get("month_sale") or {}).get("origin")
                item = {
                    "commodity_id": commodity_id,
                    "product_id": product_id,
                    "shop_id": shop_id,
                    "name": nested_value(promotion, ("name", "product_name", "title")),
                    "image_url": image_urls[0] if image_urls else None,
                    "shop_name": nested_value(shop_info, ("shop_name", "name")),
                    "shop_score": nested_value(shop_info.get("shop_score_info"), ("score",)),
                    "month_sale": month_sale,
                    "raw": promotion,
                }
                if not commodity_id or not product_id:
                    skipped += 1
                    skipped_items.append({
                        "name": item["name"],
                        "reason": "missing commodity_id or product_id",
                        "raw": promotion,
                    })
                    continue

                normalized_shop_id = str(shop_id) if shop_id else None
                if normalized_shop_id and normalized_shop_id in known_shop_ids:
                    skipped += 1
                    skipped_items.append({
                        "name": item["name"],
                        "commodity_id": commodity_id,
                        "product_id": product_id,
                        "shop_id": normalized_shop_id,
                        "reason": "shop already exists in history",
                    })
                    continue
                if normalized_shop_id:
                    known_shop_ids.add(normalized_shop_id)

                query = urlencode({
                    "commodity_id": commodity_id,
                    "commodity_location": 1,
                    "id": commodity_id,
                    "product_id": product_id,
                    "shop_id": shop_id or "",
                })
                detail_page = context.new_page()
                try:
                    # Start a batch of navigations before waiting for any one page.
                    detail_page.goto(
                        "https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting?" + query,
                        wait_until="commit",
                        timeout=DETAIL_PAGE_TIMEOUT_MS,
                    )
                    pending.append((index, item, commodity_id, product_id, detail_page))
                except Exception as exc:
                    skipped += 1
                    skipped_items.append({
                        "name": item["name"],
                        "commodity_id": commodity_id,
                        "product_id": product_id,
                        "reason": str(exc),
                    })
                    detail_page.close()

            for pending_position, (index, item, commodity_id, product_id, detail_page) in enumerate(pending):
                if qualified_count >= target:
                    detail_page.close()
                    continue
                try:
                    detail_page.wait_for_load_state("domcontentloaded", timeout=DETAIL_PAGE_TIMEOUT_MS)
                    detail_page.wait_for_timeout(DETAIL_SETTLE_MS)
                    contacts = reveal_public_contacts(detail_page)
                    item["merchant_product_id"] = contacts["merchant_product_id"]
                    item["mobile"] = contacts["mobile"]
                    if not has_public_contact(item):
                        no_contact += 1
                        if no_contact == 1:
                            DATA_DIR.mkdir(parents=True, exist_ok=True)
                            detail_page.screenshot(
                                path=str(DATA_DIR / "debug_detail_first_skipped.png"),
                                full_page=True,
                            )
                    else:
                        qualified_count += 1
                    item["detail_url"] = detail_page.url
                except ContactRateLimitedError as exc:
                    stopped_rate_limited = True
                    skipped += 1
                    skipped_items.append({
                        "name": item["name"],
                        "commodity_id": commodity_id,
                        "product_id": product_id,
                        "reason": str(exc),
                    })
                except Exception as exc:
                    skipped += 1
                    skipped_items.append({
                        "name": item["name"],
                        "commodity_id": commodity_id,
                        "product_id": product_id,
                        "reason": str(exc),
                    })
                    continue
                finally:
                    detail_page.close()
                if stopped_rate_limited:
                    for _, _, _, _, remaining_page in pending[pending_position + 1:]:
                        remaining_page.close()
                    break
                history_store.save_records(
                    [item],
                    collection_id=collection["collection_id"],
                    collected_at=collection["collected_at"],
                )
                results.append(item)
                print(
                    f"[collect] processed {index}/{len(promotions)} "
                    f"qualified={qualified_count}/{target}",
                    flush=True,
                )

            if stopped_rate_limited:
                break

        output = DATA_DIR / "selection_results.json"
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        SKIPPED_FILE.write_text(
            json.dumps(skipped_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        history = {**collection, "count": len(results)}
        return {
            "count": len(results),
            "qualified_count": qualified_count,
            "skipped": skipped,
            "no_contact": no_contact,
            "stopped_rate_limited": stopped_rate_limited,
            "skipped_existing_shops": skipped_existing,
            "requested_new_shops": target,
            "file": str(output),
            "collection_id": history["collection_id"],
            "history_database": str(HISTORY_DATABASE),
            "message": (
                f"采集完成：新增店铺 {len(results)} 个，"
                f"跳过历史店铺 {skipped_existing} 个。"
                + ("检测到操作过于频繁，本次任务已停止，已完成的数据已保存。" if stopped_rate_limited else "")
                + (f"目标 {target} 个，当前可用 {qualified_count} 个。" if qualified_count < target else "")
            ),
        }


def close_browser() -> None:
    with state_lock:
        _close_browser_context()


def logout_session() -> None:
    """Clear the persisted Buyin session so the next login starts fresh."""
    with state_lock:
        if context is not None:
            for open_page in list(context.pages):
                try:
                    open_page.evaluate(
                        """() => {
                            localStorage.clear();
                            sessionStorage.clear();
                        }"""
                    )
                except Exception:
                    pass
            try:
                context.clear_cookies()
            except Exception:
                pass
        _close_browser_context()
        AUTH_MARKER.unlink(missing_ok=True)


def toggle_browser_mode() -> dict[str, object]:
    """Restart the current browser context in the opposite visibility mode."""
    global HEADLESS_BROWSER
    with state_lock:
        if page is None or page.is_closed() or context is None:
            raise RuntimeError("浏览器尚未打开")
        target_headless = not browser_headless
        if not target_headless and os.name != "nt" and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            raise RuntimeError("Linux server has no display environment for foreground mode")
        current_url = page.url
        HEADLESS_BROWSER = target_headless
        _close_browser_context()
        _launch_browser(headless=target_headless)
        if current_url:
            page.goto(current_url, wait_until="commit", timeout=DETAIL_PAGE_TIMEOUT_MS)
            page.wait_for_load_state("domcontentloaded", timeout=DETAIL_PAGE_TIMEOUT_MS)
        return {"headless": target_headless, "url": page.url}


def _excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _excel_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    # Excel cells cannot contain control characters and are limited to 32,767 chars.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[:32767]


def build_history_workbook(records: list[dict]) -> bytes:
    """Build a minimal standards-compliant XLSX workbook without extra packages."""
    fields: list[str] = []
    for record in records:
        for field in record:
            if field not in fields:
                fields.append(field)
    if not fields:
        fields = [
            "commodity_id", "product_id", "shop_id", "merchant_product_id", "mobile",
            "name", "image_url", "shop_name", "shop_score", "month_sale", "detail_url",
            "history_id", "collection_id", "collected_at", "raw",
        ]

    def row_xml(row_number: int, values: list[object]) -> str:
        cells = []
        for column_number, value in enumerate(values, start=1):
            text = escape(_excel_value(value))
            reference = f"{_excel_column_name(column_number)}{row_number}"
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
            )
        return f'<row r="{row_number}">{"".join(cells)}</row>'

    sheet_rows = [row_xml(1, fields)]
    sheet_rows.extend(
        row_xml(row_number, [record.get(field) for field in fields])
        for row_number, record in enumerate(records, start=2)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="History" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    package_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


RESULTS_PAGE = RESULTS_PAGE.replace(
    "<th>\u5546\u5bb6\u5546\u54c1 ID</th><th>\u91c7\u96c6\u65f6\u95f4</th>",
    "<th>\u5546\u5bb6\u5546\u54c1 ID</th><th>\u624b\u673a\u53f7</th><th>\u91c7\u96c6\u65f6\u95f4</th>",
).replace(
    "+esc(item.merchant_product_id||'-')+'</td><td>'+esc(item.collected_at||'-')",
    "+esc(item.merchant_product_id||'-')+'</td><td>'+esc(item.mobile||'-')+'</td><td>'+esc(item.collected_at||'-')",
).replace(
    '</button><a class="btn" href="/">',
    '<input class="input" id="shopScoreLt" type="number" min="0" step="0.1" placeholder="评分低于"><input class="input" id="monthSaleGt" type="number" min="0" step="1" placeholder="商品销量大于"><a class="btn" href="/">',
).replace(
    "const q=encodeURIComponent(document.getElementById('query').value.trim());const items=await fetch('/api/results?limit='+size+'&offset='+offset+'&q='+q).then(r=>r.json());",
    "const q=document.getElementById('query').value.trim();const scoreLt=document.getElementById('shopScoreLt').value.trim();const saleGt=document.getElementById('monthSaleGt').value.trim();const params=new URLSearchParams({limit:String(size),offset:String(offset),q});if(scoreLt)params.set('shop_score_lt',scoreLt);if(saleGt)params.set('month_sale_gt',saleGt);const items=await fetch('/api/results?'+params).then(r=>r.json());",
)

RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".panel-head:has(.toolbar){display:grid;grid-template-columns:140px minmax(0,1fr);gap:20px;align-items:center}.panel-head .toolbar{min-width:0;display:grid;grid-template-columns:minmax(260px,1fr) 68px 150px 170px 100px;gap:12px;align-items:center;margin-left:0}.panel-head .toolbar .input{width:100%;min-width:0;height:36px;padding:0 12px;border:1px solid #d5dce7;border-radius:5px;background:#fff;color:var(--text);font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}.panel-head .toolbar .input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.12)}.panel-head .toolbar .btn{width:100%;white-space:nowrap;height:36px;padding:0 12px;border-radius:5px}.panel-head .toolbar .btn.primary{font-weight:600}.panel-head .toolbar a{justify-self:stretch;text-align:center;text-decoration:none}@media(max-width:1000px){.panel-head:has(.toolbar){grid-template-columns:110px minmax(0,1fr);gap:14px}.panel-head .toolbar{grid-template-columns:minmax(200px,1fr) 68px 135px 150px 96px;gap:8px}}@media(max-width:800px){.panel-head:has(.toolbar){display:flex;align-items:flex-start;flex-direction:column;gap:12px}.panel-head .toolbar{width:100%;display:grid;grid-template-columns:1fr 1fr;gap:10px}.panel-head .toolbar #query{grid-column:1 / -1}.panel-head .toolbar .btn.primary{grid-column:1}.panel-head .toolbar a{justify-self:stretch;text-align:center}}</style>",
)

RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".history-hero{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:22px}.history-hero h2{font-size:26px;letter-spacing:-.02em;margin:0 0 5px}.history-hero p{color:var(--muted);margin:0}.history-tag{padding:7px 10px;background:#eaf1ff;color:#275dc9;border-radius:5px;font-size:12px;font-weight:600}.history-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:22px}.history-stat{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px 18px;position:relative;overflow:hidden}.history-stat:after{content:\"\";position:absolute;width:56px;height:56px;border:7px solid #edf3ff;border-radius:50%;right:-14px;top:-17px}.history-stat-label{color:var(--muted);font-size:12px}.history-stat-value{font-size:24px;font-weight:700;margin-top:7px}.history-stat-note{font-size:12px;color:#0f9f7a;margin-top:4px}.history-thumb-fallback{width:56px;height:56px;border-radius:7px;display:grid;place-items:center;background:linear-gradient(135deg,#e9f0ff,#dce8ff);color:#4c73c6;font-size:18px}.history-id{font-family:Consolas,monospace;font-size:12px;color:#64748b}.history-date{white-space:nowrap;color:#536174;font-size:12px}.history-table tbody tr:hover{background:#f8fbff}@media(max-width:800px){.history-stats{grid-template-columns:1fr}.history-hero{align-items:flex-start;gap:14px;flex-direction:column}}</style>",
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    '<div class="crumb">工作台 / 历史数据</div>',
    '<div class="crumb">工作台 / 历史数据</div><div class="history-hero"><div><h2>历史资产库</h2><p>集中查看和检索已采集的商品数据，支持按关键词快速定位。</p></div><span class="history-tag">SQLite indexed storage</span></div><div class="history-stats"><div class="history-stat"><div class="history-stat-label">累计商品记录</div><div class="history-stat-value" id="historyTotal">-</div><div class="history-stat-note">持续追加保存</div></div><div class="history-stat"><div class="history-stat-label">当前页结果</div><div class="history-stat-value" id="historyPage">-</div><div class="history-stat-note">按采集时间倒序</div></div><div class="history-stat"><div class="history-stat-label">数据状态</div><div class="history-stat-value">已索引</div><div class="history-stat-note">SQLite WAL 持久化</div></div></div>',
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "<table><thead>",
    '<table class="history-table"><thead>',
).replace(
    "<img src=\"'+esc(item.image_url)+'\" loading=\"lazy\">",
    '<img class="thumb" src="\'+esc(item.image_url)+\'" loading="lazy" onerror="this.outerHTML=\'<div class=\\\"history-thumb-fallback\\\">▦</div>\'">',
).replace(
    "<td>'+esc(item.merchant_product_id||'-')+'</td>",
    "<td><span class=\"history-id\">'+esc(item.merchant_product_id||'-')+'</span></td>",
).replace(
    "<td>'+esc(item.collected_at||'-').replace('T',' ').slice(0,19)+'</td>",
    "<td><span class=\"history-date\">'+esc(item.collected_at||'-').replace('T',' ').slice(0,19)+'</span></td>",
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".panel-head:has(.toolbar){display:block;padding:18px 20px}.panel-head:has(.toolbar)>h2{margin-bottom:14px}.panel-head .toolbar{display:grid;grid-template-columns:minmax(300px,1fr) 92px 190px 210px 122px;gap:10px;width:100%;align-items:center}.panel-head .toolbar .input{width:100%;min-width:0}.panel-head .toolbar #query{grid-column:1}.panel-head .toolbar #shopScoreLt,.panel-head .toolbar #monthSaleGt{width:100%;min-width:0}.panel-head .toolbar .btn,.panel-head .toolbar a{width:100%;min-width:0}.panel-head .toolbar #query{font-size:13px}.panel-head .toolbar #shopScoreLt,.panel-head .toolbar #monthSaleGt{font-size:12px}@media(max-width:1120px){.panel-head .toolbar{grid-template-columns:minmax(260px,1fr) 84px 170px 185px 110px}}@media(max-width:800px){.panel-head:has(.toolbar)>h2{margin-bottom:12px}.panel-head .toolbar{display:grid;grid-template-columns:1fr 1fr;gap:10px}.panel-head .toolbar #query{grid-column:1 / -1}.panel-head .toolbar .btn.primary{grid-column:auto}.panel-head .toolbar a{grid-column:1 / -1}}</style>",
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "document.getElementById('summary').textContent='本页 '+items.length+' 条';",
    "document.getElementById('summary').textContent='本页 '+items.length+' 条';document.getElementById('historyPage').textContent=items.length.toLocaleString();",
).replace(
    "load()</script></body></html>",
    "document.getElementById('query').placeholder='搜索商品、店铺或 ID';document.getElementById('shopScoreLt').placeholder='店铺评分低于，例如 4.5';document.getElementById('monthSaleGt').placeholder='商品销量大于，例如 1000';async function loadHistoryTotal(){try{const data=await fetch('/api/history').then(r=>r.json());document.getElementById('historyTotal').textContent=(data.count||0).toLocaleString()}catch(error){document.getElementById('historyTotal').textContent='-'}}loadHistoryTotal();load()</script></body></html>",
)

# Final layout overrides for the history filters.
RESULTS_PAGE = RESULTS_PAGE.replace(
    '<header class="top">',
    '<header class="top"><a class="console-link" href="/">&#36820;&#22238;&#25511;&#21046;&#21488;</a>',
).replace(
    "</style>",
    ".top .console-link{height:34px;display:inline-flex;align-items:center;padding:0 12px;border:1px solid #cbd5e1;border-radius:4px;color:#334155;background:#fff;text-decoration:none;font-size:13px}.top .console-link:hover{border-color:var(--blue);color:var(--blue)}.panel-head:has(.toolbar){display:block;padding:18px 20px}.panel-head:has(.toolbar)>h2{margin-bottom:14px}.panel-head .toolbar{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 92px;gap:10px;width:100%;align-items:center}.panel-head .toolbar .input{width:100%;min-width:0}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{display:none}.panel-head .toolbar .btn.primary{grid-column:3;grid-row:1;width:100%}.panel-head .toolbar>a{display:none}@media(max-width:800px){.top .console-link{padding:0 9px;font-size:12px}.panel-head:has(.toolbar)>h2{margin-bottom:12px}.panel-head .toolbar{grid-template-columns:1fr 1fr;gap:10px}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar .btn.primary{grid-column:1 / -1;grid-row:2}}</style>",
)

RESULTS_PAGE = RESULTS_PAGE.replace(
    '<header class="top"><a class="console-link" href="/">&#36820;&#22238;&#25511;&#21046;&#21488;</a>',
    '<header class="top">',
    1,
).replace(
    '</header>',
    '<a class="console-link" href="/">&#36820;&#22238;&#25511;&#21046;&#21488;</a></header>',
    1,
)
RESULTS_PAGE = re.sub(
    r'id="query" placeholder="[^"]*"',
    'id="query" placeholder="商品 ID"',
    RESULTS_PAGE,
    count=1,
)
RESULTS_PAGE = re.sub(
    r"document\.getElementById\('query'\)\.placeholder='[^']*'",
    "document.getElementById('query').placeholder='商品 ID'",
    RESULTS_PAGE,
    count=1,
)

# Rebuild the toolbar after the legacy string substitutions above so stale
# buttons cannot remain in the layout or overlap the filter inputs.
RESULTS_PAGE = re.sub(
    r'<div class="toolbar">.*?</div>',
    '<div class="toolbar"><input class="input" id="query" type="text" placeholder="商品 ID"><input class="input" id="shopScoreLt" type="number" min="0" step="0.1" placeholder="评分低于，例如 4.5"><button class="btn primary" onclick="search()">查询</button></div>',
    RESULTS_PAGE,
    count=1,
    flags=re.DOTALL,
)
RESULTS_PAGE = re.sub(
    r'<header class="top">.*?</header>',
    '<header class="top"><h1>历史数据</h1><span class="meta">SQLite indexed storage</span><a class="console-link" href="/">返回控制台</a></header>',
    RESULTS_PAGE,
    count=1,
    flags=re.DOTALL,
)
RESULTS_PAGE = re.sub(
    r'<div class="toolbar">.*?</div>',
    '<div class="toolbar"><input class="input" id="query" type="text" placeholder="商品 ID"><input class="input" id="shopScoreLt" type="number" min="0" step="0.1" placeholder="评分低于，例如 4.5"><input class="input" id="monthSaleGt" type="number" min="0" step="1" placeholder="销量大于，例如 1000"><button class="btn primary" onclick="search()">查询</button></div>',
    RESULTS_PAGE,
    count=1,
    flags=re.DOTALL,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".panel-head .toolbar{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1fr) 92px}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{display:block;grid-column:3;grid-row:1}.panel-head .toolbar .btn.primary{grid-column:4;grid-row:1}@media(max-width:800px){.panel-head .toolbar{grid-template-columns:1fr 1fr}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{grid-column:1;grid-row:2}.panel-head .toolbar .btn.primary{grid-column:2;grid-row:2}}</style>",
)
RESULTS_PAGE = re.sub(
    r' onerror="this\.outerHTML=.*?\'">',
    "",
    RESULTS_PAGE,
)

RESULTS_PAGE = re.sub(
    r'<div class="toolbar">.*?</div>',
    '<div class="toolbar"><input class="input" id="query" type="text" placeholder="商品 ID"><input class="input" id="shopScoreLt" type="number" min="0" step="0.1" placeholder="评分低于，例如 4.5"><input class="input" id="monthSaleGt" type="number" min="0" step="1" placeholder="销量大于，例如 1000"><button class="btn primary" id="queryBtn" onclick="search()">查询</button><button class="btn" id="exportBtn" onclick="exportResults()">导出 Excel</button></div>',
    RESULTS_PAGE,
    count=1,
    flags=re.DOTALL,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".panel-head .toolbar{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1fr) 92px 110px}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{display:block;grid-column:3;grid-row:1}.panel-head .toolbar #queryBtn{grid-column:4;grid-row:1}.panel-head .toolbar #exportBtn{grid-column:5;grid-row:1;white-space:nowrap}@media(max-width:800px){.panel-head .toolbar{grid-template-columns:1fr 1fr}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{grid-column:1;grid-row:2}.panel-head .toolbar #queryBtn{grid-column:2;grid-row:2}.panel-head .toolbar #exportBtn{grid-column:1 / -1;grid-row:3}}</style>",
    1,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "load()</script>",
    "async function exportResults(){const button=document.getElementById('exportBtn');const params=new URLSearchParams();const query=document.getElementById('query').value.trim();const scoreLt=document.getElementById('shopScoreLt').value.trim();const saleGt=document.getElementById('monthSaleGt').value.trim();if(query)params.set('q',query);if(scoreLt)params.set('shop_score_lt',scoreLt);if(saleGt)params.set('month_sale_gt',saleGt);button.disabled=true;try{const response=await fetch('/api/results/export?'+params.toString());if(!response.ok)throw new Error('导出失败');const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='history-export.xlsx';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(error){showModal('导出失败',error.message)}finally{button.disabled=false}}load()</script>",
    1,
)

RESULTS_PAGE = re.sub(
    r'(<button class="btn" id="exportBtn" onclick="exportResults\(\)">.*?</button>)</div>',
    r'\1<button class="btn danger" id="deleteBtn" disabled onclick="deleteSelected()">批量删除</button></div>',
    RESULTS_PAGE,
    count=1,
    flags=re.DOTALL,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    '<table class="history-table"><thead><tr>',
    '<table class="history-table"><thead><tr><th class="select-col"><input id="selectAll" type="checkbox" aria-label="全选"></th>',
    1,
).replace(
    "items.map(item=>'<tr><td>'+",
    "items.map(item=>'<tr><td class=\"select-cell\"><input class=\"row-select\" type=\"checkbox\" value=\"'+esc(item.history_id)+'\"></td><td>'+",
    1,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".history-table .select-col,.history-table .select-cell{width:42px;padding-left:12px;padding-right:8px;text-align:center}.history-table .select-col input,.history-table .select-cell input{width:16px;height:16px;margin:0;vertical-align:middle}.panel-head .toolbar{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1fr) 92px 110px 110px}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{display:block;grid-column:3;grid-row:1}.panel-head .toolbar #queryBtn{grid-column:4;grid-row:1}.panel-head .toolbar #exportBtn{grid-column:5;grid-row:1}.panel-head .toolbar #deleteBtn{grid-column:6;grid-row:1;white-space:nowrap}.panel-head .toolbar #deleteBtn:disabled{opacity:.45;cursor:not-allowed}@media(max-width:800px){.panel-head .toolbar{grid-template-columns:1fr 1fr}.panel-head .toolbar #query{grid-column:1;grid-row:1}.panel-head .toolbar #shopScoreLt{grid-column:2;grid-row:1}.panel-head .toolbar #monthSaleGt{grid-column:1;grid-row:2}.panel-head .toolbar #queryBtn{grid-column:2;grid-row:2}.panel-head .toolbar #exportBtn{grid-column:1 / -1;grid-row:3}.panel-head .toolbar #deleteBtn{grid-column:1 / -1;grid-row:4}}</style>",
    1,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "document.getElementById('empty').hidden=items.length>0;",
    "syncSelection();document.getElementById('empty').hidden=items.length>0;",
    1,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "load()</script>",
    "function selectedHistoryIds(){return Array.from(document.querySelectorAll('.row-select:checked')).map(input=>input.value)}function syncSelection(){const boxes=Array.from(document.querySelectorAll('.row-select'));const checked=boxes.filter(box=>box.checked).length;const selectAll=document.getElementById('selectAll');selectAll.checked=boxes.length>0&&checked===boxes.length;selectAll.indeterminate=checked>0&&checked<boxes.length;document.getElementById('deleteBtn').disabled=checked===0}document.addEventListener('change',event=>{if(event.target.id==='selectAll'){document.querySelectorAll('.row-select').forEach(box=>{box.checked=event.target.checked})}if(event.target.classList.contains('row-select')||event.target.id==='selectAll')syncSelection()});async function deleteSelected(){const ids=selectedHistoryIds();if(!ids.length)return;if(!window.confirm('确定删除选中的 '+ids.length+' 条历史记录吗？'))return;const button=document.getElementById('deleteBtn');button.disabled=true;try{const response=await fetch('/api/results/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'删除失败');window.alert('已删除 '+data.deleted+' 条历史记录');offset=0;await load();await loadHistoryTotal()}catch(error){window.alert(error.message)}finally{syncSelection()}}load()</script>",
    1,
)
RESULTS_PAGE = RESULTS_PAGE.replace(
    "</style>",
    ".panel-head .toolbar #deleteBtn{color:#b42318;border-color:#f0b5b0}.panel-head .toolbar #deleteBtn:disabled{color:#94a3b8;border-color:#d8dee8}</style>",
    1,
)

RESULTS_PAGE = RESULTS_PAGE.replace(
    '<a class="nav-item" href="/">采集控制台</a>',
    '<!-- ADMIN_CONSOLE_NAV -->',
    1,
)

LOGIN_PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>系统登录</title>
<style>
:root{--navy:#172235;--blue:#2563eb;--bg:#f3f5f8;--line:#d9e0ea;--text:#1f2937;--muted:#64748b;--red:#b42318}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;display:grid;place-items:center}.login{width:min(420px,calc(100% - 32px));background:#fff;border:1px solid var(--line);border-radius:8px;padding:32px;box-shadow:0 14px 40px rgba(15,23,42,.08)}h1{margin:0;font-size:22px}.subtitle{margin:8px 0 26px;color:var(--muted)}.field{display:flex;flex-direction:column;gap:7px;margin-bottom:16px}.field label{font-size:13px;color:#475569}.field input{height:42px;border:1px solid #cbd5e1;border-radius:5px;padding:0 12px;font:inherit}.btn{width:100%;height:42px;border:1px solid var(--blue);border-radius:5px;background:var(--blue);color:#fff;font:inherit;cursor:pointer}.error{margin:0 0 16px;padding:10px 12px;background:#fff1f0;color:var(--red);border:1px solid #f3c1bd;border-radius:5px}
</style>
</head>
<body><main class="login"><h1>选品采集系统</h1><p class="subtitle">请输入账号和密码登录</p>{% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post" action="/auth/login"><div class="field"><label for="username">账号</label><input id="username" name="username" autocomplete="username" required autofocus></div><div class="field"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required></div><button class="btn" type="submit">登录</button></form></main></body>
</html>
"""

ADMIN_ACCOUNTS_PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>账号管理</title>
<style>
:root{--navy:#172235;--blue:#2563eb;--bg:#f3f5f8;--line:#e5e7eb;--text:#1f2937;--muted:#64748b;--red:#b42318}.shell{min-height:100vh;display:flex}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}.side{width:232px;background:var(--navy);color:#dbe5f4;padding:22px 14px;flex:none}.brand{font-size:18px;font-weight:700;color:#fff;padding:0 12px 26px}.brand small{display:block;color:#91a1b8;font-size:11px;font-weight:400;margin-top:4px}.nav-title{padding:12px;font-size:11px;color:#8191a8}.nav-item{display:block;padding:10px 12px;border-radius:5px;color:#c5d2e4;text-decoration:none;margin:3px 0}.nav-item.active,.nav-item:hover{background:#26364e;color:#fff}.main{flex:1;min-width:0}.top{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 34px}.top h1{margin:0;font-size:18px}.top .user{margin-left:auto;color:var(--muted)}.content{max-width:1100px;margin:0 auto;padding:28px 34px}.panel{background:#fff;border:1px solid var(--line);border-radius:6px;margin-bottom:20px;padding:22px}.panel h2{font-size:16px;margin:0 0 18px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:12px;color:var(--muted)}.field input{height:38px;border:1px solid #cbd5e1;border-radius:4px;padding:0 10px;font:inherit}.btn{height:38px;border:1px solid #cbd5e1;background:#fff;border-radius:4px;padding:0 14px;font:inherit;cursor:pointer}.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue)}.form-actions{display:flex;align-items:end;gap:12px;margin-top:16px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{background:#f8fafc;color:#475569}.muted{color:var(--muted)}@media(max-width:800px){.side{width:70px;padding:18px 8px}.brand{font-size:0;padding:0 10px 24px}.brand:before{content:"AI";font-size:18px}.brand small,.nav-title{display:none}.nav-item{font-size:0;text-align:center}.nav-item:before{content:"●";font-size:15px}.top{padding:0 18px}.content{padding:20px 16px}.form-grid{grid-template-columns:1fr}}
</style>
</head>
<body><div class="shell"><aside class="side"><div class="brand">选品采集<small>Buyin Data Console</small></div><div class="nav-title">工作台</div><a class="nav-item" href="/">采集控制台</a><a class="nav-item" href="/results">历史数据</a><a class="nav-item active" href="/admin/accounts">账号管理</a></aside><main class="main"><header class="top"><h1>账号管理</h1><span class="user">管理员：{{ current_user.username }}　<a href="/auth/logout">退出系统</a></span></header><section class="content"><div class="panel"><h2>创建普通账号</h2><form method="post" action="/admin/accounts/create"><div class="form-grid"><div class="field"><label for="new_username">账号</label><input id="new_username" name="username" minlength="2" maxlength="32" required></div><div class="field"><label for="new_password">初始密码</label><input id="new_password" name="password" type="password" minlength="6" required></div></div><div class="form-actions"><button class="btn primary" type="submit">创建账号</button></div></form></div><div class="panel"><h2>修改账号密码</h2><form method="post" action="/admin/accounts/password"><div class="form-grid"><div class="field"><label for="password_username">账号</label><select id="password_username" name="username" required>{% for user in users %}<option value="{{ user.username }}">{{ user.username }}{% if user.role == 'admin' %}（管理员）{% endif %}</option>{% endfor %}</select></div><div class="field"><label for="changed_password">新密码</label><input id="changed_password" name="password" type="password" minlength="6" required></div></div><div class="form-actions"><button class="btn primary" type="submit">保存密码</button></div></form></div><div class="panel"><h2>已有账号</h2><div class="table-wrap"><table><thead><tr><th>账号</th><th>权限</th><th>创建时间</th><th>修改时间</th></tr></thead><tbody>{% for user in users %}<tr><td>{{ user.username }}</td><td>{{ '管理员' if user.role == 'admin' else '普通用户' }}</td><td>{{ user.created_at }}</td><td>{{ user.updated_at }}</td></tr>{% endfor %}</tbody></table></div></div></section></main></div></body>
</html>
"""

ADMIN_ACCOUNTS_PAGE = ADMIN_ACCOUNTS_PAGE.replace(
    "</style>",
    ".notice{padding:10px 12px;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;border-radius:5px;margin:0 0 20px}</style>",
    1,
).replace(
    '<section class="content">',
    '<section class="content">{% if message %}<p class="notice">{{ message }}</p>{% endif %}',
    1,
)

@app.route("/login", methods=["GET", "POST"])
@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    current = _current_user()
    if current is not None:
        return redirect("/" if current["role"] == "admin" else "/results")
    if request.method == "GET":
        return render_template_string(LOGIN_PAGE, error=None)

    body = request.get_json(silent=True) if request.is_json else request.form
    username = str((body or {}).get("username", "")).strip()
    password = str((body or {}).get("password", ""))
    user = auth_store.authenticate(username, password)
    if user is None:
        error = "账号或密码错误"
        if request.is_json:
            return jsonify(ok=False, error=error), 401
        return render_template_string(LOGIN_PAGE, error=error), 401

    new_session_id = secrets.token_urlsafe(32)
    if user["role"] == "admin":
        try:
            _claim_admin_session(new_session_id)
        except RuntimeError as exc:
            if request.is_json:
                return jsonify(ok=False, error=str(exc)), 409
            return render_template_string(LOGIN_PAGE, error=str(exc)), 409
    session.clear()
    session["username"] = user["username"]
    session["auth_session_id"] = new_session_id
    session["role"] = user["role"]
    return redirect("/" if user["role"] == "admin" else "/results")


@app.route("/auth/logout", methods=["GET", "POST"])
def auth_logout():
    session_id = session.get("auth_session_id")
    username = session.get("username")
    if username == "admin" and session_id:
        with auth_state_lock:
            auth_store.clear_admin_session(str(session_id))
    session.clear()
    if request.is_json:
        return jsonify(ok=True)
    return redirect("/login")


def _render_accounts(message: str | None = None, status: int = 200):
    response = make_response(render_template_string(
        ADMIN_ACCOUNTS_PAGE,
        users=auth_store.list_users(),
        current_user=g.current_user,
        message=message,
    ), status)
    return response


@app.get("/admin/accounts")
def accounts_page():
    denied = _require_admin()
    if denied:
        return denied
    return _render_accounts()


@app.post("/admin/accounts/create")
def create_account():
    denied = _require_admin()
    if denied:
        return denied
    username = str(request.form.get("username", "")).strip()
    password = str(request.form.get("password", ""))
    if username.lower() == "admin":
        return _render_accounts("admin 是系统保留管理员账号，不能重复创建。", 400)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{2,32}", username):
        return _render_accounts("账号只能包含字母、数字、下划线或短横线，长度为 2 到 32 位。", 400)
    if len(password) < 6:
        return _render_accounts("密码长度不能少于 6 位。", 400)
    try:
        auth_store.create_user(username, password)
    except ValueError as exc:
        return _render_accounts(str(exc), 400)
    return _render_accounts(f"账号 {username} 已创建。")


@app.post("/admin/accounts/password")
def change_account_password():
    denied = _require_admin()
    if denied:
        return denied
    username = str(request.form.get("username", "")).strip()
    password = str(request.form.get("password", ""))
    if len(password) < 6:
        return _render_accounts("密码长度不能少于 6 位。", 400)
    try:
        auth_store.update_password(username, password)
    except ValueError as exc:
        return _render_accounts(str(exc), 400)
    return _render_accounts(f"账号 {username} 的密码已修改。")


@app.get("/")
def index():
    denied = _require_admin()
    if denied:
        return redirect("/results")
    return render_template_string(PAGE)


@app.get("/results")
def results_page():
    admin_nav = '<a class="nav-item" href="/">采集控制台</a><a class="nav-item" href="/admin/accounts">账号管理</a>' if g.current_user["role"] == "admin" else ""
    page = RESULTS_PAGE.replace("<!-- ADMIN_CONSOLE_NAV -->", admin_nav)
    page = page.replace(
        '<span class="meta">SQLite indexed storage</span>',
        f'<span class="meta">当前账号：{g.current_user["username"]}　<a href="/auth/logout">退出系统</a></span>',
        1,
    )
    if g.current_user["role"] != "admin":
        page = page.replace(
            '<button class="btn danger" id="deleteBtn"',
            '<button class="btn danger" style="display:none" id="deleteBtn"',
            1,
        )
    return render_template_string(page, current_user=g.current_user)


@app.get("/api/results")
def results_api():
    return jsonify(history_store.list_records(
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
        query=request.args.get("q"),
        collection_id=request.args.get("collection_id"),
        shop_score_lt=request.args.get("shop_score_lt", type=float),
        month_sale_gt=request.args.get("month_sale_gt", type=float),
    ))


@app.get("/api/results/export")
def results_export():
    records = history_store.list_all_records(
        query=request.args.get("q"),
        collection_id=request.args.get("collection_id"),
        shop_score_lt=request.args.get("shop_score_lt", type=float),
        month_sale_gt=request.args.get("month_sale_gt", type=float),
    )
    response = make_response(build_history_workbook(records))
    response.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response.headers["Content-Disposition"] = "attachment; filename=history-export.xlsx"
    return response


@app.post("/api/results/delete")
def results_delete():
    denied = _require_admin()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    history_ids = body.get("ids")
    if not isinstance(history_ids, list):
        return jsonify(ok=False, error="请选择要删除的历史记录"), 400
    deleted = history_store.delete_records(history_ids)
    return jsonify(ok=True, deleted=deleted)


@app.get("/api/history")
def history_api():
    collection_id = request.args.get("collection_id")
    return jsonify({"count": history_store.count(collection_id=collection_id), "database": str(HISTORY_DATABASE)})


@app.post("/open-login")
def open_login():
    denied = _require_admin()
    if denied:
        return denied
    try:
        open_login_page(force_visible=True)
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/go-selection")
def go_selection():
    denied = _require_admin()
    if denied:
        return denied
    try:
        open_selection_page()
        return jsonify(ok=True, url=page.url, title=page.title())
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/collect-selection")
def collect_selection():
    denied = _require_admin()
    if denied:
        return denied
    clear_high_sales_filter()
    session_id = str(session.get("auth_session_id"))
    try:
        _begin_admin_task(session_id)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    try:
        body = request.get_json(silent=True) or {}
        raw_limit = body.get("limit")
        limit = int(raw_limit) if raw_limit is not None else None
        if limit is not None and not 1 <= limit <= 10000:
            raise ValueError("采集数量必须在 1 到 10000 之间")
        result = collect_selection_data(limit=limit)
        return jsonify(ok=True, **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    finally:
        _end_admin_task(session_id)
        clear_high_sales_filter()


@app.get("/login-qr")
def login_qr():
    """Return the current server-side login page as a PNG for remote scanning."""
    denied = _require_admin()
    if denied:
        return denied
    with state_lock:
        if page is None or page.is_closed():
            return jsonify(ok=False, error="登录浏览器尚未打开"), 404
        try:
            if "/account/login" not in page.url:
                return jsonify(ok=False, error="Current page is not the login page"), 409
            image = page.screenshot(type="png", full_page=True)
            response = make_response(image)
            response.headers["Content-Type"] = "image/png"
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return response
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500


@app.get("/status")
def status():
    denied = _require_admin()
    if denied:
        return denied
    with state_lock:
        if page is None or page.is_closed():
            return jsonify(open=False, authenticated=False, login_page=False, headless=False, url=None, title=None)
        try:
            _switch_to_headless_after_login()
            authenticated = (
                "buyin.jinritemai.com" in page.url
                and "/account/login" not in page.url
            )
            return jsonify(
                open=True,
                authenticated=authenticated,
                login_page="/account/login" in page.url,
                headless=browser_headless,
                url=page.url,
                title=page.title(),
            )
        except Exception as exc:
            return jsonify(open=False, authenticated=False, headless=False, error=str(exc))


@app.post("/close")
def close():
    denied = _require_admin()
    if denied:
        return denied
    close_browser()
    return jsonify(ok=True)


@app.post("/toggle-browser")
def toggle_browser():
    denied = _require_admin()
    if denied:
        return denied
    try:
        result = toggle_browser_mode()
        return jsonify(ok=True, **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/logout")
def logout():
    denied = _require_admin()
    if denied:
        return denied
    try:
        logout_session()
        return jsonify(ok=True, message="已退出登录，请重新扫码登录")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


if __name__ == "__main__":
    try:
        open_login_page()
        print(f"Login page opened: {LOGIN_URL}")
    except Exception as exc:
        print(f"Could not open the login page: {exc}")
        print("You can still open http://127.0.0.1:8000 and retry from the console.")
    # Playwright's sync API is bound to the thread where it was created.
    # Keep Flask single-threaded so requests use the same thread.
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=False)
