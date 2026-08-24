import urllib.parse
import aiohttp

async def analyze_image_with_vision(url: str, prompt: str) -> dict:
    """Uses OCR to extract text/stats from an image since the vision model is restricted."""
    try:
        api_url = f"https://api.ocr.space/parse/imageurl?apikey=helloworld&url={urllib.parse.quote(url)}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
                data = await resp.json()
                
        if data.get("IsErroredOnProcessing"):
            return {"error": "Failed to read image: " + str(data.get("ErrorMessage"))}
            
        text = ""
        for result in data.get("ParsedResults", []):
            text += result.get("ParsedText", "") + "\\n"
            
        if not text.strip():
            return {"error": "I couldn't detect any readable text or stats in that image. (Make sure it's a clear screenshot of text)."}
            
        return {"content": f"Here is the exact text/stats I extracted from the image:\\n{text.strip()}"}
        
    except Exception as e:
        return {"error": f"Failed to analyze image: {str(e)}"}
