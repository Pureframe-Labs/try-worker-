#!/usr/bin/env python3
"""
Unified Land Records Worker
Single Chrome Browser | One Anchor Tab | New Tab Per Request
Handles: Property Card, Ferfar, 7-12 Satbara, 8A Satbara
Based on working 7-12 backend with payment polling
"""

import sys
import io
import os
import asyncio
import pika
import json
import httpx
import datetime as _dt
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import utils
import re
import api as health_api
from api import worker_state
from difflib import get_close_matches
from pdf2image import convert_from_path
import pytesseract

# Encoding Fix
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

load_dotenv()
log = utils.log

# -------------------------------------------------
# EVENT LOOP & BROWSER STATE
# -------------------------------------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

playwright = None
browser = None
context = None
anchor_page = None
connection = None
channel = None

# Backend URL for webhook notifications
# Must be configured via BACKEND_URL in production
BACKEND_URL = os.getenv("BACKEND_URL") or os.getenv("NGROK_BASE_URL")
if not BACKEND_URL:
    log.warning("⚠️ BACKEND_URL is not set! Worker will not be able to notify the backend.")

# Download directories - read from .env (must match document-send.service)
def get_download_dir(doc_type: str) -> str:
    env_map = {
        'property_card': "DOWNLOAD_DIR_PROPERTYCARD",
        'ferfar': "DOWNLOAD_DIR_FERFAR",
        '7_12': "DOWNLOAD_DIR_SATBARA",
        '8a': "DOWNLOAD_DIR_8A",
    }
    env_key = env_map.get(doc_type, "")
    env_val = os.getenv(env_key)
    if not env_val:
        raise RuntimeError(f"Missing {env_key} in .env - required for PDF download path")
    
    project_root = os.getcwd()
    
    full_path = os.path.join(project_root, env_val)
    os.makedirs(full_path, exist_ok=True)
    return full_path


# -------------------------------------------------
# PDF EXTRACTION LOGIC (Merged from extract_712.py)
# -------------------------------------------------

# OCR CONFIG
# Note: In production, Tesseract & Poppler must be installed in the environment.
# Their paths can be set via env vars if they aren't in the system's PATH.
POPPLER_PATH = os.getenv("POPPLER_PATH")
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

DEVANAGARI_TO_ASCII = str.maketrans("०१२३४५६७८९", "0123456789")

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()

def pick_pattern(pattern: str, text: str):
    if not text: return None
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None

def normalize_place_name(s):
    if not s or not isinstance(s, str): return ""
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\(\d+", "", s)
    s = re.sub(r"\(\S+", "", s)
    noise_words = ["हि", "ता", "ता.", "दि", "जि", "जि."]
    words = [w for w in s.strip().split() if w not in noise_words]
    return " ".join(words).strip()

def cut_at_next_label(val):
    if not val or not isinstance(val, str): return ""
    stops = ["तालुका", "जिल्हा", "गट", "PU-ID", "नमुना", "अधिकार", "अभिलेख", "पत्रक"]
    for s in stops:
        if s in val: val = val.split(s)[0]
    return val.strip(" :-—")

def normalize_ocr_text(t):
    if not t: return ""
    t = t.translate(DEVANAGARI_TO_ASCII)
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"[^\w\sऀ-ॿ:.\-]", " ", t)
    t = re.sub(r"([0-9])\s+[ऀ-ॿ]\b", r"\1", t)
    return t

def extract_gat_number(text: str):
    t = normalize_ocr_text(clean_text(text))
    m = re.search(r"गट\s*क्रमांक[^0-9]{0,30}?([0-9]{1,5})", t)
    if m: return m.group(1)
    m = re.search(r"गट\s*क्रमांक\s*व\s*उपवि[^\n]{0,20}?([0-9]{1,5})", t)
    if m: return m.group(1)
    for line in t.splitlines():
        if "गट" in line:
            nums = re.findall(r"\b([0-9]{1,5})\b", line)
            for n in nums:
                if int(n) > 10: return n
    return None

def match_fuzzy(extracted, options):
    if not extracted or not options: return extracted
    match = get_close_matches(extracted, options, n=1, cutoff=0.6)
    return match[0] if match else extracted

