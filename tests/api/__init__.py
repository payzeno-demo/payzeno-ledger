"""HTTP surface tests — one module per api-surface.md §10 group.

The app is built by `app.main.create_app` with the container's dependencies overridden
(tests/api/conftest.py), so routing, `response_model=` coercion, the middleware chain and
`app/api/error_handlers.py`'s single exception handler are all real. Only the services
below the routers are doubles.
"""
