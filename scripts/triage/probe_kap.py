"""Probe where KAP's 308 redirect actually goes."""
import httpx


def main() -> None:
    url = "https://www.kap.org.tr/"
    print(f"=== Probing {url} ===\n")

    # First: see the immediate redirect target
    print("--- Without following redirects ---")
    r = httpx.get(url, follow_redirects=False)
    print(f"  Status: {r.status_code}")
    print(f"  Location header: {r.headers.get('location')}")
    print(f"  All response headers:")
    for k, v in r.headers.items():
        print(f"    {k}: {v}")

    print()
    print("--- Following redirect chain ---")
    r = httpx.get(url, follow_redirects=True)
    print(f"  Final URL: {r.url}")
    print(f"  Final status: {r.status_code}")
    print(f"  Content-Type: {r.headers.get('content-type')}")
    print(f"  Content length: {len(r.text)} chars")
    print(f"  First 400 chars of body:")
    print(f"  {r.text[:400]!r}")

    print()
    print("--- Looking for an actual RSS/data endpoint ---")
    candidates = [
        "https://www.kap.org.tr/tr/api",
        "https://www.kap.org.tr/tr/RssService/Rss",
        "https://www.kap.org.tr/tr/disclosure/index",
        "https://www.kap.org.tr/en/disclosure/index",
        "https://www.kap.org.tr/tr/disclosure/feed",
    ]
    for candidate in candidates:
        try:
            r = httpx.get(candidate, follow_redirects=True, timeout=10)
            ct = r.headers.get("content-type", "")
            print(f"  {candidate}")
            print(f"    status: {r.status_code} · content-type: {ct} · size: {len(r.text)}")
            if "xml" in ct.lower() or "rss" in ct.lower():
                print(f"    *** LOOKS LIKE RSS/XML ***")
            preview = r.text[:120].replace("\n", " ")
            print(f"    preview: {preview!r}")
        except Exception as e:
            print(f"  {candidate} -> ERROR: {type(e).__name__}: {e}")
        print()


if __name__ == "__main__":
    main()
