"""Password hashing, JWT issuance, and the shared authentication dependency."""
from datetime import datetime, timedelta, timezone

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from pymongo.errors import PyMongoError

from config import (ACCESS_TOKEN_EXPIRE_MINUTES, JWT_ALGORITHM,
                    JWT_SECRET_KEY, validate_auth_config)
from database import users_collection

password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hash.hash("unused-dummy-password")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def credentials_error():
    return HTTPException(status_code=401, detail="Could not validate credentials",
                         headers={"WWW-Authenticate": "Bearer"})


def create_access_token(user_id: str):
    validate_auth_config()
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "iat": now,
                       "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
                      JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    validate_auth_config()
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM],
                             options={"require": ["sub", "exp", "iat"]})
        user_id = payload["sub"]
        if not isinstance(user_id, str) or not ObjectId.is_valid(user_id):
            raise credentials_error()
    except InvalidTokenError:
        raise credentials_error() from None
    try:
        user = users_collection.find_one({"_id": ObjectId(user_id)})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    if user is None:
        raise credentials_error()
    return {"id": str(user["_id"]), "username": user["username"]}
