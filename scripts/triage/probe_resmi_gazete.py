"""Diagnose the resmi_gazete silent failure.

Reproduces what the ingester does:
1. Builds today's expected URL
2. Builds yesterday's expected URL (the fallback)
3. Fetches both with the same UA the ingester uses
4. Inspects what came back · is it actually a PDF?
"""
from datetime import date, timedelta

import httpx

USER_AGENT = "MUSAHIT/0.1 (personal OSINT)"


def build_pdf_url(target_date: date, mukerrer: int = 0) -> str:
    """Same logic as musahit/ingest/resmi_gazete.py::_build_pdf_url"""
    yyyymmdd = target_date.strftime("%Y%m%d")
    suffix = f"-{mukerrer}" if mukerrer > 0 else ""
    return (
        f"https://www.resmigazete.gov.tr/eskiler/"
        f"{target_date.year:04d}/{target_date.month:02d}/{yyyymmdd}{suffix}.pdf"
    )


def probe(url: str) -> None:
    print(f"=== {url} ===")
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30,
        )
        print(f"  status:        {r.status_code}")
        print(f"  content-type:  {r.headers.get('content-type')}")
        print(f"  content-length: {len(r.content)} bytes")
        print(f"  is_pdf:        {r.content.startswith(b'%PDF')}")
        if r.content.startswith(b"%PDF"):
            print(f"  pdf_version:   {r.content[:8].decode('latin-1', errors='replace').strip()}")
        else:
            # Show first ~200 chars to see what we actually got
            preview = r.content[:300].decode("utf-8", errors="replace")
            preview = preview.replace("\n", "\\n").replace("\r", "")
            print(f"  preview (first 300 chars):")
            print(f"    {preview}")
        # Print the redirect chain if any
        if r.history:
            print(f"  redirect chain ({len(r.history)} hops):")
            for h in r.history:
                print(f"    {h.status_code} -> {h.headers.get('location')}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()


def main() -> None:
    today = date.today()
    yesterday = today - timedelta(days=1)

    print(f"TR-local today: {today}\n")
    print("--- The two candidate URLs the ingester probes ---\n")

    for d in [today, yesterday]:
        probe(build_pdf_url(d, mukerrer=0))

    # Also try the mukerrer (supplement) URLs in case those are what's working
    print("--- Mukerrer candidates for today ---\n")
    for n in range(1, 4):
        probe(build_pdf_url(today, mukerrer=n))

    # And the homepage · in case the PDF URL pattern changed entirely
    print("--- Homepage probe ---\n")
    probe("https://www.resmigazete.gov.tr/")


if __name__ == "__main__":
    main()
