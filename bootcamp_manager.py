import os
import json
import re
from typing import Dict, Any, Optional, Tuple, List

import config # To get TOTAL_BOOTCAMP_CHAPTERS

# Define the base path to your lesson content relative to this file's directory
LESSON_BASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootcamp_lessons")
TOTAL_CHAPTERS = config.TOTAL_BOOTCAMP_CHAPTERS

def load_content(file_name: str) -> Optional[str]:
    """Loads content from a file in the bootcamp_lessons directory."""
    try:
        if ".." in file_name or file_name.startswith("/"):
            print(f"Warning: Invalid file name format for load_content: {file_name}")
            return None
        file_path = os.path.join(LESSON_BASE_PATH, file_name)
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: Content file not found - {file_path}")
        return None
    except Exception as e:
        print(f"Error loading content from {file_path}: {e}")
        return None

def parse_quiz_file(quiz_content: str) -> List[Dict[str, Any]]:
    """
    Parses quiz content string into a list of question objects.
    Each question object: {"text": "question text", "options": {"a": "option a", ...}, "id": "q1"}
    Assumes format:
    Question X:
    Full question text here
    a) Option A text
    b) Option B text
    ...
    """
    questions = []
    # Regex to find "Question X:" followed by text, then options
    # This regex is complex and might need refinement based on exact quiz format
    question_blocks = re.split(r'\n*(?=Question \d+:)', quiz_content.strip())

    for i, block in enumerate(question_blocks):
        if not block.strip():
            continue
        
        lines = block.strip().split('\n')
        if not lines:
            continue

        # First line is "Question X: ..." or just the question text if split removed "Question X:" prefix for first block
        q_match = re.match(r'(Question \d+:)?(.*)', lines[0].strip(), re.IGNORECASE)
        question_text_full = q_match.group(2).strip() if q_match else lines[0].strip()
        
        # Subsequent lines for question text if it spans multiple lines before options
        option_start_index = 1
        for line_idx in range(1, len(lines)):
            if re.match(r'^[a-zA-Z]\s?\)', lines[line_idx].strip()): # Detects "a)" or "a )"
                break
            question_text_full += "\n" + lines[line_idx].strip()
            option_start_index += 1
        
        options = {}
        for line_idx in range(option_start_index, len(lines)):
            line = lines[line_idx].strip()
            # Match "a) Option text" or "a. Option text"
            option_match = re.match(r'^([a-zA-Z])\s?[\)\.]\s?(.*)', line)
            if option_match:
                key = option_match.group(1).lower()
                value = option_match.group(2).strip()
                options[key] = value
        
        if question_text_full and options:
            questions.append({
                "id": f"q{len(questions) + 1}", # q1, q2, ...
                "text": question_text_full.strip(),
                "options": options
            })
        elif question_text_full and not options and i > 0: # Likely part of previous question's text if no options found
             if questions:
                 questions[-1]["text"] += "\n" + question_text_full # Append to previous question
             else: # Should not happen if parsing Question X: correctly
                 print(f"Warning: Orphaned question text found in quiz: {question_text_full}")


    return questions


