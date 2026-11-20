"""Zoek in de (dynamisch geladen) JavaScript van de webapp naar de export-tool (statische bestanden, geen API)."""

import asyncio
import json
import re

import aiohttp

HEADERS = {"User-Agent": "Mozilla/5.0"}


async def main() -> None:
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        html = await (await session.get("https://monitoring.solaredge.com/one")).text()
        start = re.findall(r'src="(https://[^"]+\.js)"', html)
        queue = list(start)
        seen: set[str] = set()
        hits = []
        while queue and len(seen) < 900:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                response = await session.get(url)
                if response.status != 200:
                    continue
                text = await response.text()
            except aiohttp.ClientError:
                continue
            base = url.rsplit("/", 1)[0]
            root = re.match(r"(https://[^/]+/cni/[^/]+/)", url)
            for ref in set(re.findall(r'["\'`]((?:\.{1,2}/|/|https://)?[\w\-./]*?[\w\-]+\.js)["\'`]', text)):
                if ref.startswith("https://"):
                    full = ref
                elif ref.startswith("/"):
                    full = "https://cni-prod-cdn.solaredge.com" + ref
                elif ref.startswith(("./", "../")):
                    full = base + "/" + ref
                elif root and ref.startswith("assets/"):
                    full = root.group(1) + ref
                else:
                    full = base + "/" + ref
                full = re.sub(r"/[^/]+/\.\./", "/", full).replace("/./", "/")
                if "solaredge" in full and full not in seen:
                    queue.append(full)
            for kw in ("export-report", "exportReport", "ExportReport", "export_report", "exportTool", "export tool"):
                for m in list(re.finditer(re.escape(kw), text))[:3]:
                    hits.append({"file": url, "kw": kw, "ctx": text[max(0, m.start() - 1500): m.end() + 1500]})
        json.dump({"scanned": len(seen), "hits": hits}, open("/config/export_hits.json", "w"), indent=1)
        print("gescand", len(seen), "treffers", len(hits))


asyncio.run(main())
