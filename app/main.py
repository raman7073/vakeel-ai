from database import client, init_db
from fastapi import FastAPI, Depends
from config import validate_auth_config
from auth import get_current_user
from routes.auth import router as auth_router
from contextlib import asynccontextmanager
from routes.contracts import router as contract_router
from routes.analysis import router as analysis_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    print("Starting up: initializing database and tables...")
    validate_auth_config()
    init_db()

    try:
        yield
    finally:
        client.close()

app = FastAPI(
    title="Vakeel Contracts API",
    description="AI Powered contract analysis using gemini",
    lifespan=lifespan
)

app.include_router(contract_router)
app.include_router(analysis_router)
app.include_router(auth_router)


@app.get("/", dependencies=[Depends(get_current_user)])
def read_root():
    return {
        "app": "Vakeel Contract API",

    }
