"""Local controller for an authorized Buyin browser session."""

from pathlib import Path
import shutil
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from threading import Lock

from flask import Flask, jsonify, render_template_string, request
from playwright.sync_api import BrowserContext, Page, sync_playwright
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
CAPTURE_NETWORK = os.getenv("CAPTURE_NETWORK") == "1"
USE_DIRECT_MATERIAL = os.getenv("BUYIN_DIRECT_MATERIAL") == "1"

history_store = HistoryStore(HISTORY_DATABASE)
history_store.migrate_json_once(DATA_DIR / "selection_results.json")


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

app = Flask(__name__)
state_lock = Lock()
playwright = None
context: BrowserContext | None = None
page: Page | None = None
browser_headless = False

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
async function openLogin(){await action('/open-login',{method:'POST'},'重新扫码登录')}
async function goSelection(){await action('/go-selection',{method:'POST'},'进入选品页')}
async function collectSelection(){const button=$('collectBtn');const limit=Number($('collectLimit').value);if(!Number.isInteger(limit)||limit<1||limit>10000){showModal('参数错误','采集数量必须在 1 到 10000 之间');return}button.disabled=true;button.textContent='采集中...';await action('/collect-selection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit})},'采集任务');button.disabled=false;button.textContent='开始采集';await refreshHistory()}
async function closeBrowser(){await action('/close',{method:'POST'},'关闭浏览器')}
async function refreshStatusAction(){await refreshStatus();showModal('刷新状态','浏览器状态已更新')}
document.querySelector('[onclick="refreshStatus()"]')?.addEventListener('click',refreshStatusAction)
refreshStatus();refreshHistory();setInterval(refreshStatus,5000);
</script></body></html>
"""

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
    if browser_headless or page is None or page.is_closed():
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
            _launch_browser(headless=False)
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
            _launch_browser(headless=AUTH_MARKER.exists())
        _open_login_url()
        if browser_headless and "/account/login" in page.url:
            _close_browser_context()
            _launch_browser(headless=False)
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


def direct_material_list(current_page: Page, body_override: dict | None = None) -> dict:
    spec = json.loads(REQUEST_SPEC_FILE.read_text(encoding="utf-8"))
    if body_override is not None:
        spec = {**spec, "body": json.dumps(body_override, ensure_ascii=False)}
    result = current_page.evaluate(
        """async spec => {
            const headers = {};
            if (spec.content_type) headers['content-type'] = spec.content_type;
            const response = await fetch(spec.url, {
                method: spec.method,
                headers,
                body: spec.body || undefined,
                credentials: 'include'
            });
            return {status: response.status, text: await response.text()};
        }""",
        spec,
    )
    if result["status"] < 200 or result["status"] >= 300:
        raise RuntimeError(f"Direct material_list request failed: HTTP {result['status']}")
    payload = json.loads(result["text"])
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else result["text"][:300]
        raise RuntimeError(f"material_list returned no data: code={code}, msg={message}")
    return payload


def collect_material_pages(current_page: Page, first_payload: dict, minimum: int = 90) -> tuple[list, list]:
    """Fetch the first page and trigger subsequent pages through the live page."""
    pages = [first_payload]
    promotions = list(first_payload.get("data", {}).get("summary_promotions") or [])
    last_payload = first_payload
    cursor = 0
    size = 30

    while len(promotions) < minimum and last_payload.get("data", {}).get("has_more"):
        expected_cursor = cursor + size

        def is_expected_page(response) -> bool:
            if MATERIAL_LIST_PATH not in response.url or not response.ok:
                return False
            try:
                body = json.loads(response.request.post_data or "{}")
                return int(body.get("cursor") or 0) == expected_cursor
            except Exception:
                return False

        with current_page.expect_response(is_expected_page, timeout=30000) as response_info:
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
        next_payload = response_info.value.json()
        pages.append(next_payload)
        promotions.extend(next_payload.get("data", {}).get("summary_promotions") or [])
        last_payload = next_payload
        cursor = expected_cursor

    return promotions[:minimum], pages


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


def reveal_public_business_id(detail_page: Page) -> str | None:
    """Click the eye and read the official contact endpoint response."""
    detail_page.bring_to_front()
    try:
        detail_page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    detail_page.wait_for_timeout(1500)
    contact_candidates = detail_page.get_by_text("\u8054\u7cfb\u5546\u5bb6", exact=True)
    if not contact_candidates.count():
        return None
    contact = first_visible(contact_candidates)
    label_pattern = re.compile(r"(?:\u5fae\u4fe1\u53f7|merchant_product_id)")
    label_candidates = None
    hover_target = contact
    for _ in range(3):
        hover_target.hover(force=True)
        detail_page.wait_for_timeout(700)
        candidates = detail_page.get_by_text(label_pattern)
        if candidates.count() and any(
            candidates.nth(index).is_visible() for index in range(candidates.count())
        ):
            label_candidates = candidates
            break
        hover_target = hover_target.locator("..").first
    if label_candidates is None:
        return None
    label = first_visible(label_candidates)
    row = label.locator("..").first
    container = row
    for _ in range(5):
        controls = container.locator(
            "button, [role='button'], svg, img, i, [class*='icon'], [class*='eye']"
        )
        for index in range(controls.count() - 1, -1, -1):
            candidate = controls.nth(index)
            if not candidate.is_visible():
                continue
            try:
                with detail_page.expect_response(
                    lambda response: "/square_pc_api/common/contact" in response.url
                    and response.ok,
                    timeout=5000,
                ) as response_info:
                    candidate.click(force=True)
                payload = response_info.value.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                contact = data.get("contact") if isinstance(data, dict) else None
                if contact:
                    return str(contact)
            except Exception:
                continue
        container = container.locator("..").first

    return None


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
            with page.expect_request(
                lambda request: MATERIAL_LIST_PATH in request.url,
                timeout=30000,
            ) as request_info, page.expect_response(
                lambda response: MATERIAL_LIST_PATH in response.url and response.ok,
                timeout=30000,
            ) as response_info:
                dom_click(high_sales)
            request = request_info.value
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
            payload = response_info.value.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("material_list response data is empty")
        target = limit if limit is not None else int(os.getenv("COLLECT_LIMIT", "90"))
        target = max(1, min(target, 10000))
        promotions, material_pages = collect_material_pages(page, payload, target)
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
        skipped_items = []
        for promotion in promotions:
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
                "shop_score": nested_value(
                    shop_info.get("shop_score_info"), ("score",)
                ),
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
            query = urlencode({
                    "commodity_id": commodity_id,
                    "commodity_location": 1,
                    "id": commodity_id,
                    "product_id": product_id,
                    "shop_id": shop_id or "",
                })
            detail_page = context.new_page()
            try:
                detail_page.goto(
                    "https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting?" + query,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                detail_page.wait_for_timeout(1500)
                item["merchant_product_id"] = reveal_public_business_id(detail_page)
                if not item["merchant_product_id"]:
                    skipped += 1
                    if skipped == 1:
                        DATA_DIR.mkdir(parents=True, exist_ok=True)
                        detail_page.screenshot(
                            path=str(DATA_DIR / "debug_detail_first_skipped.png"),
                            full_page=True,
                        )
                    skipped_items.append({
                        "name": item["name"],
                        "commodity_id": commodity_id,
                        "product_id": product_id,
                        "detail_url": detail_page.url,
                        "reason": "public business id was not revealed",
                    })
                    continue
                item["detail_url"] = detail_page.url
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
            results.append(item)

        output = DATA_DIR / "selection_results.json"
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        history = history_store.save_records(results)
        SKIPPED_FILE.write_text(
            json.dumps(skipped_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "count": len(results),
            "skipped": skipped,
            "file": str(output),
            "collection_id": history["collection_id"],
            "history_database": str(HISTORY_DATABASE),
        }


def close_browser() -> None:
    with state_lock:
        _close_browser_context()


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/results")
def results_page():
    return render_template_string(RESULTS_PAGE)


@app.get("/api/results")
def results_api():
    return jsonify(history_store.list_records(
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
        query=request.args.get("q"),
        collection_id=request.args.get("collection_id"),
    ))


@app.get("/api/history")
def history_api():
    collection_id = request.args.get("collection_id")
    return jsonify({"count": history_store.count(collection_id=collection_id), "database": str(HISTORY_DATABASE)})


@app.post("/open-login")
def open_login():
    try:
        open_login_page(force_visible=True)
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/go-selection")
def go_selection():
    try:
        open_selection_page()
        return jsonify(ok=True, url=page.url, title=page.title())
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/collect-selection")
def collect_selection():
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


@app.get("/status")
def status():
    with state_lock:
        if page is None or page.is_closed():
            return jsonify(open=False, url=None, title=None)
        try:
            _switch_to_headless_after_login()
            return jsonify(open=True, url=page.url, title=page.title())
        except Exception as exc:
            return jsonify(open=False, error=str(exc))


@app.post("/close")
def close():
    close_browser()
    return jsonify(ok=True)


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
