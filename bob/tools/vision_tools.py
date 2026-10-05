"""Vision / image analysis tool — OCR-based extraction."""
import urllib.parse
import aiohttp

VISION_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": (
                "Analyze an image from a URL and extract text, stats, or describe its content. "
                "Use this whenever the user attaches an image or provides an image URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the image to analyze.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What you want to know about the image "
                                       "(e.g. 'Extract all stats', 'What does this show?')",
                    },
                },
                "required": ["url", "prompt"],
            },
        },
    },
]

VISION_TOOL_NAMES: frozenset[str] = frozenset(s["function"]["name"] for s in VISION_SCHEMAS)


async def execute_vision_tool(name: str, args: dict) -> dict:
    if name == "analyze_image":
        return await analyze_image(args.get("url", ""), args.get("prompt", "Describe this image."))
    return {"error": f"Unknown vision tool: {name}"}


async def analyze_image(url: str, prompt: str) -> dict:
    """Uses OCR Space API to extract text from an image."""
    try:
        encoded_url = urllib.parse.quote(url, safe="/:?=&")
        api_url = f"https://api.ocr.space/parse/imageurl?apikey=helloworld&url={encoded_url}"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        if data.get("IsErroredOnProcessing"):
            return {"error": "Failed to read image: " + str(data.get("ErrorMessage", "Unknown error"))}

        text = ""
        for result in data.get("ParsedResults", []):
            text += result.get("ParsedText", "") + "\n"

        if not text.strip():
            return {"error": "No readable text detected in this image. Make sure it's a clear screenshot."}

        return {"content": f"Extracted text/stats from image:\n{text.strip()}"}

    except Exception as e:
        return {"error": f"Failed to analyze image: {e}"}
