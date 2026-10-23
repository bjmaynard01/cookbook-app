"""Cache-Control header helpers for auth-sensitive routes."""

_PRIVATE = {"Cache-Control": "private, no-store"}
_PUBLIC = {"Cache-Control": "public, max-age=300, s-maxage=600"}

_NO_CACHE_EXACT = {"/login", "/setup", "/logout", "/healthz", "/readyz"}


def cache_headers_for(path: str, *, behind_login: bool) -> dict[str, str]:
    """Return the correct Cache-Control headers for a request path.

    Auth pages and edit/admin routes are never cached; recipe listings
    and public content may be cached for 5 min client / 10 min proxy.
    """
    if behind_login:
        return dict(_PRIVATE)
    if path in _NO_CACHE_EXACT:
        return dict(_PRIVATE)
    # Exact-match then slash-prefixed so /editorial does not match /edit.
    if path == "/admin" or path.startswith("/admin/"):
        return dict(_PRIVATE)
    if path == "/edit" or path.startswith("/edit/"):
        return dict(_PRIVATE)
    if path == "/recipes/new" or (
        path.startswith("/recipes/") and path.endswith("/edit")
    ):
        return dict(_PRIVATE)
    # Copy the dicts so callers can't mutate the shared module-level values.
    return dict(_PUBLIC)