async def get_bootcamp_step(user_bootcamp_progress: Dict[str, Any]) -> Dict[str, Any]:
    """
    Determines the next step for the user in the bootcamp.
    Handles multi-step quizzes.
    """
    current_chapter = user_bootcamp_progress.get("current_bootcamp_chapter", 1)
    last_step_completed = str(user_bootcamp_progress.get("last_bootcamp_step_completed", "none")).strip()
    current_quiz_session = user_bootcamp_progress.get("current_quiz_session", {}) # {"chapter_number": X, "current_question_index": Y, "answers": {"q1":"a"}}

    if not isinstance(current_chapter, int) or current_chapter < 1: current_chapter = 1
    if not isinstance(current_quiz_session, dict): current_quiz_session = {}


    if current_chapter > TOTAL_CHAPTERS:
        return {"type": "completed", "data": "You have completed all chapters of the AI Bootcamp!", "next_mode": "BOOTCAMP_COMPLETED_FINAL"}

    # --- Quiz In Progress ---
    if current_quiz_session.get("chapter_number") == current_chapter and \
       last_step_completed == f"quiz_interactive_started_ch{current_chapter}":
        
        quiz_file_content = load_content(f"Quiz {current_chapter}.txt")
        if not quiz_file_content:
            return {"type": "error", "data": f"Sorry, I couldn't load Quiz {current_chapter}.", "next_mode": "BOOTCAMP_ERROR_STATE"}
        
        parsed_questions = parse_quiz_file(quiz_file_content)
        if not parsed_questions:
            return {"type": "error", "data": f"Sorry, I couldn't parse Quiz {current_chapter}.", "next_mode": "BOOTCAMP_ERROR_STATE"}

        current_question_index = current_quiz_session.get("current_question_index", 0)
        
        if 0 <= current_question_index < len(parsed_questions):
            question_data = parsed_questions[current_question_index]
            return {
                "type": "quiz_question", # New type for individual questions
                "chapter_number": current_chapter,
                "question_data": question_data, # {"id": "qX", "text": "...", "options": {"a":"..."}}
                "current_question_num": current_question_index + 1,
                "total_quiz_questions": len(parsed_questions),
                "next_mode": f"AWAITING_BOOTCAMP_QUIZ_ANSWER_CH{current_chapter}_Q{current_question_index + 1}",
                # No update_last_step_to here, that's handled when answer is received
            }
        else: # All questions for this quiz are done
            return { # This signals to main.py to finalize quiz submission
                "type": "quiz_all_questions_answered",
                "chapter_number": current_chapter,
                "collected_answers": current_quiz_session.get("answers", {}),
                "next_mode": f"SHOWING_BOOTCAMP_QUIZ_ANSWERS_CH{current_chapter}", # Will be set after processing
                "update_last_step_to": f"quiz_submitted_ch{current_chapter}"
            }

    # --- Standard Step Progression ---
    if last_step_completed == "onboarding_complete" or \
       last_step_completed == f"answer_{current_chapter - 1}" or \
       (last_step_completed == "none" and current_chapter == 1):
        chapter_title_from_file = f"Chapter {current_chapter} Title Placeholder" # TODO: Extract title from file if possible
        # For chunking, we send an intro, main.py will handle sending actual content parts
        return {
            "type": "chapter_intro", # Signals start of chapter delivery
            "chapter_number": current_chapter,
            "chapter_title": chapter_title_from_file, 
            "data": load_content(f"Chapter {current_chapter}.txt"), # Full content for main.py to chunk
            "next_mode": f"DELIVERING_BOOTCAMP_CHAPTER_CONTENT_CH{current_chapter}", # Intermediate state for chunking
            "update_last_step_to": f"chapter_intro_delivered_ch{current_chapter}"
        }

    elif last_step_completed == f"chapter_content_ended_ch{current_chapter}" or \
         last_step_completed == f"quiz_ack_ch{current_chapter}": # User acknowledged readiness for quiz
        # This means it's time to START the interactive quiz session
        return {
            "type": "quiz_start_interactive", # Signals to initialize quiz session
            "chapter_number": current_chapter,
            "next_mode": f"AWAITING_BOOTCAMP_QUIZ_ANSWER_CH{current_chapter}_Q1", # Will go to first question
            "update_last_step_to": f"quiz_interactive_started_ch{current_chapter}"
        }
        
    elif last_step_completed == f"quiz_submitted_ch{current_chapter}":
        answer_content_full = load_content(f"Answer {current_chapter}.txt")
        quiz_scores = user_bootcamp_progress.get("bootcamp_quiz_scores", {})
        if isinstance(quiz_scores, str):
            try: quiz_scores = json.loads(quiz_scores)
            except: quiz_scores = {}
        if not isinstance(quiz_scores, dict): quiz_scores = {}
        user_score_for_quiz = quiz_scores.get(f"quiz{current_chapter}", "N/A")
        
        # Include detailed feedback if possible (e.g., which questions were right/wrong)
        # For now, just the score and the answer key.
        feedback_text = f"Your score for this quiz: *{user_score_for_quiz}%*\n\nHere are the correct answers and explanations:\n\n{answer_content_full or 'Answers not available.'}"
        
        next_mode_after_answer = f"AWAITING_NEXT_BOOTCAMP_CHAPTER_ACK_CH{current_chapter}" \
            if current_chapter < TOTAL_CHAPTERS else "BOOTCAMP_COMPLETED_FINAL"
        
        return {
            "type": "answer_feedback", # New type for clarity
            "chapter_number": current_chapter,
            "data": feedback_text,
            "next_mode": next_mode_after_answer,
            "update_last_step_to": f"answer_{current_chapter}"
        }

    print(f"get_bootcamp_step: No specific next content step for current_chapter={current_chapter}, last_step_completed='{last_step_completed}'. Main handler will use current_mode.")
    return {"type": "wait_for_user_action", "data": "Waiting for user action based on current mode."}


