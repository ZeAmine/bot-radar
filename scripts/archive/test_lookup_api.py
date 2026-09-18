import asyncio
import httpx
from app.providers.chasse_aux_livres import CHASSE_BROWSER_HEADERS


async def main():
    item_id = "2756035734"
    headers = dict(CHASSE_BROWSER_HEADERS)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Referer"] = f"https://www.chasse-aux-livres.fr/prix/{item_id}/les-indes-fourbes"
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
        # 1. Fetch search page or product page to get session cookies
        r0 = await client.get(f"https://www.chasse-aux-livres.fr/prix/{item_id}/les-indes-fourbes")
        print("Page status:", r0.status_code)

        # 2. Call lookup results
        url = f"https://www.chasse-aux-livres.fr/rest/lookup/results?calls=offers&itemId={item_id}&retry=0"
        r = await client.get(url)
        print("Results status:", r.status_code)
        if r.status_code == 200:
            print("Results length:", len(r.text))
            with open("/tmp/offers_response.html", "w") as f:
                f.write(r.text)
            print("Results sample:", r.text[:500])


if __name__ == "__main__":
    asyncio.run(main())
