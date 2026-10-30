import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/auth_tests")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-" * 4)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import jwt
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import auth
from routes import auth as auth_routes, contracts, analysis


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.users = Mock()
        self.records = {}
        def insert(doc):
            if any(u['username'] == doc['username'] for u in self.records.values()):
                raise DuplicateKeyError('duplicate')
            doc['_id'] = ObjectId()
            self.records[doc['_id']] = doc.copy()
            return Mock(inserted_id=doc['_id'])
        def find(query):
            return next((u for u in self.records.values()
                         if all(u.get(k) == v for k, v in query.items())), None)
        self.users.insert_one.side_effect = insert
        self.users.find_one.side_effect = find
        for module in (auth, auth_routes):
            p = patch.object(module, 'users_collection', self.users)
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        for router in (auth_routes.router, contracts.router, analysis.router):
            app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def register_login(self, username='alice'):
        response = self.client.post('/auth/register', json={
            'username': username, 'password': 'correct password'})
        self.assertEqual(response.status_code, 201)
        token = self.client.post('/auth/login', data={
            'username': username, 'password': 'correct password'})
        self.assertEqual(token.status_code, 200)
        self.assertEqual(token.headers['cache-control'], 'no-store')
        return response.json(), {'Authorization': 'Bearer ' + token.json()['access_token']}

    def test_register_login_and_me(self):
        user, headers = self.register_login(' Alice ')
        self.assertEqual(user['username'], 'alice')
        self.assertNotIn('hashed_password', user)
        stored = next(iter(self.records.values()))
        self.assertTrue(stored['hashed_password'].startswith('$argon2'))
        self.assertNotIn('password', stored)
        self.assertEqual(self.client.get('/auth/me', headers=headers).json(), user)
        self.assertEqual(self.client.post('/auth/register', json={
            'username': 'ALICE', 'password': 'other password'}).status_code, 409)

    def test_invalid_credentials_and_registration(self):
        self.register_login()
        for name, password in [('alice', 'wrong'), ('missing', 'wrong')]:
            r = self.client.post('/auth/login', data={'username': name, 'password': password})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.headers['www-authenticate'], 'Bearer')
        for name, password in [('ab', 'long password'), ('bad name', 'long password'),
                               ('valid', 'short')]:
            self.assertEqual(self.client.post('/auth/register', json={
                'username': name, 'password': password}).status_code, 422)

    def test_all_business_routes_require_auth(self):
        for method, path in [('get', '/contracts/'), ('post', '/contracts/upload'),
                             ('get', '/contracts/' + str(ObjectId())),
                             ('get', '/analysis/saved'),
                             ('post', '/analysis/analyze/' + str(ObjectId())),
                             ('post', '/analysis/ask/' + str(ObjectId())),
                             ('get', '/analysis/contract/' + str(ObjectId()))]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 401)

    def test_invalid_expired_tampered_and_deleted_user_tokens(self):
        user, headers = self.register_login()
        now = datetime.now(timezone.utc)
        payload = {'sub': user['id'], 'iat': now, 'exp': now + timedelta(minutes=1)}
        tokens = ['invalid',
                  jwt.encode({**payload, 'exp': now - timedelta(seconds=1)}, auth.JWT_SECRET_KEY, algorithm='HS256'),
                  jwt.encode(payload, 'different-signing-key-' * 3, algorithm='HS256'),
                  jwt.encode({'sub': user['id']}, auth.JWT_SECRET_KEY, algorithm='HS256'),
                  jwt.encode({**payload, 'sub': 'invalid'}, auth.JWT_SECRET_KEY, algorithm='HS256')]
        for token in tokens:
            self.assertEqual(self.client.get('/auth/me', headers={
                'Authorization': 'Bearer ' + token}).status_code, 401)
        self.records.clear()
        self.assertEqual(self.client.get('/auth/me', headers=headers).status_code, 401)

    def test_contract_ownership_and_upload(self):
        user, headers = self.register_login()
        contract_id = ObjectId()
        with patch.object(contracts, 'contracts_collection') as collection:
            collection.find.return_value = []
            self.assertEqual(self.client.get('/contracts/', headers=headers).json(), {'contracts': []})
            collection.find.assert_called_with({'owner_id': user['id']})
            collection.find_one.return_value = None
            self.assertEqual(self.client.get('/contracts/' + str(contract_id), headers=headers).status_code, 404)
            collection.find_one.assert_called_with({'_id': contract_id, 'owner_id': user['id']})
            collection.insert_one.return_value.inserted_id = contract_id
            with tempfile.TemporaryDirectory() as directory, patch.object(contracts, 'UPLOAD_DIR', directory):
                r = self.client.post('/contracts/upload', headers=headers,
                                     files={'file': ('agreement.txt', b'Agreement', 'text/plain')})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(collection.insert_one.call_args.args[0]['owner_id'], user['id'])

    def test_unreadable_or_empty_uploads_are_rejected(self):
        _, headers = self.register_login()
        with patch.object(contracts, 'contracts_collection') as collection, \
                tempfile.TemporaryDirectory() as directory, \
                patch.object(contracts, 'UPLOAD_DIR', directory):
            for name, content, detail in [('broken.pdf', b'not a pdf', 'Could not read'),
                                          ('latin1.txt', 'Café'.encode('latin-1'), 'Could not read'),
                                          ('empty.txt', b'  \n ', 'No text found')]:
                r = self.client.post('/contracts/upload', headers=headers,
                                     files={'file': (name, content, 'application/octet-stream')})
                self.assertEqual(r.status_code, 400)
                self.assertIn(detail, r.json()['detail'])
            self.assertEqual(os.listdir(directory), [])
            collection.insert_one.assert_not_called()

    def test_analysis_ownership(self):
        user, headers = self.register_login()
        contract_id = ObjectId()
        with patch.object(analysis, 'contracts_collection') as collection, \
                patch.object(analysis, 'analysis_collection') as results, \
                patch.object(analysis, 'GEMINI_API_KEY', 'test'), \
                patch.object(analysis, 'analyze_contract') as generate:
            collection.find_one.return_value = None
            self.assertEqual(self.client.post('/analysis/analyze/' + str(contract_id), headers=headers).status_code, 404)
            collection.find_one.assert_called_with({'_id': contract_id, 'owner_id': user['id']})
            generate.assert_not_called()
            results.find_one.return_value = {'analysis_id': 'saved', 'contract_id': str(contract_id)}
            self.assertEqual(self.client.get('/analysis/saved', headers=headers).status_code, 404)
            collection.find_one.assert_called_with({'_id': contract_id, 'owner_id': user['id']}, {'_id': 1})
            collection.find_one.return_value = {'_id': contract_id}
            self.assertEqual(self.client.get('/analysis/saved', headers=headers).status_code, 200)


if __name__ == '__main__':
    unittest.main()
