import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

HTTP_CLIENT = httpx.AsyncClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
config = types.ModuleType("config")
config.GEMINI_API_KEY = "test-key"
config.GEMINI_MODEL = "test-model"
config.GEMINI_FALLBACK_MODELS = ["fallback-model"]
config.JWT_SECRET_KEY = "test-secret-" * 4
config.JWT_ALGORITHM = "HS256"
config.ACCESS_TOKEN_EXPIRE_MINUTES = 30
config.validate_auth_config = lambda: None
database = types.ModuleType("database")
database.contracts_collection = Mock()
database.analysis_collection = Mock()
database.users_collection = Mock()
with patch.dict(sys.modules, {"config": config, "database": database}):
    service = importlib.import_module("service.analyse_contract")
    route = importlib.import_module("routes.analysis")


def valid_analysis():
    return {
        "contract_type": None,
        "summary": "A sample agreement.",
        "parties": [],
        "key_terms": {key: None for key in service.KeyTerms.model_fields},
        "obligations": [],
        "risks": [],
        "missing_or_unclear_terms": [],
        "overall_risk_level": "unknown",
        "overall_risk_explanation": "Limited information.",
        "questions_for_review": [],
        "review_limitations": ["Automated review requires verification."],
    }


SAMPLE_CONTRACT = ("7. Indemnity. The Tenant shall indemnify the Landlord against all losses.\n"
                   "8. Rent is payable monthly.")


def sample_result(analysis_type):
    if analysis_type is service.AnalysisType.risk:
        return {
            "overall_risk_level": "high",
            "overall_risk_explanation": "Indemnity is uncapped.",
            "risks": [{
                "title": "Uncapped indemnity", "severity": "high", "basis": "stated_term",
                "affected_party": "Tenant", "clause_reference": "7",
                "evidence": "The Tenant shall indemnify the Landlord against all losses.",
                "explanation": "No limit applies.", "potential_impact": "Large liability.",
                "recommendation": "Cap the indemnity.",
            }],
            "priority_actions": ["Negotiate a cap."],
            "questions_for_review": [],
            "review_limitations": ["Automated review requires verification."],
        }
    if analysis_type is service.AnalysisType.summary:
        return {
            "contract_type": "Lease", "overview": "A lease.", "parties": [],
            "key_obligations": [{"party": None, "description": "Pay rent monthly.", "deadline": None,
                                 "clause_reference": "8", "evidence": "Rent is payable monthly."}],
            "payment_terms": "Monthly.", "important_dates": [{"event": "Rent", "date_or_deadline": "Monthly"}],
            "duration": None, "renewal": None, "termination": None, "governing_law": None,
            "dispute_resolution": None, "other_key_terms": [], "unclear_or_missing_information": [],
            "summary_limitations": ["Automated summary."],
        }
    if analysis_type is service.AnalysisType.clauses:
        return {
            "clauses": [{
                "position": 1, "clause_reference": "7", "heading": "Indemnity",
                "categories": ["indemnity"],
                "text": "7. Indemnity. The Tenant shall indemnify the Landlord against all losses.",
                "summary": "Tenant covers landlord losses.", "parties": ["Tenant", "Landlord"],
                "referenced_clauses": [],
            }],
            "extraction_limitations": [],
        }
    return valid_analysis()


