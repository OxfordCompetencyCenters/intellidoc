"""
Container health endpoints.

Three things poll these: the Dockerfile.prod HEALTHCHECK, the
docker-compose.prod.yml healthcheck (which gates `depends_on`), and
scripts/production.sh while it waits for the backend to come up.

Deliberately NOT reusing vector_search.api_views.health_check: that one
requires authentication (a probe would get 401) and reports on Milvus, so a
Milvus blip would mark the whole backend unhealthy.

Both views are unauthenticated on purpose, and neither discloses anything an
anonymous caller could not already infer. Keep it that way — a health endpoint
that leaks versions or settings is a reconnaissance gift.

Note: these are served through Django's normal request path, so the probe's
Host header must be in ALLOWED_HOSTS. The probes use localhost, which is
present in the settings.py default and in .env.example. Do not remove it.
"""

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def health(request):
    """
    Liveness: is this process up and serving WSGI requests?

    No external dependencies, so it stays green while a dependency is briefly
    down and the container is not killed for something it cannot fix.
    """
    return JsonResponse({'status': 'ok'})


@never_cache
@require_GET
def health_ready(request):
    """
    Readiness: can this process actually serve traffic?

    Checks PostgreSQL, because the application can serve nothing useful
    without it. Milvus and Redis are excluded on purpose: document search
    degrades without them, but the rest of the site works, so they must not be
    able to take the backend out of rotation.

    503 on failure, so an orchestrator can hold traffic back.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:
        # The class name alone is enough to tell apart "cannot connect" from
        # "authentication failed" in the logs, without echoing credentials or
        # connection strings back to an anonymous caller.
        return JsonResponse(
            {'status': 'unavailable', 'database': type(exc).__name__},
            status=503,
        )

    return JsonResponse({'status': 'ready', 'database': 'ok'})
