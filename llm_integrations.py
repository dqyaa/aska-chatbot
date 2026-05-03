import httpx # Modern async HTTP client
import config # Your config.py
from typing import List, Dict, Optional, Any
import json
import logging # Added for logging

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = config.DEEPSEEK_API_KEY
DEEPSEEK_API_URL = config.DEEPSEEK_API_URL

async def query_deepseek(
    prompt: str,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    max_tokens: int = 1500,
    is_json_mode: bool = False,
    system_message: Optional[str] = None # Added for more control
) -> Optional[str]:
    """
    Queries the DeepSeek API with a given prompt and optional conversation history.
    """
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not configured.")
        return "Error: LLM service not configured."

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    messages = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    elif is_json_mode and not any(msg["role"] == "system" for msg in (conversation_history or [])):
        messages.append({"role": "system", "content": "You are a helpful assistant designed to output JSON."})

    if conversation_history:
        messages.extend(conversation_history)
    
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
    if is_json_mode:
        # For DeepSeek, if it supports a direct JSON output mode like OpenAI:
        # payload["response_format"] = {"type": "json_object"}
        # If not, reliance is on the prompt instructing JSON output.
        pass # Assuming prompting is the main method for now.

    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            response = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            
            response_data = response.json()
            
            if response_data.get("choices") and len(response_data["choices"]) > 0:
                content = response_data["choices"][0].get("message", {}).get("content")
                if content:
                    if is_json_mode:
                        content_stripped = content.strip()
                        if content_stripped.startswith("```json"):
                            content_stripped = content_stripped[7:]
                            if content_stripped.endswith("```"):
                                content_stripped = content_stripped[:-3]
                            return content_stripped.strip()
                        elif content_stripped.startswith("```"):
                             content_stripped = content_stripped[3:]
                             if content_stripped.endswith("```"):
                                content_stripped = content_stripped[:-3]
                             return content_stripped.strip()
                    return content.strip()
                else:
                    logger.error(f"DeepSeek API response missing content: {response_data}")
                    return "Sorry, I received an unexpected response from the LLM."
            else:
                logger.error(f"DeepSeek API response missing choices: {response_data}")
                if "error" in response_data:
                    logger.error(f"DeepSeek API Error: {response_data['error']}")
                return "Sorry, I couldn't get a response from the LLM."
                
        except httpx.HTTPStatusError as e:
            error_text = e.response.text
            logger.error(f"HTTP error occurred while querying DeepSeek: {e.response.status_code} - {error_text}")
            try:
                error_json = json.loads(error_text)
                detail = error_json.get("error", {}).get("message", "Unknown error detail.")
                return f"Sorry, there was an issue communicating with the LLM (HTTP {e.response.status_code}: {detail})."
            except json.JSONDecodeError:
                 return f"Sorry, there was an issue communicating with the LLM (HTTP {e.response.status_code})."
        except httpx.RequestError as e:
            logger.error(f"Request error occurred while querying DeepSeek: {e}")
            return "Sorry, I couldn't connect to the LLM service. Please check your connection."
        except Exception as e:
            logger.error(f"An unexpected error occurred while querying DeepSeek: {e}", exc_info=True)
            return "Sorry, an unexpected error occurred with the LLM."

async def get_intent_and_entities(
    text: str,
    current_mode: str,
) -> Dict[str, Any]:
    """
    Uses DeepSeek to determine user intent and extract relevant entities.
    (Keeping this function as it might be useful for other intents, but name extraction will be more specific)
    """
    prompt_context = f"The user is currently in '{current_mode}' mode. User's message: \"{text}\""
    possible_intents = [
        "GREETING", "START_BOOTCAMP_ONBOARDING", "PROVIDE_ONBOARDING_INFO",
        "REQUEST_BOOTCAMP_CHAPTER", "ACK_BOOTCAMP_CHAPTER_READY_FOR_QUIZ",
        "SUBMIT_BOOTCAMP_QUIZ_ANSWERS", "REQUEST_NEXT_BOOTCAMP_STEP", "COMPLETE_BOOTCAMP",
        "START_GENERAL_LEARNING", "PROVIDE_LEARNING_TOPIC_DETAILS", "CONFIRM_GENERAL_LEARNING_PLAN",
        "REQUEST_GENERAL_LESSON", "CASUAL_CHAT_QUERY", "ASK_FOR_HELP", "AMBIGUOUS_OR_UNKNOWN",
        "AFFIRMATIVE_RESPONSE", "NEGATIVE_RESPONSE", "PROVIDE_NAME"
    ]
    llm_prompt = f"""
    Analyze the user's message: "{text}"
    Context: {prompt_context}
    Identify the primary intent from the list: {', '.join(possible_intents)}.
    Extract relevant entities. For "PROVIDE_NAME", the entity should be "name".
    Respond ONLY with a valid JSON object: {{"intent": "CHOSEN_INTENT", "entities": {{"entity_name": "value"}}, "confidence_score": "HIGH|MEDIUM|LOW"}}
    """
    response_str = await query_deepseek(llm_prompt, model="deepseek-chat", is_json_mode=True)
    if response_str:
        try:
            intent_data = json.loads(response_str)
            if "intent" in intent_data and "entities" in intent_data:
                return intent_data
            else:
                logger.warning(f"LLM returned JSON for intent but with missing fields: {response_str}")
                return {"intent": "LLM_INVALID_JSON_STRUCTURE", "entities": {}, "raw_response": response_str}
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from LLM for intent: {e}. Response: {response_str}")
            return {"intent": "LLM_JSON_DECODE_ERROR", "entities": {}, "raw_response": response_str}
    return {"intent": "LLM_NO_RESPONSE", "entities": {}}

