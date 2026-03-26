"""
FastAPI health/status server for the worker process.
Runs in a background thread so Railway can:
  - Route HTTP health checks to /health
  - View worker status via /status
  - Monitor session state via /session

Start it by running: uvicorn api:app --host 0.0.0.0 --port 8080
OR let the worker auto-start it (see worker.py start_api_server()).
"""

import os
import threading
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Land Records Worker API", version="1.0.0")

# Shared state — updated by worker.py
worker_state = {
    "status": "starting",
    "session_active": False,
    "last_heartbeat": None,
    "jobs_processed": 0,
    "jobs_failed": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "rabbitmq_connected": False,
}


@app.get("/health")
def health():
    """Railway health check endpoint — always returns 200 if server is alive."""
    return {"status": "ok", "worker": worker_state["status"]}


@app.get("/status")
def status():
    """Detailed worker status."""
    return JSONResponse(content={
        **worker_state,
        "uptime_seconds": (
            datetime.now(timezone.utc) -
            datetime.fromisoformat(worker_state["started_at"])
        ).seconds,
    })


@app.get("/session")
def session_info():
    """Browser session state."""
    return {
        "session_active": worker_state["session_active"],
        "last_heartbeat": worker_state["last_heartbeat"],
        "rabbitmq_connected": worker_state["rabbitmq_connected"],
    }


def start_api_server(port: int = 8080):
    """Start the FastAPI server in a background daemon thread."""
    import uvicorn

    def run():
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",  # keep logs clean alongside worker output
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
