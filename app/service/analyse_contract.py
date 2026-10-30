import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

import httpx
from bson import ObjectId
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL
from database import analysis_collection, contracts_collection
from .prompt import (CLAUSE_EXTRACTION_PROMPT, CONTRACT_ANALYSIS_PROMPT,
                     LANGUAGE_INSTRUCTION, QUESTION_ANSWER_PROMPT,
                     RISK_ASSESSMENT_PROMPT, SUMMARY_PROMPT)

logger = logging.getLogger(__name__)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class AnalysisType(str, Enum):
    full = "full"
    risk = "risk"
    summary = "summary"
    clauses = "clauses"


class Language(str, Enum):
    english = "english"
    hindi = "hindi"
    marathi = "marathi"
    gujarati = "gujarati"
    bengali = "bengali"
    tamil = "tamil"
    telugu = "telugu"
    kannada = "kannada"


class AnalysisItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Party(AnalysisItem):
    name: str | None
    role: str | None


class KeyTerms(AnalysisItem):
    effective_date: str | None
    expiration_date: str | None
    duration: str | None
    payment_terms: str | None
    renewal: str | None
    termination: str | None
    confidentiality: str | None
    intellectual_property: str | None
    liability: str | None
    indemnity: str | None
    governing_law: str | None
    dispute_resolution: str | None


class Obligation(AnalysisItem):
    party: str | None
    description: str
    deadline: str | None
    clause_reference: str | None
    evidence: str


class Risk(AnalysisItem):
    title: str
    severity: Literal["low", "medium", "high"]
    affected_party: str | None
    clause_reference: str | None
    evidence: str | None
    explanation: str
    recommendation: str


class UnclearTerm(AnalysisItem):
    term: str
    explanation: str


class ContractAnalysis(AnalysisItem):
    contract_type: str | None
    summary: str
    parties: list[Party]
    key_terms: KeyTerms
    obligations: list[Obligation]
    risks: list[Risk]
    missing_or_unclear_terms: list[UnclearTerm]
    overall_risk_level: Literal["low", "medium", "high", "unknown"]
    overall_risk_explanation: str
    questions_for_review: list[str]
    review_limitations: list[str]


class RiskFinding(AnalysisItem):
    title: str
    severity: Literal["low", "medium", "high"]
    basis: Literal["stated_term", "ambiguity", "conflict", "omission"]
    affected_party: str | None
    clause_reference: str | None
    evidence: str | None
    explanation: str
    potential_impact: str
    recommendation: str


class RiskAssessment(AnalysisItem):
    overall_risk_level: Literal["low", "medium", "high", "unknown"]
    overall_risk_explanation: str
    risks: list[RiskFinding]
    priority_actions: list[str]
    questions_for_review: list[str]
    review_limitations: list[str]


class ImportantDate(AnalysisItem):
    event: str
    date_or_deadline: str


class ContractSummary(AnalysisItem):
    contract_type: str | None
    overview: str
    parties: list[Party]
    key_obligations: list[Obligation]
    payment_terms: str | None
    important_dates: list[ImportantDate]
    duration: str | None
    renewal: str | None
    termination: str | None
    governing_law: str | None
    dispute_resolution: str | None
    other_key_terms: list[str]
    unclear_or_missing_information: list[str]
    summary_limitations: list[str]


ClauseCategory = Literal[
    "definitions", "scope", "obligations", "payment", "term", "renewal",
    "termination", "confidentiality", "intellectual_property", "data_protection",
    "liability", "indemnity", "warranties", "dispute_resolution", "governing_law",
    "force_majeure", "assignment", "notices", "amendment", "severability",
    "entire_agreement", "signatures", "other",
]


class Clause(AnalysisItem):
    position: int
    clause_reference: str | None
    heading: str | None
    categories: list[ClauseCategory]
    text: str
    summary: str
    parties: list[str]
    referenced_clauses: list[str]


class ClauseExtraction(AnalysisItem):
    clauses: list[Clause]
    extraction_limitations: list[str]


class Citation(AnalysisItem):
    clause_reference: str | None
    evidence: str


class ContractAnswer(AnalysisItem):
    answer_status: Literal["answered", "partially_answered", "not_in_contract", "ambiguous"]
    answer: str
    citations: list[Citation]
    caveats: list[str]


ANALYSES = {
    AnalysisType.full: (CONTRACT_ANALYSIS_PROMPT, ContractAnalysis),
    AnalysisType.risk: (RISK_ASSESSMENT_PROMPT, RiskAssessment),
    AnalysisType.summary: (SUMMARY_PROMPT, ContractSummary),
    AnalysisType.clauses: (CLAUSE_EXTRACTION_PROMPT, ClauseExtraction),
}

# Result fields that must quote the contract verbatim.
QUOTE_FIELDS = ("evidence", "text")
_CURLY_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _normalize(text: str) -> str:
    """Ignore case, whitespace, hyphens and curly quotes, which PDF extraction and models alter."""
    return re.sub(r"[\s\-‐-―]+", "", text.translate(_CURLY_QUOTES).casefold())


def _quote_in_text(quote: str, normalized_source: str) -> bool:
    """Each part of the quote, split at any ellipsis, must appear in order in the source."""
    parts = [_normalize(part).strip("\"'.,;:") for part in re.split(r"\.\.\.|…", quote)]
    parts = [part for part in parts if part]
    position = 0
    for part in parts:
        position = normalized_source.find(part, position)
        if position < 0:
            return False
        position += len(part)
    return bool(parts)


