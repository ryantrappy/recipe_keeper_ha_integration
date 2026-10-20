"""Authenticated recipe reads and explicitly enabled cloud edits."""

import asyncio
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .cloud_backend import CacheUnavailable, CloudDatabase, SyncBusy, SyncWorker
from .cloud_edits import (
    CloudEditor,
    EditConflict,
    EditingDisabled,
    EntityNotFound,
)
from .cloud_probe import ProbeError
from .database import Database, SchemaError
from .models import (
    DeleteRequest,
    MealCreateRequest,
    MealPlan,
    MealUpdateRequest,
    Recipe,
    RecipeCreateRequest,
    RecipePage,
    RecipeUpdateRequest,
    ShoppingPreview,
    ShoppingSelectionRequest,
    Status,
)
from .shopping import SelectionChangedError, select_ingredients

LOGGER = logging.getLogger(__name__)


def create_app(
    database: Database,
    token: str,
    worker: SyncWorker | None = None,
    pantry_staples: tuple[str, ...] = (),
    editor: CloudEditor | None = None,
) -> FastAPI:
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await asyncio.to_thread(worker.stop)

    app = FastAPI(
        title="Recipe Assistant API",
        version="0.1.0",
        description="Recipe Keeper access with optional cloud editing.",
        dependencies=[Depends(authenticate)],
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if isinstance(database, CloudDatabase):
            try:
                state = await asyncio.to_thread(database.status)
                response.headers["X-Recipe-Assistant-Stale"] = str(state.stale).lower()
                if state.last_successful_sync_at is not None:
                    response.headers["X-Recipe-Assistant-Last-Sync"] = (
                        state.last_successful_sync_at.isoformat()
                    )
            except (sqlite3.Error, OSError, SchemaError):
                response.headers["X-Recipe-Assistant-Stale"] = "true"
        return response

    async def unavailable(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.warning("Database unavailable (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Recipe Keeper database unavailable or unsupported; "
                "run recipe-assistant inspect for the configured backend"
            },
        )

    for error in (sqlite3.Error, OSError, SchemaError, CacheUnavailable):
        app.add_exception_handler(error, unavailable)

    async def edit_error(request: Request, exc: Exception) -> JSONResponse:
        code, message = {
            EditingDisabled: (403, "Recipe Keeper editing is disabled"),
            EditConflict: (409, str(exc)),
            EntityNotFound: (404, str(exc)),
            ProbeError: (503, "Cloud refresh failed; cached data was preserved"),
            SyncBusy: (409, "Cloud sync is busy; retry later"),
        }.get(type(exc), (503, "Cloud operation unavailable"))
        return JSONResponse(status_code=code, content={"detail": message})

    for error in (EditingDisabled, EditConflict, EntityNotFound, ProbeError, SyncBusy):
        app.add_exception_handler(error, edit_error)

    def cloud_editor() -> CloudEditor:
        if editor is None or not isinstance(database, CloudDatabase):
            raise EditingDisabled("Recipe Keeper editing is disabled")
        return editor

    def require_edits() -> None:
        if not cloud_editor().enabled:
            raise EditingDisabled("Recipe Keeper editing is disabled")

    write_guard = [Depends(require_edits)]

    def edit_result(result: dict) -> JSONResponse:
        code = (
            200
            if result["state"] in ("confirmed", "cancelled")
            else 409
            if result["state"] == "conflict"
            else 202
        )
        return JSONResponse(status_code=code, content=result)

    def date_range(start: date, end: date) -> tuple[date, date]:
        if end < start or (end - start).days > 365 or end == date.max:
            raise HTTPException(422, "Use an ordered date range of at most 366 days")
        return start, end

    dates = Annotated[tuple[date, date], Depends(date_range)]

    @app.get("/v1/status", response_model=Status)
    def status() -> Status:
        result = database.status()
        if editor is not None and isinstance(database, CloudDatabase):
            result.allow_edits = editor.enabled
            result.edit_operations = editor.counts()
        return result

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
        return select_ingredients(database.shopping_preview(*bounds), pantry_staples)

    @app.post("/v1/shopping-preview", response_model=ShoppingPreview)
    def shopping_selection(selection: ShoppingSelectionRequest) -> ShoppingPreview:
        start, end = date_range(selection.start, selection.end)
        try:
            return select_ingredients(
                database.shopping_preview(start, end),
                pantry_staples,
                selection.selected_keys,
            )
        except SelectionChangedError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/recipes/{recipe_id}/editing")
    def recipe_editing(recipe_id: str) -> dict:
        return cloud_editor().view("Recipe", recipe_id)

    @app.get("/v1/meals/{meal_id}/editing")
    def meal_editing(meal_id: str) -> dict:
        return cloud_editor().view("Meal", meal_id)

    @app.post("/v1/recipes", dependencies=write_guard)
    def create_recipe(body: RecipeCreateRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Recipe",
                "create",
                str(body.operation_id),
                body.changes.model_dump(mode="json", exclude_unset=True),
            )
        )

    @app.patch("/v1/recipes/{recipe_id}", dependencies=write_guard)
    def update_recipe(recipe_id: str, body: RecipeUpdateRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Recipe",
                "update",
                str(body.operation_id),
                body.changes.model_dump(mode="json", exclude_unset=True),
                entity_id=recipe_id,
                expected_revision=body.revision,
            )
        )

    @app.delete("/v1/recipes/{recipe_id}", dependencies=write_guard)
    def delete_recipe(recipe_id: str, body: DeleteRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Recipe",
                "delete",
                str(body.operation_id),
                {},
                entity_id=recipe_id,
                expected_revision=body.revision,
            )
        )

    @app.post("/v1/meals", dependencies=write_guard)
    def create_meal(body: MealCreateRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Meal",
                "create",
                str(body.operation_id),
                body.changes.model_dump(mode="json", exclude_unset=True),
            )
        )

    @app.patch("/v1/meals/{meal_id}", dependencies=write_guard)
    def update_meal(meal_id: str, body: MealUpdateRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Meal",
                "update",
                str(body.operation_id),
                body.changes.model_dump(mode="json", exclude_unset=True),
                entity_id=meal_id,
                expected_revision=body.revision,
            )
        )

    @app.delete("/v1/meals/{meal_id}", dependencies=write_guard)
    def delete_meal(meal_id: str, body: DeleteRequest) -> JSONResponse:
        return edit_result(
            cloud_editor().edit(
                "Meal",
                "delete",
                str(body.operation_id),
                {},
                entity_id=meal_id,
                expected_revision=body.revision,
            )
        )

    @app.get("/v1/edit-operations/{operation_id}")
    def operation(operation_id: str) -> dict:
        result = cloud_editor().operation(operation_id)
        if result is None:
            raise EntityNotFound("Operation not found")
        return result

    @app.post("/v1/edit-operations/{operation_id}/reconcile")
    def reconcile(operation_id: str) -> JSONResponse:
        return edit_result(cloud_editor().reconcile(operation_id))

    return app
