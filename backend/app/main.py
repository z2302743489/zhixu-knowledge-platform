import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from .config import settings
from .db import init_db, engine
from .providers import ProviderError
from .routes_management import router as management_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from .chat import recover_interrupted_chats
    recover_interrupted_chats()
    from .tasks import recover_local_jobs
    await asyncio.to_thread(recover_local_jobs)
    try:
        yield
    finally:
        from . import retrieval
        if retrieval._client is not None:
            retrieval._client.close()
            retrieval._client = None


app = FastAPI(title="知序 · 企业知识中台 API", version="1.0.0", lifespan=lifespan,
              description="企业知识导入、检索与问答。当前角色为可自由切换的操作模式，非真实身份认证。")
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",") if x.strip()],
                   allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-Role"])


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default error output includes rejected input, possibly API keys.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": str(error["msg"]), "type": error["type"]}
        for error in exc.errors()
    ]})


@app.middleware("http")
async def browser_identity(request: Request, call_next):
    cookie = request.cookies.get("zhixu_session", "")
    try:
        browser_id = str(UUID(cookie))
    except (ValueError, AttributeError):
        browser_id = str(uuid4())
    request.state.owner = browser_id
    response = await call_next(request)
    if cookie != browser_id:
        response.set_cookie("zhixu_session", browser_id, httponly=True, secure=settings.cookie_secure,
                            samesite="lax", max_age=365*24*3600, path="/")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ProviderError)
async def provider_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def invalid_configuration(request, exc):
    return JSONResponse(status_code=422, content={"detail": "服务配置或请求数据无效，请检查地址、加密密钥和参数"})


@app.get("/api/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "version": "1.0.0", "mode": "live", "taskMode": settings.task_mode,
            "authentication": "role-selection-only"}


app.include_router(management_router)
from .routes_documents import router as documents_router
from .chat import router as chat_router
app.include_router(documents_router)
app.include_router(chat_router)
