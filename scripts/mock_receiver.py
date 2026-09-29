"""Tiny local destination server for exercising HookRelay deliveries."""

from __future__ import annotations

import argparse
import json
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response


def build_app(status_code: int) -> FastAPI:
    receiver = FastAPI(title="HookRelay mock receiver")

    @receiver.post("/{path:path}")
    async def receive(path: str, request: Request) -> Response:
        payload: Any = await request.json()
        print(json.dumps({"path": f"/{path}", "payload": payload}, indent=2), flush=True)
        return Response(
            content=json.dumps({"accepted": 200 <= status_code < 300}),
            status_code=status_code,
            media_type="application/json",
        )

    return receiver


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local webhook destination")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--status", type=int, default=200)
    args = parser.parse_args()
    uvicorn.run(build_app(args.status), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
