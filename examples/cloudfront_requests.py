"""Confirm a CloudFront Parquet object with HTTP basic auth.

The body is Parquet, not JSON. This reads the first bytes only.
A full hour is often tens or hundreds of megabytes; use
cloudfront_duckdb.py to look at rows.
"""

import requests

HOUR = "2026-05-30T21"
URL = f"https://d1yyqbluk0oz0t.cloudfront.net/events-{HOUR}.parquet"
AUTH = ("polymarketdatabase", "PolymarketData2026!")

response = requests.get(URL, auth=AUTH, stream=True, timeout=60)

if response.status_code != 200:
    body = response.text[:500]
    raise SystemExit(f"Error {response.status_code}: {body}")

magic = next(response.iter_content(4), b"")
response.close()

print(f"status: {response.status_code}")
print(f"content-type: {response.headers.get('content-type')}")
print(f"content-length: {response.headers.get('content-length')} bytes")
print(f"magic: {magic!r}")
if magic == b"PAR1":
    print("Parquet file. Read rows with examples/cloudfront_duckdb.py")
else:
    print("Unexpected payload. Expected Parquet magic PAR1.")
