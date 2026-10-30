from fastapi import APIRouter, HTTPException, Depends
from auth import get_current_user
from config import GEMINI_API_KEY
from database import contracts_collection, analysis_collection
from bson import ObjectId
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool
from service.analyse_contract import (AnalysisType, Language, analyze_contract,
                                      answer_question)

router = APIRouter(
    prefix='/analysis',
    tags=["analysis"], dependencies=[Depends(get_current_user)]
)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    language: Language = Language.english


async def _owned_contract_text(contract_id: str, user_id: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500, detail="AI API key is not configured")
    if not ObjectId.is_valid(contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    try:
        doc = await run_in_threadpool(
            contracts_collection.find_one, {"_id": ObjectId(contract_id), "owner_id": user_id}
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503, detail="Database unavailable") from None
    if doc is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    if not doc.get("text_content"):
        raise HTTPException(
            status_code=400, detail="Contract has no text content to analyse")
    return doc["text_content"]


@router.post("/analyze/{contract_id}")
async def analyse_contract(contract_id: str,
                           analysis_type: AnalysisType = AnalysisType.full,
                           language: Language = Language.english,
                           user: dict = Depends(get_current_user)):
    """
    Analyze a contract using AI and return insights.

    analysis_type: full review, risk assessment, plain-language summary, or clause extraction.
    language: language of the explanations; quotes stay exactly as in the contract.
    """
    text_content = await _owned_contract_text(contract_id, user["id"])
    result = await analyze_contract(contract_id, text_content, analysis_type, language)
    return {"message": "Contract analysed successfully", "analysis": result}


@router.post("/ask/{contract_id}")
async def ask_contract(contract_id: str, data: QuestionRequest,
                       user: dict = Depends(get_current_user)):
    """Answer a question using only the contract text, citing exact passages."""
    text_content = await _owned_contract_text(contract_id, user["id"])
    result = await answer_question(text_content, data.question, data.language)
    return {"contract_id": contract_id, "question": data.question, **result}


@router.get("/contract/{contract_id}")
async def list_contract_analyses(contract_id: str, user: dict = Depends(get_current_user)):
    """List a contract's saved analyses, newest first, without their full results."""
    if not ObjectId.is_valid(contract_id):
        raise HTTPException(status_code=400, detail="Invalid contract ID")
    try:
        owner = await run_in_threadpool(
            contracts_collection.find_one,
            {"_id": ObjectId(contract_id), "owner_id": user["id"]}, {"_id": 1},
        )
        if owner is None:
            raise HTTPException(status_code=404, detail="Contract not found")
        docs = await run_in_threadpool(lambda: list(analysis_collection.find(
            {"contract_id": contract_id},
            {"_id": 0, "analysis_id": 1, "analysis_type": 1, "language": 1, "created_at": 1,
             "model": 1, "analysis.overall_risk_level": 1, "evidence_check": 1},
        ).sort("created_at", -1)))
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    # Analyses saved before review types and languages existed are full English reviews.
    return {"analyses": [{
        "analysis_id": doc["analysis_id"],
        "analysis_type": doc.get("analysis_type", AnalysisType.full.value),
        "language": doc.get("language", Language.english.value),
        "created_at": doc.get("created_at"),
        "model": doc.get("model"),
        "overall_risk_level": doc.get("analysis", {}).get("overall_risk_level"),
        "evidence_check": doc.get("evidence_check"),
    } for doc in docs]}


@router.get("/{analysis_id}")
async def get_analysis(analysis_id: str, user: dict = Depends(get_current_user)):
    """Retrieve a saved result by its analysis_id, not its contract_id."""
    try:
        doc = await run_in_threadpool(
            analysis_collection.find_one, {"analysis_id": analysis_id}, {"_id": 0}
        )
        if doc is not None:
            contract_id = doc.get("contract_id", "")
            owner = None
            if ObjectId.is_valid(contract_id):
                owner = await run_in_threadpool(
                    contracts_collection.find_one,
                    {"_id": ObjectId(contract_id), "owner_id": user["id"]},
                    {"_id": 1},
                )
            if owner is None:
                doc = None
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    if doc is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"analysis": doc}
