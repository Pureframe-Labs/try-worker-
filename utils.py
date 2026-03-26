import os
import logging
import base64
import httpx
from dotenv import load_dotenv

# -------------------------------------------------
# LOAD ENVIRONMENT VARIABLES
# -------------------------------------------------
load_dotenv()

# URLs for different services
PROPERTY_CARD_URL = os.getenv("PROPERTY_CARD_URL", "https://digitalsatbara.mahabhumi.gov.in/DSLR/PropertyCard/PropertyCard")
FERFAR_URL = os.getenv("FERFAR_URL", "https://digitalsatbara.mahabhumi.gov.in/DSLR/Satbara/eFerfar")
SEVEN_TWELVE_URL = os.getenv("SEVEN_TWELVE_URL", "https://digitalsatbara.mahabhumi.gov.in/DSLR/Satbara/LiveSatBara")
EIGHT_A_URL = os.getenv("EIGHT_A_URL", "https://digitalsatbara.mahabhumi.gov.in/DSLR/Satbara/Live8a")

# Credentials (same for all services)
SATBARA_USER_ID = os.getenv("SATBARA_USER_ID")
SATBARA_PASSWORD = os.getenv("SATBARA_PASSWORD")

# OpenAI for captcha
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# -------------------------------------------------
# LOGGING SETUP
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("unified-worker")

# -------------------------------------------------
# CAPTCHA SOLVER (OpenAI Vision)
# -------------------------------------------------
async def solve_captcha(image_bytes: bytes) -> str:
    """Solves captcha using OpenAI Vision API"""
    if not OPENAI_API_KEY:
        log.error("❌ OPENAI_API_KEY is missing from .env")
        return ""

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Read the captcha and return ONLY the 5-character text."},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                            ]
                        }
                    ],
                    "max_tokens": 10
                }
            )
            
            response.raise_for_status() 
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
            
        except httpx.HTTPStatusError as e:
            log.error(f"❌ OpenAI API Error: {e.response.status_code} - {e.response.text}")
            return ""
        except Exception as e:
            log.error(f"❌ Unexpected Captcha Error: {e}")
            return ""

# Database config for Supabase Data API
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
# Supports both legacy JWT keys (eyJ...) and new publishable keys (sb_publishable_...)
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY",
               os.getenv("SUPABASE_ANON_KEY",
               os.getenv("SUPABASE_PUBLISHABLE_DEFAULT_KEY", "")))

if not SUPABASE_URL or not SUPABASE_KEY:
    log.warning("⚠️ SUPABASE_URL or SUPABASE_SERVICE_KEY/SUPABASE_ANON_KEY is not set!")

# Lazy Supabase client — initialized on first use to prevent import-time crashes
_supabase_client = None

def get_supabase():
    """Return the Supabase client, initializing it lazily on first call."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("❌ Cannot create Supabase client: URL or KEY is missing.")
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("✅ Supabase client initialized.")
        return _supabase_client
    except Exception as e:
        log.error(f"❌ Failed to create Supabase client: {e}")
        log.error("   → Check that SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY is a valid JWT (starts with eyJ...)")
        return None

# Keep a module-level alias for backward compatibility (resolves lazily)
class _SupabaseLazyProxy:
    """Thin proxy that forwards attribute access to the lazy client."""
    def __getattr__(self, name):
        client = get_supabase()
        if client is None:
            raise RuntimeError("Supabase client is not available. Check your API key.")
        return getattr(client, name)
    def __bool__(self):
        return get_supabase() is not None

supabase = _SupabaseLazyProxy()

# -------------------------------------------------
# DATABASE FUNCTIONS
# -------------------------------------------------
def update_db(doc_type: str, req_id: int, status: str, filename: str = None) -> None:
    """
    Update database record for any document type
    
    Args:
        doc_type: One of 'property_card', 'ferfar', '7_12', '8a'
        req_id: Request ID
        status: New status
        filename: PDF filename (optional)
    """
    table_map = {
        'property_card': 'requests_property_card',
        'ferfar': 'requests_ferfar',
        '7_12': 'requests_7_12',
        '8a': 'requests_8a'
    }
    
    table_name = table_map.get(doc_type)
    if not table_name:
        log.error(f"❌ Invalid doc_type: {doc_type}")
        return
        
    client = get_supabase()
    if not client:
        log.error("❌ Supabase client not initialized")
        return
    
    try:
        from datetime import datetime
        update_data = {
            "status": status,
            "updated_at": datetime.utcnow().isoformat()
        }
        if filename:
            update_data["pdf_url"] = filename
            
        client.table(table_name).update(update_data).eq("id", req_id).execute()
        
        log.info(f"✅ DB Updated → {doc_type.upper()} ID {req_id}: {status}")
    except Exception as e:
        log.error(f"❌ DB Update Error for {doc_type} ID {req_id}: {e}")

def get_db_status(doc_type: str, req_id: int) -> str:
    """Get current status of a request"""
    table_map = {
        'property_card': 'requests_property_card',
        'ferfar': 'requests_ferfar',
        '7_12': 'requests_7_12',
        '8a': 'requests_8a'
    }
    
    table_name = table_map.get(doc_type)
    if not table_name:
        return "invalid_doc_type"
        
    client = get_supabase()
    if not client:
        log.error("❌ Supabase client not initialized")
        return "error"
    
    try:
        response = client.table(table_name).select("status").eq("id", req_id).execute()
        data = response.data
        return data[0]["status"] if data else "not_found"
    except Exception as e:
        log.error(f"❌ Database Query Error: {e}")
        return "error"

def get_credentials(doc_type: str) -> tuple:
    """Get username and password for a specific document type"""
    # All services use the same credentials
    return (SATBARA_USER_ID, SATBARA_PASSWORD)

def get_url(doc_type: str) -> str:
    """Get URL for a specific document type"""
    url_map = {
        'property_card': PROPERTY_CARD_URL,
        'ferfar': FERFAR_URL,
        '7_12': SEVEN_TWELVE_URL,
        '8a': EIGHT_A_URL
    }
    return url_map.get(doc_type, "")