"""Session-based curl_cffi probe.

The first spike showed:
* Firefox impersonation gets the resmi_gazete HOMEPAGE
* No impersonation gets the resmi_gazete PDF directly

This probe tests whether visiting the homepage first (to establish session
cookies) then fetching the PDF with a Referer header makes the PDF fetch
work. If it does, we have the full ingester pattern for gov sources.
"""
from curl_cffi import requests


def main() -> None:
    session = requests.Session()
    impersonation = "firefox133"

    print("=== Step 1 · GET homepage to establish session ===\n")
    r1 = session.get(
        "https://www.resmigazete.gov.tr/",
        impersonate=impersonation,
        timeout=15,
    )
    print(f"  Status: {r1.status_code}")
    print(f"  Content-Type: {r1.headers.get('content-type')}")
    print(f"  Body length: {len(r1.content)} bytes")
    print(f"  Cookies set after homepage visit: {len(session.cookies)}")
    if session.cookies:
        for name, value in session.cookies.items():
            print(f"    {name} = {value[:40]}{'...' if len(value) > 40 else ''}")
    print()

    print("=== Step 2 · GET PDF in same session WITH referer ===\n")
    pdf_url = "https://www.resmigazete.gov.tr/eskiler/2026/05/20260525.pdf"
    try:
        r2 = session.get(
            pdf_url,
            impersonate=impersonation,
            headers={"Referer": "https://www.resmigazete.gov.tr/"},
            timeout=30,
        )
        print(f"  Status: {r2.status_code}")
        print(f"  Content-Type: {r2.headers.get('content-type')}")
        print(f"  Body length: {len(r2.content)} bytes")
        print(f"  Starts with %PDF: {r2.content.startswith(b'%PDF')}")
        if r2.content.startswith(b'%PDF'):
            print(f"  PDF version: {r2.content[:8].decode('latin-1', errors='replace').strip()}")
            print()
            print("  *** SUCCESS · PDF fetched cleanly via session pattern ***")
            print()
            print("  Saving to scripts/triage/captured_gazette.pdf for inspection...")
            with open("scripts/triage/captured_gazette.pdf", "wb") as f:
                f.write(r2.content)
            print(f"  Saved {len(r2.content)} bytes")
        else:
            preview = r2.content[:300].decode("utf-8", errors="replace")
            preview = preview.replace("\n", "\\n")
            print(f"  *** Not a PDF · preview: {preview}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()

    print("=== Step 3 · GET PDF in same session WITHOUT referer ===\n")
    try:
        r3 = session.get(
            pdf_url,
            impersonate=impersonation,
            timeout=30,
        )
        print(f"  Status: {r3.status_code}")
        print(f"  Content-Type: {r3.headers.get('content-type')}")
        print(f"  Body length: {len(r3.content)} bytes")
        print(f"  Starts with %PDF: {r3.content.startswith(b'%PDF')}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()

    print("=== Conclusion ===")
    print("If Step 2 succeeded but Step 3 failed · Referer header is required.")
    print("If both Step 2 and Step 3 succeeded · session cookies alone suffice.")
    print("If neither succeeded · the PDF endpoint needs more (Playwright?).")


if __name__ == "__main__":
    main()