def gemini_response(text, finish_reason="STOP"):
    return {"candidates": [{
        "finishReason": finish_reason,
        "content": {"parts": [{"text": text}]},
    }]}


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.contracts = Mock()
        self.contracts.update_one.return_value.matched_count = 1
        self.analyses = Mock()
        for name, value in (("contracts_collection", self.contracts),
                            ("analysis_collection", self.analyses),
                            ("GEMINI_API_KEY", "test-key"), ("GEMINI_MODEL", "test-model"),
                            ("GEMINI_FALLBACK_MODELS", ["fallback-model"])):
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.contract_id = str(ObjectId())

    def mock_http(self, handler):
        # Each model attempt opens its own client, so create a fresh one per call.
        patcher = patch.object(service.httpx, "AsyncClient", side_effect=lambda **kwargs:
                               HTTP_CLIENT(transport=httpx.MockTransport(handler)))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_overloaded_model_falls_back_and_records_the_model_used(self):
        requested = []

        def handler(request):
            requested.append(request.url.path.split('/')[-1])
            if 'test-model' in request.url.path:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=gemini_response(json.dumps(valid_analysis())))

        self.mock_http(handler)
        result = await service.analyze_contract(self.contract_id, 'Agreement')
        self.assertEqual(requested, ['test-model:generateContent', 'fallback-model:generateContent'])
        self.assertEqual(result['model'], 'fallback-model')

        requested.clear()
        self.mock_http(lambda request: requested.append(1) or httpx.Response(401, json={}))
        with self.assertRaises(HTTPException) as caught:
            await service.analyze_contract(self.contract_id, 'Agreement')
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(len(requested), 1)

    async def test_success_preserves_full_text_and_returns_json_safe_unique_ids(self):
        contract_text = "A" * 16000 + " FINAL TERMINATION CLAUSE"

        def handler(request):
            self.assertEqual(request.headers['x-goog-api-key'], 'test-key')
            payload = json.loads(request.content)
            self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
            self.assertEqual(payload['generationConfig']['responseJsonSchema'],
                             service.ContractAnalysis.model_json_schema())
            self.assertNotIn('responseFormat', payload['generationConfig'])
            self.assertIn(contract_text, payload['contents'][0]['parts'][0]['text'])
            return httpx.Response(200, json=gemini_response(json.dumps(valid_analysis())))

        def insert(document):
            document['_id'] = ObjectId()

        self.analyses.insert_one.side_effect = insert
        ids = []
        for _ in range(2):
            self.mock_http(handler)
            result = await service.analyze_contract(self.contract_id, contract_text)
            json.dumps(result)
            self.assertNotIn('_id', result)
            ids.append(result['analysis_id'])
        self.assertNotEqual(*ids)
        self.assertEqual(self.contracts.update_one.call_args.args[1],
                         {'$set': {'analysis_status': 'completed'}})

    async def test_review_types_use_their_prompt_schema_and_language(self):
        for analysis_type, (prompt, schema) in service.ANALYSES.items():
            with self.subTest(analysis_type=analysis_type):
                sample = sample_result(analysis_type)

                def handler(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload['contents'][0]['parts'][0]['text'],
                                     prompt.format(contract_text=SAMPLE_CONTRACT))
                    self.assertEqual(payload['generationConfig']['responseJsonSchema'],
                                     schema.model_json_schema())
                    self.assertIn('in Hindi', payload['systemInstruction']['parts'][0]['text'])
                    return httpx.Response(200, json=gemini_response(json.dumps(sample)))

                self.mock_http(handler)
                result = await service.analyze_contract(
                    self.contract_id, SAMPLE_CONTRACT, analysis_type.value, 'hindi')
                self.assertEqual(result['analysis_type'], analysis_type.value)
                self.assertEqual(result['language'], 'hindi')
                self.assertEqual(result['analysis'], sample)
                self.assertEqual(result['evidence_check']['unverified'], [])
                self.assertEqual(self.analyses.insert_one.call_args.args[0]['analysis_type'],
                                 analysis_type.value)

    async def test_answer_question(self):
        answer = {"answer_status": "answered", "answer": "Monthly.",
                  "citations": [{"clause_reference": "8", "evidence": "Rent is payable monthly."}],
                  "caveats": ["Automated answer requires verification."]}

        def handler(request):
            payload = json.loads(request.content)
            text = payload['contents'][0]['parts'][0]['text']
            self.assertIn('QUESTION:\nWhen is rent due?\n', text)
            self.assertTrue(text.rstrip().endswith(SAMPLE_CONTRACT))
            self.assertEqual(payload['generationConfig']['responseJsonSchema'],
                             service.ContractAnswer.model_json_schema())
            return httpx.Response(200, json=gemini_response(json.dumps(answer)))

        self.mock_http(handler)
        result = await service.answer_question(SAMPLE_CONTRACT, 'When is rent due?')
        self.assertEqual(result['answer'], answer)
        self.assertEqual(result['language'], 'english')
        self.assertEqual(result['evidence_check']['quotes_verified'], 1)
        self.analyses.insert_one.assert_not_called()

        invalid = {**answer, "answer_status": "maybe"}
        self.mock_http(lambda request: httpx.Response(200, json=gemini_response(json.dumps(invalid))))
        with self.assertRaises(HTTPException) as caught:
            await service.answer_question(SAMPLE_CONTRACT, 'When is rent due?')
        self.assertEqual(caught.exception.status_code, 502)

    async def test_bad_responses_are_not_stored_and_mark_failed(self):
        cases = [
            (200, gemini_response('not json'), 502),
            (200, gemini_response('{}'), 502),
            (200, gemini_response(json.dumps(valid_analysis()), 'MAX_TOKENS'), 502),
            (200, {'promptFeedback': {'blockReason': 'SAFETY'}}, 502),
            (429, {}, 503),
            (401, {}, 502),
            (400, {}, 502),
            (404, {}, 502),
            (503, {}, 503),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, body=body):
                self.mock_http(lambda request: httpx.Response(status, json=body))
                with self.assertRaises(HTTPException) as caught:
                    await service.analyze_contract(self.contract_id, 'Agreement')
                self.assertEqual(caught.exception.status_code, expected)
                self.analyses.insert_one.assert_not_called()
                self.assertEqual(self.contracts.update_one.call_args.args[1],
                                 {'$set': {'analysis_status': 'failed'}})

    async def test_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout('timeout', request=request)
        self.mock_http(handler)
        with self.assertRaises(HTTPException) as caught:
            await service.analyze_contract(self.contract_id, 'Agreement')
        self.assertEqual(caught.exception.status_code, 504)
        self.analyses.insert_one.assert_not_called()

    async def test_database_failure(self):
        self.mock_http(lambda request: httpx.Response(
            200, json=gemini_response(json.dumps(valid_analysis()))))
        self.analyses.insert_one.side_effect = PyMongoError('unavailable')
        with self.assertRaises(HTTPException) as caught:
            await service.analyze_contract(self.contract_id, 'Agreement')
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.contracts.update_one.call_args.args[1],
                         {'$set': {'analysis_status': 'failed'}})

    async def test_invalid_id_empty_text_and_missing_contract(self):
        for contract_id, text, expected in [('invalid', 'Text', 400),
                                             (self.contract_id, '  ', 400)]:
            with self.assertRaises(HTTPException) as caught:
                await service.analyze_contract(contract_id, text)
            self.assertEqual(caught.exception.status_code, expected)
        self.contracts.update_one.assert_not_called()
        self.contracts.update_one.return_value.matched_count = 0
        with self.assertRaises(HTTPException) as caught:
            await service.analyze_contract(self.contract_id, 'Agreement')
        self.assertEqual(caught.exception.status_code, 404)
        self.analyses.insert_one.assert_not_called()


