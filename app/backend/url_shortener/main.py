from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from url_shortener.api.dependencies import get_analytics_service, get_pool_service
from url_shortener.api.routes import router
from url_shortener.storage.database import get_database


def create_app() -> FastAPI:
    app = FastAPI(title="URL Shortener", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if not frontend_dir.exists():
        frontend_dir = Path("/app/frontend")

    @app.get("/auth.html", include_in_schema=False)
    def auth_page():
        return FileResponse(frontend_dir / "auth.html")

    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    @app.on_event("startup")
    def startup() -> None:
        get_database().create_all()
        get_pool_service().initialize_runtime()
        if get_analytics_service().settings.run_analytics_worker_in_api:
            get_analytics_service().start_worker()

    @app.on_event("shutdown")
    def shutdown() -> None:
        if get_analytics_service().settings.run_analytics_worker_in_api:
            get_analytics_service().stop_worker()

    return app


app = create_app()
