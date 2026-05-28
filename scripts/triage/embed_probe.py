import asyncio, httpx

async def main():
    url = "http://localhost:11434/api/embed"
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(60):
            # identical short benign input every time · no content variation
            r = await client.post(url, json={"model": "bge-m3", "input": ["test cumlesi numara %d" % i]})
            print(i, r.status_code, "OK" if r.status_code == 200 else r.text[:120])
            if r.status_code != 200:
                print(">>> first failure at call", i)
                break

asyncio.run(main())
