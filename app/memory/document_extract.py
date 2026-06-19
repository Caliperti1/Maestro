from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from pypdf import PdfReader

TEXT_DROPBOX_SUFFIXES = {".txt", ".md", ".json", ".csv", ".tsv"}
HTML_DROPBOX_SUFFIXES = {".html", ".htm"}
DOCUMENT_DROPBOX_SUFFIXES = {".pdf", ".docx"}
SUPPORTED_DROPBOX_SUFFIXES = (
    TEXT_DROPBOX_SUFFIXES | HTML_DROPBOX_SUFFIXES | DOCUMENT_DROPBOX_SUFFIXES
)


class DocumentExtractionError(ValueError):
    pass


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)

    def text(self) -> str:
        return "\n".join(self.parts)


def extract_dropbox_text(path: Path) -> tuple[str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_DROPBOX_SUFFIXES:
        raise DocumentExtractionError(f"Unsupported dropbox file type: {suffix}")

    if suffix in TEXT_DROPBOX_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace"), {
            "extraction_method": "utf8_text"
        }
    if suffix in HTML_DROPBOX_SUFFIXES:
        return _extract_html_text(path), {"extraction_method": "html_text"}
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path), {"extraction_method": "docx_text"}

    raise DocumentExtractionError(f"No extractor configured for {suffix}")


def _extract_html_text(path: Path) -> str:
    parser = _HTMLTextParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return _require_text(parser.text(), path)


def _extract_pdf_text(path: Path) -> tuple[str, dict[str, Any]]:
    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise DocumentExtractionError(f"Could not decrypt PDF {path.name}.") from exc

    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"[Page {index}]\n{page_text.strip()}")

    text = "\n\n".join(pages)
    if text.strip():
        return _require_text(text, path), {
            "extraction_method": "pdf_text",
            "page_count": len(reader.pages),
        }

    ocr_text, ocr_metadata = _extract_pdf_ocr_text(reader, path)
    return _require_text(
        ocr_text,
        path,
        empty_message=(
            f"No extractable text found in {path.name}. The PDF appears to be image-only, "
            "and OCR did not recover readable text."
        ),
    ), {
        "extraction_method": "pdf_ocr",
        "page_count": len(reader.pages),
        **ocr_metadata,
    }


def _extract_pdf_ocr_text(reader: PdfReader, path: Path) -> tuple[str, dict[str, Any]]:
    tesseract_path = shutil.which("tesseract")
    if tesseract_path is None:
        raise DocumentExtractionError(
            f"No extractable text found in {path.name}. The PDF appears to be image-only; "
            "install Tesseract OCR or export the PDF with selectable text."
        )

    pages: list[str] = []
    image_count = 0
    with tempfile.TemporaryDirectory(prefix="maestro-pdf-ocr-") as temp_dir:
        temp_path = Path(temp_dir)
        for page_index, page in enumerate(reader.pages, start=1):
            try:
                images = list(page.images)
            except ImportError as exc:
                raise DocumentExtractionError(
                    f"No extractable text found in {path.name}. The PDF appears to be "
                    "image-only; install Pillow to enable embedded-image OCR."
                ) from exc

            page_parts: list[str] = []
            for image_index, image_file in enumerate(images, start=1):
                image_count += 1
                image_path = temp_path / f"page-{page_index}-image-{image_index}.png"
                image_file.image.save(image_path)
                result = subprocess.run(
                    [tesseract_path, str(image_path), "stdout", "--psm", "6"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=90,
                )
                if result.returncode != 0:
                    raise DocumentExtractionError(
                        f"OCR failed for {path.name}: {result.stderr.strip()}"
                    )
                if result.stdout.strip():
                    page_parts.append(result.stdout.strip())
            if page_parts:
                page_text = "\n\n".join(page_parts)
                pages.append(f"[Page {page_index} OCR]\n{page_text}")

    if image_count == 0:
        raise DocumentExtractionError(
            f"No extractable text found in {path.name}. The PDF has no embedded text and "
            "no embedded images available for OCR."
        )

    return "\n\n".join(pages), {
        "ocr_engine": "tesseract",
        "ocr_image_count": image_count,
    }


def _extract_docx_text(path: Path) -> str:
    try:
        with ZipFile(path) as docx:
            xml = docx.read("word/document.xml")
    except Exception as exc:
        raise DocumentExtractionError(f"Could not read DOCX {path.name}.") from exc

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DocumentExtractionError(f"Could not parse DOCX text for {path.name}.") from exc

    text_nodes = root.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
    text = "\n".join(node.text or "" for node in text_nodes)
    return _require_text(text, path)


def _require_text(text: str, path: Path, *, empty_message: str | None = None) -> str:
    stripped = text.strip()
    if not stripped:
        raise DocumentExtractionError(empty_message or f"No extractable text found in {path.name}.")
    return stripped
