import aiohttp
from bs4 import BeautifulSoup
import re

async def read_webpage(url: str) -> dict:
    """Fetch a URL and return its text content. Includes special handling for Trello."""
    url = url.strip("<> \n\t\"'")
    
    # Aggressively extract just the URL in case the AI passes extra text
    match = re.search(r'(https?://[^\s<>"]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s<>"]*)?)', url)
    if match:
        url = match.group(1)
        
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        # Trello trick: public Trello boards return full data if you append .json
        if "trello.com/b/" in url and not url.endswith(".json"):
            # Strip trailing slash if present, then add .json
            base_url = url.split("?")[0].rstrip("/")
            url = f"{base_url}.json"

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    return {"error": f"Failed to fetch {url}. Status code: {response.status}"}
                
                content_type = response.headers.get("Content-Type", "")
                
                if "application/json" in content_type or url.endswith(".json"):
                    data = await response.json()
                    # If it's a Trello board, we want to summarize it instead of returning 1MB of JSON
                    if "trello.com" in url:
                        return _parse_trello_json(data)
                    
                    # Convert json to string, truncate if too long
                    text_data = str(data)
                    if len(text_data) > 15000:
                        text_data = text_data[:15000] + "... (truncated)"
                    return {"content": text_data}
                else:
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Remove scripts and styles
                    for script in soup(["script", "style"]):
                        script.extract()
                    
                    text = soup.get_text(separator="\n")
                    # Clean up multiple newlines
                    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
                    
                    if len(text) > 15000:
                        text = text[:15000] + "... (truncated)"
                        
                    return {"content": text}

    except Exception as e:
        return {"error": f"Failed to fetch {url}: {str(e)}"}

def _parse_trello_json(data: dict) -> dict:
    """Extract lists and cards from a Trello JSON export so it fits in the AI's context."""
    board_name = data.get("name", "Unknown Board")
    
    lists = {lst["id"]: lst["name"] for lst in data.get("lists", []) if not lst.get("closed")}
    
    parsed_lists = {name: [] for name in lists.values()}
    
    for card in data.get("cards", []):
        if card.get("closed"): continue
        list_id = card.get("idList")
        if list_id in lists:
            list_name = lists[list_id]
            desc = card.get("desc", "").strip()
            if desc:
                parsed_lists[list_name].append(f"- {card['name']}: {desc}")
            else:
                parsed_lists[list_name].append(f"- {card['name']}")
                
    # Flatten into a readable string
    output = f"Trello Board: {board_name}\n\n"
    for list_name, cards in parsed_lists.items():
        output += f"### {list_name}\n"
        if not cards:
            output += "(Empty)\n"
        else:
            output += "\n".join(cards) + "\n"
        output += "\n"
        
    return {"content": output[:15000]}
