import json
import re
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel, Field, ConfigDict
from processing import close_processing
from streaming import (
    create_session,
    run_background_stream,
    get_session_state,
    control_stream,
    calculate_plan,
)
from visualization import create_map


class EmployeeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str
    workplace_address: str
    shift_end: str = "22:00"
    request_time: str = "22:05"
    home_address: str
    passengers: int = Field(default=1, ge=1, strict=True)


class VehicleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_id: str
    capacity: int = Field(default=4, ge=1, le=20, strict=True)
    start_address: str
    color: str = "red"


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["batch", "stream"] = "batch"
    employees: list[EmployeeInput] = Field(min_length=1, max_length=25)
    vehicles: list[VehicleInput] = Field(min_length=1, max_length=10)


def create_app(output=None, delay=0.8, running=True):
    root = Path(__file__).resolve().parent
    output = Path(output) if output is not None else root / "results" / "live"
    plan_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        session = create_session(root, output, delay, running)
        app.state.session = session
        thread = threading.Thread(
            target=run_background_stream, args=(session,), daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            session["stop"].set()
            thread.join()
            with session["lock"]:
                if session["context"] is not None:
                    close_processing(session["context"])

    app = FastAPI(
        title="Employee Dispatch",
        version="2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def check_origin(request: Request, call_next):
        from fastapi.responses import JSONResponse

        origin = request.headers.get("origin")
        if (
            request.method == "POST"
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def browser():
        return (root / "browser.html").read_text(encoding="utf-8")

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="Employee Dispatch — API"
        )

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/state")
    def state(request: Request):
        return get_session_state(request.app.state.session)

    @app.get("/api/{name}")
    def read_result(
        name: Literal["metrics", "requests", "vehicles", "trips", "events", "example"],
        request: Request,
    ):
        if name == "example":
            return example
        return state(request)[name]

    example = json.loads(
        (root / "data" / "example_request.json").read_text(encoding="utf-8")
    )

    @app.post("/api/optimize", summary="Submit JSON and calculate routes")
    def optimize(payload: PlanInput, request: Request):
        if not plan_lock.acquire(blocking=False):
            raise HTTPException(429, "Another calculation is still running")
        try:
            id = uuid.uuid4().hex
            directory = root / "results" / "plans" / id
            result = calculate_plan(root, directory, payload.model_dump())
            return {
                "plan_id": id,
                "mode": result["mode"],
                "summary": result["summary"],
                "optimization_runs": result["optimization_runs"],
                "routes": result["trips"],
                "requests": result["requests"],
                "map_url": f"/maps/{id}",
                "graph_url": f"/graphs/{id}",
                "note": "OPTIMAL applies to the current model; FEASIBLE is the best feasible plan found. batch uses the complete submitted list; stream respects request arrival times.",
            }
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        finally:
            plan_lock.release()

    @app.post("/api/control/{action}", summary="Control the CSV simulation")
    def control(
        action: Literal["start", "pause", "step", "finish", "reset", "speed"],
        request: Request,
        payload: dict | None = None,
    ):
        try:
            return control_stream(request.app.state.session, action, payload or {})
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/{action}", include_in_schema=False)
    def legacy_control(action: str, request: Request, payload: dict | None = None):
        if action not in ("start", "pause", "step", "finish", "reset", "speed"):
            raise HTTPException(404, "Unknown command")
        return control(action, request, payload)

    @app.get("/map", response_class=HTMLResponse, include_in_schema=False)
    def live_map(request: Request):
        session = request.app.state.session
        with session["lock"]:
            if session["context"] is None:
                return "<p>Input data error. See /api/state.</p>"
            return create_map(
                get_session_state(session), session["context"]["locations"]
            )

    def artifact(id, filename):
        if not re.fullmatch(r"[0-9a-f]{32}", id):
            raise HTTPException(404, "Result not found")
        path = root / "results" / "plans" / id / filename
        if not path.is_file():
            raise HTTPException(404, "Result not found")
        return path

    @app.get("/maps/{id}", include_in_schema=False)
    def plan_map(id: str):
        return FileResponse(artifact(id, "map.html"), media_type="text/html")

    @app.get("/graphs/{id}", include_in_schema=False)
    def plan_graph(id: str):
        return FileResponse(artifact(id, "indicators.png"), media_type="image/png")

    @app.get("/api/example", include_in_schema=False)
    def sample():
        return example

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema["paths"]["/api/optimize"]["post"]["requestBody"]["content"][
            "application/json"
        ]["example"] = example
        return schema

    app.openapi = openapi
    return app


app = create_app()
