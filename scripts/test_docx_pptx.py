"""Test DOCX + PPTX end-to-end: upload -> parse -> ingest -> verify."""
import httpx, time, json
from io import BytesIO

BASE = "http://127.0.0.1:8000"

r = httpx.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123456"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
print("LOGIN OK\n")

# --- DOCX (table) ---
print("=== DOCX (table) ===")
import docx
doc = docx.Document()
doc.add_paragraph("测试段落：RAG 知识库上传解析管线验证。")
doc.add_paragraph("验证目标：表格内容是否被正确提取到切块中。")
tbl = doc.add_table(rows=4, cols=3)
tbl.style = "Light Grid Accent 1"
for i, row_data in enumerate([
    ["代码", "名称", "行业"],
    ["000001", "平安银行", "银行"],
    ["600519", "贵州茅台", "白酒"],
    ["300750", "宁德时代", "新能源"],
]):
    for j, val in enumerate(row_data):
        tbl.rows[i].cells[j].text = val
buf = BytesIO()
doc.save(buf)
buf.seek(0)

r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("test_table.docx", buf.read(),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
               headers=h, timeout=60)
print(f"Upload: HTTP {r.status_code}")
data = r.json()
print(f"  {json.dumps(data, ensure_ascii=False)}")
uid = data.get("upload_id")

if uid:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        r = httpx.get(f"{BASE}/api/knowledge/uploads/{uid}", headers=h)
        s = r.json()
        st = s.get("status", "?")
        if st in ("indexed", "empty", "failed"):
            print(f"  => {st}  chunks={s['chunk_count']}")
            if st == "indexed" and s["chunk_count"] > 0:
                # Verify chunks contain table data
                r2 = httpx.get(f"{BASE}/api/knowledge/documents/{data['doc_id']}/chunks", headers=h)
                chunks = r2.json()
                for c in chunks:
                    content = c.get("content", "")
                    if "000001" in content or "平安银行" in content:
                        print(f"  TABLE CONTENT FOUND in chunk {c['chunk_index']}: {content[:120]}...")
            break
        time.sleep(1.5)

# --- PPTX ---
print("\n=== PPTX ===")
from pptx import Presentation
prs = Presentation()
s1 = prs.slides.add_slide(prs.slide_layouts[1])
s1.shapes.title.text = "Q4 策略报告"
s1.placeholders[1].text = "核心观点：超配新能源、半导体、消费板块。"
s2 = prs.slides.add_slide(prs.slide_layouts[1])
s2.shapes.title.text = "风险提示"
s2.placeholders[1].text = "主要风险：美联储政策、地缘冲突、经济复苏不及预期。"
buf2 = BytesIO()
prs.save(buf2)
buf2.seek(0)

r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("strategy.pptx", buf2.read(),
                       "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
               headers=h, timeout=60)
print(f"Upload: HTTP {r.status_code}")
data2 = r.json()
print(f"  {json.dumps(data2, ensure_ascii=False)}")
uid2 = data2.get("upload_id")

if uid2:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        r = httpx.get(f"{BASE}/api/knowledge/uploads/{uid2}", headers=h)
        s = r.json()
        st = s.get("status", "?")
        if st in ("indexed", "empty", "failed"):
            print(f"  => {st}  chunks={s['chunk_count']}")
            if st == "indexed" and s["chunk_count"] > 0:
                r2 = httpx.get(f"{BASE}/api/knowledge/documents/{data2['doc_id']}/chunks", headers=h)
                chunks = r2.json()
                for c in chunks:
                    content = c.get("content", "")
                    if "新能源" in content or "策略" in content:
                        print(f"  SLIDE CONTENT in chunk {c['chunk_index']}: {content[:150]}...")
            break
        time.sleep(1.5)

print("\nALL DONE")
