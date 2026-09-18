import asyncio
import re
import httpx

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def test():
    async with httpx.AsyncClient(headers=headers, timeout=10.0, follow_redirects=True) as client:
        r = await client.get("https://www.vinted.fr/items/9952419039")
        text = r.text
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+\.js)["\']', text)
        print("Scripts count:", len(scripts))
        for s in scripts:
            if (
                "item" in s.lower()
                or "app" in s.lower()
                or "chunk" in s.lower()
                or "page" in s.lower()
            ):
                try:
                    js_resp = await client.get(s)
                    if "item-buy-button" in js_resp.text:
                        print("FOUND item-buy-button in:", s)
                        pos = js_resp.text.find("item-buy-button")
                        print("Snippet around item-buy-button:")
                        print(js_resp.text[max(0, pos - 300) : min(len(js_resp.text), pos + 600)])
                except Exception as e:
                    print("Err:", e)


if __name__ == "__main__":
    asyncio.run(test())
