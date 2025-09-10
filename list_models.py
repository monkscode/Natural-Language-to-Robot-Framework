import os
from dotenv import load_dotenv

# Load environment variables from src/backend/.env
# This is to ensure the script can find the .env file
dotenv_path = os.path.join(os.path.dirname(__file__), 'src', 'backend', '.env')
load_dotenv(dotenv_path=dotenv_path)

# The google-generativeai library automatically picks up the GOOGLE_API_KEY from the environment
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found in src/backend/.env file")

print("Environment variables loaded successfully.")
print(f"GEMINI_API_KEY: {api_key[:4]}...{api_key[-4:]}")
