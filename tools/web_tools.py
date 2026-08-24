"""
Web search and page-fetching tools.

Provides:
  - web_search(query)  — DuckDuckGo by default, optional Brave/SerpAPI
  - read_webpage(url)  — fetches and extracts text from any URL (Trello-aware)
"""

import asyncio
import re
import aiohttp
import logging

import config

logger = logging.getLogger("discord")

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

WEB_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information, recent events, documentation, "
                "or anything else you need to look up. Returns a list of results with "
                "titles, URLs, and snippets. Use when asked about current events, "
                "latest updates, or anything you don't know from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": (
                "Fetch and read the content of a specific webpage or URL. "
                "Use when the user provides a link, or when you want to read a "
                "specific page found via web_search. Handles Trello boards specially."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch (e.g. https://example.com/page)",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

WEB_TOOL_NAMES: frozenset[str] = frozenset(s["function"]["name"] for s in WEB_SCHEMAS)

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def execute_web_tool(name: str, args: dict) -> dict:
    if name == "web_search":
        return await web_search(
            args.get("query", ""),
            max_results=min(int(args.get("max_results", 5)), 10),
        )
    elif name == "read_webpage":
        return await read_webpage(args.get("url", ""))
    return {"error": f"Unknown web tool: {name}"}


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


async def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web. Uses DuckDuckGo by default."""
    if not query.strip():
        return {"error": "Empty query"}

    engine = config.SEARCH_ENGINE

    if engine == "brave" and config.WEB_SEARCH_API_KEY:
        return await _brave_search(query, max_results)
    elif engine == "serpapi" and config.WEB_SEARCH_API_KEY:
        return await _serpapi_search(query, max_results)
    else:
        return await _ddg_search(query, max_results)


async def _ddg_search(query: str, max_results: int) -> dict:
    """DuckDuckGo search — no API key required."""
    try:
        loop = asyncio.get_event_loop()

        def _run():
            try:
                from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=max_results))
            except ImportError:
                return None

        results = await loop.run_in_executor(None, _run)

        if results is None:
            # Fallback: DuckDuckGo HTML scrape without library
            return await _ddg_html_search(query, max_results)

        formatted = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in results
        ]
        return {"query": query, "results": formatted}

    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return {"error": f"Search failed: {e}", "query": query}


async def _ddg_html_search(query: str, max_results: int) -> dict:
    """Fallback: scrape DuckDuckGo HTML search results."""
    try:
        encoded = query.replace(" ", "+")
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"error": f"Search returned HTTP {resp.status}", "query": query}
                html = await resp.text()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for result in soup.select(".result__body")[:max_results]:
            title_el = result.select_one(".result__title")
            url_el = result.select_one(".result__url")
            snippet_el = result.select_one(".result__snippet")
            if title_el:
                results.append({
                    "title": title_el.get_text(strip=True),
                    "url": url_el.get_text(strip=True) if url_el else "",
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })

        return {"query": query, "results": results}
    except Exception as e:
        return {"error": f"Search failed: {e}", "query": query}


async def _brave_search(query: str, max_results: int) -> dict:
    try:
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": config.WEB_SEARCH_API_KEY,
        }
        params = {"q": query, "count": max_results}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return {"query": query, "results": results}
    except Exception as e:
        return {"error": f"Brave search failed: {e}"}


async def _serpapi_search(query: str, max_results: int) -> dict:
    try:
        url = "https://serpapi.com/search"
        params = {"q": query, "api_key": config.WEB_SEARCH_API_KEY, "num": max_results}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

        results = []
        for item in data.get("organic_results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return {"query": query, "results": results}
    except Exception as e:
        return {"error": f"SerpAPI search failed: {e}"}


# ---------------------------------------------------------------------------
# read_webpage
# ---------------------------------------------------------------------------


async def read_webpage(url: str) -> dict:
    """Fetch a URL and return its text content."""
    # Sanitize URL
    url = url.strip("<> \n\t\"'")
    match = re.search(r'(https?://[^\s<>"]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s<>"]*)?)', url)
    if match:
        url = match.group(1)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        # Trello boards: append .json for structured data
        if "trello.com/b/" in url and not url.endswith(".json"):
            base_url = url.split("?")[0].rstrip("/")
            url = f"{base_url}.json"

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    return {"error": f"HTTP {response.status} from {url}"}

                content_type = response.headers.get("Content-Type", "")

                if "application/json" in content_type or url.endswith(".json"):
                    data = await response.json()
                    if "trello.com" in url:
                        return _parse_trello_json(data)
                    text_data = str(data)
                    if len(text_data) > 15000:
                        text_data = text_data[:15000] + "... (truncated)"
                    return {"content": text_data, "url": url}
                else:
                    from bs4 import BeautifulSoup
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                        tag.extract()
                    text = soup.get_text(separator="\n")
                    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
                    if len(text) > 15000:
                        text = text[:15000] + "... (truncated)"
                    return {"content": text, "url": url}

    except Exception as e:
        return {"error": f"Failed to fetch {url}: {e}"}


def _parse_trello_json(data: dict) -> dict:
    board_name = data.get("name", "Unknown Board")
    lists = {lst["id"]: lst["name"] for lst in data.get("lists", []) if not lst.get("closed")}
    parsed_lists: dict[str, list] = {name: [] for name in lists.values()}

    for card in data.get("cards", []):
        if card.get("closed"):
            continue
        list_id = card.get("idList")
        if list_id in lists:
            list_name = lists[list_id]
            desc = card.get("desc", "").strip()
            if desc:
                parsed_lists[list_name].append(f"- {card['name']}: {desc}")
            else:
                parsed_lists[list_name].append(f"- {card['name']}")

    output = f"Trello Board: {board_name}\n\n"
    for list_name, cards in parsed_lists.items():
        output += f"### {list_name}\n"
        output += ("\n".join(cards) or "(Empty)") + "\n\n"

    return {"content": output[:15000]}
