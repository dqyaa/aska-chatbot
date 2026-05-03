import asyncio
import os
import re
import time
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from typing import Optional, Dict, Any, List, Tuple
import httpx

# Load environment variables from .env file
load_dotenv()

# Assuming your existing config.py and database.py are in the python path
import config # This will have GOOGLE_SERVICE_ACCOUNT_INFO, GOOGLE_SERVICE_ACCOUNT_FILE, etc.
import database # This should give access to the initialized Supabase client

# --- Logging Setup ---
LOG_FILE = "sync_service.log"
logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s")
    
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# --- Configuration from config.py ---
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
# Credentials will be loaded dynamically based on config

SHEET_ID = config.GOOGLE_SHEET_ID
# SHEET_NAME is now consistently from config.py
SHEET_NAME = config.GOOGLE_SHEET_NAME

HEADER_ROW_NUM = 1 # Assuming header is in row 1
DATA_START_ROW_NUM = int(os.getenv("GOOGLE_SHEET_DATA_START_ROW", "2"))
# Default to A:D; Assumes: Date, Email, Phone, Name. Ensure this matches your sheet.
SHEET_COLUMNS_TO_FETCH = os.getenv("GOOGLE_SHEET_COLUMNS_TO_FETCH", "A:D")

# Construct the full range for initial data fetch using config.SHEET_NAME
# Example: If SHEET_COLUMNS_TO_FETCH is "A:D" and DATA_START_ROW_NUM is 2,
# RANGE_NAME_FULL will be "YourSheetName!A2:D"
try:
    start_col_initial = SHEET_COLUMNS_TO_FETCH.split(':')[0]
    end_col_initial = SHEET_COLUMNS_TO_FETCH.split(':')[1]
    RANGE_NAME_FULL = f"{SHEET_NAME}!{start_col_initial}{DATA_START_ROW_NUM}:{end_col_initial}"
except IndexError:
    logger.critical(f"Invalid GOOGLE_SHEET_COLUMNS_TO_FETCH format: '{SHEET_COLUMNS_TO_FETCH}'. Expected format like 'A:D'.")
    RANGE_NAME_FULL = f"{SHEET_NAME}!A{DATA_START_ROW_NUM}:D" # Fallback, adjust if needed
    logger.warning(f"Using fallback RANGE_NAME_FULL: {RANGE_NAME_FULL}")


KEY_COLUMN_FOR_ROW_COUNT = os.getenv("GOOGLE_SHEET_KEY_COLUMN_FOR_COUNT", "B")

POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "60"))
MAX_RETRIES = int(os.getenv("MAX_API_RETRIES", "3"))
INITIAL_BACKOFF_SECONDS = int(os.getenv("INITIAL_BACKOFF_SECONDS", "5"))

MAIN_APP_URL = config.MAIN_APP_URL
INTERNAL_API_KEY = config.INTERNAL_API_KEY
INITIATE_MESSAGE_ENDPOINT_PATH = config.INITIATE_MESSAGE_ENDPOINT_PATH

# --- State Management ---
STATE_FILE = "sync_state.json"
LAST_PROCESSED_ROW_COUNT_KEY = "last_processed_row_count"

def load_sync_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                logger.info(f"Loaded sync state from {STATE_FILE}: {state}")
                return state
    except Exception as e:
        logger.error(f"Error loading state from {STATE_FILE}: {e}. Starting with default state.")
    # Default state: last processed is the header row, so next processing starts from DATA_START_ROW_NUM
    return {LAST_PROCESSED_ROW_COUNT_KEY: HEADER_ROW_NUM}

def save_sync_state(state: dict):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
        logger.info(f"Saved sync state to {STATE_FILE}: {state}")
    except Exception as e:
        logger.error(f"Error saving state to {STATE_FILE}: {e}")

