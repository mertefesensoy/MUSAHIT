"""Final session + referer + adequate timeout probe.

Step 2 from the previous probe nearly succeeded · 11MB of 19MB received
before the 30-second timeout. With a generous timeout this should land
the full PDF.
"""
import time
from curl_cffi import requests


def main() -> None:
    session = requests.Session()
    impersonation = "firefox133"

    print("=== Step 1 · establish session via homepage ===")
    t0 = time.time()
    r1 = session.get(
        "https://www.resmigazete.gov.tr/",
        impersonate=impersonation,
        timeout=30,
    )
    t1 = time.time()
    print(f"  Homepage: {r1.status_code} · {len(r1.content)} bytes · {t1-t0:.1f}s")
    print(f"  Cookies: {[c for c in session.cookies]}")
    print()

    print("=== Step 2 · fetch PDF with referer · 180s timeout ===")
    pdf_url = "https://www.resmigazete.gov.tr/eskiler/2026/05/20260525.pdf"
    t0 = time.time()
    try:
        r2 = session.get(
            pdf_url,
            impersonate=impersonation,
            headers={"Referer": "https://www.resmigazete.gov.tr/"},
            timeout=180,  # generous · the file is ~19MB
        )
        t1 = time.time()
        print(f"  Status: {r2.status_code}")
        print(f"  Content-Type: {r2.headers.get('content-type')}")
        print(f"  Body length: {len(r2.content)} bytes ({len(r2.content)/1024/1024:.1f} MB)")
        print(f"  Time: {t1-t0:.1f}s")
        print(f"  Effective rate: {len(r2.content)/(t1-t0)/1024:.0f} KB/s")
        print(f"  Starts with %PDF: {r2.content.startswith(b'%PDF')}")

        if r2.content.startswith(b'%PDF'):
            print()
            print("  *** SUCCESS · Full PDF fetched ***")
            out_path = "scripts/triage/captured_gazette.pdf"
            with open(out_path, "wb") as f:
                f.write(r2.content)
            print(f"  Saved to {out_path}")
            print(f"  Open it to verify content (should be today's gazette)")
    except Exception as e:
        t1 = time.time()
        print(f"  ERROR after {t1-t0:.1f}s: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
