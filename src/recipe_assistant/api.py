"""Authenticated HTTP interface to the local database."""

import logging
import secrets
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import Database, SchemaError
from .models import MealPlan, Recipe, RecipePage, ShoppingPreview, Status

LOGGER = logging.getLogger(__name__)


def create_app(database: Database, token: str) -> FastAPI:
    if len(token) < 32 or token == "replace-with-a-long-random-token":
        raise ValueError("Set RECIPE_ASSISTANT_TOKEN to at least 32 random characters")
    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title="Recipe Assistant API",
        version="0.1.0",
        description="Read-only access to this Mac's synced Recipe Keeper data.",
        dependencies=[Depends(authenticate)],
    )

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def unavailable(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.warning("Database unavailable (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Recipe Keeper database unavailable or unsupported; "
                "run recipe-assistant inspect on the Mac"
            },
        )

    for error in (sqlite3.Error, OSError, SchemaError):
        app.add_exception_handler(error, unavailable)

    def date_range(start: date, end: date) -> tuple[date, date]:
        if end < start or (end - start).days > 365 or end == date.max:
            raise HTTPException(422, "Use an ordered date range of at most 366 days")
        return start, end

    dates = Annotated[tuple[date, date], Depends(date_range)]

    @app.get("/v1/status", response_model=Status)
    def status() -> Status:
        return database.status()

    @app.get("/v1/recipes", response_model=RecipePage)
    def recipes(
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RecipePage:
        return database.search(q, limit, offset)

    @app.get("/v1/recipes/{recipe_id}", response_model=Recipe)
    def recipe(recipe_id: str) -> Recipe:
        result = database.recipe(recipe_id)
        if result is None:
            raise HTTPException(404, "Recipe not found")
        return result

    @app.get("/v1/meal-plan", response_model=MealPlan)
    def meal_plan(bounds: dates) -> MealPlan:
        return database.meal_plan(*bounds)

    @app.get("/v1/shopping-preview", response_model=ShoppingPreview)
    def shopping_preview(bounds: dates) -> ShoppingPreview:
        return database.shopping_preview(*bounds)

    return app
