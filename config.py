import os
from dotenv import load_dotenv
import json # Import json module

# Load environment variables from .env file
load_dotenv()

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") # Not typically used server-side with service role
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# WhatsApp (pywa)
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID") # Optional
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET") # Optional
WHATSAPP_WEBHOOK_ENDPOINT = os.getenv("WHATSAPP_WEBHOOK_ENDPOINT", "/webhook")

# DeepSeek
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")

# Chatbot
BOT_NAME = os.getenv("BOT_NAME", "Aska")
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", 4000)) # Ensure it's an int

# --- Google Sheet Configuration ---
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "User") # Default sheet name

# Securely load Google Service Account credentials
# Option 1: From an environment variable containing the JSON string
GOOGLE_SERVICE_ACCOUNT_JSON_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_STR")
GOOGLE_SERVICE_ACCOUNT_INFO = None
if GOOGLE_SERVICE_ACCOUNT_JSON_STR:
    try:
        GOOGLE_SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON_STR)
        print("Successfully loaded Google Service Account info from GOOGLE_SERVICE_ACCOUNT_JSON_STR.")
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON_STR: {e}. Ensure it's a valid JSON string.")
        GOOGLE_SERVICE_ACCOUNT_INFO = None # Ensure it's None if parsing fails
else:
    print("WARNING: GOOGLE_SERVICE_ACCOUNT_JSON_STR environment variable is not set. Google API calls requiring service account auth will fail.")

# Option 2: Fallback to file path (less secure for production, use with caution)
# This is kept for local development convenience if GOOGLE_SERVICE_ACCOUNT_JSON_STR is not set.
# In production, GOOGLE_SERVICE_ACCOUNT_JSON_STR should always be used.
GOOGLE_SERVICE_ACCOUNT_FILE_PATH_ENV = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") # Original env var for file path
GOOGLE_SERVICE_ACCOUNT_FILE = "service_account.json" # Default file name if path not in env

if not GOOGLE_SERVICE_ACCOUNT_INFO and GOOGLE_SERVICE_ACCOUNT_FILE_PATH_ENV:
    GOOGLE_SERVICE_ACCOUNT_FILE = GOOGLE_SERVICE_ACCOUNT_FILE_PATH_ENV # Use path from env if provided
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        print(f"WARNING: Loading Google Service Account info from file '{GOOGLE_SERVICE_ACCOUNT_FILE}'. "
              "For production, using GOOGLE_SERVICE_ACCOUNT_JSON_STR is recommended.")
        # The actual loading of the file content into credentials happens in the Google API client library,
        # using this GOOGLE_SERVICE_ACCOUNT_FILE path.
    else:
        print(f"ERROR: Google Service Account file '{GOOGLE_SERVICE_ACCOUNT_FILE}' specified by GOOGLE_SERVICE_ACCOUNT_FILE env var not found, "
              "and GOOGLE_SERVICE_ACCOUNT_JSON_STR is not set. Google API calls requiring service account auth will fail.")
        # Set GOOGLE_SERVICE_ACCOUNT_FILE to None or handle appropriately if file must exist
        GOOGLE_SERVICE_ACCOUNT_FILE = None # Indicates file is not available
elif not GOOGLE_SERVICE_ACCOUNT_INFO and not GOOGLE_SERVICE_ACCOUNT_FILE_PATH_ENV:
     # If JSON_STR is not set and GOOGLE_SERVICE_ACCOUNT_FILE env var is also not set,
     # check for the default "service_account.json" file.
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        print(f"WARNING: Loading Google Service Account info from default file '{GOOGLE_SERVICE_ACCOUNT_FILE}'. "
              "For production, using GOOGLE_SERVICE_ACCOUNT_JSON_STR is recommended.")
    else:
        print(f"ERROR: Default Google Service Account file '{GOOGLE_SERVICE_ACCOUNT_FILE}' not found, "
              "and GOOGLE_SERVICE_ACCOUNT_JSON_STR is not set. Google API calls requiring service account auth will fail.")
        GOOGLE_SERVICE_ACCOUNT_FILE = None # Indicates file is not available


