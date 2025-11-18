import os
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from pypdf import PdfMerger, PdfReader, PdfWriter

app = FastAPI(title="PDF Toolkit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "PDF Toolkit API Running"}


# 1) Merge PDFs
@app.post("/api/pdf/merge")
async def merge_pdfs(files: List[UploadFile] = File(...)):
    if not files or len(files) < 2:
        raise HTTPException(status_code=400, detail="Provide at least two PDF files to merge")
    merger = PdfMerger()
    try:
        for f in files:
            if not f.filename.lower().endswith('.pdf'):
                raise HTTPException(status_code=400, detail=f"{f.filename} is not a PDF")
            merger.append(BytesIO(await f.read()))
        out = BytesIO()
        merger.write(out)
        out.seek(0)
        headers = {"Content-Disposition": "attachment; filename=merged.pdf"}
        return StreamingResponse(out, media_type="application/pdf", headers=headers)
    finally:
        merger.close()


# 2) Compress PDF (best-effort using pypdf stream compression)
@app.post("/api/pdf/compress")
async def compress_pdf(file: UploadFile = File(...), quality: Optional[str] = Form("default")):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")
    try:
        original_bytes = await file.read()
        reader = PdfReader(BytesIO(original_bytes))
        writer = PdfWriter()

        for page in reader.pages:
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)

        try:
            if reader.metadata:
                writer.add_metadata({k: str(v) for k, v in reader.metadata.items() if v is not None})
        except Exception:
            pass

        out = BytesIO()
        writer.write(out)
        out.seek(0)
        headers = {"Content-Disposition": f"attachment; filename=compressed_{file.filename}"}
        return StreamingResponse(out, media_type="application/pdf", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Compression failed: {str(e)}")


# 3) Add page numbers
@app.post("/api/pdf/number")
async def number_pdf(
    file: UploadFile = File(...),
    position: str = Form("bottom-right"),
    start_at: int = Form(1),
    font_size: int = Form(10)
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    try:
        from reportlab.pdfgen import canvas  # import lazily to avoid startup failures if unavailable
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Numbering unavailable: reportlab not installed ({e})")

    src_bytes = await file.read()
    reader = PdfReader(BytesIO(src_bytes))
    writer = PdfWriter()

    try:
        for i, page in enumerate(reader.pages, start=1):
            packet = BytesIO()
            try:
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                page_size = (width, height)
            except Exception:
                page_size = letter
            c = canvas.Canvas(packet, pagesize=page_size)

            page_number = start_at + (i - 1)
            text = str(page_number)

            margin = 0.5 * inch
            if position == "bottom-left":
                x, y = margin, margin
            elif position == "bottom-center":
                x, y = page_size[0] / 2, margin
            elif position == "top-right":
                x, y = page_size[0] - margin, page_size[1] - margin
            elif position == "top-left":
                x, y = margin, page_size[1] - margin
            elif position == "top-center":
                x, y = page_size[0] / 2, page_size[1] - margin
            else:
                x, y = page_size[0] - margin, margin

            c.setFont("Helvetica", font_size)
            if "center" in position:
                c.drawCentredString(x, y, text)
            else:
                c.drawString(x, y, text)
            c.save()

            packet.seek(0)
            overlay_reader = PdfReader(packet)
            overlay_page = overlay_reader.pages[0]
            page.merge_page(overlay_page)
            writer.add_page(page)

        out = BytesIO()
        writer.write(out)
        out.seek(0)
        headers = {"Content-Disposition": f"attachment; filename=numbered_{file.filename}"}
        return StreamingResponse(out, media_type="application/pdf", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Numbering failed: {str(e)}")


# 4) Split PDF (by page ranges or every page)
@app.post("/api/pdf/split")
async def split_pdf(
    file: UploadFile = File(...),
    ranges: Optional[str] = Form(None)  # e.g., "1-3,5,7-9"
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    data = await file.read()
    reader = PdfReader(BytesIO(data))

    def parse_ranges(r: Optional[str], total: int) -> List[List[int]]:
        if not r:
            return [[i] for i in range(1, total + 1)]
        groups: List[List[int]] = []
        for part in r.split(','):
            part = part.strip()
            if '-' in part:
                a, b = part.split('-', 1)
                try:
                    start = max(1, int(a))
                    end = min(total, int(b))
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid range: {part}")
                if start > end:
                    start, end = end, start
                groups.append(list(range(start, end + 1)))
            else:
                try:
                    idx = int(part)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid page: {part}")
                if not 1 <= idx <= total:
                    raise HTTPException(status_code=400, detail=f"Page out of bounds: {part}")
                groups.append([idx])
        return groups

    try:
        groups = parse_ranges(ranges, len(reader.pages))
        outputs: List[bytes] = []
        for gi, grp in enumerate(groups, start=1):
            writer = PdfWriter()
            for p in grp:
                writer.add_page(reader.pages[p - 1])
            buf = BytesIO()
            writer.write(buf)
            buf.seek(0)
            outputs.append(buf.read())

        import zipfile
        zip_buf = BytesIO()
        with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for i, content in enumerate(outputs, start=1):
                zf.writestr(f"split_{i}.pdf", content)
        zip_buf.seek(0)
        headers = {"Content-Disposition": f"attachment; filename=split_{file.filename}.zip"}
        return StreamingResponse(zip_buf, media_type="application/zip", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Split failed: {str(e)}")


@app.get("/test")
def test_endpoint():
    return {"backend": "running"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