async def run_ocr_extraction(pdf_path: str):
    """Main OCR logic for 7-12 Satbara"""
    try:
        pages = convert_from_path(
            pdf_path,
            dpi=350,
            first_page=1,
            last_page=1,
            poppler_path=POPPLER_PATH
        )
        if not pages: return {"success": False, "error": "No pages converted"}
        
        page = pages[0]
        w, h = page.size
        band1 = page.crop((0, int(h * 0.08), w, int(h * 0.22)))
        band2 = page.crop((0, int(h * 0.22), w, int(h * 0.50)))

        text1 = pytesseract.image_to_string(band1, lang="mar")
        text2 = pytesseract.image_to_string(band2, lang="mar")
        
        t1_clean = clean_text(text1)
        sep = r"(?::\-|:-|:|—|-)"
        
        village_raw = pick_pattern(rf"गाव\s*{sep}\s*([^\n]+)", t1_clean)
        taluka_raw  = pick_pattern(rf"तालुका\s*{sep}\s*([^\n]+)", t1_clean)
        district_raw = pick_pattern(rf"जिल्हा\s*{sep}\s*([^\n]+)", t1_clean)

        loc = {
            "Village": normalize_place_name(cut_at_next_label(village_raw)),
            "Taluka": normalize_place_name(cut_at_next_label(taluka_raw)),
            "District": normalize_place_name(cut_at_next_label(district_raw))
        }
        
        gat = extract_gat_number(text1) or extract_gat_number(text2)

        # Load master JSON for fuzzy matching
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "output.json")
        district_std, taluka_std, village_std = loc["District"], loc["Taluka"], loc["Village"]
        
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                master = json.load(f)
                
                dist_opts = [d["backend_value"] for d in master.get("district_menu", [])]
                district_std = match_fuzzy(loc["District"], dist_opts)
                
                if district_std in master.get("taluka_menu", {}):
                    tal_opts = [t["backend_value"] for t in master["taluka_menu"][district_std]]
                    taluka_std = match_fuzzy(loc["Taluka"], tal_opts)
                    
                    if taluka_std in master.get("village_menu", {}):
                        vil_opts = [v["backend_value"] for v in master["village_menu"][taluka_std]]
                        village_std = match_fuzzy(loc["Village"], vil_opts)

        return {
            "success": True,
            "GatNumber": gat,
            "Village": village_std,
            "Taluka": taluka_std,
            "District": district_std,
            "pdfPath": pdf_path
        }
    except Exception as e:
        log.error(f"OCR Error: {e}")
        return {"success": False, "error": str(e)}


# -------------------------------------------------
# BROWSER INITIALIZATION
# -------------------------------------------------
async def init_browser():
    """Initialize single browser with one context and anchor tab"""
    global playwright, browser, context, anchor_page
    
    log.info("🚀 Launching unified browser...")
    
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-sandbox",
            "--disable-dev-shm-usage"
        ]
    )
    
    # Single context for all operations
    context = await browser.new_context(accept_downloads=True)
    
    # Single anchor page for session management
    anchor_page = await context.new_page()
    
    log.info("✅ Browser initialized with single context and anchor tab")

# -------------------------------------------------
# LOGIN FUNCTION
# -------------------------------------------------
async def login_to_service(service_name: str, url: str, username: str, password: str) -> bool:
    """Login to a specific service using the anchor page"""
    global anchor_page
    
    log.info(f"Logging in to {service_name}...")
    
    try:
        await anchor_page.goto(url, wait_until="domcontentloaded")
        
        # Login attempts
        logged_in = False
        attempt = 0
        
        while not logged_in and attempt < 5:
            attempt += 1
            log.info(f"Login attempt {attempt} for {service_name}")
            
            try:
                await anchor_page.wait_for_selector("#txtlogid", timeout=10000)
                
                await anchor_page.fill("#txtlogid", username)
                await anchor_page.fill("#txtpasslogin", password)
                
                captcha_img = await anchor_page.locator("#myimg").screenshot()
                captcha_text = await utils.solve_captcha(captcha_img)
                
                log.info(f"Solved captcha: {captcha_text}")
                
                if len(captcha_text) == 5:
                    await anchor_page.fill("#CaptchaText", captcha_text)
                    await asyncio.sleep(5)
                    await anchor_page.click("#btnSubmit2")
                    await asyncio.sleep(5)
                    
                    if "Login" not in anchor_page.url:
                        log.info(f"✅ LOGIN SUCCESS for {service_name}")
                        logged_in = True
                        break
                    else:
                        log.warning("Login failed, retrying...")
                else:
                    log.warning("Invalid captcha, refreshing...")
                    await anchor_page.click("#myimg")
                    await asyncio.sleep(2)
                    
            except Exception as e:
                log.error(f"Login error: {e}")
                await anchor_page.reload()
        
        if not logged_in:
            log.error(f"❌ Login failed for {service_name} after 5 attempts")
            return False
        
        return True
        
    except Exception as e:
        log.error(f"Critical login error for {service_name}: {e}")
        return False

