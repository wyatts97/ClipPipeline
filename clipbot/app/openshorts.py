"""Minimal client for a self-hosted OpenShorts backend (no API key in self-host mode).

Endpoints used (OpenShorts app.py): POST /api/process, POST /api/uploads,
PUT /api/uploads/{id}, GET /api/status/{job_id}, GET /videos/{job_id}/{file}.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx


class OpenShortsError(RuntimeError):
    pass


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail:
            return str(detail)
    except ValueError:
        pass
    return resp.text[:300]


class OpenShorts:
    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, read=600))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def process(self, *, url: Optional[str] = None, upload_id: Optional[str] = None,
                      options: dict[str, Any]) -> str:
        body: dict[str, Any] = {"acknowledged": True, **options}
        if url:
            body["url"] = url
        if upload_id:
            body["upload_id"] = upload_id
        resp = await self._client.post(f"{self.base_url}/api/process", json=body)
        if resp.status_code >= 400:
            raise OpenShortsError(f"OpenShorts refused the job ({resp.status_code}): {_detail(resp)}")
        data = resp.json()
        if data.get("needs_confirmation"):
            height = (data.get("quality_check") or {}).get("max_height")
            raise OpenShortsError(
                f"Source is only {height}p; OpenShorts' quality gate wants confirmation "
                "(set QUALITY_GATE_MIN_HEIGHT=0 for the openshorts service).")
        job_id = data.get("job_id")
        if not job_id:
            raise OpenShortsError(f"OpenShorts answered without a job_id: {data}")
        return job_id

    async def upload_file(self, path: Path) -> str:
        resp = await self._client.post(f"{self.base_url}/api/uploads", json={"filename": path.name})
        if resp.status_code >= 400:
            raise OpenShortsError(f"Upload slot refused ({resp.status_code}): {_detail(resp)}")
        upload_id = resp.json()["upload_id"]

        async def chunks():
            with path.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    yield chunk

        # Build the PUT URL ourselves: the one OpenShorts returns may use its public base URL.
        resp = await self._client.put(f"{self.base_url}/api/uploads/{upload_id}", content=chunks(),
                                      headers={"Content-Type": "application/octet-stream"})
        if resp.status_code >= 400:
            raise OpenShortsError(f"Upload failed ({resp.status_code}): {_detail(resp)}")
        return upload_id

    async def status(self, job_id: str) -> Optional[dict[str, Any]]:
        """The job's status document, or None when OpenShorts no longer knows it."""
        resp = await self._client.get(f"{self.base_url}/api/status/{job_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise OpenShortsError(f"Status check failed ({resp.status_code}): {_detail(resp)}")
        return resp.json()

    async def download(self, video_url: str, dst: Path) -> None:
        url = video_url if video_url.startswith("http") else f"{self.base_url}{video_url}"
        async with self._client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise OpenShortsError(f"Clip download failed ({resp.status_code}) for {video_url}")
            with dst.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    fh.write(chunk)
