"""Application factory: FastAPI backend + static single-page UI."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router
from .research.pipeline import JobManager
from .storage import Storage

STATIC_DIR = config.BASE_DIR / "static"
_SECRET_PAT = re.compile(r"(gsk_[A-Za-z0-9]{8,}|tvly-[A-Za-z0-9\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


class RedactSecrets(logging.Filter):
    """Defence in depth: scrub anything that looks like an API key from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _SECRET_PAT.search(msg):
            record.msg, record.args = _SECRET_PAT.sub("[REDACTED]", msg), ()
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_rs", False) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(config.data_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(RedactSecrets())
        h._rs = True
        root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(store: Storage | None = None) -> FastAPI:
    setup_logging()
    app = FastAPI(title="Research Synthesist", version="1.0")
    app.state.store = store or Storage()
    app.state.jobs = JobManager(app.state.store)
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
        return JSONResponse(status_code=422, content={"detail": "; ".join(msgs)})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        logging.getLogger(__name__).exception("Unhandled error")
        return JSONResponse(status_code=500, content={"detail": f"Internal error: {type(exc).__name__}"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app