async def init_primary_login():
    """Login to the primary service (7-12) on anchor tab"""
    username, password = utils.get_credentials('7_12')
    
    if not username or not password:
        raise RuntimeError("7-12 credentials not found")
    
    worker_state["session_active"] = False
    success = await login_to_service("7-12 Satbara", utils.SEVEN_TWELVE_URL, username, password)
    
    if not success:
        raise RuntimeError("Failed to login to 7-12 Satbara")
    
    worker_state["session_active"] = True
    worker_state["last_heartbeat"] = __import__('datetime').datetime.utcnow().isoformat()
    log.info("✅ Primary login complete (Anchor tab session active)")

async def ensure_logged_in():
    """Check if session is active, if not, re-login"""
    global anchor_page, context
    
    if not anchor_page:
        log.warning("Anchor page not found, re-initializing...")
        await init_browser()
        await init_primary_login()
        return

    try:
        current_url = anchor_page.url
        # If the URL contains 'Login' or 'login', we might be logged out
        if "Login" in current_url or "login" in current_url or "Error" in current_url:
            log.warning(f"🔄 Session appears to be logged out (URL: {current_url}). Re-logging in...")
            await init_primary_login()
            return

        # Explicit check for login form elements on the page
        login_form = await anchor_page.query_selector("#txtlogid")
        if login_form and await login_form.is_visible():
            log.warning("🔄 Login form detected on anchor page. Re-logging in...")
            await init_primary_login()
            
    except Exception as e:
        log.error(f"Error checking session status: {e}")
        await init_primary_login()

# -------------------------------------------------
# SESSION KEEP-ALIVE
# -------------------------------------------------
async def keep_session_alive():
    """Keep anchor page session active and healthy"""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        if anchor_page:
            try:
                # Check health before activity
                await ensure_logged_in()
                
                await anchor_page.evaluate(
                    "() => document.dispatchEvent(new Event('mousemove'))"
                )

                import datetime as _dt
                worker_state["last_heartbeat"] = _dt.datetime.utcnow().isoformat()
                log.info("🔄 Session heartbeat sent")
                
                # Check if session is still valid
                if "login" in anchor_page.url.lower():
                    log.warning("⚠️ Session expired, attempting re-login...")
                    worker_state["session_active"] = False
                    await init_primary_login()
                    
            except Exception as e:
                worker_state["session_active"] = False
                log.error(f"Session keep-alive error: {e}")

# -------------------------------------------------
# PAYMENT POLLING WITH SESSION HEARTBEAT
# -------------------------------------------------
async def wait_for_payment(req_id: str, doc_type: str, worker_page, whatsapp_phone: str) -> bool:
    """
    Poll database for payment confirmation
    Includes session heartbeat to prevent timeout
    """
    log.info(f"⏳ Waiting for payment from {whatsapp_phone}...")
    
    for i in range(120):  # Poll every 5s for 10 minutes
        current_status = utils.get_db_status(doc_type, req_id)
        
        if str(current_status).lower() == "paid":
            log.info(f"💰 Payment detected for {doc_type} ID {req_id}!")
            return True
        elif str(current_status).lower() == "cancelled":
            log.info(f"🚫 Request {req_id} cancelled.")
            return False
        
        # Session heartbeat every 30 seconds (6 iterations)
        if i % 6 == 0:
            await worker_page.mouse.move(10, 10)
            
            # Check if session expired
            if "login" in worker_page.url.lower():
                log.error("Portal session timed out during payment wait.")
                utils.update_db(doc_type, req_id, "session_timeout")
                return False
        
        await asyncio.sleep(5)
    
    # Timeout reached
    log.error("⌛ Payment timeout reached.")
    utils.update_db(doc_type, req_id, "payment_timeout")
    return False

