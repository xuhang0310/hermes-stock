"""Full upload parsing pipeline test — all 4 file types."""
import httpx, time, json, sys, os
from io import BytesIO

BASE = "http://127.0.0.1:8000"

# ---- helpers ----
def login():
    r = httpx.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123456"})
    if r.status_code != 200:
        print(f"LOGIN FAIL: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    print(f"LOGIN OK: {r.json().get('username','?')}")
    return r.json()["access_token"]

def do_upload(token, filename, content, mime):
    h = {"Authorization": f"Bearer {token}"}
    r = httpx.post(f"{BASE}/api/knowledge/upload", files={"file": (filename, content, mime)}, headers=h, timeout=60)
    data = r.json()
    print(f"  HTTP {r.status_code}  =>  {json.dumps(data, ensure_ascii=False)}")
    if r.status_code == 409:
        print("  (duplicate — OK, content_hash already exists)")
    return data

def poll_until_done(token, upload_id, timeout=45):
    h = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = httpx.get(f"{BASE}/api/knowledge/uploads/{upload_id}", headers=h)
        if r.status_code == 404:
            print(f"  [404 upload not found — DB flush race?]")
            time.sleep(1)
            continue
        s = r.json()
        st = s.get("status", "?")
        if st in ("indexed", "empty", "failed"):
            print(f"  => FINAL: {st}  chunks={s.get('chunk_count', 0)}  error={s.get('error', '')}")
            return st
        print(f"  poll: {st}")
        time.sleep(1.5)
    print("  => TIMEOUT")
    return "timeout"

# ---- main ----
token = login()
h = {"Authorization": f"Bearer {token}"}

results = {}

# ============================================================
# 1. TXT — baseline
# ============================================================
print("\n=== 1. TXT (utf-8) ===")
txt = "# RAG Upload Test\n\n这份文档用于测试知识库上传解析管线的各个环节。\n\n包含中英文混合内容，以及足够的长度来触发切块逻辑。默认的 chunk_size 是 700 字符，overlap 是 80 字符。"

# Pad to ~500 chars to ensure at least one chunk meets MIN_CHUNK_CHARS=20
extra = "\n\n" + "这是一段补充内容。本段落用来验证文本切块功能是否按预期工作。" * 15
content = (txt + extra).encode("utf-8")
print(f"  bytes={len(content)}  chars={len(content.decode('utf-8'))}")

res = do_upload(token, "test_rag_pipeline.txt", content, "text/plain")
if res.get("upload_id"):
    results["txt"] = poll_until_done(token, res["upload_id"])
else:
    results["txt"] = res.get("detail", "no upload_id")

# ============================================================
# 2. DOCX — paragraphs + table
# ============================================================
print("\n=== 2. DOCX (paragraphs + table) ===")
try:
    import docx

    doc = docx.Document()
    doc.add_heading("测试报告", level=1)
    doc.add_paragraph("这是一份用于测试 RAG 解析管线的 Word 文档。")
    doc.add_paragraph("目标：验证段落文本和表格内容是否都能正确提取。")
    doc.add_paragraph("文档中包含一个沪深300成分股的简要表格。")
    # Table with stock data
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

    res = do_upload(token, "test_report.docx", buf.read(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if res.get("upload_id"):
        results["docx"] = poll_until_done(token, res["upload_id"])
    else:
        results["docx"] = res.get("detail", "no upload_id")
except ImportError:
    print("  SKIP: python-docx not installed")
    results["docx"] = "SKIP"

# ============================================================
# 3. PPTX — text + table
# ============================================================
print("\n=== 3. PPTX (slides) ===")
try:
    from pptx import Presentation

    prs = Presentation()
    slide1 = prs.slides.add_slide(prs.slide_layouts[1])
    slide1.shapes.title.text = "季度策略报告"
    slide1.placeholders[1].text = "2025Q4 核心观点：A股震荡上行，建议超配新能源、半导体、消费板块。"

    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    slide2.shapes.title.text = "风险提示"
    slide2.placeholders[1].text = "主要风险：美联储政策不确定性、地缘政治冲突升级、国内经济复苏不及预期。"

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)

    res = do_upload(token, "strategy.pptx", buf.read(),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    if res.get("upload_id"):
        results["pptx"] = poll_until_done(token, res["upload_id"])
    else:
        results["pptx"] = res.get("detail", "no upload_id")
except ImportError:
    print("  SKIP: python-pptx not installed")
    results["pptx"] = "SKIP"

# ============================================================
# 4. PDF — reportlab text (pdfplumber unavailable on Win → pypdf fallback)
# ============================================================
print("\n=== 4. PDF ===")
try:
    # Try reportlab first for text PDF; if not available generate via pypdf + drawString
    from pypdf import PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(72, 750, "RAG Pipeline Test Document")
    c.setFont("Helvetica", 11)
    y = 720
    for line in [
        "This PDF document is created to test the upload parsing pipeline.",
        "It contains multiple lines of text suitable for chunking.",
        "The pypdf library should extract all these lines correctly.",
        "If pdfplumber were installed, it would be preferred for table extraction.",
        "",
        "Financial data summary:",
        "  沪深300 Index: 4,200 (up 2.3%)",
        "  SSE Composite: 3,350 (up 1.8%)",
        "  ChiNext: 2,180 (up 3.1%)",
    ]:
        c.drawString(72, y, line)
        y -= 16
    c.showPage()
    c.save()
    buf.seek(0)

    res = do_upload(token, "test_report.pdf", buf.read(), "application/pdf")
    if res.get("upload_id"):
        results["pdf"] = poll_until_done(token, res["upload_id"])
    else:
        results["pdf"] = res.get("detail", "no upload_id")
except ImportError as e:
    print(f"  SKIP: {e}")
    results["pdf"] = "SKIP"

# ============================================================
# 5. Edge cases
# ============================================================
print("\n=== 5. Edge Cases ===")

# 5a. Empty file
print("  5a. Empty file:")
r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("empty.txt", b"", "text/plain")}, headers=h)
print(f"    HTTP {r.status_code}  {r.json().get('detail','')[:150]}")

# 5b. Unsupported ext
print("  5b. Unsupported .csv:")
r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("data.csv", b"a,b,c\n1,2,3", "text/csv")}, headers=h)
print(f"    HTTP {r.status_code}  {r.json().get('detail','')[:150]}")

# 5c. No auth
print("  5c. No auth token:")
r = httpx.post(f"{BASE}/api/knowledge/upload",
               files={"file": ("x.txt", b"hello", "text/plain")})
print(f"    HTTP {r.status_code}  {r.json().get('detail','')[:150]}")

# ============================================================
print("\n======================")
print("RESULTS SUMMARY:")
for k, v in results.items():
    print(f"  {k:8s}  {v}")
print("======================")
