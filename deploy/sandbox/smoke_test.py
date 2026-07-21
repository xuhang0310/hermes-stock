"""Production smoke script copied into a disposable user's workspace."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


output = Path("exports")
output.mkdir(exist_ok=True)

workbook = Workbook()
sheet = workbook.active
sheet.title = "验收"
sheet.append(["项目", "结果"])
sheet.append(["Python 沙箱", "正常"])
workbook.save(output / "sandbox-smoke.xlsx")

document = Document()
document.add_heading("Python 沙箱验收", level=1)
document.add_paragraph("Word 文档生成正常。")
document.save(output / "sandbox-smoke.docx")

presentation = Presentation()
slide = presentation.slides.add_slide(presentation.slide_layouts[5])
slide.shapes.title.text = "Python 沙箱验收"
presentation.save(output / "sandbox-smoke.pptx")

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
pdf = canvas.Canvas(str(output / "sandbox-smoke.pdf"))
pdf.setFont("STSong-Light", 18)
pdf.drawString(72, 760, "Python 沙箱验收：PDF 中文正常")
pdf.save()

secret_env_visible = any(key.startswith(("STOCK_", "RABBIT_")) for key in os.environ)
host_env_visible = Path("/opt/hermes-stock/current/backend/.env").exists()
try:
    socket.create_connection(("1.1.1.1", 53), timeout=1).close()
    network_blocked = False
except OSError:
    network_blocked = True

print(json.dumps({
    "secret_env_visible": secret_env_visible,
    "host_env_visible": host_env_visible,
    "network_blocked": network_blocked,
    "artifacts": sorted(path.name for path in output.iterdir()),
}, ensure_ascii=False))
