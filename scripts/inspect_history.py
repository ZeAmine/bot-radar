import asyncio
import re
import httpx
from app.providers.chasse_aux_livres import CHASSE_BROWSER_HEADERS


async def main():
    url = "https://www.chasse-aux-livres.fr/prix/2756035734/les-indes-fourbes-juanjo-guarnido-9782756035734"
    async with httpx.AsyncClient(
        headers=CHASSE_BROWSER_HEADERS, follow_redirects=True, timeout=15.0
    ) as client:
        r = await client.get(url)
        text = r.text
        # Search for "Amazon" or "hors Amazon"
        for m in re.finditer(
            r".{0,80}(?:hors amazon|suivi\/graphique|très bon état \+).{0,80}", text, re.I
        ):
            print("Match:", m.group(0))

        # Search for all script tags or iframes
        for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', text):
            print("Iframe:", m.group(1))

        # Look for words containing 'chart'
        for m in re.finditer(r'https?://[^\s"\'<>]*chart[^\s"\'<>]*', text):
            print("Chart URL:", m.group(0))


if __name__ == "__main__":
    asyncio.run(main())