# --- Application Settings ---
MAIN_APP_URL = os.getenv("MAIN_APP_URL", "http://localhost:8000") # Default to localhost if not set

# Internal API Key (for securing internal endpoints)
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

# Define endpoint path as a constant if it's shared
INITIATE_MESSAGE_ENDPOINT_PATH = "/internal/send-initial-message" # Ensure this matches the endpoint in main.py

# Define total number of bootcamp chapters (centralized here)
TOTAL_BOOTCAMP_CHAPTERS = 11

# Define a default number of quiz questions if not parsable, for progress display
DEFAULT_QUIZ_QUESTIONS_COUNT = 5

# Constants for message handling and interaction
MESSAGE_CHUNK_DELAY_SECONDS = float(os.getenv("MESSAGE_CHUNK_DELAY_SECONDS", 1.0)) # Delay in seconds between sending message chunks
TYPING_INDICATOR_DELAY_BEFORE_MESSAGE_SECONDS = float(os.getenv("TYPING_INDICATOR_DELAY_BEFORE_MESSAGE_SECONDS", 0.75)) # Delay after showing typing, before sending message

VALID_GREETINGS = [
    "hi", "hello", "hey", "greetings", "hai", "helo", "halo",
    "good morning", "good afternoon", "good evening",
    "morning", "afternoon", "evening"
]


# Basic validation
critical_vars_map = {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
    "WHATSAPP_VERIFY_TOKEN": WHATSAPP_VERIFY_TOKEN,
    "WHATSAPP_PHONE_ID": WHATSAPP_PHONE_ID,
    "WHATSAPP_ACCESS_TOKEN": WHATSAPP_ACCESS_TOKEN,
    "WHATSAPP_WEBHOOK_ENDPOINT": WHATSAPP_WEBHOOK_ENDPOINT,
    "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
    "INTERNAL_API_KEY": INTERNAL_API_KEY,
    "GOOGLE_SHEET_ID": GOOGLE_SHEET_ID,
    "MAIN_APP_URL": MAIN_APP_URL
}
# Add GOOGLE_SERVICE_ACCOUNT_INFO or GOOGLE_SERVICE_ACCOUNT_FILE to validation
# This logic is a bit more complex due to the two loading options.
google_auth_configured = bool(GOOGLE_SERVICE_ACCOUNT_INFO or (GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE)))
if not google_auth_configured:
    print("CRITICAL ERROR: Google Service Account credentials are not configured correctly (neither GOOGLE_SERVICE_ACCOUNT_JSON_STR nor a valid file path).")
    # You might want to add this to missing_vars or handle it as a critical failure.

missing_vars = [name for name, var in critical_vars_map.items() if not var]
if not google_auth_configured:
    missing_vars.append("Google Service Account Credentials (JSON string or file)")


if missing_vars:
    error_message = f"CRITICAL ERROR: One or more critical environment variables/configurations are missing or invalid: {', '.join(missing_vars)}. Please check your .env file or environment configuration. Application may not function correctly."
    print(error_message)
    # Consider raising an error here if running in a context where startup should fail hard
    # raise ValueError(error_message)

print(f"Config loaded. WHATSAPP_WEBHOOK_ENDPOINT is set to: {WHATSAPP_WEBHOOK_ENDPOINT}")
print(f"Message Chunk Delay: {MESSAGE_CHUNK_DELAY_SECONDS}s")
print(f"Typing Indicator Delay: {TYPING_INDICATOR_DELAY_BEFORE_MESSAGE_SECONDS}s")
print(f"Valid Greetings Loaded: {len(VALID_GREETINGS)} greetings configured.")
print(f"Google Sheet Name configured: {GOOGLE_SHEET_NAME}")
if GOOGLE_SERVICE_ACCOUNT_INFO:
    print("Google Service Account: Loaded from JSON string.")
elif GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
    print(f"Google Service Account: Will attempt to load from file '{GOOGLE_SERVICE_ACCOUNT_FILE}'.")
else:
    print("Google Service Account: NOT CONFIGURED.")

