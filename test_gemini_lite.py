import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

try:
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    models = client.models.list()
    lite_models = [m.name for m in models if "lite" in m.name.lower()]
    print("Found Lite Models:")
    for model in lite_models:
        print(f" - {model}")

    print("\nAttempting to call gemini-3.1-flash-lite-preview...")
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-preview",
        contents="Hello, world!"
    )
    print("Response:", response.text)
except Exception as e:
    print(f"Error: {e}")
