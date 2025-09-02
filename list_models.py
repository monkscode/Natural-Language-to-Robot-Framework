import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load environment variables from backend/.env
# This is to ensure the script can find the .env file
dotenv_path = os.path.join(os.path.dirname(__file__), 'backend', '.env')
load_dotenv(dotenv_path=dotenv_path)

# The google-generativeai library automatically picks up the GOOGLE_API_KEY from the environment
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    raise ValueError("GOOGLE_API_KEY not found in backend/.env file")

genai.configure(api_key=api_key)

print("Available models that support 'generateContent':")
for m in genai.list_models():
  if 'generateContent' in m.supported_generation_methods:
    print(m.name)
