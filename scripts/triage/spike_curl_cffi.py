"""curl_cffi spike · test browser-impersonating TLS against gov sources.

curl_cffi is a Python HTTP client built on libcurl-impersonate. It can
impersonate Chrome, Firefox, Edge, and Safari at the TLS handshake level.
If standard Python httpx fails because the CDN does TLS fingerprinting,
curl_cffi should succeed by presenting a real browser fingerprint.

This spike checks if we can fetch the 5 gov sources cleanly with each of
several browser impersonations. Output is short and decisive.
"""
from curl_cffi import requests

# The five sources that fail with standard httpx
URLS = [
    ("resmi_gazete_homepage", "https://www.resmigazete.gov.tr/"),
    ("resmi_gazete_pdf",      "https://www.resmigazete.gov.tr/eskiler/2026/05/20260525.pdf"),
    ("anayasa_mahkemesi",     "https://www.anayasa.gov.tr/"),
    ("cumhurbaskanligi",      "https://www.tccb.gov.tr/"),
    ("danistay",              "https://www.danistay.gov.tr/"),
    ("yargitay",              "https://www.yargitay.gov.tr/"),
]

# Browser impersonations to try · these match real browser TLS fingerprints
# Full list at https://github.com/lexiforest/curl_cffi (chrome120 is recent)
IMPERSONATIONS = ["chrome120", "firefox133", "safari17_0"]


def probe(name: str, url: str, impersonate: str) -> dict:
    """One probe attempt. Returns a small dict for compact reporting."""
    try:
        r = requests.get(url, impersonate=impersonate, timeout=15)
        return {
            "status": r.status_code,
            "size": len(r.content),
            "content_type": r.headers.get("content-type", "")[:50],
            "is_pdf": r.content.startswith(b"%PDF"),
            "error": None,
        }
    except Exception as e:
        return {
            "status": None,
            "size": 0,
            "content_type": "",
            "is_pdf": False,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
        }


def main() -> None:
    print(f"curl_cffi spike · testing {len(IMPERSONATIONS)} browser impersonations against {len(URLS)} URLs\n")

    for name, url in URLS:
        print(f"=== {name} ===")
        print(f"    {url}")
        for imp in IMPERSONATIONS:
            r = probe(name, url, imp)
            if r["error"]:
                print(f"    [{imp:12s}] ERROR · {r['error']}")
            else:
                pdf_marker = " · PDF ✓" if r["is_pdf"] else ""
                print(f"    [{imp:12s}] {r['status']} · {r['size']:>7d} bytes · {r['content_type']}{pdf_marker}")
        print()

    print("Spike complete. If any impersonation got non-error responses for the")
    print("currently-failing sources, curl_cffi is the fix path.")


if __name__ == "__main__":
    main()