# --- Retry Decorator ---
def retry_async(max_retries=MAX_RETRIES, initial_backoff=INITIAL_BACKOFF_SECONDS, exceptions_to_catch=(HttpError, asyncio.TimeoutError, httpx.RequestError, Exception)):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            retries = 0
            backoff = initial_backoff
            while retries < max_retries:
                try:
                    return await func(*args, **kwargs)
                except exceptions_to_catch as e:
                    retries += 1
                    logger.warning(f"Attempt {retries}/{max_retries} for {func.__name__} failed: {type(e).__name__} - {e}. Retrying in {backoff}s...")
                    if retries >= max_retries:
                        logger.error(f"Max retries reached for {func.__name__}. Error: {type(e).__name__} - {e}", exc_info=True)
                        raise
                    await asyncio.sleep(backoff)
                    backoff *= 2
        return wrapper
    return decorator

# --- Helper Functions ---
def get_google_credentials():
    """Loads Google credentials based on config.py settings."""
    if config.GOOGLE_SERVICE_ACCOUNT_INFO:
        logger.info("Using Google Service Account info from JSON string (config.GOOGLE_SERVICE_ACCOUNT_INFO).")
        return Credentials.from_service_account_info(config.GOOGLE_SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    elif config.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
        logger.info(f"Using Google Service Account file: {config.GOOGLE_SERVICE_ACCOUNT_FILE}")
        return Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    else:
        logger.critical("Google Service Account credentials NOT CONFIGURED in config.py (neither JSON_STR nor valid file path).")
        return None

@retry_async()
async def get_google_sheet_data_api(service, sheet_range: str) -> Optional[List[List[Any]]]:
    logger.info(f"Attempting to fetch Google Sheet data for Sheet ID: '{SHEET_ID}', Range: '{sheet_range}'")
    sheet = service.spreadsheets()
    result = await asyncio.to_thread(
        lambda: sheet.values().get(spreadsheetId=SHEET_ID, range=sheet_range).execute()
    )
    values = result.get('values', [])
    logger.info(f"Successfully fetched {len(values)} rows for range: '{sheet_range}'")
    return values

async def parse_sheet_rows(rows: List[List[Any]], sheet_range_info: str) -> List[Dict[str, Any]]:
    # SHEET_COLUMNS_TO_FETCH (e.g., "A:D") defines the columns we expect to get.
    # Example: If A:D, we expect 4 columns.
    # header_map assumes the *order* of columns fetched matches: Date, Email, Phone, Name
    # This mapping is relative to the fetched columns.
    # Column 0 of `rows` -> "date", Column 1 -> "email", etc.
    column_map_default = {0: "date", 1: "email", 2: "phone_number_raw", 3: "name"}
    
    # Determine how many columns we actually expect based on SHEET_COLUMNS_TO_FETCH
    try:
        start_col_char_parse, end_col_char_parse = SHEET_COLUMNS_TO_FETCH.split(':')
        num_expected_columns = ord(end_col_char_parse.upper()) - ord(start_col_char_parse.upper()) + 1
    except Exception as e_col_parse:
        logger.error(f"Could not parse SHEET_COLUMNS_TO_FETCH ('{SHEET_COLUMNS_TO_FETCH}') to determine expected columns: {e_col_parse}. Defaulting to 4.")
        num_expected_columns = 4 # Default if parsing fails (e.g., A:D)

    header_map = {i: column_map_default[i] for i in range(num_expected_columns) if i in column_map_default}
    if len(header_map) != num_expected_columns:
        logger.warning(f"Mismatch between expected columns ({num_expected_columns} from '{SHEET_COLUMNS_TO_FETCH}') and mapped columns ({len(header_map)}). Check column_map_default and SHEET_COLUMNS_TO_FETCH.")

    formatted_data = []
    for i, row_data in enumerate(rows):
        # Determine the actual row number in the sheet for logging
        actual_row_number_in_sheet = DATA_START_ROW_NUM + i # Default if range_info is simple
        if "!" in sheet_range_info: # e.g. "SheetName!A5:D10"
            try:
                range_part = sheet_range_info.split('!')[1] # "A5:D10"
                # Regex to find the first number (start row) in the cell part of the range
                match = re.search(r'[A-Za-z]+(\d+)', range_part)
                if match:
                    actual_row_number_in_sheet = int(match.group(1)) + i
            except Exception as e_parse_row:
                logger.warning(f"Could not parse start row from sheet_range_info '{sheet_range_info}' for row {i}: {e_parse_row}. Using default calculation.")

        if not any(str(cell_val).strip() for cell_idx, cell_val in enumerate(row_data) if cell_idx < len(header_map)):
            logger.debug(f"Skipping empty or sparsely populated row {actual_row_number_in_sheet} from range '{sheet_range_info}'.")
            continue

        record = {}
        valid_record = True
        
        for col_idx, col_name in header_map.items():
            if col_idx < len(row_data) and row_data[col_idx] is not None:
                record[col_name] = str(row_data[col_idx]).strip()
            else:
                record[col_name] = None
        
        if record.get("email"):
            record["email"] = record["email"].lower()
            if "@" not in record["email"] or "." not in record["email"].split('@')[-1]:
                logger.warning(f"Row {actual_row_number_in_sheet} (Range: '{sheet_range_info}'): Invalid email format '{record['email']}'. Skipping record.")
                valid_record = False
        else:
            logger.warning(f"Row {actual_row_number_in_sheet} (Range: '{sheet_range_info}'): Missing email. Skipping record.")
            valid_record = False
        
        if not record.get("name"):
            record["name"] = "User"
            logger.debug(f"Row {actual_row_number_in_sheet} (Range: '{sheet_range_info}'): Missing name, defaulting to 'User'.")

        if valid_record:
            formatted_data.append(record)
        else:
            logger.debug(f"Skipped invalid record from sheet (Row {actual_row_number_in_sheet}, Range: '{sheet_range_info}'): {record}")
            
    logger.info(f"Successfully parsed {len(formatted_data)} valid records from {len(rows)} raw rows (Range: '{sheet_range_info}').")
    return formatted_data

async def get_google_sheet_data_orchestrator(sheet_range: str, creds) -> Optional[List[Dict[str, Any]]]:
    if not SHEET_ID:
        logger.error("GOOGLE_SHEET_ID not configured. Skipping sheet data fetch.")
        return None
    if not creds:
        logger.error("Google credentials not available. Skipping sheet data fetch.")
        return None

    try:
        service = await asyncio.to_thread(lambda: build('sheets', 'v4', credentials=creds, cache_discovery=False))
        raw_values = await get_google_sheet_data_api(service, sheet_range)

        if raw_values is None:
            logger.error(f"Failed to fetch data from Google Sheet (ID: '{SHEET_ID}', Range: '{sheet_range}') after retries.")
            return None
        if not raw_values:
            logger.info(f"No data found in Google Sheet (ID: '{SHEET_ID}', Range: '{sheet_range}').")
            return []
        
        return await parse_sheet_rows(raw_values, sheet_range)
        
    except HttpError as e:
        error_reason = e._get_reason() if hasattr(e, '_get_reason') else str(e)
        status_code = e.resp.status if hasattr(e, 'resp') and hasattr(e.resp, 'status') else 'N/A'
        logger.error(f"Google API HttpError for range '{sheet_range}': {status_code} - {error_reason}", exc_info=True)
        if status_code == 400 and "Unable to parse range" in error_reason:
            logger.critical(f"CRITICAL: The range '{sheet_range}' is invalid. Check SHEET_NAME ('{SHEET_NAME}') and column/row definitions in config and environment.")
        return None
    except Exception as e:
        logger.error(f"Error in get_google_sheet_data_orchestrator for range '{sheet_range}': {e}", exc_info=True)
        return None

@retry_async()
async def get_current_sheet_row_count(service) -> int:
    # Construct range to check for data in the key column, e.g., "YourSheetName!B1:B"
    range_to_check = f"{SHEET_NAME}!{KEY_COLUMN_FOR_ROW_COUNT}{HEADER_ROW_NUM}:{KEY_COLUMN_FOR_ROW_COUNT}"
    logger.info(f"Fetching column '{KEY_COLUMN_FOR_ROW_COUNT}' to determine row count from range '{range_to_check}'.")
    
    sheet = service.spreadsheets()
    result = await asyncio.to_thread(
        lambda: sheet.values().get(spreadsheetId=SHEET_ID, range=range_to_check).execute()
    )
    values = result.get('values', [])
    
    for i in range(len(values) - 1, -1, -1):
        if values[i] and any(str(cell).strip() for cell in values[i] if cell is not None):
            last_row_with_data_in_key_col = HEADER_ROW_NUM + i # Relative to start of fetch (HEADER_ROW_NUM)
            logger.info(f"Determined last row with data in key column '{KEY_COLUMN_FOR_ROW_COUNT}' of sheet '{SHEET_NAME}' is: {last_row_with_data_in_key_col}")
            return last_row_with_data_in_key_col
            
    logger.info(f"No data found in key column '{KEY_COLUMN_FOR_ROW_COUNT}' of sheet '{SHEET_NAME}' beyond header. Assuming only header row or empty.")
    return HEADER_ROW_NUM

def clean_phone_number_for_whatsapp(phone_number_str: str) -> Optional[str]:
    """
    Cleans an international phone number string to produce a WhatsApp ID (digits only, no '+').
    Handles common international prefixes like '+' or '00'.
    It does NOT make assumptions about specific country codes like Malaysia unless
    the number is already formatted with a country code after a '+'.
    """
    if not phone_number_str or not isinstance(phone_number_str, str):
        logger.warning(f"Input phone number is None or not a string: {phone_number_str}")
        return None

    original_input = str(phone_number_str) 
    logger.debug(f"Original phone input for WhatsApp ID cleaning: {original_input}")

    cleaned_number = original_input.strip()

    if cleaned_number.startswith('00'):
        cleaned_number = '+' + cleaned_number[2:]
        logger.debug(f"Converted '00' prefix: {cleaned_number}")
    
    if cleaned_number.startswith('+'):
        digits_part = re.sub(r'[^\d]', '', cleaned_number[1:])
        logger.debug(f"Digits part after stripping '+': {digits_part} (from {cleaned_number})")
    else:
        digits_part = re.sub(r'[^\d]', '', cleaned_number)
        logger.debug(f"Digits part (no '+' prefix found/added): {digits_part} (from {cleaned_number})")

    if not digits_part:
        logger.warning(f"After all cleaning of '{original_input}', the resulting WhatsApp ID (digits_part) is empty. Skipping.")
        return None

    if not digits_part.isdigit():
        logger.error(f"Critical error: After cleaning '{original_input}', result '{digits_part}' still contains non-digits. This should not happen. Skipping.")
        return None

    min_len, max_len = 9, 15 
    if not (min_len <= len(digits_part) <= max_len):
        logger.warning(f"Cleaned WhatsApp ID '{digits_part}' (from original '{original_input}') has length {len(digits_part)}, which is outside the expected range [{min_len}-{max_len}]. Skipping.")
        return None
        
    logger.info(f"Successfully cleaned phone '{original_input}' to WhatsApp ID (digits only): '{digits_part}'")
    return digits_part

async def send_initial_messages_via_api(users_to_message: List[Dict[str, Any]]):
    if not MAIN_APP_URL or not INITIATE_MESSAGE_ENDPOINT_PATH or not INTERNAL_API_KEY:
        logger.error("Main app URL, endpoint path, or internal API key for sending messages is not configured. Skipping API calls.")
        return

    sent_count = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"X-Internal-API-Key": INTERNAL_API_KEY, "Content-Type": "application/json"}
        for user_db_record in users_to_message:
            user_db_id = str(user_db_record["id"])
            name = user_db_record.get("name", "User")
            
            whatsapp_id_to_send = user_db_record.get("whatsapp_id") # This should be the cleaned one from DB
            
            if not whatsapp_id_to_send:
                raw_phone_from_db = user_db_record.get("phone_number")
                if not raw_phone_from_db:
                    logger.info(f"User {name} (DB ID: {user_db_id}) has no phone_number or whatsapp_id in DB. Skipping initial message.")
                    continue
                whatsapp_id_to_send = clean_phone_number_for_whatsapp(raw_phone_from_db)

            if not whatsapp_id_to_send:
                logger.warning(f"User {name} (DB ID: {user_db_id}) phone details could not be formed into a valid WhatsApp ID. Skipping initial message.")
                continue

            payload_for_api = {"whatsapp_id": whatsapp_id_to_send, "name": name, "user_db_id": user_db_id}
            try:
                api_url = f"{MAIN_APP_URL.rstrip('/')}{INITIATE_MESSAGE_ENDPOINT_PATH}"
                logger.info(f"Calling API to send initial message: POST {api_url} for {name} ({whatsapp_id_to_send})")
                response = await client.post(api_url, json=payload_for_api, headers=headers)
                
                if response.status_code == 200:
                    logger.info(f"Successfully triggered initial message for {name} ({whatsapp_id_to_send}) via API. Response: {response.json()}")
                    sent_count += 1
                elif response.status_code == 403:
                    logger.error(f"API Key Error: Failed to trigger initial message for {name} ({whatsapp_id_to_send}). Status: {response.status_code}. Response: {response.text}")
                else:
                    logger.error(f"Error triggering initial message for {name} ({whatsapp_id_to_send}) via API: {response.status_code} - {response.text}")
            except httpx.RequestError as e_req:
                logger.error(f"Request error sending initial message for {name} ({whatsapp_id_to_send}): {e_req}")
            except Exception as e_api_call:
                logger.error(f"Unexpected error during API call for {name} ({whatsapp_id_to_send}): {e_api_call}", exc_info=True)
    
    if sent_count > 0:
        logger.info(f"Attempted to trigger initial messages for {sent_count} users via API.")