# -------------------------------------------------
# PROPERTY CARD AUTOMATION
# -------------------------------------------------
async def process_property_card(data):
    """Process Property Card request in a new tab"""
    worker_page = await context.new_page()

    req_id = data["id"]
    whatsapp_phone = data.get("whatsapp_phone")

    try:
        log.info(f"📄 Processing Property Card ID: {req_id}")

        # -------------------------------------------------
        # OPEN PAGE
        # -------------------------------------------------
        await worker_page.goto(
            utils.PROPERTY_CARD_URL,
            wait_until="domcontentloaded"
        )

        # -------------------------------------------------
        # REGION
        # -------------------------------------------------
        await worker_page.select_option("#ddlRegion", label=data["region"])
        await worker_page.wait_for_load_state("networkidle")

        # -------------------------------------------------
        # DISTRICT
        # -------------------------------------------------
        await worker_page.select_option("#ddlPCDist", label=data["district"])
        await worker_page.wait_for_load_state("networkidle")

        # -------------------------------------------------
        # OFFICE
        # -------------------------------------------------
        await worker_page.select_option("#ddlPCOffice", label=data["office"])
        await worker_page.wait_for_load_state("networkidle")

        # -------------------------------------------------
        # VILLAGE
        # -------------------------------------------------
        try:
            await worker_page.select_option("#ddlvillage", label=data["village"])
        except:
            await worker_page.evaluate(
                """v => {
                    const el = document.querySelector('#ddlVillage');
                    const i = [...el.options].findIndex(o => o.text.includes(v));
                    if (i >= 0) {
                        el.selectedIndex = i;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                data["village"]
            )

        await worker_page.wait_for_timeout(3000)

        # -------------------------------------------------
        # HANDLE SWEET ALERT SAFELY
        # -------------------------------------------------
        swal = worker_page.locator(".swal2-confirm")
        if await swal.count() > 0:
            await swal.click()

        # -------------------------------------------------
        # ENTER CTS NUMBER
        # -------------------------------------------------
        cts_input = worker_page.locator("#txt_ctsono")
        await cts_input.fill("")
        await cts_input.type(str(data["cts_no"]), delay=80)
        await cts_input.press("Tab")

        # WAIT FOR CTS DROPDOWN OPTIONS (IMPORTANT FIX)
        await worker_page.wait_for_selector(
            "#ddlctso option",
            timeout=20000
        )

        # SELECT CTS
        try:
            await worker_page.select_option(
                "#ddlctso",
                label=str(data["cts_no"])
            )
        except:
            await worker_page.select_option("#ddlctso", index=1)

        # CONFIRM POPUP
        if await swal.count() > 0:
            await swal.click()

        # -------------------------------------------------
        # VERIFIED
        # -------------------------------------------------
        log.info(f"✅ Property Card Verified for ID {req_id}")
        utils.update_db('property_card', req_id, "pdf_verified")

        confirmed_and_paid = await wait_for_payment(
            req_id,
            'property_card',
            worker_page,
            whatsapp_phone
        )

        if not confirmed_and_paid:
            return

        # -------------------------------------------------
        # DOWNLOAD
        # -------------------------------------------------
        btn = worker_page.locator("input#submit.btn-primary")

        await btn.wait_for(state="visible", timeout=30000)

        # WAIT UNTIL ENABLED
        await worker_page.wait_for_function(
            "btn => !btn.disabled",
            btn
        )

        worker_page.on(
            "dialog",
            lambda dialog: asyncio.create_task(dialog.accept())
        )

        async with worker_page.expect_download(timeout=90000) as download_info:

            log.info("🖱️ Clicking Property Card Download button...")
            await btn.click(force=True)

            confirm_btn = worker_page.locator(
                "button:has-text('हो'), .swal2-confirm"
            )

            try:
                await confirm_btn.wait_for(timeout=5000)
                await confirm_btn.click(force=True)
            except:
                pass

        # -------------------------------------------------
        # SAVE FILE
        # -------------------------------------------------
        download = await download_info.value

        file_name = f"PropertyCard_{data['cts_no']}_{req_id}.pdf"

        download_dir = get_download_dir('property_card')
        os.makedirs(download_dir, exist_ok=True)

        save_path = os.path.join(download_dir, file_name)
        await download.save_as(save_path)

        utils.update_db('property_card', req_id, "completed", file_name)

        log.info(f"✅ Property Card saved: {save_path}")

        await notify_backend(
            'property_card',
            req_id,
            file_name,
            whatsapp_phone
        )

    except Exception as e:
        log.error(f"❌ Property Card Job {req_id} failed: {e}")
        utils.update_db('property_card', req_id, "failed")

    finally:
        await worker_page.close()

# -------------------------------------------------
# FERFAR AUTOMATION
# -------------------------------------------------
async def process_ferfar(data):
    """Process Ferfar request in a new tab"""
    worker_page = await context.new_page()
    req_id = data["id"]
    whatsapp_phone = data.get("whatsapp_phone")
    
    try:
        log.info(f"📄 Processing Ferfar ID: {req_id}")
        
        await worker_page.goto(utils.FERFAR_URL, wait_until="domcontentloaded")
        
        # Select district
        await worker_page.select_option("#ddlDist1", label=data["district"])
        await asyncio.sleep(2)
        
        # Select taluka
        await worker_page.select_option("#ddlTahsil", label=data["taluka"])
        await asyncio.sleep(2)
        
        # Select village with fallback
        try:
            await worker_page.select_option("#ddlVillage", label=data["village"])
        except:
            await worker_page.evaluate(
                """(villageName) => {
                    const select = document.querySelector('#ddlVillage');
                    const options = Array.from(select.options);
                    const option = options.find(opt => opt.text.includes(villageName));
                    if (option) {
                        select.value = option.value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                data["village"]
            )
        
        await asyncio.sleep(3)
        
        # Handle popups
        if await worker_page.locator(".swal2-confirm").is_visible():
            await worker_page.locator(".swal2-confirm").click()
        
        # Enter mutation number (queue may send mutation_no or gat_no)
        mutation_no = data.get("mutation_no") or data.get("gat_no")
        mut_input = worker_page.locator("#txt_mutationno")
        await mut_input.fill("")
        await mut_input.type(str(mutation_no), delay=100)
        await mut_input.press("Tab")
        await asyncio.sleep(5)
        
        # Handle confirmation
        if await worker_page.locator(".swal2-confirm").is_visible():
            await worker_page.locator(".swal2-confirm").click()
            await asyncio.sleep(1)
        
        # Update status to verified
        log.info(f"✅ Ferfar Verified for ID {req_id}. Updating to pdf_verified...")
        utils.update_db('ferfar', req_id, "pdf_verified")
        
        # Poll for payment
        confirmed_and_paid = await wait_for_payment(req_id, 'ferfar', worker_page, whatsapp_phone)
        
        if not confirmed_and_paid:
            return
        
        # Download - Ferfar uses input#submit.btn-primary; confirm dialog may use Marathi "हो" or swal2-confirm
        btn = worker_page.locator("input#submit.btn-primary")
        await btn.wait_for(state="visible", timeout=25000)
        await btn.scroll_into_view_if_needed()
        
        if await btn.is_disabled():
            raise Exception("Download button disabled")
        
        worker_page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        
        async with worker_page.expect_download(timeout=90000) as download_info:
            log.info("🖱️ Clicking Ferfar Download button...")
            await btn.click(force=True)
            await asyncio.sleep(1)
            
            # Handle SweetAlert2 confirmation - Ferfar may use Marathi "हो" (Yes) like 8a
            confirm_btn = worker_page.locator("button:has-text('हो'), button.swal2-confirm, .swal2-confirm")
            try:
                await confirm_btn.wait_for(state="visible", timeout=8000)
                await confirm_btn.click(force=True)
                log.info("✅ Clicked confirmation dialog")
            except Exception as e:
                log.info(f"No confirmation popup (or already dismissed): {e}")
        
        # Save file
        download = await download_info.value
        file_name = f"Ferfar_{data.get('mutation_no') or data.get('gat_no', 'unknown')}_{req_id}.pdf"
        
        download_dir = get_download_dir('ferfar')
        os.makedirs(download_dir, exist_ok=True)
        save_path = os.path.join(download_dir, file_name)
        await download.save_as(save_path)
        
        # Update DB
        utils.update_db('ferfar', req_id, "completed", file_name)
        log.info(f"✅ Ferfar saved: {save_path}")
        
        # Notify backend
        await notify_backend('ferfar', req_id, file_name, whatsapp_phone)
        
    except Exception as e:
        log.error(f"❌ Ferfar Job {req_id} failed: {e}")
        utils.update_db('ferfar', req_id, "failed")
    finally:
        await worker_page.close()

# -------------------------------------------------
# 7-12 SATBARA AUTOMATION
# -------------------------------------------------
async def process_7_12(data):
    """Process 7-12 Satbara request in a new tab"""
    worker_page = await context.new_page()
    req_id = data["id"]
    gat_no = str(data.get("gat_no"))
    whatsapp_phone = data.get("whatsapp_phone")
    
    try:
        log.info(f"📄 Processing 7-12 ID: {req_id} | Gat: {gat_no}")
        
        await worker_page.goto(utils.SEVEN_TWELVE_URL, wait_until="domcontentloaded")
        
        # Select district
        await worker_page.select_option("#ddlDist1", label=str(data["district"]))
        await asyncio.sleep(2)
        
        # Select taluka
        await worker_page.select_option("#ddlTahsil", label=str(data["taluka"]))
        await asyncio.sleep(2)
        
        # Select village with fallback
        try:
            await worker_page.select_option("#ddlVillage", label=data["village"])
        except Exception:
            log.info(f"Using JS fallback for village: {data['village']}")
            await worker_page.evaluate(
                """v => {
                    const el = document.querySelector('#ddlVillage');
                    const i = [...el.options].findIndex(o => o.text.includes(v));
                    if (i >= 0) {
                        el.selectedIndex = i;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                data["village"]
            )
        await asyncio.sleep(3)
        
        # Enter gat number
        gat_input = worker_page.locator("#txgt_sarveno")
        await gat_input.fill("")
        await gat_input.type(gat_no, delay=100)
        await gat_input.press("Tab")
        await asyncio.sleep(2.5)
        
        # Check for error popup
        if await worker_page.locator(".swal2-popup").is_visible():
            error_msg = await worker_page.locator(".swal2-html-container").inner_text()
            log.warning(f"❌ Portal Error: {error_msg}")
            await worker_page.locator(".swal2-confirm").click()
            utils.update_db('7_12', req_id, "failed_portal_error")
            return
        
        # Select from dropdown
        survey_dropdown = worker_page.locator("#ddlsurvey")
        await survey_dropdown.wait_for(state="visible", timeout=30000)
        await survey_dropdown.select_option(index=1)
        
        # Wait for verification popups
        await asyncio.sleep(2)
        swal_confirm = worker_page.locator(".swal2-confirm")
        if await swal_confirm.is_visible():
            await swal_confirm.click()
        
        # Update status to verified
        log.info(f"✅ 7-12 Record Verified for ID {req_id}. Updating to pdf_verified...")
        utils.update_db('7_12', req_id, "pdf_verified")
        
        # Poll for payment with session heartbeat
        confirmed_and_paid = await wait_for_payment(req_id, '7_12', worker_page, whatsapp_phone)
        
        if not confirmed_and_paid:
            return
        
        # Download
        btn = worker_page.locator("input#submit.btn-primary")
        await btn.wait_for(state="visible", timeout=15000)
        
        if await btn.is_disabled():
            raise Exception("Download button is disabled")
        
        worker_page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        
        async with worker_page.expect_download(timeout=60000) as download_info:
            log.info("🖱️ Clicking Download button...")
            await btn.click(force=True)
            
            # Handle SweetAlert2 confirmation
            confirm_btn = worker_page.locator("button.swal2-confirm")
            try:
                await confirm_btn.wait_for(state="visible", timeout=5000)
                await confirm_btn.click(force=True)
            except Exception:
                log.info("No secondary confirmation popup appeared")
        
        # Save file
        download = await download_info.value
        file_name = f"712_{req_id}.pdf"
        
        download_dir = get_download_dir('7_12')
        os.makedirs(download_dir, exist_ok=True)
        save_path = os.path.join(download_dir, file_name)

        await download.save_as(save_path)
        
        # Update DB
        utils.update_db('7_12', req_id, "completed", file_name)
        log.info(f"✅ 7-12 saved: {save_path}")
        
        # Notify backend
        await notify_backend('7_12', req_id, file_name, whatsapp_phone)
        
    except Exception as e:
        log.error(f"❌ 7-12 Job {req_id} failed: {e}")
        utils.update_db('7_12', req_id, "failed")
    finally:
        await worker_page.close()

# -------------------------------------------------
# 8A SATBARA AUTOMATION
# -------------------------------------------------

async def process_8a(data):
    """Process 8A Satbara request in a new tab"""
    worker_page = await context.new_page()

    req_id = data["id"]
    gat_no = str(data.get("gat_no"))
    whatsapp_phone = data.get("whatsapp_phone")

    try:
        log.info(f"📄 Processing 8A ID: {req_id} | Gat: {gat_no}")

        await worker_page.goto(
            utils.EIGHT_A_URL,
            wait_until="domcontentloaded"
        )

        # -------------------------------------------------
        # DISTRICT
        # -------------------------------------------------
        await worker_page.wait_for_selector("#ddlDist1")

        await worker_page.select_option(
            "#ddlDist1",
            label=data["district"]
        )

        # wait until taluka dropdown populated
        await worker_page.wait_for_function(
            "document.querySelector('#ddlTahsil').options.length > 1"
        )

        # -------------------------------------------------
        # TALUKA
        # -------------------------------------------------
        await worker_page.select_option(
            "#ddlTahsil",
            label=data["taluka"]
        )

        # wait until village dropdown populated
        await worker_page.wait_for_function(
            "document.querySelector('#ddlVillage').options.length > 1"
        )

        # -------------------------------------------------
        # VILLAGE
        # -------------------------------------------------
        await worker_page.select_option(
            "#ddlVillage",
            label=data["village"]
        )

        await worker_page.wait_for_timeout(2000)

        # -------------------------------------------------
        # ENTER GAT NUMBER
        # -------------------------------------------------
        gat_input = worker_page.locator("#txt_khtano")

        await gat_input.fill("")
        await gat_input.type(gat_no, delay=80)
        await gat_input.press("Tab")

        await worker_page.wait_for_timeout(2500)

        # -------------------------------------------------
        # HANDLE PORTAL ERROR POPUP
        # -------------------------------------------------
        swal_popup = worker_page.locator(".swal2-popup")

        if await swal_popup.count() > 0:
            error_msg = await worker_page.locator(
                ".swal2-html-container"
            ).inner_text()

            log.warning(f"❌ Portal Error: {error_msg}")

            await worker_page.locator(".swal2-confirm").click()
            utils.update_db('8a', req_id, "failed_portal_error")
            return

        # -------------------------------------------------
        # VERIFIED
        # -------------------------------------------------
        log.info(f"✅ 8A Record Verified for ID {req_id}")
        utils.update_db('8a', req_id, "pdf_verified")

        confirmed_and_paid = await wait_for_payment(
            req_id,
            '8a',
            worker_page,
            whatsapp_phone
        )

        if not confirmed_and_paid:
            return

        # -------------------------------------------------
        # DOWNLOAD - Set up listener BEFORE any click
        # -------------------------------------------------
        worker_page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        submit_btn = worker_page.locator("input#submit.btn-primary")
        await submit_btn.wait_for(state="visible", timeout=15000)
        await submit_btn.scroll_into_view_if_needed()

        async with worker_page.expect_download(timeout=90000) as download_info:
            # 1. Click Download (opens "Yes, download" popup)
            await submit_btn.click(force=True)
            await asyncio.sleep(2)
            # 2. Click "Yes, download" - triggers actual download
            confirm_btn = worker_page.get_by_role("button", name="Yes, download")
            await confirm_btn.wait_for(state="visible", timeout=10000)
            await confirm_btn.click(force=True)

        # -------------------------------------------------
        # SAVE FILE
        # -------------------------------------------------
        download = await download_info.value

        file_name = f"8A_{gat_no}_{req_id}.pdf"

        download_dir = get_download_dir('8a')
        os.makedirs(download_dir, exist_ok=True)

        save_path = os.path.join(download_dir, file_name)
        await download.save_as(save_path)

        utils.update_db('8a', req_id, "completed", file_name)

        log.info(f"✅ 8A saved: {save_path}")

        await notify_backend(
            '8a',
            req_id,
            file_name,
            whatsapp_phone
        )

    except Exception as e:
        log.error(f"❌ 8A Job {req_id} failed: {e}")
        utils.update_db('8a', req_id, "failed")

    finally:
        await worker_page.close()


# -------------------------------------------------
# BACKEND NOTIFICATION (Unified /complete endpoint)
# -------------------------------------------------
async def notify_backend(doc_type: str, req_id: str, filename: str, whatsapp_phone: str, extra_payload: dict = None):
    """
    Notify backend that job is complete. Uses unified /complete or /extraction-complete endpoint.
    Backend sends PDF to WhatsApp via document-send.service
    Falls back to localhost if primary URL fails (e.g. ngrok offline).
    """
    if extra_payload is None:
        extra_payload = {}
        
    payload = {
        'doc_type': doc_type,
        'request_id': req_id,
        'status': 'completed',
        'pdf_url': filename,
        'phone': whatsapp_phone,
        'service': doc_type,
        'phoneNumber': whatsapp_phone
    }
    payload.update(extra_payload)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f'{BACKEND_URL}/complete' if not doc_type.startswith('extraction') else f'{BACKEND_URL}/{doc_type.replace("_", "-")}',
                json=payload,
                timeout=15
            )

            if response.status_code == 200:
                log.info(f"✅ Backend notified for {doc_type} request {req_id} - PDF sent to WhatsApp")
                return
            else:
                log.warning(f"Backend {BACKEND_URL} returned {response.status_code}")

    except Exception as e:
        log.warning(f"Backend {BACKEND_URL} failed: {e}")

    log.error(f"❌ Failed to notify backend for {doc_type} request {req_id}")

# -------------------------------------------------
# UNIFIED JOB HANDLER
# -------------------------------------------------
async def process_job(data):
    """Route job to appropriate processor based on doc_type"""
    doc_type = data.get('doc_type')
    req_id = data['id']
    
    log.info(f"🔄 Processing {doc_type} job ID: {req_id}")
    
    processors = {
        'property_card': process_property_card,
        'ferfar': process_ferfar,
        '7_12': process_7_12,
        '8a': process_8a
    }
    
    if doc_type == 'extraction':
        result = await run_ocr_extraction(data['pdfPath'])
        await notify_backend(
            'extraction_complete',
            data.get('mediaId', '0'),
            data['pdfPath'],
            data['phoneNumber'],
            extra_payload={'result': result}
        )
        return

    processor = processors.get(doc_type)
    if not processor:
        raise ValueError(f"Unknown doc_type: {doc_type}")

    if doc_type in ['property_card', 'ferfar', '7_12', '8a']:
        await ensure_logged_in()
        
    await processor(data)

def handle_job(ch, method, properties, body):
    """Handle incoming RabbitMQ messages"""
    data = json.loads(body)
    req_id = data["id"]
    doc_type = data.get("doc_type", "unknown")
    
    log.info(f"📥 Received Job {req_id} ({doc_type}) from queue")
    worker_state["status"] = f"processing:{doc_type}:{req_id}"
    
    try:
        # Run the async job safely in the existing event loop from a different thread
        future = asyncio.run_coroutine_threadsafe(process_job(data), loop)
        future.result() # Wait for completion or error
        
        # Acknowledge the message
        ch.basic_ack(delivery_tag=method.delivery_tag)
        worker_state["jobs_processed"] = worker_state.get("jobs_processed", 0) + 1
        
    except Exception as e:
        log.error(f"❌ Critical error in handle_job for {req_id}: {e}")
        utils.update_db(doc_type, req_id, "failed")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        worker_state["jobs_failed"] = worker_state.get("jobs_failed", 0) + 1
    finally:
        worker_state["status"] = "idle"

# -------------------------------------------------
# MAIN ENTRY POINT
# -------------------------------------------------
import signal

def setup_signal_handlers():
    def stop_worker(sig, frame):
        log.info(f"Received signal {sig}, shutting down...")
        # We can't easily stop the blocking consumer from here 
        # without complex thread/loop management, so we'll just exit.
        # Docker/Railway will kill the process anyway after a timeout.
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

async def run_worker():
    global connection, channel
    
    # Start FastAPI health server in background (for Railway health checks)
    api_port = int(os.getenv("PORT", "8080"))
    health_api.start_api_server(port=api_port)
    log.info(f"✅ Health API running on port {api_port} (GET /health)")
    worker_state["status"] = "initializing"
    
    # Ensure download dirs from env exist
    for env_key in ["DOWNLOAD_DIR_PROPERTYCARD", "DOWNLOAD_DIR_FERFAR", "DOWNLOAD_DIR_SATBARA", "DOWNLOAD_DIR_8A"]:
        p = os.getenv(env_key)
        if p:
            os.makedirs(os.path.join(os.getcwd(), p), exist_ok=True)
    
    await init_browser()
    await init_primary_login()
    loop.create_task(keep_session_alive())
    worker_state["status"] = "idle"
    
    mq_url = os.getenv("RABBITMQ_URL")
    
    while True:
        try:
            if mq_url:
                log.info(f"Connecting to RabbitMQ via URL: {mq_url.split('@')[-1]}")
                params = pika.URLParameters(mq_url)
            else:
                log.info(f"Connecting to RabbitMQ via host: {os.getenv('RABBITMQ_HOST', 'localhost')}")
                params = pika.ConnectionParameters(
                    host=os.getenv("RABBITMQ_HOST", "localhost"),
                    heartbeat=60,
                    blocked_connection_timeout=300
                )

            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            worker_state["rabbitmq_connected"] = True
            
            queues = ['property_card_queue', 'ferfar_queue', '7_12_queue', '8a_queue', 'extraction_queue']
            for queue in queues:
                channel.queue_declare(queue=queue, durable=True)
                channel.basic_consume(queue=queue, on_message_callback=handle_job)
            
            channel.basic_qos(prefetch_count=1)
            
            log.info("=" * 60)
            log.info("✅ UNIFIED LAND RECORDS WORKER ONLINE")
            log.info("=" * 60)
            
            # Run the blocking consumer in a separate thread so the 
            # asyncio loop can keep running for heartbeats and API.
            import threading
            consumer_thread = threading.Thread(target=channel.start_consuming, daemon=True)
            consumer_thread.start()
            
            # Keep the main async loop running
            while consumer_thread.is_alive():
                await asyncio.sleep(1)
            
        except pika.exceptions.ConnectionClosedByBroker:
            worker_state["rabbitmq_connected"] = False
            log.warning("Connection closed by broker, retrying in 5s...")
            await asyncio.sleep(5)
            continue
        except pika.exceptions.AMQPChannelError as e:
            worker_state["rabbitmq_connected"] = False
            log.error(f"Channel error: {e}, stopping...")
            break
        except pika.exceptions.AMQPConnectionError:
            worker_state["rabbitmq_connected"] = False
            log.warning("Connection failed/lost, retrying in 5s...")
            await asyncio.sleep(5)
            continue
        except Exception as e:
            worker_state["rabbitmq_connected"] = False
            log.error(f"Unexpected error in worker loop: {e}")
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    setup_signal_handlers()
    try:
        loop.run_until_complete(run_worker())
    except KeyboardInterrupt:
        log.info("Worker stopped by user")
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'browser' in globals() and browser:
            loop.run_until_complete(browser.close())
        if 'playwright' in globals() and playwright:
            loop.run_until_complete(playwright.stop())