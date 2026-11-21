import asyncio
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

PROBE_TOKEN = os.getenv("PROBE_TOKEN", "").strip()
DEFAULT_TIMEOUT_MS = int(os.getenv("DEFAULT_TIMEOUT_MS", "3000"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "50"))

app = FastAPI(title="MSK Node Probe Agent", version="1.1.0")


class ProbeTarget(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    timeout_ms: int | None = Field(default=None, ge=100, le=30000)


class ProbeRequest(BaseModel):
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=100, le=30000)
    targets: list[ProbeTarget] = Field(default_factory=list, max_length=500)


class ProbeResultItem(BaseModel):
    id: str
    reachable: bool
    latency_ms: int | None
    error: str | None
    status: Literal["ok", "timeout", "unreachable"]


class ProbeResponse(BaseModel):
    probe_checked_at_utc: str
    results: list[ProbeResultItem]


class HttpProbeTarget(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    url: str = Field(..., min_length=1, max_length=2048)
    expected_status: int = Field(default=200, ge=100, le=599)
    verify_ssl: bool = Field(default=True)
    timeout_ms: int | None = Field(default=None, ge=100, le=30000)


class HttpProbeRequest(BaseModel):
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=100, le=30000)
    targets: list[HttpProbeTarget] = Field(default_factory=list, max_length=500)


class HttpProbeResultItem(BaseModel):
    id: str
    reachable: bool
    http_status: int | None
    latency_ms: int | None
    error: str | None
    status: Literal["ok", "http_error", "timeout", "unreachable"]


class HttpProbeResponse(BaseModel):
    probe_checked_at_utc: str
    results: list[HttpProbeResultItem]


def verify_token(x_probe_token: str | None = Header(default=None, alias="X-Probe-Token")) -> None:
    if not PROBE_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Probe agent token is not configured",
        )
    if not x_probe_token or not secrets.compare_digest(x_probe_token, PROBE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid probe token",
        )


async def _check_tcp_target(
    target: ProbeTarget,
    fallback_timeout_ms: int,
    sem: asyncio.Semaphore,
) -> ProbeResultItem:
    timeout_sec = (target.timeout_ms or fallback_timeout_ms) / 1000.0
    start = time.perf_counter()

    async with sem:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host=target.host, port=target.port),
                timeout=timeout_sec,
            )
            elapsed_ms = max(1, round((time.perf_counter() - start) * 1000))
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return ProbeResultItem(
                id=target.id,
                reachable=True,
                latency_ms=elapsed_ms,
                error=None,
                status="ok",
            )
        except asyncio.TimeoutError:
            return ProbeResultItem(
                id=target.id,
                reachable=False,
                latency_ms=None,
                error="timeout",
                status="timeout",
            )
        except OSError as exc:
            err_text = exc.strerror or str(exc) or "connection error"
            return ProbeResultItem(
                id=target.id,
                reachable=False,
                latency_ms=None,
                error=err_text[:200],
                status="unreachable",
            )
        except Exception as exc:
            return ProbeResultItem(
                id=target.id,
                reachable=False,
                latency_ms=None,
                error=(str(exc) or "unexpected error")[:200],
                status="unreachable",
            )


async def _check_http_target(
    target: HttpProbeTarget,
    fallback_timeout_ms: int,
    sem: asyncio.Semaphore,
) -> HttpProbeResultItem:
    timeout_sec = (target.timeout_ms or fallback_timeout_ms) / 1000.0
    start = time.perf_counter()

    async with sem:
        try:
            async with httpx.AsyncClient(timeout=timeout_sec, verify=target.verify_ssl, follow_redirects=False) as client:
                resp = await client.get(target.url)
            elapsed_ms = max(1, round((time.perf_counter() - start) * 1000))
            is_ok = resp.status_code == target.expected_status
            return HttpProbeResultItem(
                id=target.id,
                reachable=is_ok,
                http_status=resp.status_code,
                latency_ms=elapsed_ms,
                error=None if is_ok else f"expected {target.expected_status}, got {resp.status_code}",
                status="ok" if is_ok else "http_error",
            )
        except httpx.TimeoutException:
            return HttpProbeResultItem(
                id=target.id,
                reachable=False,
                http_status=None,
                latency_ms=None,
                error="timeout",
                status="timeout",
            )
        except httpx.HTTPError as exc:
            err_text = str(exc) or exc.__class__.__name__
            return HttpProbeResultItem(
                id=target.id,
                reachable=False,
                http_status=None,
                latency_ms=None,
                error=err_text[:200],
                status="unreachable",
            )
        except Exception as exc:
            return HttpProbeResultItem(
                id=target.id,
                reachable=False,
                http_status=None,
                latency_ms=None,
                error=(str(exc) or "unexpected error")[:200],
                status="unreachable",
            )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.post("/probe", response_model=ProbeResponse, dependencies=[Depends(verify_token)])
async def probe_targets(payload: ProbeRequest) -> ProbeResponse:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not payload.targets:
        return ProbeResponse(probe_checked_at_utc=checked_at, results=[])

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [_check_tcp_target(target, payload.timeout_ms, sem) for target in payload.targets]
    results = await asyncio.gather(*tasks)
    return ProbeResponse(probe_checked_at_utc=checked_at, results=list(results))


@app.post("/probe-http", response_model=HttpProbeResponse, dependencies=[Depends(verify_token)])
async def probe_http_targets(payload: HttpProbeRequest) -> HttpProbeResponse:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not payload.targets:
        return HttpProbeResponse(probe_checked_at_utc=checked_at, results=[])

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [_check_http_target(target, payload.timeout_ms, sem) for target in payload.targets]
    results = await asyncio.gather(*tasks)
    return HttpProbeResponse(probe_checked_at_utc=checked_at, results=list(results))