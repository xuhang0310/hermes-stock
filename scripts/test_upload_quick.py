"""Quick upload test — just txt, checking the full pipeline."""
import httpx, time, sys

BASE = "http://127.0.0.1:8000"

# Re-login fresh
r = httpx.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123456"})
assert r.status_code == 200, f"login failed: {r.text}"
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
print("LOGIN OK")

# Upload a txt file with enough content to chunk
content = b"# RAG Test\n\nThis is a comprehensive test of the RAG knowledge base upload parsing pipeline.\n\nIt contains multiple sentences that should be long enough to produce at least one chunk when processed by the chunk_text function with the default 700 character size and 80 character overlap.\n\nThe document covers several important aspects of the upload flow: file validation, text extraction, document creation, and background ingestion.\n\nWe need to verify that the parse function correctly handles UTF-8 encoded plain text files and that the ingest pipeline successfully creates document chunks in both the MySQL database and the vector store."
print(f"Content size: {len(content)} bytes")

r = httpx.post(
    f"{BASE}/api/knowledge/upload",
    files={"file": ("test_parse.txt", content, "text/plain")},
    headers=headers,
    timeout=30,
)
print(f"Upload HTTP {r.status_code}")
upload_data = r.json()
print(f"Upload response: {upload_data}")

uid = upload_data.get("upload_id")
did = upload_data.get("doc_id")
if not uid:
    print("FAIL: no upload_id in response")
    sys.exit(1)

# Poll status
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    r = httpx.get(f"{BASE}/api/knowledge/uploads/{uid}", headers=headers)
    if r.status_code != 200:
        print(f"  Poll HTTP {r.status_code}: {r.text[:200]}")
        time.sleep(1.5)
        continue
    s = r.json()
    status = s.get("status", "unknown")
    print(f"  Status: {status} chunks={s.get('chunk_count', 0)}")
    if status in ("indexed", "empty", "failed"):
        if status == "indexed":
            print(f"  OK: {s['chunk_count']} chunks indexed")
        elif status == "empty":
            print("  WARN: document too short to chunk")
        else:
            print(f"  FAIL: {s.get('error', 'unknown')}")
        break
    time.sleep(1.5)
else:
    print("  TIMEOUT: ingestion did not complete in 30s")

# Verify document page shows it
r = httpx.get(f"{BASE}/api/knowledge/documents?page=1&page_size=5", headers=headers)
docs = r.json()
print(f"\nRecent docs: total={docs['total']}, top doc_id={docs['items'][0]['id'] if docs['items'] else 'none'}")
print("DONE")
