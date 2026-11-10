import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from database import init_db
from routers import api, oidc, ui
from backup import backup_scheduler
from held import purge_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    tasks = [asyncio.create_task(backup_scheduler()), asyncio.create_task(purge_scheduler())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="E-Mail Relay", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("API_SECRET", "changeme"))
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(api.router)
app.include_router(oidc.router)
app.include_router(ui.router)
