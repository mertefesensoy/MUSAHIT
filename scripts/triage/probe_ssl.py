"""Probe SSL cert chains for the four gov sources suspected of SSL issues.

Output:
* The certificate issuer and subject (so we can see which CA signs them)
* The exact SSL verification error if any
"""
import ssl
import socket
from urllib.parse import urlparse

URLS = [
    "https://www.anayasa.gov.tr/",
    "https://www.tccb.gov.tr/",
    "https://www.danistay.gov.tr/",
    "https://www.yargitay.gov.tr/",
]


def probe(url: str) -> None:
    host = urlparse(url).hostname
    print(f"=== {host} ===")
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                issuer = dict(x[0] for x in cert["issuer"])
                subject = dict(x[0] for x in cert["subject"])
                print(f"  status:  VERIFIED")
                print(f"  issuer:  {issuer}")
                print(f"  subject: {subject}")
    except ssl.SSLCertVerificationError as e:
        print(f"  status:  VERIFY FAILED")
        print(f"  code:    {e.verify_code}")
        print(f"  message: {e.verify_message}")
    except socket.gaierror as e:
        print(f"  status:  DNS FAILURE")
        print(f"  error:   {e}")
    except Exception as e:
        print(f"  status:  OTHER ERROR")
        print(f"  error:   {type(e).__name__}: {e}")
    print()


if __name__ == "__main__":
    for url in URLS:
        probe(url)
