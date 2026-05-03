from supabase import create_client, Client
from typing import Optional, Dict, List, Any
import config # Your config.py
import asyncio
import json # For handling JSONB data safely
from datetime import datetime, timezone
import logging

# Configure logger for this module
logger = logging.getLogger(__name__)
if not logger.hasHandlers(): # Avoid adding multiple handlers if already configured
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')

# Initialize Supabase client
try:
    supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    logger.info("Supabase client initialized successfully in database.py.")
except Exception as e:
    logger.error(f"Error initializing Supabase client in database.py: {e}", exc_info=True)
    supabase = None # type: ignore

# --- User Management ---
async def get_or_create_user(
    whatsapp_id: Optional[str] = None,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone_number_from_sheet: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Retrieves an existing user or creates a new one.
    Initializes new users with default bootcamp/learning states.
    Ensures JSONB fields are initialized as dicts.
    """
    if not supabase:
        logger.error("Supabase client not initialized in get_or_create_user.")
        return None

    user_data = None
    try:
        # Priority 1: Find by whatsapp_id if provided
        if whatsapp_id:
            logger.info(f"Attempting to find user by whatsapp_id: {whatsapp_id}")
            response = await asyncio.to_thread(
                lambda: supabase.table("users").select("*").eq("whatsapp_id", whatsapp_id).maybe_single().execute()
            )
            user_data = response.data
            if user_data:
                logger.info(f"User found by whatsapp_id: {user_data.get('id')}")
            else:
                logger.info(f"User not found by whatsapp_id: {whatsapp_id}")

        # Priority 2: If not found by whatsapp_id and email is provided
        if not user_data and email:
            logger.info(f"Attempting to find user by email: {email.lower()}")
            response = await asyncio.to_thread(
                lambda: supabase.table("users").select("*").eq("email", email.lower()).maybe_single().execute()
            )
            user_data = response.data
            if user_data:
                logger.info(f"User found by email: {user_data.get('id')}")
                if whatsapp_id and user_data.get("whatsapp_id") != whatsapp_id:
                    logger.info(f"Linking whatsapp_id {whatsapp_id} to user {user_data.get('id')} found by email.")
                    await asyncio.to_thread(
                        lambda: supabase.table("users").update({"whatsapp_id": whatsapp_id, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", user_data["id"]).execute()
                    )
                    user_data["whatsapp_id"] = whatsapp_id
            else:
                logger.info(f"User not found by email: {email.lower()}")

        update_payload = {}
        if name and (not user_data or user_data.get("name") != name):
            update_payload["name"] = name
        if email and (not user_data or user_data.get("email") != email.lower()):
            update_payload["email"] = email.lower()
        if whatsapp_id and (not user_data or user_data.get("whatsapp_id") != whatsapp_id):
            update_payload["whatsapp_id"] = whatsapp_id
        if phone_number_from_sheet and (not user_data or user_data.get("phone_number") != phone_number_from_sheet):
            update_payload["phone_number"] = phone_number_from_sheet

        if user_data: # User exists
            if update_payload:
                logger.info(f"Updating existing user {user_data.get('id')} with payload: {update_payload}")
                update_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                await asyncio.to_thread(
                    lambda: supabase.table("users").update(update_payload).eq("id", user_data["id"]).execute()
                )
                response_updated = await asyncio.to_thread(
                    lambda: supabase.table("users").select("*").eq("id", user_data["id"]).single().execute()
                )
                user_data = response_updated.data
            
            if user_data: # Re-check user_data after potential update
                if user_data.get("current_mode") is None:
                    logger.info(f"User {user_data.get('id')} missing current_mode. Setting to AWAITING_GREETING.")
                    await update_user_mode(user_data["id"], "AWAITING_GREETING")
                    user_data["current_mode"] = "AWAITING_GREETING"
                
                jsonb_fields_to_initialize = ["bootcamp_onboarding_data", "bootcamp_quiz_scores", "current_quiz_session", "draft_learning_plan", "additional_data"]
                needs_jsonb_initialization_update = False
                update_for_jsonb_init = {}

                for field in jsonb_fields_to_initialize:
                    if user_data.get(field) is None or not isinstance(user_data.get(field), dict):
                        logger.info(f"User {user_data.get('id')}: Initializing JSONB field '{field}' to {{}}.")
                        user_data[field] = {} 
                        update_for_jsonb_init[field] = {}
                        needs_jsonb_initialization_update = True
                
                if needs_jsonb_initialization_update:
                    logger.info(f"User {user_data.get('id')}: Performing DB update for JSONB initializations: {update_for_jsonb_init}")
                    update_for_jsonb_init["updated_at"] = datetime.now(timezone.utc).isoformat()
                    await asyncio.to_thread(
                        lambda: supabase.table("users").update(update_for_jsonb_init).eq("id", user_data["id"]).execute()
                    )
            return user_data
        else: # User does not exist, create new one
            logger.info(f"Creating new user. WA ID: {whatsapp_id}, Email: {email}")
            new_user_payload = {
                "whatsapp_id": whatsapp_id,
                "name": name or "User", 
                "email": email.lower() if email else None,
                "phone_number": phone_number_from_sheet,
                "current_mode": "AWAITING_GREETING", 
                "initial_message_sent": False,
                "has_completed_bootcamp_onboarding": False,
                "current_bootcamp_chapter": 1,
                "bootcamp_quiz_scores": {}, 
                "total_bootcamp_score": 0,
                "last_bootcamp_step_completed": "none",
                "bootcamp_onboarding_data": {}, 
                "current_quiz_session": {}, 
                "draft_learning_plan": {}, 
                "additional_data": {}, 
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            new_user_payload_filtered = {k: v for k, v in new_user_payload.items() if v is not None or k in ["email", "phone_number", "whatsapp_id"]} 
            if not new_user_payload_filtered.get("name"): new_user_payload_filtered["name"] = "User"

            insert_response = await asyncio.to_thread(
                lambda: supabase.table("users").insert(new_user_payload_filtered).execute()
            )
            if insert_response.data:
                logger.info(f"New user created successfully: {insert_response.data[0].get('id')}")
                return insert_response.data[0]
            else:
                error_detail = insert_response.error if hasattr(insert_response, 'error') else 'Unknown error'
                logger.error(f"Error creating user (WA: {whatsapp_id}, Email: {email}): {error_detail}")
                return None
    except Exception as e:
        logger.error(f"Exception in get_or_create_user (WA: {whatsapp_id}, Email: {email}): {e}", exc_info=True)
        return None

async def update_user_mode(user_id: str, new_mode: str) -> bool:
    """Updates the user's current conversational mode."""
    if not supabase:
        logger.error(f"Supabase client not initialized. Cannot update mode for user {user_id}.")
        return False
    logger.info(f"Attempting to update mode for user {user_id} to '{new_mode}'")
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table("users").update({"current_mode": new_mode, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", user_id).execute()
        )
        if hasattr(response, 'error') and response.error:
            logger.error(f"Supabase error updating user mode for {user_id} to {new_mode}: {response.error}")
            return False
        logger.info(f"Successfully updated mode for user {user_id} to '{new_mode}'.")
        return True
    except Exception as e:
        logger.error(f"Exception updating user mode for {user_id} to {new_mode}: {e}", exc_info=True)
        return False

async def update_user_context(user_id: str, updates: Dict[str, Any]) -> bool:
    """
    Updates various context fields for a user.
    Handles JSONB fields by merging if they exist and the update value is a dictionary.
    """
    if not supabase:
        logger.error(f"Supabase client not initialized. Cannot update context for user {user_id}.")
        return False
    
    logger.info(f"Attempting to update context for user {user_id} with updates: {updates}")
    try:
        # Identify JSONB fields that might need merging
        jsonb_fields_for_merge = ["bootcamp_onboarding_data", "draft_learning_plan", "current_quiz_session", "bootcamp_quiz_scores", "additional_data"]
        
        # Prepare fields to select for fetching current JSONB data if merging is needed
        fields_to_select_list = ["id"] # Always select id for safety, though not strictly needed for update
        for jf in jsonb_fields_for_merge:
            if jf in updates and isinstance(updates[jf], dict):
                 fields_to_select_list.append(jf)
        fields_to_select_str = ", ".join(list(set(fields_to_select_list))) # Unique fields

        current_data = {}
        if len(fields_to_select_list) > 1: # More than just 'id' means we need to fetch for merge
            current_data_response = await asyncio.to_thread(
                lambda: supabase.table("users").select(fields_to_select_str).eq("id", user_id).maybe_single().execute()
            )
            current_data = current_data_response.data if current_data_response.data else {}

        final_updates = updates.copy()

        for json_field in jsonb_fields_for_merge:
            if json_field in updates and isinstance(updates[json_field], dict): # Only merge if update value is a dict
                existing_json_value = current_data.get(json_field, {})
                
                if not isinstance(existing_json_value, dict): # Ensure existing value is a dict
                    logger.warning(f"User {user_id}, field '{json_field}' is not a dict in DB (is {type(existing_json_value)}). Overwriting with new dict from updates.")
                    existing_json_value = {} 
                
                merged_json = existing_json_value.copy()
                merged_json.update(updates[json_field]) # Merge the update into existing
                final_updates[json_field] = merged_json
            elif json_field in updates: # If update for a json_field is not a dict (e.g., None or a string), assign directly
                final_updates[json_field] = updates[json_field]

        final_updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        response = await asyncio.to_thread(
            lambda: supabase.table("users").update(final_updates).eq("id", user_id).execute()
        )
        if hasattr(response, 'error') and response.error:
            logger.error(f"Supabase error updating user context for {user_id}: {response.error}")
            return False
        
        logger.info(f"Successfully updated context for user {user_id}.")
        return True
    except Exception as e:
        logger.error(f"Exception updating user context for {user_id} with {updates}: {e}", exc_info=True)
        return False

# --- Chat History ---
async def store_chat_message(
    user_id: str,
    sender_type: str, 
    message_text: Optional[str] = None,
    message_type: str = 'text',
    whatsapp_message_id: Optional[str] = None,
    interactive_payload: Optional[Dict[str, Any]] = None,
    context_learning_plan_id: Optional[str] = None,
    context_lesson_id: Optional[str] = None,
    context_bootcamp_chapter: Optional[int] = None
) -> bool:
    """Stores a chat message in the database."""
    if not supabase:
        logger.error(f"Supabase client not initialized. Cannot store chat message for user {user_id}.")
        return False
    try:
        message_data = {
            "user_id": user_id, "sender_type": sender_type, "message_text": message_text,
            "message_type": message_type, "whatsapp_message_id": whatsapp_message_id,
            "interactive_payload": interactive_payload, "context_learning_plan_id": context_learning_plan_id,
            "context_lesson_id": context_lesson_id, "context_bootcamp_chapter": context_bootcamp_chapter,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        response = await asyncio.to_thread(
            lambda: supabase.table("chat_history").insert(message_data).execute()
        )
        if hasattr(response, 'error') and response.error:
            logger.error(f"Supabase error storing chat message for user {user_id}: {response.error}")
            return False
        return True
    except Exception as e:
        logger.error(f"Exception storing chat message for user {user_id}: {e}", exc_info=True)
        return False

# --- General Learning Plan Management (Merged from learning_manager.py) ---
async def create_learning_plan(
    user_id: str, topic: str, goals: str, 
    specific_interest: Optional[str] = None, 
    prior_knowledge: Optional[str] = None, 
    commitment: Optional[str] = None # Changed to string to match state_prompts usage
) -> Optional[Dict[str, Any]]:
    """Creates a new general (non-bootcamp) learning plan for a user."""
    if not supabase:
        logger.error("Supabase client not initialized. Cannot create learning plan.")
        return None
    try:
        plan_data = {
            "user_id": user_id, "topic": topic, "goals": goals,
            "specific_interest": specific_interest, "prior_knowledge": prior_knowledge,
            "commitment_time_per_week_hours": commitment, # Storing as string as received
            "status": "pending_generation", 
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        response = await asyncio.to_thread(
            lambda: supabase.table("learning_plans").insert(plan_data).execute()
        )
        if response.data:
            logger.info(f"Successfully created learning plan for user {user_id}, topic '{topic}'. Plan ID: {response.data[0].get('id')}")
            return response.data[0]
        
        error_detail = response.error if hasattr(response, 'error') else 'Unknown error'
        logger.error(f"Error creating general learning plan for user {user_id}: {error_detail}")
        return None
    except Exception as e:
        logger.error(f"Exception in create_learning_plan for user {user_id}: {e}", exc_info=True)
        return None

async def get_user_learning_plans(user_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves general learning plans for a user, optionally filtered by status."""
    if not supabase:
        logger.error("Supabase client not initialized. Cannot get learning plans.")
        return []
    try:
        query = supabase.table("learning_plans").select("id, topic, specific_interest, status").eq("user_id", user_id)
        if status:
            query = query.eq("status", status)
        response = await asyncio.to_thread(lambda: query.execute())
        return response.data if response.data else []
    except Exception as e:
        logger.error(f"Error getting general learning plans for user {user_id}: {e}", exc_info=True)
        return []

# --- Bootcamp Progress ---
async def get_user_bootcamp_details(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves all bootcamp-related and other user details from the 'users' table."""
    if not supabase:
        logger.error("Supabase client not initialized. Cannot get user bootcamp details.")
        return None
    try:
        select_query = (
            "id, name, email, whatsapp_id, phone_number, current_mode, "
            "initial_message_sent, has_completed_bootcamp_onboarding, "
            "current_bootcamp_chapter, bootcamp_quiz_scores, total_bootcamp_score, "
            "last_bootcamp_step_completed, bootcamp_onboarding_data, current_quiz_session, "
            "draft_learning_plan, additional_data, created_at, updated_at"
        )
        response = await asyncio.to_thread(
            lambda: supabase.table("users").select(select_query).eq("id", user_id).maybe_single().execute()
        )
        
        if response.data:
            user_details = response.data
            # Ensure JSONB fields are dictionaries
            jsonb_fields = ["bootcamp_quiz_scores", "bootcamp_onboarding_data", "current_quiz_session", "draft_learning_plan", "additional_data"]
            for field in jsonb_fields:
                if user_details.get(field) is None or not isinstance(user_details.get(field), dict):
                    if isinstance(user_details.get(field), str): # If stored as string, try to parse
                        try:
                            parsed_json = json.loads(user_details.get(field))
                            user_details[field] = parsed_json if isinstance(parsed_json, dict) else {}
                        except json.JSONDecodeError:
                            logger.warning(f"Could not parse JSON string for field '{field}' for user {user_id}. Defaulting to {{}}.")
                            user_details[field] = {} 
                    else: # If None or not string/dict, default to empty dict
                        user_details[field] = {}
            return user_details
        return None
    except Exception as e:
        logger.error(f"Error getting user bootcamp details for {user_id}: {e}", exc_info=True)
        return None

async def update_user_bootcamp_progress(
    user_id: str,
    current_chapter: Optional[int] = None,
    last_step_completed: Optional[str] = None,
    quiz_scores_to_set: Optional[Dict[str, Any]] = None, 
    total_score_to_set: Optional[int] = None,
    has_completed_onboarding_val: Optional[bool] = None,
    current_quiz_session_to_set: Optional[Dict[str, Any]] = None 
) -> bool:
    """Updates specific bootcamp progress fields in the 'users' table."""
    if not supabase: return False
    try:
        payload = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if current_chapter is not None: payload["current_bootcamp_chapter"] = current_chapter
        if last_step_completed is not None: payload["last_bootcamp_step_completed"] = last_step_completed
        if quiz_scores_to_set is not None: payload["bootcamp_quiz_scores"] = quiz_scores_to_set
        if total_score_to_set is not None: payload["total_bootcamp_score"] = total_score_to_set
        if has_completed_onboarding_val is not None: payload["has_completed_bootcamp_onboarding"] = has_completed_onboarding_val
        if current_quiz_session_to_set is not None: payload["current_quiz_session"] = current_quiz_session_to_set

        if len(payload) > 1: 
            response = await asyncio.to_thread(
                lambda: supabase.table("users").update(payload).eq("id", user_id).execute()
            )
            if hasattr(response, 'error') and response.error:
                logger.error(f"Supabase error updating user bootcamp progress for {user_id} with payload {payload}: {response.error}")
                return False
        logger.info(f"Successfully updated bootcamp progress for user {user_id}.")
        return True
    except Exception as e:
        logger.error(f"Exception updating user bootcamp progress for {user_id} with payload {payload}: {e}", exc_info=True)
        return False

async def initialize_bootcamp_for_user(user_id: str) -> bool:
    """Sets initial bootcamp progress fields for a user after onboarding is confirmed complete."""
    logger.info(f"Initializing bootcamp for user {user_id}.")
    return await update_user_bootcamp_progress(
        user_id=user_id, current_chapter=1, last_step_completed="onboarding_complete", 
        quiz_scores_to_set={}, total_score_to_set=0, has_completed_onboarding_val=True, 
        current_quiz_session_to_set={"answers": {}, "current_question_index": 0, "chapter_number": 1} 
    )

async def record_quiz_score(user_id: str, chapter_number: int, score_percentage: int) -> bool:
    """Records a quiz score and updates the total score by re-summing all quiz scores."""
    if not supabase: return False
    try:
        user_data = await get_user_bootcamp_details(user_id)
        if not user_data:
            logger.error(f"User not found for recording quiz score: {user_id}")
            return False

        current_quiz_scores = user_data.get("bootcamp_quiz_scores", {})
        if not isinstance(current_quiz_scores, dict): current_quiz_scores = {} 

        quiz_key = f"quiz{chapter_number}"
        current_quiz_scores[quiz_key] = score_percentage 

        new_total_score = sum(v for k, v in current_quiz_scores.items() if isinstance(v, (int, float)))

        logger.info(f"Recording quiz score for user {user_id}, chapter {chapter_number}: {score_percentage}%. New total score: {new_total_score}")
        return await update_user_bootcamp_progress(
            user_id=user_id, quiz_scores_to_set=current_quiz_scores,
            total_score_to_set=new_total_score, last_step_completed=f"quiz_submitted_ch{chapter_number}", 
            current_quiz_session_to_set={} 
        )
    except Exception as e:
        logger.error(f"Error recording quiz score for user {user_id}, chapter {chapter_number}: {e}", exc_info=True)
        return False

# --- Functions for Google Sheet Sync & Initial Message Sending ---
async def upsert_users_from_sheet(users_data: List[Dict[str, Any]]) -> int:
    """
    Upserts users from sheet data. Uses email as conflict target.
    Initializes bootcamp fields to default if user is new.
    `users_data` should contain records with 'email', 'name', 'phone_number' (raw), and 'whatsapp_id' (cleaned).
    """
    if not supabase or not users_data:
        logger.error("Supabase client not initialized or no data to upsert from sheet.")
        return 0

    upserted_count = 0
    for record_from_sheet in users_data:
        try:
            email_val = record_from_sheet.get("email")
            if not email_val:
                logger.warning(f"Skipping sheet record due to missing email: {record_from_sheet}")
                continue
            email_val = email_val.lower().strip()

            # Data to be upserted for this user
            user_payload = {
                "name": record_from_sheet.get("name", "User").strip(),
                "email": email_val,
                "phone_number": record_from_sheet.get("phone_number"), # Raw phone from sheet
                "whatsapp_id": record_from_sheet.get("whatsapp_id"),   # Cleaned WA ID
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            
            # Add sheet registration date to additional_data
            sheet_reg_date = record_from_sheet.get("date")
            additional_data_update = {"sheet_registration_date": sheet_reg_date} if sheet_reg_date else {}

            # Check if user exists by email
            existing_user_resp = await asyncio.to_thread(
                lambda: supabase.table("users").select("id, additional_data").eq("email", email_val).maybe_single().execute()
            )
            existing_user = existing_user_resp.data

            if existing_user: # User exists, prepare for update
                logger.info(f"User {email_val} found (ID: {existing_user.get('id')}). Preparing update.")
                # Merge additional_data
                current_additional_data = existing_user.get("additional_data", {})
                if not isinstance(current_additional_data, dict): current_additional_data = {}
                current_additional_data.update(additional_data_update)
                user_payload["additional_data"] = current_additional_data
                
                response = await asyncio.to_thread(
                    lambda: supabase.table("users").update(user_payload).eq("email", email_val).execute()
                )
            else: # New user from sheet, prepare for insert
                logger.info(f"User {email_val} NOT found. Preparing insert.")
                user_payload.update({
                    "current_mode": "AWAITING_GREETING", "initial_message_sent": False,
                    "has_completed_bootcamp_onboarding": False, "current_bootcamp_chapter": 1,
                    "bootcamp_quiz_scores": {}, "total_bootcamp_score": 0,
                    "last_bootcamp_step_completed": "none", "bootcamp_onboarding_data": {},
                    "current_quiz_session": {}, "draft_learning_plan": {},
                    "additional_data": additional_data_update, # Set initial additional_data
                    "created_at": datetime.now(timezone.utc).isoformat()
                })
                response = await asyncio.to_thread(
                    lambda: supabase.table("users").insert(user_payload).execute()
                )
            
            if response.data:
                upserted_count += 1
                logger.info(f"Successfully upserted/inserted record for email {email_val}. User ID: {response.data[0].get('id')}")
            elif hasattr(response, 'error') and response.error:
                 logger.error(f"Error upserting record with email {email_val}: {response.error}")
            elif not response.data and not (hasattr(response, 'error') and response.error):
                 # This case can happen with HTTP 406 if RLS prevents returning data but operation was "successful"
                 logger.warning(f"Upsert for email {email_val} returned no data and no explicit error. Check RLS. Assuming operation might have succeeded if no error logged.")
                 # Consider if upserted_count should be incremented here based on policy. For now, only on explicit data.

        except Exception as e:
            logger.error(f"Exception during upsert_users_from_sheet loop for record (email: {record_from_sheet.get('email')}): {e}", exc_info=True)
            
    return upserted_count

async def get_users_for_initial_message() -> List[Dict[str, Any]]:
    """Fetches users who need an initial message."""
    if not supabase: return []
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table("users")
            .select("id, name, phone_number, email, whatsapp_id") 
            .eq("initial_message_sent", False)
            .not_.is_("whatsapp_id", "null") # Must have a cleaned whatsapp_id
            .neq("whatsapp_id", "")          # Ensure whatsapp_id is not an empty string
            .execute()
        )
        return response.data if response.data else []
    except Exception as e:
        logger.error(f"Error fetching users for initial message: {e}", exc_info=True)
        return []

async def mark_initial_message_sent(user_id: str) -> bool:
    """Marks that an initial message has been sent to the user."""
    if not supabase: return False
    try:
        logger.info(f"Marking initial message sent for user_id: {user_id}")
        response = await asyncio.to_thread(
            lambda: supabase.table("users")
            .update({"initial_message_sent": True, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", user_id)
            .execute()
        )
        if hasattr(response, 'error') and response.error:
            logger.error(f"Supabase error marking initial message sent for user {user_id}: {response.error}")
            return False
        return True
    except Exception as e:
        logger.error(f"Exception marking initial message sent for user {user_id}: {e}", exc_info=True)
        return False
