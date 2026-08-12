from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from memory_agent.api import router
from memory_agent.bootstrap import ensure_local_identity
from memory_agent.config import get_settings
from memory_agent.database import SessionLocal, create_schema_for_development


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.auto_create_schema:
        create_schema_for_development()
    if settings.auth_mode == "local":
        session = SessionLocal()
        try:
            ensure_local_identity(session, settings)
        finally:
            session.close()
    yield


app = FastAPI(
    title="Memory Agent MVP API",
    version="0.1.0",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


def run() -> None:
    uvicorn.run("memory_agent.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