async def process_and_trigger_initial_messages():
    logger.info("Checking for users in DB to send initial messages...")
    users_needing_message_from_db = await database.get_users_for_initial_message()
    
    if users_needing_message_from_db:
        logger.info(f"Found {len(users_needing_message_from_db)} users in DB who need an initial message.")
        await send_initial_messages_via_api(users_needing_message_from_db)
    else:
        logger.info("No users found in DB currently needing an initial message.")

async def sync_initial_rows_to_supabase(creds):
    sync_state = load_sync_state()
    
    logger.info(f"[{datetime.now()}] Starting initial sync of all rows from Google Sheet '{SHEET_NAME}' to Supabase...")
    logger.info(f"Fetching all data from range: '{RANGE_NAME_FULL}'")
    
    if not database.supabase:
        logger.critical("Supabase client in database.py is not initialized. Aborting initial sync.")
        return

    sheet_data_list = await get_google_sheet_data_orchestrator(RANGE_NAME_FULL, creds)

    if sheet_data_list is not None:
        if sheet_data_list:
            logger.info(f"Fetched {len(sheet_data_list)} valid records for initial sync from '{SHEET_NAME}'.")
            
            db_ready_sheet_data = []
            for sheet_rec in sheet_data_list:
                raw_phone = sheet_rec.get("phone_number_raw")
                cleaned_wa_id = clean_phone_number_for_whatsapp(raw_phone) if raw_phone else None
                
                db_ready_sheet_data.append({
                    "email": sheet_rec.get("email"), "name": sheet_rec.get("name"),
                    "phone_number": raw_phone, "whatsapp_id": cleaned_wa_id,
                    "date": sheet_rec.get("date")
                })

            upserted_count = await database.upsert_users_from_sheet(db_ready_sheet_data)
            logger.info(f"Successfully upserted/updated {upserted_count} records in Supabase from '{SHEET_NAME}'.")
            
            if upserted_count > 0:
                await process_and_trigger_initial_messages()
        else:
            logger.info(f"No data found in Google Sheet '{SHEET_NAME}' for initial sync.")

        try:
            service = await asyncio.to_thread(lambda: build('sheets', 'v4', credentials=creds, cache_discovery=False))
            current_sheet_total_rows = await get_current_sheet_row_count(service)
            sync_state[LAST_PROCESSED_ROW_COUNT_KEY] = current_sheet_total_rows
            save_sync_state(sync_state)
            logger.info(f"Updated sync state: last_processed_row_count for '{SHEET_NAME}' set to {current_sheet_total_rows}.")
        except Exception as e_count:
            logger.error(f"Error determining total rows for state update after initial sync of '{SHEET_NAME}': {e_count}. State might be inaccurate.", exc_info=True)
            fallback_count = (len(sheet_data_list) + HEADER_ROW_NUM) if sheet_data_list else HEADER_ROW_NUM
            sync_state[LAST_PROCESSED_ROW_COUNT_KEY] = fallback_count
            save_sync_state(sync_state)
            logger.warning(f"Using fallback count for sync state: {fallback_count} for '{SHEET_NAME}'.")
    else:
        logger.error(f"Error occurred during initial Google Sheet data fetch for '{SHEET_NAME}'. State of LAST_PROCESSED_ROW_COUNT may be inaccurate.")
        await process_and_trigger_initial_messages()
    
    logger.info(f"[{datetime.now()}] Initial sync process finished for '{SHEET_NAME}'.")

async def monitor_and_sync_new_rows(creds):
    logger.info(f"[{datetime.now()}] Starting continuous monitoring for new rows in sheet '{SHEET_NAME}'...")

    if not database.supabase:
        logger.critical("Supabase client in database.py is not initialized. Aborting monitoring.")
        return
    if not creds:
        logger.critical("Google credentials not available for monitoring. Aborting.")
        return

    google_api_service = await asyncio.to_thread(lambda: build('sheets', 'v4', credentials=creds, cache_discovery=False))

    while True:
        sync_state = load_sync_state()
        last_processed_row_count = sync_state.get(LAST_PROCESSED_ROW_COUNT_KEY, HEADER_ROW_NUM)
        
        try:
            logger.info(f"Checking for new rows in '{SHEET_NAME}'. Last processed actual row number in sheet: {last_processed_row_count}")
            current_total_rows_in_sheet = await get_current_sheet_row_count(google_api_service)

            if current_total_rows_in_sheet > last_processed_row_count:
                num_new_rows = current_total_rows_in_sheet - last_processed_row_count
                logger.info(f"Found {num_new_rows} potential new row(s) in '{SHEET_NAME}'.")

                new_rows_start_sheet_num = last_processed_row_count + 1
                new_rows_end_sheet_num = current_total_rows_in_sheet
                
                start_col_char_new, end_col_char_new = SHEET_COLUMNS_TO_FETCH.split(':')
                range_for_new_rows = f"{SHEET_NAME}!{start_col_char_new}{new_rows_start_sheet_num}:{end_col_char_new}{new_rows_end_sheet_num}"
                
                logger.info(f"Fetching new rows from range: '{range_for_new_rows}'")
                new_sheet_data = await get_google_sheet_data_orchestrator(range_for_new_rows, creds)

                if new_sheet_data:
                    logger.info(f"Parsed {len(new_sheet_data)} new valid records from '{SHEET_NAME}'.")
                    db_ready_new_data = []
                    for sheet_rec in new_sheet_data:
                        raw_phone = sheet_rec.get("phone_number_raw")
                        cleaned_wa_id = clean_phone_number_for_whatsapp(raw_phone) if raw_phone else None
                        db_ready_new_data.append({
                            "email": sheet_rec.get("email"), "name": sheet_rec.get("name"),
                            "phone_number": raw_phone, "whatsapp_id": cleaned_wa_id,
                            "date": sheet_rec.get("date")
                        })
                    
                    new_upserted_count = await database.upsert_users_from_sheet(db_ready_new_data)
                    logger.info(f"Successfully upserted/updated {new_upserted_count} new records in Supabase from '{SHEET_NAME}'.")

                    if new_upserted_count > 0:
                        await process_and_trigger_initial_messages()
                elif new_sheet_data is None:
                    logger.error(f"Failed to fetch or parse new rows from '{range_for_new_rows}'. Will retry in next cycle.")
                else:
                    logger.info(f"No new valid rows found in the range '{range_for_new_rows}' despite row count increase.")

                sync_state[LAST_PROCESSED_ROW_COUNT_KEY] = current_total_rows_in_sheet
                save_sync_state(sync_state)
                logger.info(f"Updated sync state: last_processed_row_count for '{SHEET_NAME}' set to {current_total_rows_in_sheet}.")
            elif current_total_rows_in_sheet < last_processed_row_count:
                logger.warning(f"Row count in sheet '{SHEET_NAME}' ({current_total_rows_in_sheet}) is less than last processed ({last_processed_row_count}). Resetting count.")
                sync_state[LAST_PROCESSED_ROW_COUNT_KEY] = current_total_rows_in_sheet
                save_sync_state(sync_state)
            else:
                logger.info(f"No new rows detected in '{SHEET_NAME}'. Current total rows: {current_total_rows_in_sheet}")

        except HttpError as e:
            error_reason = e._get_reason() if hasattr(e, '_get_reason') else str(e)
            status_code = e.resp.status if hasattr(e, 'resp') and hasattr(e.resp, 'status') else 'N/A'
            logger.error(f"Google API HttpError in monitoring loop for '{SHEET_NAME}': {status_code} - {error_reason}", exc_info=True)
            if status_code == 400 and "Unable to parse range" in error_reason:
                 logger.critical(f"CRITICAL RANGE PARSE ERROR in monitoring loop for '{SHEET_NAME}'. Check sheet configuration. Service may not recover without intervention.")
                 # Consider breaking the loop or having a max critical error count if this persists
        except Exception as e:
            logger.error(f"Error in monitoring loop for '{SHEET_NAME}': {e}", exc_info=True)

        logger.info(f"Waiting for {POLLING_INTERVAL_SECONDS} seconds before next check for '{SHEET_NAME}'...")
        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