class EvidenceCheckTests(unittest.TestCase):
    def test_quotes_survive_formatting_differences(self):
        contract = ("12. Termina-\ntion: Either party may terminate this\n agreement on "
                    "“thirty (30) days” written notice. Rent is due by the 5th.")
        result = {
            "risks": [
                {"evidence": 'Either party may terminate this agreement on "thirty (30) days" written notice.'},
                {"evidence": "12. Termination: Either party ... written notice"},
                {"evidence": None},
                {"evidence": "The Landlord may enter at any time."},
            ],
            "clauses": [{"text": "RENT IS DUE BY THE 5TH"}],
            "summary": "Rent is due by the 5th.",
        }
        self.assertEqual(service.check_evidence(result, contract), {
            "quotes_checked": 4,
            "quotes_verified": 3,
            "unverified": [{"field": "risks[3].evidence",
                            "quote": "The Landlord may enter at any time."}],
        })

    def test_ellipsis_parts_must_appear_in_order_and_empty_quotes_fail(self):
        check = service.check_evidence({"citations": [
            {"evidence": "the landlord repairs ... the tenant pays"},
            {"evidence": "..."},
        ]}, "First the tenant pays. Then the landlord repairs.")
        self.assertEqual(check["quotes_verified"], 0)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.owner_patch = patch.object(route, 'contracts_collection')
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

    def test_get_analysis(self):
        app = FastAPI()
        app.include_router(route.router)
        app.dependency_overrides[route.get_current_user] = lambda: {"id": "test-user"}
        analysis_id = 'a' * 32
        document = {
            'analysis_id': analysis_id,
            'contract_id': str(ObjectId()),
            'analysis': valid_analysis(),
        }
        with TestClient(app) as client, patch.object(route, 'analysis_collection') as collection:
            collection.find_one.return_value = document
            response = client.get(f'/analysis/{analysis_id}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'analysis': document})
            collection.find_one.assert_called_once_with(
                {'analysis_id': analysis_id}, {'_id': 0})
            collection.find_one.return_value = None
            response = client.get('/analysis/missing')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()['detail'], 'Analysis not found')
            collection.find_one.side_effect = PyMongoError('unavailable')
            self.assertEqual(client.get(f'/analysis/{analysis_id}').status_code, 503)

    def test_route_validation_and_success(self):
        app = FastAPI()
        app.include_router(route.router)
        app.dependency_overrides[route.get_current_user] = lambda: {"id": "test-user"}
        contract_id = str(ObjectId())
        with TestClient(app) as client, patch.object(route, 'contracts_collection') as collection:
            self.assertEqual(client.post('/analysis/analyze/invalid').status_code, 400)
            collection.find_one.return_value = None
            self.assertEqual(client.post(f'/analysis/analyze/{contract_id}').status_code, 404)
            collection.find_one.return_value = {'text_content': ''}
            self.assertEqual(client.post(f'/analysis/analyze/{contract_id}').status_code, 400)
            collection.find_one.return_value = {'text_content': 'Agreement'}
            with patch.object(route, 'analyze_contract', return_value={'analysis_id': 'test'}) as analyze:
                response = client.post(f'/analysis/analyze/{contract_id}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['analysis']['analysis_id'], 'test')
                analyze.assert_awaited_once_with(contract_id, 'Agreement', service.AnalysisType.full,
                                                 service.Language.english)
                analyze.reset_mock()
                client.post(f'/analysis/analyze/{contract_id}?analysis_type=clauses&language=tamil')
                analyze.assert_awaited_once_with(contract_id, 'Agreement', service.AnalysisType.clauses,
                                                 service.Language.tamil)
                for query in ('analysis_type=poem', 'language=french'):
                    self.assertEqual(client.post(f'/analysis/analyze/{contract_id}?{query}').status_code, 422)

    def test_ask_and_list_analyses(self):
        app = FastAPI()
        app.include_router(route.router)
        app.dependency_overrides[route.get_current_user] = lambda: {"id": "test-user"}
        contract_id = str(ObjectId())
        question = 'Can I leave early?'
        with TestClient(app) as client, patch.object(route, 'contracts_collection') as contracts, \
                patch.object(route, 'analysis_collection') as analyses:
            contracts.find_one.return_value = {'text_content': 'Agreement'}
            for body in ({'question': 'x'}, {'question': question, 'language': 'french'}):
                self.assertEqual(client.post(f'/analysis/ask/{contract_id}', json=body).status_code, 422)
            with patch.object(route, 'answer_question', return_value={'answer': {}}) as answer:
                response = client.post(f'/analysis/ask/{contract_id}',
                                       json={'question': question, 'language': 'hindi'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(),
                                 {'contract_id': contract_id, 'question': question, 'answer': {}})
                answer.assert_awaited_once_with('Agreement', question, service.Language.hindi)
            contracts.find_one.assert_called_with({'_id': ObjectId(contract_id), 'owner_id': 'test-user'})

            analyses.find.return_value.sort.return_value = [
                {'analysis_id': 'new', 'analysis_type': 'risk', 'language': 'hindi',
                 'created_at': '2026-10-30', 'model': 'test-model',
                 'analysis': {'overall_risk_level': 'high'}, 'evidence_check': {'quotes_checked': 1}},
                {'analysis_id': 'old', 'created_at': '2026-09-23', 'analysis': {}},
            ]
            items = client.get(f'/analysis/contract/{contract_id}').json()['analyses']
            self.assertEqual([item['analysis_type'] for item in items], ['risk', 'full'])
            self.assertEqual([item['language'] for item in items], ['hindi', 'english'])
            self.assertEqual(items[0]['overall_risk_level'], 'high')
            self.assertEqual(analyses.find.call_args.args[0], {'contract_id': contract_id})
            analyses.find.return_value.sort.assert_called_once_with('created_at', -1)
            contracts.find_one.return_value = None
            self.assertEqual(client.get(f'/analysis/contract/{contract_id}').status_code, 404)
            self.assertEqual(client.post(f'/analysis/ask/{contract_id}',
                                         json={'question': question}).status_code, 404)
            self.assertEqual(client.get('/analysis/contract/invalid').status_code, 400)


if __name__ == '__main__':
    unittest.main()
