import os
import asyncio
from openai import AsyncOpenAI

async def analyze_image_with_vision(url: str, prompt: str) -> dict:
    """Uses Groq's vision model to analyze an image URL."""
    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        return {"error": "GROQ_API_KEY is missing."}
        
    try:
        # Spin up a localized client just for the vision request
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key,
        )
        
        response = await client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}}
                    ]
                }
            ],
            max_tokens=500,
            temperature=0.2
        )
        
        return {"content": response.choices[0].message.content}
        
    except Exception as e:
        return {"error": f"Failed to analyze image: {str(e)}"}
