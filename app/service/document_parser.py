import os
from pypdf import PdfReader


def extract_text_from_txt(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return {"text": text.strip(), "page_count": 1, "word_count": len(text.split())}


def extract_text_from_pdf(file_path: str) -> dict:
    reader = PdfReader(file_path)
    text = ""
    for page in reader.pages:
        text += (page.extract_text() or "") + "\n"

    return {"text": text.strip(), "page_count": len(reader.pages), "word_count": len(text.split())}


def extract_text(file_path: str) -> dict:
    """
    Extract text from a document file
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    if ext == ".txt":
        return extract_text_from_txt(file_path)
    else:
        raise ValueError("Unsupported file type")