async def process_quiz_submission(chapter_number: int, collected_answers: Dict[str, str]) -> Tuple[int, str]:
    """
    Processes collected quiz answers, compares with correct answers, and calculates score.
    Returns (score_percentage, detailed_feedback_string).
    collected_answers: Dict from current_quiz_session, e.g., {"q1": "a", "q2": "c"}
    """
    correct_answers_text = load_content(f"Answer {chapter_number}.txt")
    quiz_questions_text = load_content(f"Quiz {chapter_number}.txt") # For question text in feedback

    if not correct_answers_text or not quiz_questions_text:
        return 0, "Sorry, I couldn't load the necessary quiz files to mark your answers."

    parsed_quiz_questions = parse_quiz_file(quiz_questions_text) # List of {"id", "text", "options"}
    
    # Parse correct answers from AnswerX.txt (same logic as before)
    parsed_correct_answers = {} # e.g. {"q1": "b", "q2": "d"}
    lines = correct_answers_text.strip().split('\n')
    question_count_from_key = 0
    for line in lines:
        line_strip = line.strip()
        if not line_strip: continue
        try:
            q_num_part = line_strip.split('.')[0].strip()
            ans_char = None
            if ") " in line_strip: ans_char = line_strip.split(") ")[0].split(":")[-1].split(" ")[-1].strip().lower()
            elif ")" in line_strip: ans_char = line_strip.split(")")[-2].split(" ")[-1].strip().lower()
            if not ans_char and len(line_strip.split('.')) > 1:
                potential_ans = line_strip.split('.')[1].strip()
                if len(potential_ans) > 0 and potential_ans[0].isalpha(): ans_char = potential_ans[0].lower()

            if q_num_str := q_num_part.replace("Question ", ""):
                if q_num_str.isdigit() and ans_char and len(ans_char) == 1 and ans_char.isalpha():
                    parsed_correct_answers[f"q{q_num_str}"] = ans_char
                    question_count_from_key += 1
        except Exception: pass # Skip malformed lines silently for now

    if question_count_from_key == 0:
        return 0, "Error processing the answer key for this quiz. Please contact support."

    correct_user_answers_count = 0
    detailed_feedback_lines = []

    # Iterate through questions based on the parsed quiz file for consistent order and text
    for question_obj in parsed_quiz_questions:
        q_id = question_obj["id"] # e.g., "q1"
        user_ans = collected_answers.get(q_id)
        correct_ans = parsed_correct_answers.get(q_id)
        
        q_text_short = (question_obj['text'][:75] + '...') if len(question_obj['text']) > 75 else question_obj['text']
        feedback_line = f"\n*Q: {q_text_short}*"
        
        if user_ans and correct_ans:
            user_ans_text = question_obj["options"].get(user_ans, f"Your answer: {user_ans}")
            correct_ans_text = question_obj["options"].get(correct_ans, f"Correct: {correct_ans}")
            if user_ans == correct_ans:
                correct_user_answers_count += 1
                feedback_line += f"\n  ✅ Correct! You chose: {user_ans_text}"
            else:
                feedback_line += f"\n  ❌ Your answer: {user_ans_text}"
                feedback_line += f"\n  💡 Correct answer: {correct_ans_text}"
        elif correct_ans: # User didn't answer or answer not in options
            correct_ans_text = question_obj["options"].get(correct_ans, f"Correct: {correct_ans}")
            feedback_line += f"\n  ⚪ You didn't answer this one."
            feedback_line += f"\n  💡 Correct answer: {correct_ans_text}"
        else: # Should not happen if quiz and answers are synced
            feedback_line += f"\n  ⚠️ Could not determine correct answer for this question."
        detailed_feedback_lines.append(feedback_line)

    score_percentage = int((correct_user_answers_count / len(parsed_quiz_questions)) * 100) if parsed_quiz_questions else 0
    
    summary_feedback = f"You answered {correct_user_answers_count} out of {len(parsed_quiz_questions)} questions correctly."
    full_feedback = summary_feedback + "\n" + "\n".join(detailed_feedback_lines)
    
    return score_percentage, full_feedback
