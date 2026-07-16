"""Local controller for an authorized Buyin browser session."""

from pathlib import Path
import shutil
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from threading import Lock

from flask import Flask, jsonify, render_template_string
from playwright.sync_api import BrowserContext, Page, sync_playwright

LOGIN_URL = "https://buyin.jinritemai.com/mpa/account/login"
PROFILE_DIR = Path(__file__).parent / "data" / "buyin-browser-profile"
DATA_DIR = Path(__file__).parent / "data"
AUTH_MARKER = DATA_DIR / "buyin-authenticated.marker"
MATERIAL_LIST_PATH = "/pc/selection/common/material_list"
REQUEST_SPEC_FILE = DATA_DIR / "material_list_request.json"
SKIPPED_FILE = DATA_DIR / "selection_skipped.json"
NETWORK_CAPTURE_FILE = DATA_DIR / "network_capture.jsonl"
MATERIAL_PAGES_FILE = DATA_DIR / "material_list_pages.json"
CAPTURE_NETWORK = os.getenv("CAPTURE_NETWORK") == "1"


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

PAGE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buyin Login Console</title>
<style>body{max-width:720px;margin:48px auto;padding:0 20px;font:16px/1.6 system-ui,sans-serif;color:#202124}button{padding:9px 14px;margin-right:8px;cursor:pointer}pre{padding:14px;background:#f5f6f7;white-space:pre-wrap}.note{color:#5f6368}</style>
</head><body><h1>Buyin Login Console</h1>
<p class="note">Scan the QR code in the official Chromium window. The session stays in the local browser profile.</p>
<button onclick="openLogin()">Open login page</button><button onclick="goSelection()">Open selection</button><button onclick="collectSelection()">Collect &gt;=5000</button><a href="/results">View results</a><button onclick="refreshStatus()">Refresh status</button><button onclick="closeBrowser()">Close browser</button>
<pre id="status">Loading status...</pre>
<script>
async function refreshStatus(){const r=await fetch('/status');document.getElementById('status').textContent=JSON.stringify(await r.json(),null,2)}
async function openLogin(){await fetch('/open-login',{method:'POST'});await refreshStatus()}
async function goSelection(){const r=await fetch('/go-selection',{method:'POST'});const data=await r.json();if(!data.ok) alert(data.error);await refreshStatus()}
async function collectSelection(){const r=await fetch('/collect-selection',{method:'POST'});const data=await r.json();alert(data.ok ? `Saved ${data.count} items` : data.error);await refreshStatus()}
async function closeBrowser(){await fetch('/close',{method:'POST'});await refreshStatus()}
refreshStatus();setInterval(refreshStatus,3000);
</script></body></html>
"""

RESULTS_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Selection results</title>
<style>
body{margin:24px;font:14px/1.5 system-ui,sans-serif;color:#202124;background:#f6f7f9}
a{color:#d7003a}.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:middle}th{background:#fafafa;position:sticky;top:0}img{width:72px;height:72px;object-fit:cover;border-radius:4px}.muted{color:#6b7280}
</style></head><body><div class="toolbar"><h1>Captured products</h1><a href="/">Back to console</a></div>
<table><thead><tr><th>Image</th><th>Product</th><th>Shop</th><th>Monthly sales</th><th>merchant_product_id</th><th>IDs</th></tr></thead><tbody id="rows"></tbody></table>
<script>
async function load(){const response=await fetch('/api/results');const items=await response.json();const rows=document.getElementById('rows');
rows.innerHTML=items.map(item=>`<tr><td>${item.image_url?`<img src="${item.image_url}" loading="lazy">`:''}</td><td>${item.name||''}</td><td>${item.shop_name||''}</td><td>${item.month_sale??''}</td><td>${item.merchant_product_id||''}</td><td><span class="muted">product: ${item.product_id||''}<br>commodity: ${item.commodity_id||''}</span></td></tr>`).join('');}
load();
</script></body></html>
"""


def open_login_page() -> None:
    global playwright, context, page
    with state_lock:
        if context is None:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            playwright = sync_playwright().start()
            options = {
                "user_data_dir": str(PROFILE_DIR),
                "headless": AUTH_MARKER.exists() and os.getenv("BUYIN_VISIBLE") != "1",
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
        page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        mark_authenticated(page)


def open_selection_page() -> None:
    with state_lock:
        if page is None or page.is_closed():
            raise RuntimeError("The browser is not open. Open the login page first.")
        if "buyin.jinritemai.com" not in page.url:
            raise RuntimeError("The current page is not the official Buyin site.")
        if "/account/login" in page.url:
            raise RuntimeError("The saved login session has expired. Restart with BUYIN_VISIBLE=1 and scan again.")
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
    """Fetch pages in the current authenticated browser context."""
    spec_body = json.loads(REQUEST_SPEC_FILE.read_text(encoding="utf-8")).get("body") or "{}"
    request_body = json.loads(spec_body)
    pages = [first_payload]
    promotions = list(first_payload.get("data", {}).get("summary_promotions") or [])
    last_payload = first_payload
    cursor = int(request_body.get("cursor") or 0)
    size = int(request_body.get("size") or 30)

    while len(promotions) < minimum and last_payload.get("data", {}).get("has_more"):
        cursor += size
        request_body["cursor"] = cursor
        extra = last_payload.get("data", {}).get("extra") or {}
        if extra.get("search_id"):
            request_body.setdefault("extra", {})["search_id"] = extra["search_id"]
        if extra.get("session_id"):
            request_body.setdefault("extra", {})["session_id"] = extra["session_id"]
        next_payload = direct_material_list(current_page, request_body)
        pages.append(next_payload)
        promotions.extend(next_payload.get("data", {}).get("summary_promotions") or [])
        last_payload = next_payload

    return promotions[:max(minimum, len(promotions))], pages


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


def collect_selection_data() -> dict:
    with state_lock:
        if page is None or page.is_closed():
            raise RuntimeError("The browser is not open.")
        if "buyin.jinritemai.com" not in page.url:
            raise RuntimeError("The current page is not the official Buyin site.")
        if "/account/login" in page.url:
            raise RuntimeError("The saved login session has expired. Restart with BUYIN_VISIBLE=1 and scan again.")
        mark_authenticated(page)

        if REQUEST_SPEC_FILE.exists():
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
        target = max(90, int(os.getenv("COLLECT_LIMIT", "90")))
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
                "shop_name": shop_info.get("shop_name"),
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
        SKIPPED_FILE.write_text(
            json.dumps(skipped_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"count": len(results), "skipped": skipped, "file": str(output)}


def close_browser() -> None:
    global playwright, context, page
    with state_lock:
        if context is not None:
            context.close()
        if playwright is not None:
            playwright.stop()
        playwright = context = page = None


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/results")
def results_page():
    return render_template_string(RESULTS_PAGE)


@app.get("/api/results")
def results_api():
    output = DATA_DIR / "selection_results.json"
    if not output.exists():
        return jsonify([])
    return jsonify(json.loads(output.read_text(encoding="utf-8")))


@app.post("/open-login")
def open_login():
    try:
        open_login_page()
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
        result = collect_selection_data()
        return jsonify(ok=True, **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.get("/status")
def status():
    with state_lock:
        if page is None or page.is_closed():
            return jsonify(open=False, url=None, title=None)
        try:
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
