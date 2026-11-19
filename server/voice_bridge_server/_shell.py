"""Shared HTML shell for the local development pages.

Both the review page and the debug page are one self-contained document: no
external stylesheet, no build step and no third-party script. This module owns
the document frame, the HTML escaping helper and the security headers, so the
two pages cannot drift apart.

The pages render data with DOM text nodes only. :func:`escape_html` is for the
few values the server writes into the document itself, such as the debug config
strip. The shell does not log and does not read the environment.
"""

from __future__ import annotations

import html

#: CSP directives shared by the response header and the meta tag. No page loads
#: a script, style, font or image from another origin.
CSP_DIRECTIVES = (
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    "script-src 'unsafe-inline'",
    "media-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
)

#: Policy for the meta tag. ``frame-ancestors`` is not valid in a meta policy,
#: so it is only sent in the header.
CSP = "; ".join(CSP_DIRECTIVES)

#: Policy for the ``Content-Security-Policy`` response header.
CSP_HEADER = f"{CSP}; frame-ancestors 'none'"


def escape_html(value: object) -> str:
    """Escape ``value`` for use as HTML text or as an attribute value."""

    return html.escape("" if value is None else str(value), quote=True)


def page_headers(*, csp: bool = True) -> dict[str, str]:
    """Return the response headers that every page carries.

    ``csp=False`` omits the policy header. Both pages set it.
    """

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }
    if csp:
        headers["Content-Security-Policy"] = CSP_HEADER
    return headers


def page_shell(
    title: str, body: str, *, head: str = "", csp: bool = True
) -> str:
    """Return one self-contained HTML document.

    ``head`` and ``body`` are inserted as-is, so a caller escapes any data it
    interpolates. ``csp=True`` adds a meta policy that matches the response
    header, so the policy still applies when the document is saved to disk.
    """

    meta = (
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
        if csp
        else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape_html(title)}</title>\n"
        f"{meta}{head}\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )
