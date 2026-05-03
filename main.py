from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Security, Depends, Body
from fastapi.security.api_key import APIKeyHeader
from pywa_async import WhatsApp # Ensure this is the correct import for your pywa version
from pywa import filters as fil # Ensure this is the correct import
from pywa.types import Message, CallbackButton, Button, CallbackSelection, MessageStatus, ChatAction # Added ChatAction
from pywa.handlers import MessageHandler, CallbackButtonHandler, CallbackSelectionHandler, MessageStatusHandler # Ensure these are correct
import json
from datetime import datetime, timezone, timedelta # Added timedelta
from typing import Optional, Dict, Any, List, Tuple
from pydantic import BaseModel
import asyncio
import os
import re
import logging
import traceback
import httpx

# Project-specific imports
import config
import database # Assuming learning_manager.py is removed and database.py is consolidated
import llm_integrations
import state_prompts
import bootcamp_manager

# --- Logging Setup ---
# Basic logging; consider structured logging for production.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI(
    title=f"{config.BOT_NAME} Learning Chatbot",
    description="A WhatsApp chatbot for AI bootcamp, general learning, and casual conversation.",
    version="0.7.0" # Version incremented for refactoring
)

# --- WhatsApp Client Initialization ---
try:
    wa = WhatsApp(
        phone_id=config.WHATSAPP_PHONE_ID,
        token=config.WHATSAPP_ACCESS_TOKEN,
        server=app, # Binds pywa to the FastAPI app
        verify_token=config.WHATSAPP_VERIFY_TOKEN,
        webhook_endpoint=config.WHATSAPP_WEBHOOK_ENDPOINT # Ensure this matches your Meta App config
    )
    logger.info(f"WhatsApp client initialized for phone ID: {config.WHATSAPP_PHONE_ID}, webhook endpoint: {config.WHATSAPP_WEBHOOK_ENDPOINT}")
except Exception as e:
    logger.critical(f"Failed to initialize WhatsApp client: {e}", exc_info=True)
    wa = None # Application will run but WhatsApp integration will fail

# --- Internal API Key Security ---
API_KEY_NAME = "X-Internal-API-Key"
api_key_header_scheme = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: Optional[str] = Security(api_key_header_scheme)):
    """Dependency to validate the internal API key."""
    if not api_key_header:
        logger.warning("API Key Error: X-Internal-API-Key header missing for internal endpoint.")
        raise HTTPException(status_code=403, detail="X-Internal-API-Key header missing.")
    if api_key_header == config.INTERNAL_API_KEY:
        return api_key_header
    else:
        logger.warning(f"API Key Error: Invalid API Key provided for internal endpoint: {api_key_header[:5]}...")
        raise HTTPException(status_code=403, detail="Could not validate credentials for internal API.")

# --- Message Deduplication Cache ---
# CRITICAL FOR PRODUCTION SCALABILITY:
# This in-memory cache is NOT SUITABLE for production environments running multiple workers/instances.
# Messages might be processed multiple times if they hit different instances.
# RECOMMENDATION: Replace with a distributed cache (e.g., Redis, Memcached).
processed_message_ids_cache: Dict[str, datetime] = {}
MESSAGE_CACHE_TTL_SECONDS = 120 # Increased TTL slightly

def is_message_processed(message_id: str) -> bool:
    """Checks if a message ID has been processed recently."""
    if not message_id: return False # Should not happen with valid WAMIDs
    now = datetime.now(timezone.utc)
    # Clean up expired entries
    expired_ids = [k for k, v in processed_message_ids_cache.items() if (now - v).total_seconds() > MESSAGE_CACHE_TTL_SECONDS]
    for k_id in expired_ids:
        if k_id in processed_message_ids_cache:
             del processed_message_ids_cache[k_id]
    
    if message_id in processed_message_ids_cache:
        logger.info(f"Message Deduplication: Message ID '{message_id}' already processed within TTL. Skipping.")
        return True
    processed_message_ids_cache[message_id] = now
    logger.debug(f"Message Deduplication: Added message ID '{message_id}' to cache.")
    return False

# --- WhatsApp Utility Functions ---
async def mark_as_read(
    recipient_wa_id: str, # The user's WA ID
    incoming_message_wamid: str,
    phone_number_id: Optional[str] = config.WHATSAPP_PHONE_ID, # Bot's phone ID
    access_token: Optional[str] = config.WHATSAPP_ACCESS_TOKEN,
    api_version: str = "v20.0" # Use a recent, stable API version
):
    """Marks an incoming WhatsApp message as read."""
    if not incoming_message_wamid:
        logger.warning("mark_as_read: incoming_message_wamid is missing.")
        return False
    if not phone_number_id or not access_token:
        logger.error("mark_as_read: Missing WhatsApp phone_number_id or access_token from config.")
        return False

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": incoming_message_wamid,
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            logger.debug(f"Marking as read for WAMID: {incoming_message_wamid} via {url}")
            response = await client.post(url, json=payload, headers=headers)
            if 200 <= response.status_code < 300 and response.json().get("success") is True:
                logger.info(f"Successfully marked as read for WAMID: {incoming_message_wamid}.")
                return True
            else:
                logger.error(f"Error marking as read for WAMID: {incoming_message_wamid}. Status: {response.status_code}, Response: {response.text}")
                return False
    except httpx.RequestError as e_req:
        logger.error(f"Request error for mark_as_read (WAMID: {incoming_message_wamid}): {e_req}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error in mark_as_read (WAMID: {incoming_message_wamid}): {e}", exc_info=True)
        return False

async def send_typing_on_indicator(client: WhatsApp, recipient_wa_id: str):
    """Sends a typing indicator to the user."""
    if not client or not recipient_wa_id:
        return
    try:
        logger.debug(f"Sending typing indicator to {recipient_wa_id}")
        await client.send_chat_action(action=ChatAction.TYPING, to=recipient_wa_id)
    except Exception as e:
        logger.warning(f"Could not send typing indicator to {recipient_wa_id}: {e}")


async def send_whatsapp_message_utility(
    whatsapp_client: Optional[WhatsApp],
    recipient_wa_id: str,
    text_message: str,
    buttons_list: Optional[list] = None,
):
    """Utility to send a WhatsApp message, ensuring text is not empty."""
    if not recipient_wa_id or not text_message or not text_message.strip() or not whatsapp_client:
        logger.warning(f"send_whatsapp_message_utility: Skipping send to {recipient_wa_id} - empty message, no recipient, or no client.")
        return

    try:
        # Truncate log message for brevity
        log_text = text_message.strip().replace('\n', ' ')[:70] + "..." if len(text_message.strip()) > 70 else text_message.strip().replace('\n', ' ')
        logger.info(f"Sending to {recipient_wa_id}: \"{log_text}\" Buttons: {buttons_list is not None and len(buttons_list) > 0}")
        await whatsapp_client.send_message(to=recipient_wa_id, text=text_message.strip(), buttons=buttons_list)
    except Exception as e:
        logger.error(f"Error in send_whatsapp_message_utility sending to {recipient_wa_id}: {e}", exc_info=True)

async def send_message_chunks(
    whatsapp_client: Optional[WhatsApp],
    recipient_wa_id: str,
    full_text: str,
    initial_prompt: Optional[str] = None,
    buttons_after_last_chunk: Optional[list] = None,
):
    """Sends a potentially long message in chunks, respecting WhatsApp limits."""
    if (not full_text and not initial_prompt) or not whatsapp_client:
        logger.warning(f"send_message_chunks: No text to send to {recipient_wa_id} or no client.")
        return

    if not isinstance(full_text, str):
        logger.warning(f"Warning: full_text in send_message_chunks is not a string for {recipient_wa_id}. Converting.")
        full_text = str(full_text or "") # Ensure full_text is a string

    max_len = config.MAX_MESSAGE_LENGTH
    current_message = (initial_prompt.strip() + "\n\n") if initial_prompt and initial_prompt.strip() and full_text.strip() else (initial_prompt.strip() if initial_prompt and initial_prompt.strip() else "")
    
    # If only initial_prompt is provided (and full_text is empty/None)
    if initial_prompt and not full_text.strip():
        if current_message.strip():
            logger.info(f"Sending single initial prompt to {recipient_wa_id}: \"{current_message.strip()[:70]}...\"")
            await send_whatsapp_message_utility(whatsapp_client, recipient_wa_id, current_message.strip(), buttons_after_last_chunk)
        return

    paragraphs = full_text.strip().split('\n\n')
    num_paragraphs = len(paragraphs)

    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para: # Skip empty paragraphs
            continue

        is_last_paragraph_overall = (i == num_paragraphs - 1)

        # Check if adding this paragraph exceeds max_len
        # Add 2 for potential "\n\n" if not the first part of current_message and not the last paragraph
        separator_len = 2 if current_message and not is_last_paragraph_overall else 0
        if len(current_message) + len(para) + separator_len > max_len:
            # Send current_message if it has content
            if current_message.strip():
                await send_whatsapp_message_utility(whatsapp_client, recipient_wa_id, current_message.strip())
                current_message = "" # Reset for the new paragraph
                await asyncio.sleep(config.MESSAGE_CHUNK_DELAY_SECONDS)
            
            # If the paragraph itself is too long, split it by words
            if len(para) > max_len:
                words = para.split(' ')
                temp_para_chunk = ""
                for word_idx, word in enumerate(words):
                    is_last_word_in_para = (word_idx == len(words) -1)
                    if len(temp_para_chunk) + len(word) + (1 if temp_para_chunk else 0) > max_len:
                        if temp_para_chunk.strip():
                            await send_whatsapp_message_utility(whatsapp_client, recipient_wa_id, temp_para_chunk.strip())
                            await asyncio.sleep(config.MESSAGE_CHUNK_DELAY_SECONDS)
                        temp_para_chunk = word + " "
                    else:
                        temp_para_chunk += (word + " ")
                
                current_message = temp_para_chunk.strip() # This will be the remainder of the long paragraph
            else: # Paragraph is not too long itself, start new message with it
                current_message = para
        else: # Paragraph fits
            if current_message: # If current_message has content, add separator
                current_message += "\n\n"
            current_message += para

    # Send any remaining message (the last part)
    if current_message.strip():
        await send_whatsapp_message_utility(whatsapp_client, recipient_wa_id, current_message.strip(), buttons_after_last_chunk)

# --- Language and Translation ---
def get_target_language_name(raw_preference: Optional[str]) -> str:
    """Determines target language from raw preference string."""
    if raw_preference:
        pref_lower = raw_preference.lower()
        if "malay" in pref_lower or "melayu" in pref_lower or "bm" in pref_lower:
            return "Malay"
        # Add other language checks here if needed
    return "English" # Default

async def get_translated_text(text_en: str, target_lang_name: str, context_hint: Optional[str] = None) -> str:
    """Translates English text to the target language using LLM, with fallback."""
    if not text_en or target_lang_name.lower() == "english":
        return text_en
    try:
        translated = await llm_integrations.translate_text_with_llm(text_en, target_lang_name, context_hint=context_hint)
        return translated if translated and translated.strip() else text_en # Fallback to English if translation fails or is empty
    except Exception as e:
        logger.error(f"Error during translation to {target_lang_name} for text '{text_en[:50]}...': {e}", exc_info=True)
        return text_en # Fallback to English on error

# --- Refactored State Handlers ---
# These helper functions will be called by process_user_interaction
# Each should ideally return a tuple: (next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, updated_user_profile_fields)
# updated_user_profile_fields is a dict of fields to update in the user's DB record specifically by this handler.

async def _handle_initial_greeting_or_command(
    user_profile: Dict[str, Any], 
    interaction_input: str
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any]]:
    """Handles initial greetings and global commands like /help, /mainmenu, /bootcamp."""
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode", "AWAITING_GREETING")
    next_mode = current_mode
    response_text_en = None
    keyboard_defs = None
    context_updates = {} # Specific context updates for this handler

    input_lower = interaction_input.lower().strip()

    if input_lower == "/help":
        response_text_en = state_prompts.get_state_message("HELP_MESSAGE", name=user_name)
        # No mode change for /help typically, or could go to a general help state
    elif input_lower == "/mainmenu":
        next_mode = "POST_ONBOARDING_CHOICE" if user_profile.get("has_completed_bootcamp_onboarding") else "AWAITING_BOOTCAMP_WELCOME_AND_NAME"
        if next_mode == "AWAITING_BOOTCAMP_WELCOME_AND_NAME":
            b_data = user_profile.get("bootcamp_onboarding_data", {})
            b_data["name_provided_once"] = False # Reset for name prompt
            context_updates["bootcamp_onboarding_data"] = b_data
        # Response text and keyboard will be generated by the new mode's handler
    elif input_lower.startswith(("/bootcamp", "action:main:start_bootcamp")):
        if user_profile.get("has_completed_bootcamp_onboarding"):
            # This will trigger bootcamp_manager.get_bootcamp_step in the main dispatcher
            next_mode = user_profile.get("current_mode") # Stay in current mode to let bootcamp logic drive
            interaction_input = "bootcamp:action:deliver_step" # Simulate action to trigger step delivery
            logger.info(f"User {user_profile['id']} starting/resuming bootcamp. Simulating 'bootcamp:action:deliver_step'.")
            # No direct response here; bootcamp flow will handle it.
        else:
            next_mode = "AWAITING_BOOTCAMP_WELCOME_AND_NAME"
            b_data = user_profile.get("bootcamp_onboarding_data", {})
            b_data["name_provided_once"] = False
            context_updates["bootcamp_onboarding_data"] = b_data
    elif input_lower == "action:main:start_chatting":
        next_mode = "CASUAL_CHAT"
    elif input_lower == "action:main:start_general_learning":
        next_mode = "AWAITING_TOPIC"
    elif current_mode == "AWAITING_GREETING" and input_lower in config.VALID_GREETINGS:
        next_mode = "POST_ONBOARDING_CHOICE" if user_profile.get("has_completed_bootcamp_onboarding") else "AWAITING_BOOTCAMP_WELCOME_AND_NAME"
        if next_mode == "AWAITING_BOOTCAMP_WELCOME_AND_NAME":
            b_data = user_profile.get("bootcamp_onboarding_data", {})
            b_data["name_provided_once"] = False
            context_updates["bootcamp_onboarding_data"] = b_data
    elif current_mode == "AWAITING_GREETING": # No valid greeting, re-prompt
        response_text_en = state_prompts.get_state_message("AWAITING_GREETING", name=user_name if user_profile.get("name") and user_profile.get("name") != "User" else "there")

    return next_mode, response_text_en, keyboard_defs, context_updates, interaction_input # Return modified interaction_input

async def _handle_onboarding_flow(
    user_profile: Dict[str, Any], 
    interaction_input: str, 
    is_callback: bool
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any]]:
    """Handles the multi-step onboarding process for the bootcamp."""
    user_id = user_profile["id"]
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode")
    b_data = user_profile.get("bootcamp_onboarding_data", {}) # Ensure it's a dict

    next_mode = current_mode
    response_text_en = None
    keyboard_defs = None
    context_updates = {"bootcamp_onboarding_data": b_data} # Start with existing b_data

    # Simplified onboarding state machine (example for one step)
    if current_mode == "AWAITING_BOOTCAMP_WELCOME_AND_NAME":
        name_already_provided = b_data.get("name_provided_once", False)
        user_name_response_raw = interaction_input.strip()
        if not is_callback and user_name_response_raw:
            processed_name = await llm_integrations.process_name_with_llm(user_name_response_raw)
            if processed_name:
                b_data["name"] = processed_name
                b_data["name_provided_once"] = True
                context_updates["name"] = processed_name # Update main user name
                context_updates["bootcamp_onboarding_data"] = b_data
                next_mode = "AWAITING_BOOTCAMP_LANGUAGE"
            else: # LLM couldn't extract name, re-prompt
                response_text_en = state_prompts.get_state_message(current_mode, name=user_name if name_already_provided and user_name != "User" else "there")
        else: # Initial prompt for this state
            response_text_en = state_prompts.get_state_message(current_mode, name=user_name if name_already_provided and user_name != "User" else "there")

    elif current_mode == "AWAITING_BOOTCAMP_LANGUAGE":
        # ... (similar logic for language preference) ...
        # On successful input:
        # b_data["language_preference"] = lang_pref_raw
        # context_updates["bootcamp_onboarding_data"] = b_data
        # next_mode = "AWAITING_BOOTCAMP_WHY_JOIN"
        # else: response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
        # This is a placeholder - you'll need to fill in the logic for each onboarding step
        # For brevity, I'm showing the pattern for one step.
        # The original `process_user_interaction` has the detailed logic for each onboarding state.
        # You would move those blocks into this function or further sub-functions.
        
        # Example: If this was the last onboarding step:
        # if success:
        #   b_data["onboarding_completed_timestamp"] = datetime.now(timezone.utc).isoformat()
        #   context_updates["bootcamp_onboarding_data"] = b_data
        #   context_updates["has_completed_bootcamp_onboarding"] = True # Update main user table
        #   await database.initialize_bootcamp_for_user(user_id) # Special DB call
        #   next_mode = "POST_ONBOARDING_CHOICE"

        # Fallback to original logic for brevity in this example
        # In a full refactor, each state's logic from the original function would be here or in sub-helpers
        original_logic_result = await _original_onboarding_logic_for_state(user_profile, interaction_input, is_callback, current_mode, b_data)
        next_mode, response_text_en, keyboard_defs, context_updates_from_original = original_logic_result
        if "bootcamp_onboarding_data" in context_updates_from_original: # Merge b_data updates
            b_data.update(context_updates_from_original["bootcamp_onboarding_data"])
            context_updates_from_original["bootcamp_onboarding_data"] = b_data
        context_updates.update(context_updates_from_original)


    # If no specific response generated by this state, it means we expect the new mode to generate its own prompt
    if response_text_en is None and next_mode != current_mode:
        pass # Let the main dispatcher handle prompting for the new mode
    elif response_text_en is None: # Re-prompting for the current state
        response_text_en = state_prompts.get_state_message(current_mode, name=user_name, **b_data) # Pass b_data for prompt formatting
        # TODO: Re-generate keyboard_defs if needed for re-prompt

    return next_mode, response_text_en, keyboard_defs, context_updates

async def _original_onboarding_logic_for_state(user_profile, interaction_input, is_callback, current_mode, b_data_param):
    """
    This is a temporary helper to encapsulate the existing onboarding logic from the original
    process_user_interaction function. In a full refactor, this would be broken down.
    It's modified to return values compatible with the new handler structure.
    """
    user_id = user_profile["id"]
    user_name = user_profile.get("name", "User")
    # Make a copy to avoid modifying the original user_profile's b_data directly in this temp function
    b_data = dict(b_data_param) # Use dict() to ensure it's a mutable copy if it was from user_profile
    
    next_mode_for_db = current_mode
    bot_response_text_en = None
    response_keyboard_en_defs = None
    db_context_updates = {} # Store specific field updates for user record

    # --- PASTE THE RELEVANT ONBOARDING IF/ELIF BLOCKS FROM THE ORIGINAL process_user_interaction HERE ---
    # --- Adjust them to set bot_response_text_en, response_keyboard_en_defs, next_mode_for_db, ---
    # --- and populate db_context_updates (e.g., db_context_updates["name"] = processed_name) ---
    # --- and db_context_updates["bootcamp_onboarding_data"] = b_data for changes to b_data itself ---

    # Example for AWAITING_BOOTCAMP_WELCOME_AND_NAME (copied and adapted)
    if current_mode == "AWAITING_BOOTCAMP_WELCOME_AND_NAME":
        name_already_provided = b_data.get("name_provided_once", False)
        user_name_response_raw = interaction_input.strip()
        if not is_callback and user_name_response_raw and current_mode == "AWAITING_BOOTCAMP_WELCOME_AND_NAME": 
            processed_name = await llm_integrations.process_name_with_llm(user_name_response_raw)
            if processed_name:
                b_data["name"] = processed_name; b_data["name_provided_once"] = True
                db_context_updates["name"] = processed_name # Update main user name
                db_context_updates["bootcamp_onboarding_data"] = b_data # Store updated b_data
                next_mode_for_db = "AWAITING_BOOTCAMP_LANGUAGE"
            else: 
                bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name if name_already_provided and user_name != "User" else "there")
        elif not bot_response_text_en: 
             bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name if name_already_provided and user_name != "User" else "there")

    # Example for AWAITING_BOOTCAMP_LANGUAGE (copied and adapted)
    elif current_mode == "AWAITING_BOOTCAMP_LANGUAGE": 
        if not is_callback and interaction_input.strip() and current_mode == "AWAITING_BOOTCAMP_LANGUAGE":
            lang_pref_raw = interaction_input.strip()
            b_data["language_preference"] = lang_pref_raw
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_WHY_JOIN"
        elif not bot_response_text_en: 
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
    
    # ... Continue for all other AWAITING_BOOTCAMP_ onboarding states from original function ...
    # AWAITING_BOOTCAMP_WHY_JOIN
    elif current_mode == "AWAITING_BOOTCAMP_WHY_JOIN": 
        if not is_callback and interaction_input.strip() and current_mode == "AWAITING_BOOTCAMP_WHY_JOIN":
            b_data["why_join"] = interaction_input.strip()
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_SKILLS_HOPE"
        elif not bot_response_text_en:
             bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)

    # AWAITING_BOOTCAMP_SKILLS_HOPE
    elif current_mode == "AWAITING_BOOTCAMP_SKILLS_HOPE":
        if is_callback and interaction_input.startswith("onboard_skills:"):
            choice = interaction_input.split(":", 1)[1] 
            b_data["skills_hope"] = choice 
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_BIGGEST_CHALLENGE"
        elif not bot_response_text_en: 
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key": "AWAITING_BOOTCAMP_SKILLS_BUTTON_CORE", "data": "onboard_skills:core_ai"},
                {"text_key": "AWAITING_BOOTCAMP_SKILLS_BUTTON_APPLICATIONS", "data": "onboard_skills:applications_ethics"},
                {"text_key": "AWAITING_BOOTCAMP_SKILLS_BUTTON_TECHNICAL", "data": "onboard_skills:technical_ai"}
            ]
    
    # AWAITING_BOOTCAMP_BIGGEST_CHALLENGE
    elif current_mode == "AWAITING_BOOTCAMP_BIGGEST_CHALLENGE":
        if is_callback and interaction_input.startswith("onboard_challenge:"):
            choice = interaction_input.split(":", 1)[1]
            b_data["biggest_challenge"] = choice 
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_AI_TOOLS_PRIOR"
        elif not bot_response_text_en:
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key": "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_TIME_RESOURCES", "data": "onboard_challenge:time_resources"},
                {"text_key": "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_COMPLEXITY", "data": "onboard_challenge:complexity"},
                {"text_key": "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_FOCUS", "data": "onboard_challenge:focus_motivation"}
            ]

    # AWAITING_BOOTCAMP_AI_TOOLS_PRIOR
    elif current_mode == "AWAITING_BOOTCAMP_AI_TOOLS_PRIOR":
        if is_callback and interaction_input.startswith("onboard_tools:"):
            choice = interaction_input.split(":",1)[1] 
            b_data["ai_tools_prior"] = choice
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_COMPUTER_ACCESS"
        elif not bot_response_text_en:
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key": "AWAITING_BOOTCAMP_TOOLS_BUTTON_COMMON_APPS", "data": "onboard_tools:common_apps"},
                {"text_key": "AWAITING_BOOTCAMP_TOOLS_BUTTON_CODING_LIBS", "data": "onboard_tools:coding_libs"},
                {"text_key": "AWAITING_BOOTCAMP_TOOLS_BUTTON_NO", "data": "onboard_tools:no"}
            ]
    
    # AWAITING_BOOTCAMP_COMPUTER_ACCESS
    elif current_mode == "AWAITING_BOOTCAMP_COMPUTER_ACCESS":
        answer_val = None
        if interaction_input == "onboard_comp_access:yes": answer_val = "Yes"
        elif interaction_input == "onboard_comp_access:no": answer_val = "No"
        elif not is_callback: 
            text_input_lower = interaction_input.lower().strip()
            if text_input_lower in ["yes", "ya", "y", "ada", "yup", "yep", "i do"]: answer_val = "Yes"
            elif text_input_lower in ["no", "tidak", "n", "tiada", "nope", "not regularly"]: answer_val = "No"
        
        if answer_val:
            b_data["computer_access"] = answer_val
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT"
        elif not bot_response_text_en: 
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key": "AWAITING_BOOTCAMP_COMPUTER_ACCESS_BUTTON_YES", "data":"onboard_comp_access:yes"}, 
                {"text_key": "AWAITING_BOOTCAMP_COMPUTER_ACCESS_BUTTON_NO", "data":"onboard_comp_access:no"}
            ]

    # AWAITING_BOOTCAMP_PROGRAMMING_COMFORT
    elif current_mode == "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT":
        answer_val, answer_code = None, None
        if interaction_input.startswith("onboard_prog_comfort:"): answer_code = interaction_input.split(":")[1]
        elif not is_callback:
            text_input_lower = interaction_input.lower().strip()
            if any(kw in text_input_lower for kw in ["comfortable", "yes", "good", "familiar", "pandai", "boleh"]): answer_code = "comfortable"
            elif any(kw in text_input_lower for kw in ["little", "sikit", "basic", "sedikit", "a little familiar"]): answer_code = "little"
            elif any(kw in text_input_lower for kw in ["no", "not really", "not comfortable", "beginner", "baru", "tidak", "completely new"]): answer_code = "new"

        if answer_code:
            if answer_code == "comfortable": answer_val = "Comfortable"
            elif answer_code == "little": answer_val = "A Little Familiar"
            elif answer_code == "new": answer_val = "Completely New"
        
        if answer_val:
            b_data["programming_comfort"] = answer_val
            db_context_updates["bootcamp_onboarding_data"] = b_data
            next_mode_for_db = "AWAITING_BOOTCAMP_HEAR_ABOUT"
        elif not bot_response_text_en:
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key":"AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_COMFORTABLE", "data":"onboard_prog_comfort:comfortable"},
                {"text_key":"AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_A_LITTLE", "data":"onboard_prog_comfort:little"},
                {"text_key":"AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_NEW", "data":"onboard_prog_comfort:new"}
            ]

    # AWAITING_BOOTCAMP_HEAR_ABOUT (Last onboarding step)
    elif current_mode == "AWAITING_BOOTCAMP_HEAR_ABOUT":
        if is_callback and interaction_input.startswith("onboard_hear:"):
            choice = interaction_input.split(":",1)[1]
            b_data["hear_about"] = choice 
            b_data["onboarding_completed_timestamp"] = datetime.now(timezone.utc).isoformat()
            db_context_updates["bootcamp_onboarding_data"] = b_data
            # This flag is set by initialize_bootcamp_for_user
            # db_context_updates["has_completed_bootcamp_onboarding"] = True 
            
            # Critical: Call initialize_bootcamp_for_user here as it sets multiple flags
            # This is an awaitable function, so the helper needs to be async or this needs to be handled
            # For now, assume initialize_bootcamp_for_user will be called after this helper returns if next_mode is POST_ONBOARDING_CHOICE
            # and onboarding was just completed.
            # Let's set a flag to indicate onboarding completion for the main dispatcher.
            db_context_updates["_signal_onboarding_just_completed"] = True # Custom flag
            next_mode_for_db = "POST_ONBOARDING_CHOICE"
        elif not bot_response_text_en:
            bot_response_text_en = state_prompts.get_state_message(current_mode, name=user_name)
            response_keyboard_en_defs = [
                {"text_key": "AWAITING_BOOTCAMP_HEAR_BUTTON_ONLINE", "data": "onboard_hear:online"},
                {"text_key": "AWAITING_BOOTCAMP_HEAR_BUTTON_OFFLINE", "data": "onboard_hear:offline"},
                {"text_key": "AWAITING_BOOTCAMP_HEAR_BUTTON_WORK_SCHOOL", "data": "onboard_hear:work_school"}
            ]
    
    return next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, db_context_updates


async def _handle_bootcamp_flow(
    user_profile: Dict[str, Any], 
    interaction_input: str, 
    is_callback: bool,
    wa_client: WhatsApp, # Needed for sending messages directly from this handler
    target_lang_name: str
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any], bool]:
    """Handles bootcamp chapter delivery, quiz acknowledgements, and answer feedback."""
    user_id = user_profile["id"]
    user_wa_id = user_profile["whatsapp_id"]
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode")
    
    next_mode = current_mode
    response_text_en = None # This handler sends messages directly
    keyboard_defs = None
    db_progress_updates = {} # For fields like current_chapter, last_step_completed
    db_context_updates = {} # For JSONB fields like bootcamp_onboarding_data (for quiz feedback storage)
    send_standard_response_from_main = False # This handler will manage its own message sending

    current_bootcamp_chapter = user_profile.get("current_bootcamp_chapter", 1)

    # Handle quiz acknowledgement before calling get_bootcamp_step
    if current_mode.startswith("AWAITING_BOOTCAMP_QUIZ_ACK_CH"):
        if interaction_input == "bootcamp:action:deliver_step": # User clicked "Ready for Quiz?"
            db_progress_updates["last_step_completed"] = f"quiz_ack_ch{current_bootcamp_chapter}"
            # Update user_profile for get_bootcamp_step
            user_profile["last_bootcamp_step_completed"] = db_progress_updates["last_step_completed"]
            # The mode will change when get_bootcamp_step runs for quiz_start_interactive

    # Handle moving to next chapter after answer feedback
    elif current_mode.startswith("AWAITING_NEXT_BOOTCAMP_CHAPTER_ACK_CH"):
        if interaction_input == "bootcamp:action:deliver_step": 
            done_chapter = user_profile.get('current_bootcamp_chapter', 0)
            if done_chapter < config.TOTAL_BOOTCAMP_CHAPTERS:
                next_chap_num = done_chapter + 1
                db_progress_updates["current_bootcamp_chapter"] = next_chap_num
                db_progress_updates["last_step_completed"] = f"answer_{done_chapter}"
                # Reset quiz session for the new chapter
                db_progress_updates["current_quiz_session_to_set"] = {"answers": {}, "current_question_index": 0, "chapter_number": next_chap_num}
                user_profile["current_bootcamp_chapter"] = next_chap_num # Update local profile for get_bootcamp_step
                user_profile["last_bootcamp_step_completed"] = db_progress_updates["last_step_completed"]
                user_profile["current_quiz_session"] = db_progress_updates["current_quiz_session_to_set"]
            else: # All chapters done
                db_progress_updates["last_step_completed"] = "all_chapters_done"
                user_profile["last_bootcamp_step_completed"] = "all_chapters_done"
                next_mode = "BOOTCAMP_COMPLETED_FINAL" # Trigger completion state

    step_info = await bootcamp_manager.get_bootcamp_step(user_profile)
    step_type = step_info.get("type")
    step_data_en = step_info.get("data")
    next_mode = step_info.get("next_mode", next_mode) # Update next_mode based on step_info
    
    if step_info.get("update_last_step_to"):
        db_progress_updates["last_step_completed"] = step_info.get("update_last_step_to")

    if step_type == "chapter_intro":
        chap_num = step_info.get("chapter_number")
        chap_title_en = step_info.get("chapter_title", f"Chapter {chap_num}")
        intro_prompt_en = state_prompts.get_state_message(
            "DELIVERING_BOOTCAMP_CHAPTER_INTRO", name=user_name,
            chapter_number=chap_num, chapter_title=chap_title_en,
            total_chapters=config.TOTAL_BOOTCAMP_CHAPTERS
        )
        intro_prompt_translated = await get_translated_text(intro_prompt_en, target_lang_name, "bootcamp chapter introduction")
        chapter_content_translated = await get_translated_text(step_data_en, target_lang_name, f"bootcamp chapter {chap_num} content")
        
        button_text_en = state_prompts.get_state_message("DELIVERING_BOOTCAMP_CHAPTER_END_BUTTON_QUIZ")
        button_text_translated = await get_translated_text(button_text_en, target_lang_name, "button text")
        chapter_buttons = [Button(button_text_translated, callback_data="bootcamp:action:deliver_step")]
        
        await send_message_chunks(
            wa_client, user_wa_id, chapter_content_translated, 
            initial_prompt=intro_prompt_translated,
            buttons_after_last_chunk=chapter_buttons
        )
        db_progress_updates["last_step_completed"] = f"chapter_content_ended_ch{chap_num}"
        next_mode = f"AWAITING_BOOTCAMP_QUIZ_ACK_CH{chap_num}"


    elif step_type == "quiz_start_interactive":
        chap_num = step_info.get("chapter_number")
        current_quiz_session_init = user_profile.get("current_quiz_session", {})
        if not isinstance(current_quiz_session_init, dict) or \
           current_quiz_session_init.get("chapter_number") != chap_num or \
           "current_question_index" not in current_quiz_session_init:
            
            current_quiz_session_init = {"chapter_number": chap_num, "current_question_index": 0, "answers": {}}
            db_progress_updates["current_quiz_session_to_set"] = current_quiz_session_init 
        # The actual first question will be delivered when this mode is re-entered by get_bootcamp_step
        # No message sent here, just setting up the state. The next call to process_user_interaction
        # with the updated mode will trigger the first question via quiz_question step_type.
        # The next_mode from step_info should be like AWAITING_BOOTCAMP_QUIZ_ANSWER_CHX_Q1

    elif step_type == "quiz_all_questions_answered":
        chap_num = step_info.get("chapter_number")
        collected_answers = step_info.get("collected_answers", {})
        score_percentage, detailed_feedback_str_en = await bootcamp_manager.process_quiz_submission(
            chap_num, collected_answers
        )
        await database.record_quiz_score(user_id, chap_num, score_percentage) # This also updates last_step and total_score
        
        # Store detailed feedback in English in user's context to be retrieved by "answer_feedback" step
        b_data_feedback = user_profile.get("bootcamp_onboarding_data", {})
        b_data_feedback[f"ch{chap_num}_feedback_en"] = detailed_feedback_str_en
        db_context_updates["bootcamp_onboarding_data"] = b_data_feedback
        db_progress_updates["current_quiz_session_to_set"] = {} # Clear quiz session
        # next_mode will be set by get_bootcamp_step to show answer_feedback

    elif step_type == "answer_feedback":
        chap_num = step_info.get("chapter_number")
        # Retrieve stored English feedback
        feedback_data_en = user_profile.get("bootcamp_onboarding_data", {}).get(f"ch{chap_num}_feedback_en", "Feedback processing error.")
        
        feedback_intro_en = state_prompts.get_state_message(
            "SHOWING_BOOTCAMP_QUIZ_ANSWERS_INTRO", name=user_name, chapter_number=chap_num
        )
        feedback_intro_translated = await get_translated_text(feedback_intro_en, target_lang_name, "quiz feedback introduction")
        step_data_translated = await get_translated_text(feedback_data_en, target_lang_name, "quiz answer explanations")
        
        is_last_chapter = chap_num == config.TOTAL_BOOTCAMP_CHAPTERS
        final_buttons_translated = []
        if not is_last_chapter:
            btn_next_chap_en = state_prompts.get_state_message("ANSWER_FEEDBACK_BUTTON_NEXT_CHAPTER", next_chapter_number=chap_num + 1)
            btn_next_chap_translated = await get_translated_text(btn_next_chap_en, target_lang_name, "button text")
            final_buttons_translated.append(Button(btn_next_chap_translated, callback_data="bootcamp:action:deliver_step"))
        else:
            btn_finish_en = state_prompts.get_state_message("ANSWER_FEEDBACK_BUTTON_FINISH_BOOTCAMP")
            btn_finish_translated = await get_translated_text(btn_finish_en, target_lang_name, "button text")
            final_buttons_translated.append(Button(btn_finish_translated, callback_data="bootcamp:action:deliver_step"))
            
        await send_message_chunks(
            wa_client, user_wa_id, step_data_translated, 
            initial_prompt=feedback_intro_translated,
            buttons_after_last_chunk=final_buttons_translated
        )
        # last_step_completed is already set by record_quiz_score or get_bootcamp_step
        # next_mode is set by get_bootcamp_step (e.g., AWAITING_NEXT_BOOTCAMP_CHAPTER_ACK_CHX or BOOTCAMP_COMPLETED_FINAL)

    elif step_type == "completed":
        total_score_val = user_profile.get("total_bootcamp_score", "N/A")
        completion_message_en = state_prompts.get_state_message(
            "BOOTCAMP_COMPLETED_FINAL", name=user_name, 
            total_chapters=config.TOTAL_BOOTCAMP_CHAPTERS, total_score=total_score_val
        )
        completion_message_translated = await get_translated_text(completion_message_en, target_lang_name, "bootcamp completion message")
        
        # Re-use main menu options
        btn1_en = state_prompts.get_state_message("POST_ONBOARDING_CHOICE_BUTTON_LEARN")
        btn2_en = state_prompts.get_state_message("POST_ONBOARDING_CHOICE_BUTTON_CHAT")
        btn1_translated = await get_translated_text(btn1_en, target_lang_name, "button text")
        btn2_translated = await get_translated_text(btn2_en, target_lang_name, "button text")
        completion_buttons = [
            Button(btn1_translated, callback_data="action:main:start_general_learning"),
            Button(btn2_translated, callback_data="action:main:start_chatting")
        ]
        await send_whatsapp_message_utility(wa_client, user_wa_id, completion_message_translated, buttons_list=completion_buttons)
        # next_mode is BOOTCAMP_COMPLETED_FINAL from get_bootcamp_step

    elif step_type == "error":
        step_data_translated = await get_translated_text(step_data_en, target_lang_name, "error message")
        await send_whatsapp_message_utility(wa_client, user_wa_id, step_data_translated)
        # next_mode is BOOTCAMP_ERROR_STATE from get_bootcamp_step

    elif step_type == "wait_for_user_action":
        # This case means bootcamp_manager doesn't have an immediate content step.
        # The main dispatcher will handle prompting based on the current_mode (which might be BOOTCAMP_COMPLETED_FINAL)
        logger.info(f"Bootcamp manager is waiting for user action in mode: {current_mode} for user {user_id}")
        send_standard_response_from_main = True # Let main dispatcher handle this
        response_text_en = None # No specific message from this handler in this case

    # Consolidate DB updates
    if db_progress_updates:
        # If current_quiz_session_to_set is in updates, pass it correctly
        quiz_session_update = db_progress_updates.pop("current_quiz_session_to_set", None)
        await database.update_user_bootcamp_progress(user_id, current_quiz_session_to_set=quiz_session_update, **db_progress_updates)
    if db_context_updates:
        await database.update_user_context(user_id, db_context_updates)

    return next_mode, response_text_en, keyboard_defs, {}, send_standard_response_from_main # No user profile field updates from here, keyboard_defs is None

async def _handle_quiz_question_delivery(
    user_profile: Dict[str, Any], 
    wa_client: WhatsApp, 
    target_lang_name: str
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any], bool]:
    """Handles delivery of a single quiz question."""
    user_wa_id = user_profile["whatsapp_id"]
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode") # Should be like AWAITING_BOOTCAMP_QUIZ_ANSWER_CHX_QX

    step_info = await bootcamp_manager.get_bootcamp_step(user_profile) # This will return "quiz_question" type
    next_mode = step_info.get("next_mode", current_mode)

    if step_info.get("type") == "quiz_question":
        q_data = step_info.get("question_data", {})
        q_text_en = q_data.get("text", "Question not available.")
        q_options_en = q_data.get("options", {})
        q_id = q_data.get("id")
        chap_num = step_info.get("chapter_number")
        
        q_text_translated = await get_translated_text(q_text_en, target_lang_name, "quiz question text")
        quiz_buttons_translated = []
        if q_options_en and q_id:
            for opt_key, opt_val_en in sorted(q_options_en.items()):
                opt_val_translated = await get_translated_text(opt_val_en, target_lang_name, "quiz option")
                quiz_buttons_translated.append(
                    Button(title=f"{opt_key.upper()}. {opt_val_translated}", callback_data=f"bootcamp_quiz:ch{chap_num}:{q_id}:{opt_key}")
                )
        
        question_prompt_wrapper_en = state_prompts.get_state_message(
            "PRESENTING_BOOTCAMP_QUIZ_QUESTION", name=user_name,
            chapter_number=chap_num,
            current_question_num=step_info.get("current_question_num"),
            total_quiz_questions=step_info.get("total_quiz_questions"),
            quiz_question_text=q_text_translated 
        )
        # This wrapper is already in the target language if q_text_translated is used.
        # So, we send question_prompt_wrapper_en directly, assuming state_prompts handles the main template.
        # Or, translate the whole wrapper if state_prompts returns English.
        # For consistency, let's assume state_prompts returns English.
        final_prompt_to_send = await get_translated_text(question_prompt_wrapper_en, target_lang_name, "quiz question prompt wrapper")

        await send_whatsapp_message_utility(wa_client, user_wa_id, final_prompt_to_send, buttons_list=quiz_buttons_translated)
        return next_mode, None, None, {}, False # Message sent directly
    
    # Fallback if step_type is not quiz_question (should not happen if called correctly)
    logger.warning(f"Quiz question delivery called for user {user_profile['id']} but step_type was {step_info.get('type')}")
    return current_mode, "There was an issue loading the quiz question.", None, {}, True


async def _handle_quiz_answer_reception(
    user_profile: Dict[str, Any], 
    interaction_input: str
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any]]:
    """Handles user's answer to a quiz question."""
    user_id = user_profile["id"]
    current_mode = user_profile.get("current_mode") # e.g., AWAITING_BOOTCAMP_QUIZ_ANSWER_CH1_Q1
    current_bootcamp_chapter = user_profile.get("current_bootcamp_chapter", 1)
    current_quiz_session = user_profile.get("current_quiz_session", {})
    if not isinstance(current_quiz_session, dict): current_quiz_session = {}

    next_mode = current_mode # Default to re-prompt if answer is invalid
    db_progress_updates = {}
    
    # Example callback: "bootcamp_quiz:ch1:q1:a"
    if interaction_input.startswith(f"bootcamp_quiz:ch{current_bootcamp_chapter}:"):
        parts = interaction_input.split(':')
        if len(parts) == 4: # chX, qY, answer
            q_id, ans = parts[2], parts[3]
            current_quiz_session.setdefault("answers", {})
            current_quiz_session["answers"][q_id] = ans
            current_quiz_session["current_question_index"] = current_quiz_session.get("current_question_index", 0) + 1
            current_quiz_session["chapter_number"] = current_bootcamp_chapter
            
            db_progress_updates["current_quiz_session_to_set"] = current_quiz_session
            # The next mode will be determined by get_bootcamp_step based on the updated session
            # It will either go to the next question or to quiz_all_questions_answered
            # So, we don't set next_mode explicitly here, let the main loop re-evaluate.
            next_mode = user_profile.get("current_mode") # Stay, let get_bootcamp_step drive
            logger.info(f"User {user_id} answered {q_id} with {ans} for chapter {current_bootcamp_chapter}. Index now {current_quiz_session['current_question_index']}")
        else:
            logger.warning(f"Malformed quiz answer callback for user {user_id}: {interaction_input}")
            # Re-prompt for the current question (or let get_bootcamp_step handle it)
    
    return next_mode, None, None, db_progress_updates


async def _handle_general_learning_flow(
    user_profile: Dict[str, Any], 
    interaction_input: str, 
    is_callback: bool
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any]]:
    """Handles states related to general (non-bootcamp) learning plans."""
    user_id = user_profile["id"]
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode")
    draft_plan = user_profile.get("draft_learning_plan", {})
    if not isinstance(draft_plan, dict): draft_plan = {}

    next_mode = current_mode
    response_text_en = None
    keyboard_defs = None
    db_context_updates = {}

    if current_mode == "AWAITING_TOPIC":
        if not is_callback and interaction_input.strip():
            draft_plan["topic"] = interaction_input.strip()
            db_context_updates["draft_learning_plan"] = draft_plan
            next_mode = "AWAITING_SPECIFIC_INTEREST"
        else: # Initial prompt or invalid input
            response_text_en = state_prompts.get_state_message("AWAITING_TOPIC", name=user_name)
    
    elif current_mode == "AWAITING_SPECIFIC_INTEREST":
        # Similar logic for specific interest, prior knowledge, goals, commitment
        # ...
        # Example for specific interest:
        if not is_callback and interaction_input.strip():
            draft_plan["specific_interest"] = interaction_input.strip()
            db_context_updates["draft_learning_plan"] = draft_plan
            next_mode = "AWAITING_PRIOR_KNOWLEDGE" # Or next step in general learning
        else:
            topic_val = draft_plan.get("topic", "that topic")
            response_text_en = state_prompts.get_state_message(current_mode, name=user_name, topic=topic_val)

    # ... other general learning states ...

    # If no specific response generated, it means we expect the new mode to generate its own prompt
    if response_text_en is None and next_mode != current_mode:
        pass
    elif response_text_en is None: # Re-prompting for current state
        # This requires passing all necessary kwargs for the specific state prompt
        prompt_kwargs = {"name": user_name, **draft_plan}
        response_text_en = state_prompts.get_state_message(current_mode, **prompt_kwargs)
        # TODO: Re-generate keyboard_defs if needed for re-prompt

    return next_mode, response_text_en, keyboard_defs, db_context_updates


async def _handle_casual_chat_flow(
    user_profile: Dict[str, Any], 
    interaction_input: str,
    target_lang_name: str
) -> Tuple[str, Optional[str], Optional[List[Dict]], Dict[str, Any]]:
    """Handles casual chat interactions using the LLM."""
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode") # Should be CASUAL_CHAT

    # For casual chat, the LLM response *is* the bot_response_text_en (or translated)
    # System prompt should guide the LLM to respond in the target_lang_name
    llm_system_prompt = f"You are {config.BOT_NAME}, a helpful and friendly AI learning assistant. The user's name is {user_name}. Please respond naturally in {target_lang_name}."
    
    # Get conversation history if you implement it
    # conversation_history = await database.get_recent_chat_history(user_profile["id"], limit=5) 
    
    raw_llm_response = await llm_integrations.query_deepseek(
        prompt=interaction_input, 
        system_message=llm_system_prompt
        # conversation_history=conversation_history # If using history
    )
    
    # LLM is expected to respond in target_lang_name due to system prompt.
    # So, bot_response_text_en is actually already translated.
    # For logging, we might want the English version if the LLM was prompted in English and translated separately.
    # However, with the current setup, raw_llm_response is the final text.
    response_text_final_lang = raw_llm_response or state_prompts.get_state_message("DEFAULT_FALLBACK", name=user_name) # Fallback in English
    if not raw_llm_response: # If LLM failed, translate the English fallback
        response_text_final_lang = await get_translated_text(response_text_final_lang, target_lang_name, "chatbot fallback response")

    # Buttons for casual chat
    btn1_en = state_prompts.get_state_message("CASUAL_CHAT_BUTTON_BOOTCAMP")
    btn2_en = state_prompts.get_state_message("CASUAL_CHAT_BUTTON_MAIN_MENU")
    btn1_translated = await get_translated_text(btn1_en, target_lang_name, "button text")
    btn2_translated = await get_translated_text(btn2_en, target_lang_name, "button text")
    
    keyboard_defs_translated = [ # This structure is for the main dispatcher to build pywa Buttons
        {"text": btn1_translated, "data": "action:main:start_bootcamp"},
        {"text": btn2_translated, "data": "/mainmenu"}
    ]

    # For logging, we store the LLM's direct response (which should be in target lang)
    # If we had a separate translation step, we'd log the English version.
    return current_mode, response_text_final_lang, keyboard_defs_translated, {} # No mode change from here, no specific context updates


# --- Main Interaction Processor ---
async def process_user_interaction(
    user_profile: Dict[str, Any],
    interaction_input: str, # Can be text or callback data
    is_callback: bool,
    background_tasks: BackgroundTasks, # Retained for potential future use
    wa_client: Optional[WhatsApp],
    message_obj: Optional[Any] = None, # Original Message or Callback object
):
    if not wa_client:
        logger.error("process_user_interaction: WhatsApp client is None. Cannot proceed.")
        return

    user_id = user_profile["id"]
    user_wa_id = user_profile["whatsapp_id"]
    user_name = user_profile.get("name", "User")
    current_mode = user_profile.get("current_mode", "AWAITING_GREETING")
    
    # Ensure JSONB fields are dicts
    for field in ["bootcamp_onboarding_data", "current_quiz_session", "bootcamp_quiz_scores", "draft_learning_plan", "additional_data"]:
        if user_profile.get(field) is None or not isinstance(user_profile.get(field), dict):
            user_profile[field] = {}

    raw_lang_pref = user_profile.get("bootcamp_onboarding_data", {}).get("language_preference")
    target_lang_name = get_target_language_name(raw_lang_pref)
    
    logger.info(f"\n--- Processing Interaction ---")
    logger.info(f"User ID: {user_id}, WA ID: {user_wa_id}, Name: {user_name}")
    logger.info(f"Initial Current Mode: {current_mode}, Input: '{interaction_input}', IsCallback: {is_callback}, TargetLang: {target_lang_name}")

    # Mark as read and send typing indicator
    incoming_wamid_for_read = None
    if message_obj:
        if hasattr(message_obj, 'id') and message_obj.id and not is_callback: # Text message
            incoming_wamid_for_read = message_obj.id
        elif is_callback and hasattr(message_obj, 'reply_to_message') and message_obj.reply_to_message and \
             hasattr(message_obj.reply_to_message, 'id') and message_obj.reply_to_message.id: # Callback reply to a message
            incoming_wamid_for_read = message_obj.reply_to_message.id
    
    if incoming_wamid_for_read:
        await mark_as_read(user_wa_id, incoming_wamid_for_read) # Pass user_wa_id for logging if needed
    await send_typing_on_indicator(wa_client, user_wa_id)
    await asyncio.sleep(config.TYPING_INDICATOR_DELAY_BEFORE_MESSAGE_SECONDS)


    # --- Log incoming user message/interaction ---
    # (Moved logging to happen after fetching user profile for more context)
    text_to_log_for_db = interaction_input
    msg_type_log_for_db = 'text'
    interactive_payload_log_for_db = None
    msg_id_log_for_db = incoming_wamid_for_read # Use the ID used for marking as read

    if is_callback:
        if isinstance(message_obj, CallbackButton):
            msg_type_log_for_db = 'interactive_button_reply'
            interactive_payload_log_for_db = {"type": "button_reply", "button_reply": {"id": message_obj.data, "title": message_obj.title}}
            text_to_log_for_db = f"[Button Clicked: {message_obj.title} ({message_obj.data})]"
        elif isinstance(message_obj, CallbackSelection):
            msg_type_log_for_db = 'interactive_list_reply'
            interactive_payload_log_for_db = {"type": "list_reply", "list_reply": {"id": message_obj.data, "title": message_obj.title, "description": message_obj.description}}
            text_to_log_for_db = f"[List Selection: {message_obj.title} ({message_obj.data})]"
    elif isinstance(message_obj, Message) and message_obj.type:
        msg_type_log_for_db = message_obj.type.value
    elif interaction_input.startswith("/"): # User typed a command
        msg_type_log_for_db = "command" # Custom type for commands

    valid_db_message_types = ['text', 'image', 'audio', 'video', 'document', 'location', 'contacts', 'sticker', 'unsupported', 'reaction', 'interactive_button_reply', 'interactive_list_reply', 'order', 'system', 'command']
    if msg_type_log_for_db not in valid_db_message_types:
        logger.warning(f"msg_type_log_for_db '{msg_type_log_for_db}' not in valid DB types. Defaulting to 'unsupported'.")
        msg_type_log_for_db = 'unsupported'
    
    # Store user message, except for internal actions not directly from user
    if not interaction_input.startswith("bootcamp:action:deliver_step"): # Avoid logging system-triggered steps as user messages
        await database.store_chat_message(
            user_id=user_id, sender_type="user", message_text=text_to_log_for_db,
            whatsapp_message_id=msg_id_log_for_db, message_type=msg_type_log_for_db,
            interactive_payload=interactive_payload_log_for_db,
            context_bootcamp_chapter=user_profile.get("current_bootcamp_chapter"),
        )
    # --- End logging incoming ---

    next_mode_for_db = current_mode
    bot_response_text_en: Optional[str] = None # English version of the main text response
    response_keyboard_en_defs: Optional[List[Dict]] = None # Definitions for buttons/list items in English
    db_context_updates: Dict[str, Any] = {} # Specific field updates for user record (e.g., name, bootcamp_onboarding_data)
    db_progress_updates: Dict[str, Any] = {} # Specific field updates for user_progress (e.g., current_chapter)
    send_standard_response_flag = True # Most handlers will set this to True if they return text/keyboard
                                     # Bootcamp flow might set to False if it sends messages directly

    # Initial command handling (like /help, /mainmenu)
    if interaction_input.startswith("/"):
        next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, cmd_context_updates, interaction_input = \
            await _handle_initial_greeting_or_command(user_profile, interaction_input)
        db_context_updates.update(cmd_context_updates)
        if next_mode_for_db != current_mode: # Mode changed by command
            current_mode = next_mode_for_db
            # Re-fetch profile if mode change implies significant context reset (e.g., back to onboarding)
            if current_mode in ["AWAITING_BOOTCAMP_WELCOME_AND_NAME", "POST_ONBOARDING_CHOICE", "AWAITING_GREETING"]:
                logger.info(f"Mode changed by command to {current_mode}, re-fetching user profile for user {user_id}.")
                refreshed_profile = await database.get_user_bootcamp_details(user_id)
                if refreshed_profile: user_profile = refreshed_profile
                else: logger.error(f"Failed to re-fetch profile for user {user_id} after command-driven mode change."); # Handle error
    
    # Main state dispatching logic
    if current_mode == "AWAITING_GREETING":
        # This is also handled by _handle_initial_greeting_or_command if a valid greeting is received
        if not bot_response_text_en: # If /command didn't set a response, and it's still AWAITING_GREETING
            next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, greet_ctx_updates, _ = \
                await _handle_initial_greeting_or_command(user_profile, interaction_input)
            db_context_updates.update(greet_ctx_updates)

    elif current_mode == "POST_ONBOARDING_CHOICE":
        bot_response_text_en = state_prompts.get_state_message("POST_ONBOARDING_CHOICE", name=user_name)
        response_keyboard_en_defs = [
            {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_BOOTCAMP", "data": "action:main:start_bootcamp"},
            {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_CHAT", "data": "action:main:start_chatting"},
            {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_LEARN", "data": "action:main:start_general_learning"}
        ]
    
    elif current_mode.startswith("AWAITING_BOOTCAMP_"): # Onboarding states
        next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, onboard_ctx_updates = \
            await _handle_onboarding_flow(user_profile, interaction_input, is_callback)
        db_context_updates.update(onboard_ctx_updates)
        # Check for signal that onboarding just completed
        if db_context_updates.pop("_signal_onboarding_just_completed", False):
            logger.info(f"Onboarding just completed for user {user_id}. Initializing bootcamp.")
            await database.initialize_bootcamp_for_user(user_id)
            # User profile needs refresh after initialize_bootcamp_for_user
            refreshed_profile = await database.get_user_bootcamp_details(user_id)
            if refreshed_profile: user_profile = refreshed_profile
            else: logger.error(f"Failed to re-fetch profile for user {user_id} after bootcamp init.")

    elif interaction_input.startswith("bootcamp:action:deliver_step") or \
         current_mode.startswith("DELIVERING_BOOTCAMP_") or \
         current_mode.startswith("AWAITING_BOOTCAMP_QUIZ_ACK_CH") or \
         current_mode.startswith("SHOWING_BOOTCAMP_QUIZ_ANSWERS_CH") or \
         current_mode.startswith("AWAITING_NEXT_BOOTCAMP_CHAPTER_ACK_CH"):
        # This covers chapter delivery, quiz ack, answer feedback, and moving to next chapter
        # It does NOT cover AWAITING_BOOTCAMP_QUIZ_ANSWER_CH (which is for quiz question delivery/answer reception)
        
        # Refresh profile before bootcamp flow to get latest progress
        refreshed_profile_bootcamp = await database.get_user_bootcamp_details(user_id)
        if not refreshed_profile_bootcamp:
            logger.error(f"Failed to fetch user profile for bootcamp flow, user {user_id}")
            # Handle error, maybe send generic error message
            bot_response_text_en = state_prompts.get_state_message("ERROR_MESSAGE", name=user_name)
        else:
            user_profile = refreshed_profile_bootcamp
            next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, _, send_standard_response_flag = \
                await _handle_bootcamp_flow(user_profile, interaction_input, is_callback, wa_client, target_lang_name)
        # db_progress_updates are handled within _handle_bootcamp_flow via direct DB calls

    elif current_mode.startswith("AWAITING_BOOTCAMP_QUIZ_ANSWER_CH"):
        # This state is for when a quiz question has been presented and we are awaiting an answer.
        # Or, if no answer yet, it's time to deliver the current question.
        if interaction_input.startswith(f"bootcamp_quiz:ch{user_profile.get('current_bootcamp_chapter')}:"): # User submitted an answer
            next_mode_for_db, _, _, quiz_ans_prog_updates = \
                await _handle_quiz_answer_reception(user_profile, interaction_input)
            if quiz_ans_prog_updates: # Update quiz session in DB
                 await database.update_user_bootcamp_progress(user_id, **quiz_ans_prog_updates)
            # After processing an answer, the mode might change to deliver the next question or finalize quiz.
            # We need to re-fetch profile and let the next iteration of the dispatcher call the appropriate handler.
            # No direct message sending here; the next state will handle it.
            send_standard_response_flag = False # Let the next cycle handle message sending
        else: # No answer submitted yet for this state, means we need to deliver the current question
            next_mode_for_db, _, _, _, send_standard_response_flag = \
                await _handle_quiz_question_delivery(user_profile, wa_client, target_lang_name)
                
    elif current_mode.startswith("PRESENTING_BOOTCAMP_QUIZ_QUESTION"): # This mode is set by bootcamp_manager
         next_mode_for_db, _, _, _, send_standard_response_flag = \
            await _handle_quiz_question_delivery(user_profile, wa_client, target_lang_name)

    elif current_mode.startswith("AWAITING_TOPIC") or \
         current_mode.startswith("AWAITING_SPECIFIC_INTEREST") or \
         current_mode.startswith("AWAITING_PRIOR_KNOWLEDGE") or \
         current_mode.startswith("AWAITING_GOALS") or \
         current_mode.startswith("AWAITING_COMMITMENT") or \
         current_mode.startswith("AWAITING_PLAN_CONFIRMATION") or \
         current_mode.startswith("READY_TO_LEARN") or \
         current_mode.startswith("LEARNING_CONTINUE_PROMPT"):
        next_mode_for_db, bot_response_text_en, response_keyboard_en_defs, learn_ctx_updates = \
            await _handle_general_learning_flow(user_profile, interaction_input, is_callback)
        db_context_updates.update(learn_ctx_updates)

    elif current_mode == "CASUAL_CHAT":
        # _handle_casual_chat_flow returns text already in target_lang_name
        # and keyboard_defs with translated text.
        next_mode_for_db, translated_response_text, translated_keyboard_defs, _ = \
            await _handle_casual_chat_flow(user_profile, interaction_input, target_lang_name)
        
        # For standard response sending, we need bot_response_text_en and response_keyboard_en_defs
        # Here, we directly use the translated versions.
        if translated_response_text:
            await send_whatsapp_message_utility(
                whatsapp_client=wa_client, recipient_wa_id=user_wa_id, text_message=translated_response_text,
                buttons_list=[Button(title=btn["text"], callback_data=btn["data"]) for btn in translated_keyboard_defs] if translated_keyboard_defs else None
            )
            # Log the bot's response (which is already translated)
            await database.store_chat_message(
                user_id=user_id, sender_type="bot", message_text=translated_response_text, # Log translated
                context_bootcamp_chapter=user_profile.get("current_bootcamp_chapter"), message_type='text'
            )
        send_standard_response_flag = False # Message sent by handler

    elif current_mode == "BOOTCAMP_COMPLETED_FINAL":
        # This state is reached after _handle_bootcamp_flow sends the completion message.
        # Here, we just present the main menu options again if the user types something generic.
        bot_response_text_en = state_prompts.get_state_message("MAIN_MENU_PROMPT", name=user_name)
        response_keyboard_en_defs = [
            {"text_key":"POST_ONBOARDING_CHOICE_BUTTON_LEARN", "data":"action:main:start_general_learning"},
            {"text_key":"POST_ONBOARDING_CHOICE_BUTTON_CHAT", "data":"action:main:start_chatting"}
        ]
        next_mode_for_db = "POST_ONBOARDING_CHOICE" # Move to main menu choice

    else: # Fallback for unhandled modes or if no response generated by specific handlers
        logger.warning(f"Unhandled mode '{current_mode}' or no response generated by handlers for user {user_id}. Using default fallback.")
        bot_response_text_en = state_prompts.get_state_message("DEFAULT_FALLBACK", name=user_name)
        # Determine a sensible next mode for fallback
        if user_profile.get("has_completed_bootcamp_onboarding"):
            next_mode_for_db = "POST_ONBOARDING_CHOICE"
            response_keyboard_en_defs = [ # Main menu options
                {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_BOOTCAMP", "data": "action:main:start_bootcamp"},
                {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_CHAT", "data": "action:main:start_chatting"},
                {"text_key": "POST_ONBOARDING_CHOICE_BUTTON_LEARN", "data": "action:main:start_general_learning"}
            ]
        else: # If onboarding not complete, guide towards it
            # This logic might need refinement based on how far they got in onboarding
            next_mode_for_db = "AWAITING_BOOTCAMP_WELCOME_AND_NAME" 
            # No specific keyboard here, the AWAITING_BOOTCAMP_WELCOME_AND_NAME state will prompt for name.

    # --- Update Database ---
    if db_context_updates: # Updates to JSONB fields or simple fields like 'name'
        await database.update_user_context(user_id, db_context_updates)
    
    # Mode update should happen after all other DB updates for the current interaction
    if next_mode_for_db != user_profile.get("current_mode"):
        logger.info(f"Updating user mode in DB from '{user_profile.get('current_mode')}' to '{next_mode_for_db}' for user {user_id}")
        mode_update_success = await database.update_user_mode(user_id, next_mode_for_db)
        if not mode_update_success:
            logger.critical(f"CRITICAL: DB mode update to '{next_mode_for_db}' FAILED for user {user_id}.")
            # Override response to send critical error message
            bot_response_text_en = state_prompts.get_state_message("ERROR_MESSAGE", name=user_name)
            response_keyboard_en_defs = None
            send_standard_response_flag = True # Ensure this error message is sent
    
    # --- Send Response to User ---
    if send_standard_response_flag and bot_response_text_en:
        # If bot_response_text_en was set by a handler that didn't translate, translate now.
        # _handle_casual_chat_flow already returns translated text.
        if current_mode != "CASUAL_CHAT": # Because casual chat already returns translated
            final_bot_response_text = await get_translated_text(bot_response_text_en, target_lang_name, f"chatbot response for state {current_mode}")
        else: # Casual chat already provided translated text
            final_bot_response_text = bot_response_text_en 

        final_buttons_list = None
        if response_keyboard_en_defs:
            final_buttons_list = []
            for btn_def in response_keyboard_en_defs:
                # text_key is the key for state_prompts, text is for already translated text (from casual_chat)
                btn_text_to_translate_en = state_prompts.get_state_message(btn_def["text_key"], name=user_name) if "text_key" in btn_def else btn_def.get("text", "")
                
                if current_mode == "CASUAL_CHAT": # Casual chat already provides translated button text
                    btn_text_final_lang = btn_text_to_translate_en
                else:
                    btn_text_final_lang = await get_translated_text(btn_text_to_translate_en, target_lang_name, "button text")

                if btn_text_final_lang and btn_text_final_lang.strip():
                    final_buttons_list.append(Button(title=btn_text_final_lang, callback_data=btn_def["data"]))
                else:
                    logger.warning(f"Button title for key/text '{btn_def.get('text_key', btn_def.get('text'))}' was empty after translation to {target_lang_name}. Skipping button.")
            if not final_buttons_list: final_buttons_list = None
        
        await send_whatsapp_message_utility(
            whatsapp_client=wa_client, recipient_wa_id=user_wa_id, 
            text_message=final_bot_response_text, buttons_list=final_buttons_list
        )
        # Log bot's response (English version for internal consistency, or final if casual chat)
        text_to_log_for_bot = bot_response_text_en if current_mode != "CASUAL_CHAT" else final_bot_response_text
        await database.store_chat_message(
            user_id=user_id, sender_type="bot", message_text=text_to_log_for_bot,
            context_bootcamp_chapter=user_profile.get("current_bootcamp_chapter"), message_type='text'
        )
    elif not send_standard_response_flag:
        logger.info(f"Standard response sending skipped for user {user_id} as handler managed it directly.")

    logger.info(f"--- End Interaction Processing for User {user_id} (DB mode is now: {next_mode_for_db}) ---")


# --- FastAPI Endpoints & WhatsApp Handlers ---
if wa: # Only register handlers if WhatsApp client initialized successfully
    @wa.on_message(fil.text)
    async def text_message_handler(client: WhatsApp, msg: Message):
        if not msg.from_user or not msg.from_user.wa_id or not msg.id:
            logger.warning("Text message handler: Invalid message object (missing user/id). Skipping.")
            return
        # Use WAMID for deduplication
        if is_message_processed(msg.id): return

        user_wa_id = msg.from_user.wa_id
        user_name_from_wa = msg.from_user.name # This can be None or the user's WA profile name
        interaction_text = msg.text
        logger.info(f"\n<<< Text Message Received <<< \nFrom: {user_wa_id} ({user_name_from_wa or 'N/A'}), WAMID: {msg.id}, Text: '{interaction_text}'")

        if not database.supabase: # Critical check
            logger.critical("Supabase client not initialized. Cannot process message.")
            # Attempt to notify user if possible, though without DB, user context is lost.
            if client and user_wa_id: await send_whatsapp_message_utility(client, user_wa_id, "I'm currently experiencing technical difficulties with my database. Please try again later.")
            return

        # Get or create user. Pass name from WA as a fallback if DB name is generic.
        user_profile = await database.get_or_create_user(whatsapp_id=user_wa_id, name=user_name_from_wa)
        if not user_profile:
            logger.error(f"Failed to get or create user profile for WA ID {user_wa_id}.")
            if client and user_wa_id: await send_whatsapp_message_utility(client, user_wa_id, "I'm having trouble accessing your profile. Please try sending your message again.")
            return
        
        # Ensure the name in user_profile is the most up-to-date one.
        if user_name_from_wa and (user_profile.get("name") == "User" or user_profile.get("name") is None):
            logger.info(f"Updating user {user_profile['id']} name from WA profile: '{user_name_from_wa}'")
            await database.update_user_context(user_profile["id"], {"name": user_name_from_wa})
            user_profile["name"] = user_name_from_wa


        background_tasks = BackgroundTasks() # Though not explicitly used in process_user_interaction directly
        try:
            await process_user_interaction(user_profile, interaction_text, False, background_tasks, client, msg)
        except Exception as e:
            logger.critical(f"!!! UNHANDLED EXCEPTION IN TEXT HANDLER: User {user_profile.get('id')}, WA ID {user_wa_id} !!! Error: {e}", exc_info=True)
            traceback.print_exc() # For detailed console/log output
            # Send generic error to user
            error_msg_en = "An unexpected error occurred. Our team has been notified. Please try sending your message again or type /help if the issue persists."
            raw_lang_pref_error = user_profile.get("bootcamp_onboarding_data", {}).get("language_preference", "English")
            target_lang_name_for_error = get_target_language_name(raw_lang_pref_error)
            error_msg_translated = await get_translated_text(error_msg_en, target_lang_name_for_error, "critical error message")
            if client and user_wa_id: await send_whatsapp_message_utility(client, user_wa_id, error_msg_translated)


    @wa.on_callback_button()
    async def button_callback_handler(client: WhatsApp, clb: CallbackButton):
        # Deduplication for callbacks should use a combination of original message ID and callback data
        original_message_id = clb.reply_to_message.id if clb.reply_to_message and clb.reply_to_message.id else clb.id
        if not original_message_id: # Should always have one of these
             logger.warning(f"Button callback missing identifiable message ID. Data: {clb.data}")
             return
        
        dedup_key = f"cb_{original_message_id}_{clb.data[:30]}" # Truncate callback data for key length
        if is_message_processed(dedup_key): return

        user_wa_id = clb.from_user.wa_id
        user_name_from_wa = clb.from_user.name
        interaction_text = clb.data # This is the callback data string
        logger.info(f"\n<<< Button Callback Received <<< \nFrom: {user_wa_id} ({user_name_from_wa or 'N/A'}), OrigMsgID: {original_message_id}, CallbackData='{interaction_text}'")

        if not database.supabase: logger.critical("Supabase client not initialized."); return
        user_profile = await database.get_or_create_user(whatsapp_id=user_wa_id, name=user_name_from_wa)
        if not user_profile: logger.error(f"Failed to get/create profile for WA ID {user_wa_id}."); return
        if user_name_from_wa and (user_profile.get("name") == "User" or user_profile.get("name") is None):
            await database.update_user_context(user_profile["id"], {"name": user_name_from_wa}); user_profile["name"] = user_name_from_wa

        background_tasks = BackgroundTasks()
        try:
            await process_user_interaction(user_profile, interaction_text, True, background_tasks, client, clb)
        except Exception as e:
            logger.critical(f"!!! UNHANDLED EXCEPTION IN BUTTON HANDLER: User {user_profile.get('id')}, WA ID {user_wa_id} !!! Error: {e}", exc_info=True)
            traceback.print_exc()
            error_msg_en = "An error occurred processing your selection. Our team has been notified. Please try again or type /help."
            raw_lang_pref_error = user_profile.get("bootcamp_onboarding_data", {}).get("language_preference", "English")
            target_lang_name_for_error = get_target_language_name(raw_lang_pref_error)
            error_msg_translated = await get_translated_text(error_msg_en, target_lang_name_for_error, "critical error message")
            if client and user_wa_id: await send_whatsapp_message_utility(client, user_wa_id, error_msg_translated)


    @wa.on_callback_selection() # For list message selections
    async def list_selection_handler(client: WhatsApp, sel: CallbackSelection):
        original_message_id = sel.reply_to_message.id if sel.reply_to_message and sel.reply_to_message.id else sel.id
        if not original_message_id:
             logger.warning(f"List selection callback missing identifiable message ID. Data: {sel.data}")
             return

        dedup_key = f"sel_{original_message_id}_{sel.data[:30]}"
        if is_message_processed(dedup_key): return

        user_wa_id = sel.from_user.wa_id
        user_name_from_wa = sel.from_user.name
        interaction_text = sel.data # This is the callback data string from the selected list item
        logger.info(f"\n<<< List Selection Received <<< \nFrom: {user_wa_id} ({user_name_from_wa or 'N/A'}), OrigMsgID: {original_message_id}, CallbackData='{interaction_text}'")

        if not database.supabase: logger.critical("Supabase client not initialized."); return
        user_profile = await database.get_or_create_user(whatsapp_id=user_wa_id, name=user_name_from_wa)
        if not user_profile: logger.error(f"Failed to get/create profile for WA ID {user_wa_id}."); return
        if user_name_from_wa and (user_profile.get("name") == "User" or user_profile.get("name") is None):
            await database.update_user_context(user_profile["id"], {"name": user_name_from_wa}); user_profile["name"] = user_name_from_wa
            
        background_tasks = BackgroundTasks()
        try:
            await process_user_interaction(user_profile, interaction_text, True, background_tasks, client, sel)
        except Exception as e:
            logger.critical(f"!!! UNHANDLED EXCEPTION IN LIST SELECTION HANDLER: User {user_profile.get('id')}, WA ID {user_wa_id} !!! Error: {e}", exc_info=True)
            traceback.print_exc()
            error_msg_en = "An error occurred processing your list selection. Our team has been notified. Please try again or type /help."
            raw_lang_pref_error = user_profile.get("bootcamp_onboarding_data", {}).get("language_preference", "English")
            target_lang_name_for_error = get_target_language_name(raw_lang_pref_error)
            error_msg_translated = await get_translated_text(error_msg_en, target_lang_name_for_error, "critical error message")
            if client and user_wa_id: await send_whatsapp_message_utility(client, user_wa_id, error_msg_translated)

    # Message Status Handlers (for logging/tracking)
    @wa.on_message_status(fil.failed)
    async def failed_message_handler(_: WhatsApp, status: MessageStatus):
        error_details = "Unknown error"
        if status.error: error_details = f"Code: {status.error.code}, Title: {status.error.title}, Msg: {status.error.message}, Details: {status.error.error_data}"
        logger.error(f"Message sending FAILED. WAMID: {status.id}, To: {status.recipient_id}, Timestamp: {status.timestamp}, Status: {status.status.value if status.status else 'N/A'}, Error: {error_details}")

    @wa.on_message_status(fil.sent)
    async def sent_message_status_handler(_: WhatsApp, status: MessageStatus): logger.info(f"Message SENT. WAMID: {status.id}, To: {status.recipient_id}, Timestamp: {status.timestamp}")
    @wa.on_message_status(fil.delivered)
    async def delivered_message_status_handler(_: WhatsApp, status: MessageStatus): logger.info(f"Message DELIVERED. WAMID: {status.id}, To: {status.recipient_id}, Timestamp: {status.timestamp}")
    @wa.on_message_status(fil.read)
    async def read_message_status_handler(_: WhatsApp, status: MessageStatus): logger.info(f"Message READ. WAMID: {status.id}, To: {status.recipient_id}, Timestamp: {status.timestamp}")

else: # wa client failed to initialize
    logger.critical("WhatsApp client 'wa' is None. Handlers will not be registered. Webhook will likely fail.")

# --- Internal API Endpoint for Triggering Initial Message ---
class InitialMessagePayload(BaseModel):
    whatsapp_id: str # This should be the cleaned WA ID (digits only)
    name: str
    user_db_id: str # The UUID from your users table

@app.post(config.INITIATE_MESSAGE_ENDPOINT_PATH, tags=["Internal :: DO NOT EXPOSE PUBLICLY"])
async def trigger_initial_message_endpoint(
    payload: InitialMessagePayload = Body(...),
    api_key: str = Depends(get_api_key) # Secure this endpoint
):
    """
    Internal endpoint to trigger an initial message to a user.
    Called by the google_sheet_to_supabase.py script.
    """
    logger.info(f"API Request: Trigger initial message to WA ID: {payload.whatsapp_id} for DB User ID: {payload.user_db_id}")
    
    if not wa: # Check if WhatsApp client is available
        logger.error(f"Cannot send initial message to {payload.whatsapp_id} because WhatsApp client is not initialized.")
        raise HTTPException(status_code=500, detail="WhatsApp client not initialized, cannot send message.")

    db_user = await database.get_user_bootcamp_details(payload.user_db_id)
    if not db_user:
        logger.warning(f"User with DB ID {payload.user_db_id} not found. Cannot send initial message via API.")
        raise HTTPException(status_code=404, detail=f"User with DB ID {payload.user_db_id} not found.")

    effective_name = db_user.get("name") if db_user.get("name") and db_user.get("name") != "User" else payload.name

    if db_user.get("initial_message_sent"):
        logger.info(f"Initial message already marked as sent for user {payload.user_db_id} (WA ID: {payload.whatsapp_id}). Skipping API trigger.")
        return {"status": "skipped", "detail": "Message already marked as sent."}

    # Set user mode to AWAITING_GREETING to start the conversation flow
    target_initial_mode = "AWAITING_GREETING" 
    await database.update_user_mode(db_user["id"], target_initial_mode)
    
    # Reset name_provided_once flag for onboarding
    b_data_init = db_user.get("bootcamp_onboarding_data", {})
    if not isinstance(b_data_init, dict): b_data_init = {} # Ensure it's a dict
    b_data_init["name_provided_once"] = False 
    await database.update_user_context(db_user["id"], {"bootcamp_onboarding_data": b_data_init})

    # Get the English version of the greeting message
    message_text_en = state_prompts.get_state_message(
        target_initial_mode, 
        name=effective_name if effective_name and effective_name != "User" else "there"
    )
    
    # For initial messages, typically send in default language (English) or try to guess if possible.
    # Here, we'll send in English as language preference isn't known yet.
    # Translation can occur once the user responds and sets a preference.
    
    await send_whatsapp_message_utility(
        whatsapp_client=wa, 
        recipient_wa_id=payload.whatsapp_id, # Expects cleaned WA ID
        text_message=message_text_en 
        # No buttons for the very first greeting message usually
    )
    await database.mark_initial_message_sent(payload.user_db_id) # Mark as sent in DB
    logger.info(f"Initial message sent to {payload.whatsapp_id} and DB marked for user {payload.user_db_id}.")
    return {"status": "success", "detail": f"Initial message process initiated for {payload.whatsapp_id}"}

# --- Application Lifecycle Events ---
@app.on_event("startup")
async def startup_event():
    """Actions to perform on application startup."""
    logger.info(f"[{datetime.now(timezone.utc)}] {config.BOT_NAME} Application startup...")
    if not database.supabase: logger.critical("Supabase client FAILED to initialize (database.py). Database operations will fail.")
    else: logger.info("Supabase client appears initialized successfully.")
    
    if not os.path.isdir(bootcamp_manager.LESSON_BASE_PATH): 
        logger.critical(f"Bootcamp lessons directory NOT FOUND: {bootcamp_manager.LESSON_BASE_PATH}. Bootcamp content will fail to load.")
    else: logger.info(f"Bootcamp lessons directory verified: {bootcamp_manager.LESSON_BASE_PATH}")
    
    if wa: logger.info(f"{config.BOT_NAME} App ready. WhatsApp Webhook expected at: {config.WHATSAPP_WEBHOOK_ENDPOINT}")
    else: logger.warning(f"{config.BOT_NAME} App ready, but WhatsApp client FAILED to initialize. WhatsApp webhook will NOT function.")
    
    if not config.INTERNAL_API_KEY:
        logger.critical("CRITICAL: INTERNAL_API_KEY is not set in config. Internal API endpoints will be inaccessible.")
    else:
        logger.info("Internal API Key is loaded.")
    
    logger.info("Recommendation: For production with multiple workers, replace in-memory 'processed_message_ids_cache' with a distributed cache like Redis.")


@app.on_event("shutdown")
async def shutdown_event():
    """Actions to perform on application shutdown."""
    logger.info(f"[{datetime.now(timezone.utc)}] {config.BOT_NAME} Application shutdown...")
    if processed_message_ids_cache: # Clear in-memory cache on shutdown
        processed_message_ids_cache.clear()
        logger.info("In-memory message deduplication cache cleared.")

# --- Main Execution (for running with uvicorn directly) ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000)) # Default to 8000 if PORT env var is not set
    host = os.getenv("HOST", "127.0.0.1") # Default to localhost
    # Uvicorn reload flag from environment variable
    reload_status_str = os.getenv("UVICORN_RELOAD", "true").lower()
    reload_status = reload_status_str == "true"

    logger.info(f"Attempting to start Uvicorn server for main:app on {host}:{port} with reload: {reload_status}")
    import uvicorn
    uvicorn.run("main:app", host=host, port=port, reload=reload_status)