async def process_name_with_llm(raw_name_input: str) -> Optional[str]:
    """
    Uses LLM to extract and capitalize a name from raw user input.
    Example: "my name is deqya" -> "Deqya"
    Example: "deqya" -> "Deqya"
    Example: "call me aliya hassan" -> "Aliya Hassan"
    """
    if not raw_name_input.strip():
        return None

    system_prompt = "You are an expert at extracting and capitalizing person names from text. Respond only with the extracted and correctly capitalized name. If no name is found, respond with an empty string."
    user_prompt = f"Extract the person's name from the following text and capitalize it correctly: \"{raw_name_input}\""
    
    # Use a simpler model if available and suitable for this task to save costs/latency
    # For now, using the default chat model.
    processed_name_str = await query_deepseek(
        prompt=user_prompt,
        system_message=system_prompt,
        model="deepseek-chat", # Or a smaller, faster model if available
        temperature=0.2, # Lower temperature for more deterministic output
        max_tokens=50 # Names are usually short
    )

    if processed_name_str and not processed_name_str.startswith("Error:") and not processed_name_str.startswith("Sorry,"):
        # Clean up potential quotes or extra LLM text if any (though system prompt tries to avoid this)
        cleaned_name = processed_name_str.strip().replace('"', '')
        if cleaned_name: # Ensure it's not an empty string after stripping
            return cleaned_name
        else: # LLM might have correctly returned an empty string if no name was found
            logger.info(f"LLM indicated no name found in input: '{raw_name_input}'")
            return None 
    else:
        logger.error(f"LLM failed to process name or returned an error for input: '{raw_name_input}'. LLM response: {processed_name_str}")
        # Fallback: simple capitalization if LLM fails
        return raw_name_input.strip().title() if raw_name_input.strip() else None


async def translate_text_with_llm(
    text_to_translate: str,
    target_language_name: str, # e.g., "Malay", "Spanish"
    source_language_name: str = "English",
    context_hint: Optional[str] = None # e.g., "chatbot greeting", "quiz question"
) -> Optional[str]:
    """
    Translates text to the target language using an LLM.
    """
    if not text_to_translate.strip() or not target_language_name.strip():
        return text_to_translate # Return original if no text or target language

    if source_language_name.lower() == target_language_name.lower():
        return text_to_translate # No translation needed

    system_prompt = f"You are an expert translator. Translate the user's text from {source_language_name} to {target_language_name}. Respond ONLY with the translated text, nothing else. Maintain the original meaning and tone. If context is provided, use it to improve the translation."
    
    user_prompt_parts = [f"Translate the following text from {source_language_name} to {target_language_name}:"]
    if context_hint:
        user_prompt_parts.append(f"(Context: This text is a {context_hint})")
    user_prompt_parts.append(f"\n\nText to translate:\n\"\"\"\n{text_to_translate}\n\"\"\"")
    
    user_prompt = "\n".join(user_prompt_parts)

    translated_text = await query_deepseek(
        prompt=user_prompt,
        system_message=system_prompt,
        model="deepseek-chat", # Use a model good for translation
        temperature=0.3, # Slightly lower for more faithful translation
        max_tokens=int(len(text_to_translate) * 2.5) + 50 # Estimate tokens needed
    )

    if translated_text and not translated_text.startswith("Error:") and not translated_text.startswith("Sorry,"):
        cleaned_text = translated_text.strip()
        # Remove potential triple-quotes from LLM translation output
        if cleaned_text.startswith('"""') and cleaned_text.endswith('"""') and len(cleaned_text) >= 6:
            cleaned_text = cleaned_text[3:-3].strip()
        # Additionally, handle cases where it might be a markdown code block (e.g., ```text```)
        elif cleaned_text.startswith("```") and cleaned_text.endswith("```") and len(cleaned_text) >= 6:
            # This handles ```text```. If there's a language specifier like ```lang\ntext\n```,
            # further stripping might be needed if the initial strip() doesn't remove "lang\n".
            # For simplicity, we'll assume the common case here.
            cleaned_text = cleaned_text[3:-3].strip()
        
        return cleaned_text
    else:
        logger.error(f"LLM failed to translate text to {target_language_name} or returned an error for: '{text_to_translate[:50]}...'. LLM response: {translated_text}")
        return None # Indicate failure to translate