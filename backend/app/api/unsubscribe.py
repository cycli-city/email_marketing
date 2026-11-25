"""The public unsubscribe page (``/u/{token}``) — the one route a stranger is meant to reach.

* ``GET`` shows a confirmation page and changes nothing: mail scanners and link previewers open links, and they must not be
  able to unsubscribe people.
* ``POST`` does it. A mail client's one-click button (RFC 8058, ``List-Unsubscribe=One-Click``) sends exactly this request.

The token is the only credential and carries no personal data. Every failure looks the same (a plain "not valid" page), so the
route can't be used to probe which messages exist. The pages are static HTML with a hash-pinned stylesheet, no script, and a
CSP that forbids everything else.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape
from typing import cast

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.clock import Clock
from app.core.config import Settings
from app.services.outreach.unsubscribe import UnsubscribeLinks, UnsubscribeService

router = APIRouter(tags=["unsubscribe"], include_in_schema=False)
logger = structlog.get_logger("unsubscribe")

_MAX_TOKEN_CHARS = 2000
_STYLE = (
    "body{font:16px/1.55 system-ui,sans-serif;max-width:34rem;margin:4rem auto;padding:0 1.25rem;color:#1a1a1a}"
    "button{font:inherit;padding:.65rem 1.3rem;border:0;border-radius:.4rem;background:#1a1a1a;color:#fff;cursor:pointer}"
    "p.small{color:#555;font-size:.9rem}"
)
_STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
_CSP = (
    f"default-src 'none'; style-src '{_STYLE_HASH}'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
_HEADERS = {
    "Content-Security-Policy": _CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def _page(status_code: int, heading: str, body: str) -> HTMLResponse:
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(heading)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{escape(heading)}</h1>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status_code, headers=_HEADERS)


def _invalid() -> HTMLResponse:
    return _page(
        404,
        "This link is not valid",
        "<p>It may be incomplete or damaged. If you want to stop receiving our e-mails, "
        "reply to any of them with the word <strong>unsubscribe</strong> and we will remove you.</p>",
    )


def _links(request: Request) -> UnsubscribeLinks:
    settings = cast(Settings, request.app.state.settings)
    return UnsubscribeLinks(
        secret=settings.jwt_secret.get_secret_value(),
        backend_url=settings.backend_url,
        clock=cast(Clock, request.app.state.clock),
    )


@router.get("/u/{token}")
async def confirm(token: str, request: Request) -> HTMLResponse:
    if len(token) > _MAX_TOKEN_CHARS or _links(request).resolve(token) is None:
        return _invalid()
    return _page(
        200,
        "Unsubscribe",
        "<p>Press the button to stop receiving e-mails from us. It takes effect immediately.</p>"
        f'<form method="post" action="/u/{escape(token, quote=True)}">'
        '<button type="submit">Unsubscribe me</button></form>'
        '<p class="small">If you did not expect this, you can simply close this page: nothing has changed.</p>',
    )


@router.post("/u/{token}")
async def unsubscribe(token: str, request: Request) -> HTMLResponse:
    target = None if len(token) > _MAX_TOKEN_CHARS else _links(request).resolve(token)
    if target is None:
        return _invalid()
    service = UnsubscribeService(request.app.state.session_factory, request.app.state.clock)
    outcome = await service.unsubscribe(target)
    if outcome is None:
        return _invalid()
    logger.info(
        "unsubscribe.done",
        message_id=str(target.message_id),
        already=outcome.already,
        cancelled=outcome.cancelled_messages,
    )
    return _page(
        200,
        "You are unsubscribed",
        "<p>We have removed you from our list and will not e-mail you again. "
        "Anything still waiting to be sent to you has been cancelled.</p>",
    )
