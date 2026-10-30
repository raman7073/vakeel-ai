from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from auth import get_current_user
import os
from config import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_MB, UPLOAD_DIR
import uuid
from service.document_parser import extract_text
from models import Contract
from database import contracts_collection
from bson import ObjectId

router = APIRouter(
    prefix="/contracts",
    tags=["contracts"], dependencies=[Depends(get_current_user)]
)


@router.post("/upload")
async def upload_contract(
    file: UploadFile = File(),
    user: dict = Depends(get_current_user)
):
    """
    Upload a pdf or text contract for analysis
    """
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="File type not allowed")

    content = await file.read()
    size_mb = len(content)/(1024*1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400, detail="File size exceeds the maximum limit")
    # saving file to some directory
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(file_path, "wb") as f:
        f.write(content)

    try:
        parsed = extract_text(file_path)
    except Exception:
        # pypdf raises many exception types for damaged files; all mean unreadable.
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail="Could not read this file. Upload a valid PDF or UTF-8 text file.") from None
    if not parsed["text"]:
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail="No text found in this file. Scanned or image-only PDFs are not supported yet.")

    contract_data = Contract(
        filename=unique_name,
        original_name=file.filename,
        text_content=parsed["text"],
        page_count=parsed["page_count"],
        word_count=parsed["word_count"],

    )
    doc = contract_data.model_dump(exclude={"id"})
    doc["owner_id"] = user["id"]
    result = contracts_collection.insert_one(doc)
    contract_data.id = str(result.inserted_id)

    return {
        "message": "file uploaded and processed successfully",
        "contract": contract_data.model_dump(mode="json"),
        "id": contract_data.id
    }


@router.get("/")
async def list_contracts(user: dict = Depends(get_current_user)):
    contracts = []
    for doc in contracts_collection.find({"owner_id": user["id"]}):
        contract = Contract(**doc)
        contract.id = str(doc["_id"])
        contracts.append(contract.model_dump())

    return {"contracts": contracts}


@router.get("/{contract_id}")
async def get_contract(contract_id: str, user: dict = Depends(get_current_user)):
    if not ObjectId.is_valid(contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    doc = contracts_collection.find_one({"_id": ObjectId(contract_id), "owner_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Contract not found")

    contract = Contract(**doc)
    contract.id = str(doc["_id"])
    return {"contract": contract.model_dump()}
