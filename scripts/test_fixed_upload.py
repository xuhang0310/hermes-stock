"""Quick full-cycle upload test after db.commit() fix."""
import httpx, time, json, sys

BASE = "http://127.0.0.1:8000"

# Login
r = httpx.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123456"})
assert r.status_code == 200, f"Login failed: {r.text}"
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
print("LOGIN OK")

# Upload txt
content = b"# RAG Upload Test v2\n\nThis is a comprehensive test document for the RAG knowledge base upload parsing pipeline.\n\nIt contains multiple paragraphs with sufficient length to trigger the chunking logic. The default chunk_size is 700 characters with 80 characters overlap. This sentence helps fill the buffer to reach the minimum threshold of 20 characters per chunk which is defined as _MIN_CHUNK_CHARS in rag_ingest.py.\n\nWe are testing the full pipeline: parse -> document insert -> background ingest -> chunk -> embed -> vector store upsert -> KnowledgeUpload status update."
print(f"\nContent: {len(content)} bytes")

r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("test_v2.txt", content, "text/plain")}, headers=h, timeout=60)
print(f"Upload HTTP {r.status_code}")
data = r.json()
print(f"Response: {json.dumps(data, ensure_ascii=False)}")

uid = data.get("upload_id")
if not uid:
    print("FAIL: no upload_id")
    sys.exit(1)

# Poll
print("\nPolling...")
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    r = httpx.get(f"{BASE}/api/knowledge/uploads/{uid}", headers=h)
    if r.status_code == 404:
        print(f"  404 — row didn't commit?")
        break
    s = r.json()
    st = s.get("status", "?")
    print(f"  {st}  chunks={s.get('chunk_count', 0)}")
    if st in ("indexed", "empty", "failed"):
        print(f"\nRESULT: {st}  chunks={s['chunk_count']}  error={s.get('error','')}")
        # Verify document page
        r2 = httpx.get(f"{BASE}/api/knowledge/documents?page=1&page_size=3", headers=h)
        docs = r2.json()
        latest = docs["items"][0]
        print(f"Latest doc: id={latest['id']}  type={latest['doc_type']}  title={latest['title']}  chunks={latest['chunk_count']}  indexed={latest['indexed']}")
        break
    time.sleep(2)
else:
    print("TIMEOUT")

# Dedup test
print("\n=== Dedup test ===")
r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("test_v2.txt", content, "text/plain")}, headers=h, timeout=60)
print(f"  HTTP {r.status_code}  {r.json().get('detail','')[:120]}")

print("\nDONE")
