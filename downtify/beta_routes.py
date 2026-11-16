"""Routes for beta-build error reports: `POST /api/beta/reports` and the admin's reads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from . import api
from .beta_reports import (
    MAX_REPORT_BYTES,
    clean_install_id,
    decode_body,
    looks_like_report,
)

router = APIRouter()


def _store():
    store = getattr(api.state, 'beta_reports', None)
    if store is None:
        raise HTTPException(status_code=503, detail='Starting up')
    return store


def _client_ip(request: Request) -> str:
    # A reverse proxy in front hands the real address in the first hop.
    forwarded = request.headers.get('x-forwarded-for', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:64]
    return (request.client.host if request.client else '')[:64]


@router.post('/api/beta/reports')
async def post_beta_report(request: Request) -> dict[str, Any]:
    """Accept one report from a beta build.

    Public on purpose: a crash during pairing is exactly what nobody reports by
    hand. The body is JSON, optionally gzipped (`Content-Encoding: gzip`), capped
    at :data:`MAX_REPORT_BYTES`, and each address gets a handful per window.
    """

    store = _store()
    ip = _client_ip(request)
    if not store.allowed(ip):
        raise HTTPException(
            status_code=429,
            detail='Too many reports; try again later',
            headers={'Retry-After': '600'},
        )
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail='Empty report')
    if len(raw) > MAX_REPORT_BYTES * 2:
        raise HTTPException(status_code=413, detail='Report too large')
    payload = decode_body(raw, request.headers.get('content-encoding', ''))
    if payload is None:
        raise HTTPException(status_code=400, detail='Malformed report')
    if not looks_like_report(payload):
        raise HTTPException(
            status_code=400, detail='A report needs a message, a stack or a note'
        )
    payload['install_id'] = clean_install_id(payload.get('install_id'))
    principal = api.principal_of(request)
    user_id = api.request_user_id(request) or 0
    device_id = principal.device_id if principal is not None else ''
    report_id = store.store(
        payload,
        ip=ip,
        user_id=user_id,
        device_id=device_id or '',
        size=len(raw),
    )
    return {'accepted': True, 'id': report_id}


@router.get('/api/beta/reports')
async def list_beta_reports(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """The latest reports (admins only - enforced by the auth rules)."""

    store = _store()
    return {
        'summary': store.summary(),
        'reports': store.list(limit),
    }


@router.delete('/api/beta/reports')
async def clear_beta_reports() -> dict[str, Any]:
    """Drop every stored report (admins only)."""

    return {'deleted': _store().clear()}