async def main():
    logger.info(f"[{datetime.now()}] Starting Google Sheet to Supabase Sync Service...")
    
    creds = get_google_credentials()
    if not creds:
        logger.critical("Failed to load Google credentials. Sync service cannot start.")
        return

    critical_configs_ok = True
    if not SHEET_ID: logger.critical("CRITICAL Error: GOOGLE_SHEET_ID is not set."); critical_configs_ok = False
    if not database.supabase: logger.critical("CRITICAL Error: Supabase client not initialized."); critical_configs_ok = False
    if not MAIN_APP_URL: logger.critical("CRITICAL Error: MAIN_APP_URL not set."); critical_configs_ok = False
    if not INTERNAL_API_KEY: logger.critical("CRITICAL Error: INTERNAL_API_KEY not set."); critical_configs_ok = False
    if not INITIATE_MESSAGE_ENDPOINT_PATH: logger.critical("CRITICAL Error: INITIATE_MESSAGE_ENDPOINT_PATH not set."); critical_configs_ok = False
    
    if not critical_configs_ok:
        logger.error("Sync service cannot proceed due to missing critical configurations.")
        return
    
    logger.info(f"Target Google Sheet ID: {SHEET_ID}")
    logger.info(f"Target Sheet Name from config: {SHEET_NAME}") # Uses config.SHEET_NAME
    logger.info(f"Data will be fetched starting from row: {DATA_START_ROW_NUM} using columns: {SHEET_COLUMNS_TO_FETCH}")
    logger.info(f"Full data fetch range for initial sync: {RANGE_NAME_FULL}") # Uses config.SHEET_NAME
    logger.info(f"Key column for row count: {KEY_COLUMN_FOR_ROW_COUNT} in sheet '{SHEET_NAME}'")
    logger.info(f"Main App URL for initial message: {MAIN_APP_URL}{INITIATE_MESSAGE_ENDPOINT_PATH}")

    await sync_initial_rows_to_supabase(creds)
    await monitor_and_sync_new_rows(creds)

if __name__ == "__main__":
    logger.info(f"Running {os.path.basename(__file__)} script directly.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user (KeyboardInterrupt).")
    except Exception as e_main:
        logger.critical(f"Main execution CRITICAL error: {e_main}", exc_info=True)
    finally:
        logger.info(f"[{datetime.now()}] Sync service shutting down.")

