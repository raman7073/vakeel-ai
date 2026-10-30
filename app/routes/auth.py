from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError, PyMongoError

from auth import (DUMMY_PASSWORD_HASH, create_access_token, credentials_error,
                  get_current_user, password_hash)
from config import ACCESS_TOKEN_EXPIRE_MINUTES
from database import users_collection

router = APIRouter(prefix="/auth", tags=["authentication"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class UserResponse(BaseModel):
    id: str
    username: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/register", response_model=UserResponse, status_code=201)
def register(data: RegisterRequest):
    user = {"username": data.username,
            "hashed_password": password_hash.hash(data.password),
            "created_at": datetime.now(timezone.utc)}
    try:
        result = users_collection.insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Username already registered") from None
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"id": str(result.inserted_id), "username": data.username}


@router.post("/login", response_model=TokenResponse)
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if len(password) > 128 or len(username) > 50:
        raise credentials_error()
    try:
        user = users_collection.find_one({"username": username.strip().lower()})
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    valid = password_hash.verify(password,
                                 user["hashed_password"] if user else DUMMY_PASSWORD_HASH)
    if not valid or user is None:
        raise credentials_error()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"access_token": create_access_token(str(user["_id"])),
            "token_type": "bearer", "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60}


@router.get("/me", response_model=UserResponse)
def me(user: dict = Depends(get_current_user)):
    return user