def _find_quotes(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            field = f"{path}.{key}" if path else key
            if key in QUOTE_FIELDS and isinstance(item, str):
                yield field, item
            else:
                yield from _find_quotes(item, field)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _find_quotes(item, f"{path}[{index}]")


def check_evidence(result: dict, contract_text: str) -> dict:
    """Report which quotes in an AI result cannot be found in the contract text."""
    source = _normalize(contract_text)
    quotes = list(_find_quotes(result))
    unverified = [{"field": field, "quote": quote} for field, quote in quotes
                  if not _quote_in_text(quote, source)]
    return {"quotes_checked": len(quotes),
            "quotes_verified": len(quotes) - len(unverified),
            "unverified": unverified}


async def _generate_analysis(prompt: str, schema: type[AnalysisItem],
                             language: Language) -> tuple[dict, str]:
    """Return the validated result and the model that produced it."""
    payload = {
        "systemInstruction": {"parts": [{
            "text": LANGUAGE_INSTRUCTION.format(language=language.value.title())
        }]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema.model_json_schema(),
        },
    }
    # Overload, timeouts and retired models are specific to one model, so try the
    # next one. Key and payload errors would fail on every model, so stop at once.
    for model in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                response = await client.post(
                    GEMINI_URL.format(model=model),
                    headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload
                )
                response.raise_for_status()
            break
        except httpx.TimeoutException:
            error = HTTPException(504, "AI analysis timed out. Please try again.")
        except httpx.HTTPStatusError as exc:
            logger.warning("Gemini model %s failed with HTTP %s", model, exc.response.status_code)
            if exc.response.status_code == 429:
                error = HTTPException(
                    503, "AI service quota exceeded. Please try again later.")
            elif exc.response.status_code in (500, 502, 503, 504):
                error = HTTPException(
                    503, "AI service is temporarily unavailable or busy. Please try again later.")
            elif exc.response.status_code == 404:
                error = HTTPException(
                    502, "Configured AI model was not found. Check GEMINI_MODEL.")
            elif exc.response.status_code in (401, 403):
                raise HTTPException(
                    502, "AI service authentication or permission failed. Check the configured API key.") from None
            else:
                raise HTTPException(
                    502, "AI service rejected the request payload. Check the input and generation configuration.") from None
        except httpx.RequestError:
            error = HTTPException(502, "Unable to reach the AI service.")
        logger.warning("Gemini model %s unavailable: %s", model, error.detail)
    else:
        raise error

    try:
        candidate = response.json()["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError("Incomplete or blocked response")
        text = "".join(
            part.get("text", "") for part in candidate["content"]["parts"]
            if not part.get("thought", False)
        )
        return schema.model_validate_json(text).model_dump(mode="json"), model
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, ValidationError):
        raise HTTPException(
            502, "AI service returned an incomplete or invalid analysis.") from None


async def analyze_contract(contract_id: str, text_content: str,
                           analysis_type: AnalysisType = AnalysisType.full,
                           language: Language = Language.english) -> dict:
    """Analyze the full contract using Gemini, persist it, and return JSON-safe data."""
    if not GEMINI_API_KEY:
        raise HTTPException(500, "AI API key is not configured")
    if not ObjectId.is_valid(contract_id):
        raise HTTPException(400, "Invalid contract ID")
    if not text_content or not text_content.strip():
        raise HTTPException(400, "Contract has no text content to analyse")
    analysis_type, language = AnalysisType(analysis_type), Language(language)

    query = {"_id": ObjectId(contract_id)}
    started = False
    try:
        updated = await run_in_threadpool(
            contracts_collection.update_one, query, {
                "$set": {"analysis_status": "in_progress"}}
        )
        if not updated.matched_count:
            raise HTTPException(404, "Contract not found")
        started = True
        prompt, schema = ANALYSES[analysis_type]
        analysis, model = await _generate_analysis(
            prompt.format(contract_text=text_content), schema, language)
        document = {
            "analysis_id": uuid4().hex,
            "contract_id": str(query["_id"]),
            "analysis_type": analysis_type.value,
            "language": language.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "analysis": analysis,
            "evidence_check": check_evidence(analysis, text_content),
        }
        # PyMongo adds an ObjectId to the inserted dictionary; keep it out of the response.
        await run_in_threadpool(analysis_collection.insert_one, document.copy())
        await run_in_threadpool(
            contracts_collection.update_one, query, {
                "$set": {"analysis_status": "completed"}}
        )
        return document
    except (HTTPException, PyMongoError) as exc:
        if started:
            try:
                await run_in_threadpool(
                    contracts_collection.update_one, query, {
                        "$set": {"analysis_status": "failed"}}
                )
            except PyMongoError:
                logger.warning("Could not mark contract analysis as failed")
        if isinstance(exc, PyMongoError):
            raise HTTPException(
                503, "Database unavailable while saving analysis.") from None
        raise


async def answer_question(text_content: str, question: str,
                          language: Language = Language.english) -> dict:
    """Answer a question from the contract text alone and check its quoted evidence."""
    if not GEMINI_API_KEY:
        raise HTTPException(500, "AI API key is not configured")
    if not text_content or not text_content.strip():
        raise HTTPException(400, "Contract has no text content to analyse")
    language = Language(language)
    answer, model = await _generate_analysis(
        QUESTION_ANSWER_PROMPT.format(question=question, contract_text=text_content),
        ContractAnswer, language)
    return {"language": language.value, "model": model, "answer": answer,
            "evidence_check": check_evidence(answer, text_content)}
