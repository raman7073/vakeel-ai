# Authentication

The API supports username/password registration and login. All existing business
routes (including `/`) and `/auth/me` require a bearer token. `/auth/register`,
`/auth/login`, `/docs`, `/redoc`, and `/openapi.json` remain public.

## Run locally

The authentication dependencies are `PyJWT` and `pwdlib[argon2]`; MongoDB uses
`pymongo`. They are recorded in `requirements.txt`.

Configure `app/.env` using `app/.env.example` as a reference. Keep your existing
MongoDB and Google AI settings. `JWT_SECRET_KEY` must be a random secret of at
least 32 bytes. Generate one when setting up a new environment:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Save it as `JWT_SECRET_KEY=...` in `app/.env`; never commit that file. Use the same
secret for all server workers. Replacing the secret invalidates existing tokens.
Startup validates the secret and creates the unique MongoDB username index.

From the project root:

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir app --reload
```

## Try it in Swagger UI

1. Open `http://127.0.0.1:8000/docs`.
2. Call `POST /auth/register` with JSON:
   `{"username":"alice","password":"a-long-unique-password"}`.
3. Click **Authorize**, enter those credentials, and authorize. Swagger sends
   form data to `/auth/login` and attaches the returned token to protected calls.
4. Call `/auth/me`, upload a contract, and list or analyze your contracts.

Usernames are trimmed and lowercased, with 3–50 letters, digits, underscores,
dots, or hyphens. Passwords must contain 8–128 characters and are not trimmed.
Registration returns the user ID and username, never a password hash.
Duplicate usernames return 409. Invalid input returns 422.

For a separate frontend, send `application/x-www-form-urlencoded` data with
`username` and `password` to `POST /auth/login`. The response is:

```json
{"access_token":"<JWT>","token_type":"bearer","expires_in":1800}
```

Attach the token on subsequent requests:

```http
Authorization: Bearer <JWT>
```

## How JWT works in this FastAPI app

1. **Registration:** `app/routes/auth.py` hashes the password with Argon2 and
   stores the hash in MongoDB's `users` collection. The database's unique index
   prevents duplicate usernames, including simultaneous registration attempts.
2. **Login:** the route verifies the supplied password against that hash.
   `create_access_token()` in `app/auth.py` signs a JWT using HS256 and the
   server-only secret. Claims include `sub` (the user's MongoDB ID), `iat`
   (issued-at time), and `exp` (expiry, 30 minutes later).
3. **Extract token:** FastAPI's `OAuth2PasswordBearer` reads the bearer header
   and declares the login flow in OpenAPI. It does not itself validate the JWT.
4. **Validate:** `get_current_user()` verifies the signature using a fixed
   allowed algorithm, requires the claims, checks expiry and subject format,
   and loads the user from MongoDB. Missing, invalid, expired, or deleted-user
   credentials return 401 with `WWW-Authenticate: Bearer`.
5. **Protect endpoints:** `Depends(get_current_user)` runs before the endpoint.
   It supplies the verified user's ID for ownership checks. FastAPI caches the
   dependency result within the request, so router and endpoint dependencies
   share the same lookup.

A JWT consists of `header.payload.signature`. Its contents are encoded and
readable, not encrypted. The signature detects tampering; no password or secret
is included in the payload. See the
[official FastAPI JWT tutorial](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/).

## Ownership and token lifetime

Uploads receive an `owner_id` from the verified user, never from client input.
Listing, fetching, and analyzing contracts filter by that owner. Retrieving an
analysis checks ownership of its associated contract. Another user's records
return 404. Existing contracts with no `owner_id` remain hidden; assign each to
the correct user's string ID through a deliberate database migration if needed.

There is no refresh-token or logout/revocation endpoint. Log in again after
expiry. Removing a token from the frontend logs out locally, but a copied token
remains usable until expiry (or user deletion/signing-key rotation). Use HTTPS
when deploying and add login/registration rate limits at the application gateway.

## Tests

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use mocked database collections and AI requests, covering hashing,
registration, login, token validation, protected routes, and ownership filters.
They do not require or modify a live MongoDB database.
