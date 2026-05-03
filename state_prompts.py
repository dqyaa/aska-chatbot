from typing import Optional, Any
import config # Assuming config.py is in the same directory or accessible via PYTHONPATH
import logging

logger = logging.getLogger(__name__)

# ALL PROMPTS ARE NOW IN ENGLISH. Translation will be handled dynamically.
def get_state_message(state: str, name: Optional[str] = None, **kwargs: Any) -> str:
    """
    Returns the appropriate ENGLISH message for the given state.
    Uses f-strings for personalization.
    kwargs can be used to pass additional dynamic details.
    """
    name = name or "there"

    # General learning plan details from kwargs
    topic = kwargs.get("topic", "N/A")
    specific_interest = kwargs.get("specific_interest", "N/A")
    prior_knowledge_val = kwargs.get("prior_knowledge", "N/A")
    goals_val = kwargs.get("goals", "N/A")
    commitment_val = kwargs.get("commitment_time_per_week_hours", "N/A")

    # Bootcamp specific details from kwargs
    chapter_number = kwargs.get("chapter_number", "")
    chapter_title = kwargs.get("chapter_title", f"Chapter {chapter_number}")
    total_chapters = config.TOTAL_BOOTCAMP_CHAPTERS
    
    quiz_question_text = kwargs.get("quiz_question_text", "Quiz question not found.")
    current_question_num = kwargs.get("current_question_num", 1)
    total_quiz_questions = kwargs.get("total_quiz_questions", config.DEFAULT_QUIZ_QUESTIONS_COUNT)

    answer_feedback = kwargs.get("answer_feedback", "Answers processing...")
    next_chapter_number = kwargs.get("next_chapter_number", "")
    total_score = kwargs.get("total_score", "N/A")

    # Store all prompts in English
    prompts_en = {
        "AWAITING_GREETING": f"Hey there! I'm {config.BOT_NAME}, your AI learning assistant. Please say 'hi' or 'hello' to get started! 👋",
        "POST_ONBOARDING_CHOICE": f"Great, {{name}}! You've completed the onboarding for the AI Bootcamp. What would you like to do now?",
        "POST_ONBOARDING_CHOICE_BUTTON_BOOTCAMP": "🚀 AI Bootcamp",
        "POST_ONBOARDING_CHOICE_BUTTON_CHAT": "💬 Just Chat",
        "POST_ONBOARDING_CHOICE_BUTTON_LEARN": "📚 Learn Something New",
        
        "AWAITING_TOPIC": f"Okay {{name}}, let's find something new for you to learn (outside the bootcamp)! What general topic are you curious about? 📚\n\n(For example: Python programming, The history of space exploration, How to bake sourdough bread)",
        "AWAITING_SPECIFIC_INTEREST": f"'{str(topic).capitalize()}' sounds interesting! 😊 Any specific areas within '{topic}' that you'd like to focus on?",
        "AWAITING_PRIOR_KNOWLEDGE": f"Good choice! To tailor this for you, what's your current experience or knowledge level with '{specific_interest or topic}'?",
        "AWAITING_GOALS": f"Awesome! What are your main goals for learning about '{specific_interest or topic}'? What do you hope to achieve?",
        "AWAITING_COMMITMENT": f"Great! Roughly how much time per week can you commit to learning '{specific_interest or topic}'? (e.g., '1 hour', '2-3 hours')",
        "AWAITING_PLAN_CONFIRMATION": (
            f"Let's confirm your general learning plan, {{name}}:\n"
            f"Topic: *{topic}*\n"
            f"Specific Interest: *{specific_interest}*\n"
            f"Prior Knowledge: *{prior_knowledge_val}*\n"
            f"Goals: *{goals_val}*\n"
            f"Commitment: *{commitment_val} per week*\n\n"
            f"Shall I create this learning plan for you?"
        ),
        "READY_TO_LEARN": f"Fantastic, {{name}}! Your general learning plan for '{specific_interest or topic}' is all set. Ready to start your first lesson? 👍",
        "LEARNING_CONTINUE_PROMPT": f"Welcome back, {{name}}! You were learning about '{topic}' (general topic). Would you like to continue?",
        
        "AWAITING_BOOTCAMP_WELCOME_AND_NAME": f"Hello! 👋 Welcome to the {config.BOT_NAME} AI Bootcamp! I'm excited to guide you. First, what's your name?",
        "AWAITING_BOOTCAMP_LANGUAGE": f"Nice to meet you, {{name}}! 😊 What language do you prefer for our bootcamp communication? (e.g., English, Malay)",
        "AWAITING_BOOTCAMP_WHY_JOIN": f"Thanks, {{name}}! In a sentence or two, what sparked your interest in this AI bootcamp?",
        
        "AWAITING_BOOTCAMP_SKILLS_HOPE": f"Great to know, {{name}}! What specific AI skills are you hoping to develop or improve? Choose an area:",
        "AWAITING_BOOTCAMP_SKILLS_BUTTON_CORE": "Core AI (Prompting, LLMs)", 
        "AWAITING_BOOTCAMP_SKILLS_BUTTON_APPLICATIONS": "AI Applications & Ethics", 
        "AWAITING_BOOTCAMP_SKILLS_BUTTON_TECHNICAL": "Technical AI (Coding, Models)",

        "AWAITING_BOOTCAMP_BIGGEST_CHALLENGE": f"That's helpful, {{name}}. When it comes to learning AI, what do you see as your biggest challenge right now?",
        "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_TIME_RESOURCES": "⏰ Time / Resources", 
        "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_COMPLEXITY": "🤯 Concept Complexity",
        "AWAITING_BOOTCAMP_CHALLENGE_BUTTON_FOCUS": "🎯 Staying Focused/Motivated",

        "AWAITING_BOOTCAMP_AI_TOOLS_PRIOR": f"Got it, {{name}}. Have you used any AI tools before? (e.g., ChatGPT for text, Midjourney for images, or AI coding libraries)", # Prompt gives examples
        "AWAITING_BOOTCAMP_TOOLS_BUTTON_COMMON_APPS": "Yes, common apps (ChatGPT, etc.)", # Specific examples implied
        "AWAITING_BOOTCAMP_TOOLS_BUTTON_CODING_LIBS": "Yes, AI coding libraries",
        "AWAITING_BOOTCAMP_TOOLS_BUTTON_NO": "🌱 No, I'm new to AI tools",

        "AWAITING_BOOTCAMP_COMPUTER_ACCESS": f"Thanks, {{name}}. For some practical parts of the bootcamp, regular access to a computer or laptop will be beneficial. Do you have this?",
        "AWAITING_BOOTCAMP_COMPUTER_ACCESS_BUTTON_YES": "✅ Yes, I do",
        "AWAITING_BOOTCAMP_COMPUTER_ACCESS_BUTTON_NO": "❌ No, not regularly",

        "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT": f"Alright, {{name}}. How comfortable are you with basic programming concepts, perhaps in Python? (No worries if you're new!)",
        "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_COMFORTABLE": "👍 Comfortable",
        "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_A_LITTLE": "🤏 A Little Familiar",
        "AWAITING_BOOTCAMP_PROGRAMMING_COMFORT_BUTTON_NEW": "🌱 Completely New",

        "AWAITING_BOOTCAMP_HEAR_ABOUT": f"Nearly done, {{name}}! We'd love to know: how did you hear about this AI Bootcamp?",
        "AWAITING_BOOTCAMP_HEAR_BUTTON_ONLINE": "📱 Online (Social Media/Website)", 
        "AWAITING_BOOTCAMP_HEAR_BUTTON_OFFLINE": "🗣️ Offline (Friend/Event)", 
        "AWAITING_BOOTCAMP_HEAR_BUTTON_WORK_SCHOOL": "🏢 Work / School Recommendation",

        "BOOTCAMP_ONBOARDING_COMPLETE": f"Fantastic, {{name}}! 🎉 You're all set for the AI Bootcamp. We're ready to dive into Chapter 1 whenever you are!",
        
        "DELIVERING_BOOTCAMP_CHAPTER_INTRO": f"Alright {{name}}, let's begin *{chapter_title}* (Chapter {chapter_number} of {total_chapters}). Here's the first part:",
        "DELIVERING_BOOTCAMP_CHAPTER_END_BUTTON_QUIZ": "📝 Ready for Quiz?",
        "AWAITING_BOOTCAMP_QUIZ_ACK": f"Ready to test your knowledge on Chapter {chapter_number} ({chapter_title}) with a short quiz?",
        "PRESENTING_BOOTCAMP_QUIZ_QUESTION": f"Chapter {chapter_number} Quiz - Question {current_question_num} of {total_quiz_questions}:\n\n{quiz_question_text}",
        "QUIZ_COMPLETED_SUBMITTED": f"Great job, {{name}}! You've completed all questions for the Chapter {chapter_number} quiz. Let's see how you did!",
        "SHOWING_BOOTCAMP_QUIZ_ANSWERS_INTRO": f"Here's the feedback for your Chapter {chapter_number} Quiz:",
        "SHOWING_BOOTCAMP_QUIZ_ANSWERS_FEEDBACK": f"{answer_feedback}", 
        "SHOWING_BOOTCAMP_QUIZ_ANSWERS_END": f"Well done on completing the quiz for Chapter {chapter_number}!",
        "ANSWER_FEEDBACK_BUTTON_NEXT_CHAPTER": "➡️ Next Chapter ({next_chapter_number})",
        "ANSWER_FEEDBACK_BUTTON_FINISH_BOOTCAMP": "🎉 Finish Bootcamp!",
        "AWAITING_NEXT_BOOTCAMP_CHAPTER_ACK": f"Chapter {chapter_number} ({chapter_title}) is complete! Ready to move on to Chapter {next_chapter_number}?",
        "BOOTCAMP_COMPLETED_FINAL": f"🎉 Incredible work, {{name}}! You've successfully completed all {total_chapters} chapters of the AI Bootcamp! Your final total score is *{total_score}*. You should be very proud! 🥳\n\nWhat would you like to do next? You can explore other topics or just chat.",
        
        "BOOTCAMP_ERROR_STATE": f"Oops, {{name}}, I seem to have hit a snag with the bootcamp material. My apologies! Let's try to get you back on track. You can say '/bootcamp' to try resuming.",
        "BOOTCAMP_ALREADY_ACTIVE": f"Hi {{name}}, you're currently making your way through the AI Bootcamp (around Chapter {kwargs.get('current_bootcamp_chapter', 'N/A')}).",
        
        "CASUAL_CHAT_GREETING": f"Hi {{name}}! How can I help you today? Feel free to chat, or type '/learn' for general learning, or '/bootcamp' for the AI Bootcamp.",
        "CASUAL_CHAT_BUTTON_BOOTCAMP": "🚀 AI Bootcamp",
        "CASUAL_CHAT_BUTTON_MAIN_MENU": "⬅️ Main Menu",

        "ERROR_MESSAGE": "I'm sorry, I encountered an unexpected hiccup. Could you please try that again? If the problem persists, typing '/help' might offer some guidance.",
        "DEFAULT_FALLBACK": "Hmm, I'm not quite sure how to respond to that. You can try rephrasing, or use one of these options: '/bootcamp', '/learn', or '/help'.",
        "HELP_MESSAGE": (
            f"Hi {{name}}! I'm {config.BOT_NAME}, your AI learning assistant. Here’s how I can help:\n\n"
            f"🚀 *AI Bootcamp*: Type `/bootcamp` to start or resume our structured AI Bootcamp.\n"
            f"📚 *General Learning*: Type `/learn` to explore other topics outside the bootcamp.\n"
            f"💬 *Casual Chat*: Just type your message to chat with me about AI or other things.\n\n"
            f"➡️ *Navigation*:\n"
            f"  - During lessons/quizzes, I'll offer buttons or you can type 'next', 'quiz', 'menu'.\n"
            f"  - Type `/help` anytime to see this message again.\n"
            f"  - Type `/mainmenu` to return to the main options."
        ),
        "MAIN_MENU_PROMPT": "What would you like to do next?", 
        "CONFIRM_PAUSE_BOOTCAMP": "You're currently in the AI Bootcamp. Would you like to pause it and switch to something else?",
        "BOOTCAMP_PAUSED": "Okay, the AI Bootcamp is paused. You can resume anytime by typing `/bootcamp`.",
    }
    
    message_template = prompts_en.get(state, prompts_en["DEFAULT_FALLBACK"])
    final_kwargs = {"name": name, "BOT_NAME": config.BOT_NAME, **kwargs}

    try:
        return message_template.format(**final_kwargs)
    except KeyError as e:
        logger.error(f"KeyError formatting prompt for state '{state}': {e}. Available kwargs: {final_kwargs.keys()}")
        fallback_template = prompts_en.get(state, "Error: Prompt for '{state}' could not be fully formatted.")
        try: 
            return fallback_template.format(name=name, BOT_NAME=config.BOT_NAME)
        except KeyError:
            return fallback_template 
    except Exception as e_fmt:
        logger.error(f"Unexpected error formatting prompt for state '{state}': {e_fmt}")
        return prompts_en.get("ERROR_MESSAGE", "An unexpected error occurred.").format(name=name, BOT_NAME=config.BOT_NAME)

