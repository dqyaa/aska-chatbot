# ASKA Project - AI Learning Chatbot

ASKA is a WhatsApp-based AI learning assistant designed to deliver an AI Bootcamp, facilitate general learning, and engage in casual conversation.

## Table of Contents

1.  [Prerequisites](#prerequisites)
2.  [Project Structure](#project-structure)
3.  [Setup Instructions](#setup-instructions)
    * [1. Clone/Download Files](#1-clonedownload-files)
    * [2. Create a Virtual Environment](#2-create-a-virtual-environment)
    * [3. Install Dependencies](#3-install-dependencies)
    * [4. Configure Environment Variables (.env file)](#4-configure-environment-variables-env-file)
    * [5. Set up Supabase](#5-set-up-supabase)
    * [6. Set up Google Cloud & Service Account](#6-set-up-google-cloud--service-account)
    * [7. Set up Meta (Facebook) Developer App for WhatsApp](#7-set-up-meta-facebook-developer-app-for-whatsapp)
    * [8. Prepare Bootcamp Lesson Content](#8-prepare-bootcamp-lesson-content)
4.  [Running the Application](#running-the-application)
    * [Running the FastAPI Chatbot Application](#running-the-fastapi-chatbot-application)
    * [Running the Google Sheet to Supabase Sync Service (Optional)](#running-the-google-sheet-to-supabase-sync-service-optional)
    * [Using Ngrok for Local Development](#using-ngrok-for-local-development)
5.  [Key Scripts Overview](#key-scripts-overview)
6.  [Important Considerations](#important-considerations)

## Prerequisites

* Python 3.8+
* `pip` (Python package installer)
* Access to a Supabase project.
* Access to a Google Cloud Platform project.
* A Meta (Facebook) Developer account and an App configured for the WhatsApp Business API.
* `ngrok` (or a similar tunneling service) for local development if you want to test WhatsApp webhooks.

## Project Structure

Ensure your project files are organized as follows (this README assumes all Python scripts are in the same main directory, e.g., `ASKA/`):
ASKA/
├── bootcamp_lessons/         # Directory for bootcamp chapter and quiz files
│   ├── Chapter 1.txt
│   ├── Quiz 1.txt
│   ├── Answer 1.txt
│   └── ... (other chapters and quizzes)
├── main.py                   # FastAPI application, WhatsApp message handling
├── database.py               # Supabase database interactions
├── bootcamp_manager.py       # Logic for bootcamp progression, content loading
├── llm_integrations.py       # DeepSeek LLM API interactions
├── state_prompts.py          # Manages response templates for different states
├── config.py                 # Loads environment variables and configurations
├── google_sheet_to_supabase.py # Syncs data from Google Sheets to Supabase
├── requirements.txt          # Python package dependencies
├── .env                      # Environment variables (create this file)
└── README.md                 # This file
## Setup Instructions

### 1. Clone/Download Files

Ensure all the project files (`main.py`, `config.py`, `database.py`, etc.) are in your chosen project directory.

### 2. Create a Virtual Environment

It's highly recommended to use a virtual environment to manage project dependencies.

```bash
python -m venv venv
