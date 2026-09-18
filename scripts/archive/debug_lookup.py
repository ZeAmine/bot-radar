import asyncio
import httpx
import re
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider, CHASSE_BROWSER_HEADERS

async def debug_one(isbn, title):
    p = ChasseAuxLivresProvider()
    async with httpx.AsyncClient(headers=CHASSE_BROWSER_HEADERS, follow_redirects=True) as client:
        search_url = f'https://www.chasse-aux-livres.fr/search?query={isbn}&catalog=fr'
        resp1 = await client.get(search_url)
        data_hash = re.search(r'data-hash=["\']([a-zA-Z0-9_-]+)["\']', resp1.text).group(1)
        rest_url = f'https://www.chasse-aux-livres.fr/rest/search-results?h={data_hash}&p=1&l=1&b=d'
        resp2 = await client.get(rest_url, headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'Referer': search_url})
        d_html = resp2.json().get('d', '')
        
        url_match = re.search(r'href=["\'](/prix/[^"\']+)["\']', d_html)
        print('URL match:', url_match.group(1) if url_match else None)
        
        title_match = re.search(r'class=["\']title["\'][^>]*>\s*<a[^>]*>([^<]+)</a>', d_html, re.DOTALL)
        found_title = title_match.group(1).strip() if title_match else None
        print('Found title:', found_title)
        coherent = p.is_title_coherent(title, found_title)
        print('Coherent with', repr(title), ':', coherent)
        
        prod_url = f'https://www.chasse-aux-livres.fr{url_match.group(1)}'
        resp_prod = await client.get(prod_url)
        asin = re.search(r'data-asin="([^"]+)"', resp_prod.text).group(1)
        lvs = re.search(r'data-lvs="([^"]+)"', resp_prod.text).group(1)
        nbeng = re.search(r'data-nbeng="(\d+)"', resp_prod.text).group(1)
        print(f'asin={asin}, lvs={lvs}, nbeng={nbeng}')
        
        lookup_url = f'https://www.chasse-aux-livres.fr/rest/lookup/results?calls=offers&itemId={asin}&retry=0&duih=&lvs={lvs}&ui=&engines=0_1_2_3_4_5_6_7_8_9_10_11_12_13_14_15_16_17_18_19_20_21_22_23_24_25_26_27_28_29_30_31&f=false'
        resp_lookup = await client.get(lookup_url, headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'Referer': prod_url})
        raw_offers = resp_lookup.json().get('offers', {})
        print('Number of offer engines with results (retry 0):', len(raw_offers))
        for eng, offs in raw_offers.items():
            for o in offs:
                is_used = o.get('condition', {}).get('_name') != 'NEW'
                print(f'  Engine {eng}: is_used={is_used}, cond={o.get("condition", {}).get("_name")}, sub={o.get("subCondition", {}).get("name")}, price={o.get("totalPrice", {}).get("amount")}')

        retry_url = f'https://www.chasse-aux-livres.fr/rest/lookup/results?calls=offers&itemId={asin}&retry=1&duih=&lvs={lvs}&ui=&engines=0_1_2_3_4_5_6_7_8_9_10_11_12_13_14_15_16_17_18_19_20_21_22_23_24_25_26_27_28_29_30_31&f=false'
        resp_retry = await client.get(retry_url, headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'Referer': prod_url})
        raw_offers_retry = resp_retry.json().get('offers', {})
        print('Number of offer engines with results (retry 1):', len(raw_offers_retry))
        for eng, offs in raw_offers_retry.items():
            for o in offs:
                is_used = o.get('condition', {}).get('_name') != 'NEW'
                print(f'  RETRY Engine {eng}: is_used={is_used}, cond={o.get("condition", {}).get("_name")}, sub={o.get("subCondition", {}).get("name")}, price={o.get("totalPrice", {}).get("amount")}')

if __name__ == '__main__':
    asyncio.run(debug_one('9782344058138', '1629 — Les naufragés du Jakarta — Tome 2 (Les Martyrs de la baie des Hollandais)'))
