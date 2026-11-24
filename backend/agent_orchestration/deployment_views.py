"""
Workflow Deployment API Views
Management endpoints for deployments and public-facing chat endpoint.

Deployment / chat limitations (see DEPLOYMENT_CHAT_LIMITATIONS.md in project root):
- No user file upload in embed chat; attachments come from workflow config only.
- Streaming is simulated (full response then word-by-word, sent as fast as the
  connection allows — no artificial per-word delay); not true LLM token streaming.
- Concurrent requests with the same session_id can overwrite turns; no per-session locking.
"""
import logging
import json
import os
import mimetypes
import queue as queue_mod
import uuid
import hashlib
import threading
from datetime import datetime
from typing import Dict, Any, Optional

from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.shortcuts import get_object_or_404
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.utils import DataError
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from asgiref.sync import sync_to_async
import asyncio

from users.models import IntelliDocProject, AgentWorkflow
from .models import (
    WorkflowDeployment,
    WorkflowAllowedOrigin,
    WorkflowDeploymentRequest,
    WorkflowDeploymentRequestStatus,
    DeploymentSession,
    DeploymentSessionFile,
    DeploymentExecution,
    DeploymentPublicLoginAttempt,
)
from .deployment_executor import WorkflowDeploymentExecutor
from .deployment_rate_limiter import WorkflowDeploymentRateLimiter

logger = logging.getLogger('workflow_deployment')


# ── Public Chat URL — cookie + gate helpers ────────────────────────────
# These credentials are STRICTLY scoped to the public chat endpoint of
# one specific deployment. The cookie is path-scoped (browser will only
# send it to /api/workflow-deploy/{project_id}/), name-namespaced per
# deployment, signed with SECRET_KEY, and version-bound to the current
# password so rotation invalidates every live login. No Django User row
# is ever created.
PUBLIC_CHAT_COOKIE_MAX_AGE = 4 * 3600  # 4 h rolling
PUBLIC_CHAT_LOGIN_RL_WINDOW = 15 * 60  # 15 min brute-force window
PUBLIC_CHAT_LOGIN_RL_ATTEMPTS = 10     # attempts allowed in the window


def _public_chat_cookie_name(deployment) -> str:
    return f'pchat_{deployment.id}'


def _public_chat_cookie_path(deployment) -> str:
    return f'/api/workflow-deploy/{deployment.project.project_id}/'


def _set_public_chat_cookie(response, deployment) -> None:
    """Attach the signed auth cookie so subsequent same-origin XHRs from the
    iframe carry it automatically. Rewritten on every authenticated call so
    activity refreshes the 4 h TTL."""
    signer = TimestampSigner()
    payload = f'{deployment.id}:{deployment.public_url_password_version}'
    value = signer.sign(payload)
    response.set_cookie(
        _public_chat_cookie_name(deployment),
        value,
        max_age=PUBLIC_CHAT_COOKIE_MAX_AGE,
        path=_public_chat_cookie_path(deployment),
        httponly=True,
        samesite='Lax',
        secure=not settings.DEBUG,
    )


def _clear_public_chat_cookie(response, deployment) -> None:
    response.delete_cookie(
        _public_chat_cookie_name(deployment),
        path=_public_chat_cookie_path(deployment),
    )


def _verify_public_chat_cookie(request, deployment) -> bool:
    raw = request.COOKIES.get(_public_chat_cookie_name(deployment))
    if not raw:
        return False
    # If admin has disabled the public URL, ALL existing sessions die
    # immediately, regardless of cookie validity.
    if not getattr(deployment, 'public_url_enabled', False):
        return False
    try:
        payload = TimestampSigner().unsign(raw, max_age=PUBLIC_CHAT_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    try:
        cookie_dep_id, cookie_version = payload.split(':')
    except ValueError:
        return False
    if cookie_dep_id != str(deployment.id):
        return False
    try:
        # `public_url_password_version` is really the "session generation"
        # counter: bumps on password rotation AND on any config change
        # (enable toggle, auth toggle, username) so flipping any of those
        # settings invalidates all live cookies.
        if int(cookie_version) != deployment.public_url_password_version:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _is_public_chat_access_allowed(request, deployment) -> bool:
    """True if this request carries a valid public-chat auth token, OR the
    deployment is publicly available without auth. False otherwise.
    Composed with other allowlist checks by public endpoints — never
    widens access on its own."""
    if not getattr(deployment, 'public_url_enabled', False):
        return False
    if not getattr(deployment, 'public_url_auth_enabled', False):
        return True  # public URL open without auth
    return _verify_public_chat_cookie(request, deployment)


def _apply_public_url_password_if_provided(deployment, plaintext) -> None:
    """Hash and assign a new password, bumping the version so existing
    cookies become invalid immediately. Blank plaintext is a no-op so
    admins can edit other fields without wiping the password."""
    if not plaintext:
        return
    plaintext = str(plaintext)
    deployment.public_url_password_hash = make_password(plaintext)
    deployment.public_url_password_version = (deployment.public_url_password_version or 0) + 1


def _build_public_chat_url(request, project) -> str:
    """Build the front-end URL admins should share.  Format: <origin>/chat/<project_id>."""
    origin = request.build_absolute_uri('/').rstrip('/')
    return f'{origin}/chat/{project.project_id}'


def _client_ip(request) -> str:
    """Best-effort extraction of the originating IP from proxy headers."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _request_origin_is_allowed(request, deployment) -> bool:
    """True when the request's Origin header matches an active
    WorkflowAllowedOrigin row for this deployment."""
    origin = (request.META.get('HTTP_ORIGIN') or '').strip()
    if not origin:
        return False
    normalized = origin.rstrip('/').lower()
    return WorkflowAllowedOrigin.objects.filter(
        deployment=deployment,
        origin__iexact=normalized,
        is_active=True,
    ).exists()


def _is_trusted_admin_app_request(request) -> bool:
    """True when the request originated from the admin app itself — i.e.
    from a page on one of settings.CORS_ALLOWED_ORIGINS. Used to bypass
    public-chat auth for the in-app admin chatbot iframe, which is a
    different surface from the external public chat page.

    Signals (any one is sufficient):
      - Origin header matches a trusted app origin (set by browsers for
        cross-origin XHR and some iframe loads).
      - Referer header's origin matches a trusted app origin (always set
        for iframe document navigations initiated by an admin-app page).
    Browsers reliably set these; neither can be spoofed cross-site."""
    from django.conf import settings
    from urllib.parse import urlparse

    trusted = {
        (o or '').strip().rstrip('/').lower()
        for o in getattr(settings, 'CORS_ALLOWED_ORIGINS', [])
        if o
    }
    trusted.discard('')
    if not trusted:
        return False

    origin = (request.META.get('HTTP_ORIGIN') or '').strip().rstrip('/').lower()
    if origin and origin in trusted:
        return True

    referer = (request.META.get('HTTP_REFERER') or '').strip()
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip('/').lower()
            if ref_origin in trusted:
                return True

    return False


def _enforce_public_chat_auth(request, deployment):
    """Composed guard for public-facing endpoints.

    Rules (evaluated in this order — order matters):
      1. If the request came from the admin app itself (Referer/Origin in
         settings.CORS_ALLOWED_ORIGINS) → allow. The in-app chatbot iframe
         is a different surface from the external public chat page; the
         admin is already authenticated to the main app, and a stale pchat
         cookie from a prior /chat/<id> session should not block them.
      2. If public_url_auth_enabled=False → allow. External embeds on
         allowed origins and deployments that haven't opted into password
         auth live here. Stale pchat cookies are harmless when auth isn't
         enforced.
      3. If the request carries a pchat cookie AND public_url_enabled=False
         → reject (kill-switch: when the admin flips the public URL off
         while auth is still required, active /chat/<id> sessions must die
         immediately). Only reachable when rules 1 and 2 did NOT short-
         circuit — i.e. the request is an external one with auth required.
      4. Otherwise require one of:
           (a) valid public-chat cookie for this deployment, or
           (b) request Origin is in the per-deployment allowed-origin list.

    Returns None when allowed, or a JsonResponse with 401 when blocked.
    """
    # Rule 1 — in-app admin iframe. The admin app's own origin sits in
    # CORS_ALLOWED_ORIGINS; any request whose Referer/Origin matches is
    # definitionally an admin session (browsers don't let cross-site pages
    # forge these). This bypasses both the kill-switch and the auth gate.
    if _is_trusted_admin_app_request(request):
        return None

    # Rule 2 — public-chat auth not required. External allowed-origin
    # iframes and opt-out deployments go through here.
    if not getattr(deployment, 'public_url_auth_enabled', False):
        return None

    has_pchat_cookie = bool(request.COOKIES.get(_public_chat_cookie_name(deployment)))

    # Rule 3 — public URL flipped off with a still-circulating cookie
    # (kill-switch for live auth-gated external sessions).
    if has_pchat_cookie and not getattr(deployment, 'public_url_enabled', False):
        response = JsonResponse(
            {'error': 'This chat is no longer available', 'authRequired': True},
            status=401,
        )
        # Drop the dead cookie from the browser so the user isn't stuck in
        # a half-state when the admin re-enables the URL later.
        _clear_public_chat_cookie(response, deployment)
        return response

    # Rule 4 — external auth required, validate.
    if _verify_public_chat_cookie(request, deployment):
        return None
    if _request_origin_is_allowed(request, deployment):
        return None
    return JsonResponse(
        {'error': 'Authentication required', 'authRequired': True},
        status=401,
    )


def _maybe_refresh_public_chat_cookie(request, response, deployment) -> None:
    """Rolling 4 h TTL — rewrite the cookie on every authenticated public-chat
    request so activity extends the session. No-op when the request didn't
    come through the public-chat pathway."""
    try:
        if not getattr(deployment, 'public_url_enabled', False):
            return
        if not getattr(deployment, 'public_url_auth_enabled', False):
            return
        if _verify_public_chat_cookie(request, deployment):
            _set_public_chat_cookie(response, deployment)
    except Exception:
        # Cookie refresh is best-effort — never block the response on it.
        pass


def _embed_hex_to_rgb_csv(hex_color: str, fallback_rgb: str) -> str:
    """
    Convert #RRGGBB or #RGB to 'r, g, b' for CSS --*-rgb custom properties.
    Invalid or non-hex values return fallback_rgb (comma-separated decimals).
    """
    if not hex_color or not isinstance(hex_color, str):
        return fallback_rgb
    h = hex_color.strip().lstrip('#')
    try:
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) != 6:
            return fallback_rgb
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f'{r}, {g}, {b}'
    except ValueError:
        return fallback_rgb


def _add_cors_headers(response, request):
    """Add CORS headers to response"""
    origin = request.META.get('HTTP_ORIGIN', '')
    if origin:
        response['Access-Control-Allow-Origin'] = origin
        response['Access-Control-Allow-Credentials'] = 'true'
    else:
        response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


def _save_session_conversation_sync(deployment_session, conversation_history):
    """
    Persist current conversation_history to the deployment session (sync).
    Used when returning an error (e.g. rate limit) so the user message is not lost.
    """
    if conversation_history is None:
        return
    try:
        deployment_session.conversation_history = conversation_history
        deployment_session.message_count = len(conversation_history)
        deployment_session.last_activity = timezone.now()
        deployment_session.save()
        logger.info(f"💾 DEPLOYMENT: Saved session {deployment_session.session_id[:8]} on rate limit ({deployment_session.message_count} messages)")
    except Exception as e:
        logger.error(f"❌ DEPLOYMENT: Failed to save session on rate limit: {e}", exc_info=True)


def _save_deployment_data_async(
    deployment_session,
    conversation_history,
    assistant_response,
    execution_id,
    deployment_request,
    execution_result,
    execution_time_ms,
    workflow_execution_id,
    user_query
):
    """
    Background task to save deployment data after response is sent to client.
    This function runs in a separate thread to avoid blocking the response.
    """
    from django.db import close_old_connections
    
    # Ensure fresh database connection for this thread
    close_old_connections()
    
    try:
        # Update session whenever conversation_history is provided (success or failure)
        # So user message is never lost when a turn fails
        if conversation_history is not None:
            deployment_session.conversation_history = conversation_history
            deployment_session.message_count = len(conversation_history)
            deployment_session.last_activity = timezone.now()
            deployment_session.save()
            logger.info(f"💾 DEPLOYMENT: Updated session {deployment_session.session_id[:8]} with {deployment_session.message_count} messages (background)")
        
        # Try to get WorkflowExecution (non-blocking, optional)
        workflow_execution = None
        if workflow_execution_id:
            try:
                from users.models import WorkflowExecution
                workflow_execution = WorkflowExecution.objects.filter(execution_id=workflow_execution_id).first()
            except Exception as e:
                logger.warning(f"⚠️ DEPLOYMENT: Could not link to WorkflowExecution {workflow_execution_id}: {e}")
        
        # Create DeploymentExecution record
        try:
            from .models import DeploymentExecution, WorkflowDeploymentRequestStatus
            DeploymentExecution.objects.create(
                execution_id=execution_id,
                deployment_session=deployment_session,
                workflow_execution=workflow_execution,
                user_query=user_query,
                assistant_response=assistant_response,
                execution_time_ms=execution_time_ms,
                status=(
                    WorkflowDeploymentRequestStatus.SUCCESS
                    if execution_result.get('status') == 'success'
                    else WorkflowDeploymentRequestStatus.ERROR
                ),
                error_message=execution_result.get('error', '') if execution_result.get('status') != 'success' else None
            )
            logger.info(f"📝 DEPLOYMENT: Created execution record {execution_id[:8]} for session {deployment_session.session_id[:8]} (background)")
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Failed to create execution record in background: {e}", exc_info=True)
        
        # Update tracking record
        if deployment_request:
            try:
                deployment_request.response_generated = execution_result.get('status') == 'success'
                deployment_request.status = (
                    WorkflowDeploymentRequestStatus.SUCCESS
                    if execution_result.get('status') == 'success'
                    else WorkflowDeploymentRequestStatus.ERROR
                )
                deployment_request.execution_time_ms = execution_time_ms
                if execution_result.get('error'):
                    deployment_request.error_message = execution_result['error']
                deployment_request.save()
                logger.debug(f"📊 DEPLOYMENT: Updated request record {deployment_request.request_id[:8]} (background)")
            except Exception as e:
                logger.error(f"❌ DEPLOYMENT: Failed to update request record in background: {e}", exc_info=True)
                
    except Exception as e:
        logger.error(f"❌ DEPLOYMENT: Error in background save task: {e}", exc_info=True)
    finally:
        # Clean up database connection for this thread
        close_old_connections()


class DeploymentViewSet(viewsets.ViewSet):
    """
    ViewSet for managing workflow deployments
    """
    permission_classes = [IsAuthenticated]
    
    def retrieve(self, request, project_id=None):
        """Get deployment status for a project"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get or create deployment
            deployment, created = WorkflowDeployment.objects.get_or_create(
                project=project,
                defaults={
                    'created_by': request.user,
                    'is_active': False,
                    'rate_limit_per_minute': 10,
                    'workflow': None  # Can be None initially
                }
            )
            
            # Get allowed origins
            allowed_origins = WorkflowAllowedOrigin.objects.filter(
                deployment=deployment
            ).order_by('origin')
            
            origins_data = []
            for origin in allowed_origins:
                origins_data.append({
                    'id': origin.id,
                    'origin': origin.origin,
                    'rate_limit_per_minute': origin.rate_limit_per_minute,
                    'is_active': origin.is_active,
                    'created_at': origin.created_at.isoformat()
                })
            
            # Get workflows for dropdown
            workflows = AgentWorkflow.objects.filter(project=project).order_by('-updated_at')
            workflows_data = []
            for workflow in workflows:
                workflows_data.append({
                    'workflow_id': str(workflow.workflow_id),
                    'name': workflow.name,
                    'description': workflow.description,
                    'status': workflow.status
                })
            
            response_data = {
                'deployment': {
                    'id': deployment.id,
                    'workflow_id': str(deployment.workflow.workflow_id) if deployment.workflow else None,
                    'workflow_name': deployment.workflow.name if deployment.workflow else None,
                    'is_active': deployment.is_active,
                    'endpoint_path': deployment.endpoint_path,
                    'endpoint_url': request.build_absolute_uri(deployment.endpoint_path),
                    'rate_limit_per_minute': deployment.rate_limit_per_minute,
                    'initial_greeting': getattr(deployment, 'initial_greeting', ''),
                    # Chatbot branding customization
                    'chatbot_title': getattr(deployment, 'chatbot_title', 'AI Assistant'),
                    'chatbot_subtitle': getattr(deployment, 'chatbot_subtitle', 'Powered by AICC IntelliDoc'),
                    'primary_color': getattr(deployment, 'primary_color', '#ffffff'),
                    'secondary_color': getattr(deployment, 'secondary_color', '#ffffff'),
                    'font_color': getattr(deployment, 'font_color', '#000000'),
                    'logo_url': getattr(deployment, 'logo_url', None),
                    'file_uploads_enabled': getattr(deployment, 'file_uploads_enabled', False),
                    # Public Chat URL — safe-to-return fields only (hash/version excluded).
                    'public_url_enabled': getattr(deployment, 'public_url_enabled', False),
                    'public_url_auth_enabled': getattr(deployment, 'public_url_auth_enabled', False),
                    'public_url_username': getattr(deployment, 'public_url_username', ''),
                    'public_url_password_set': bool(getattr(deployment, 'public_url_password_hash', '')),
                    'public_chat_url': _build_public_chat_url(request, project),
                    'created_at': deployment.created_at.isoformat(),
                    'updated_at': deployment.updated_at.isoformat()
                },
                'allowed_origins': origins_data,
                'available_workflows': workflows_data
            }
            
            return Response(response_data)
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error retrieving deployment: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to retrieve deployment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def create(self, request, project_id=None):
        """Create or update deployment"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            workflow_id = request.data.get('workflow_id')
            if not workflow_id:
                return Response(
                    {'error': 'workflow_id is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate field lengths up front so oversized input returns a
            # clear 400 instead of a database-level 500.
            max_greeting_length = WorkflowDeployment._meta.get_field('initial_greeting').max_length
            initial_greeting = request.data.get('initial_greeting')
            if initial_greeting is not None and len(str(initial_greeting)) > max_greeting_length:
                return Response(
                    {'error': f'Initial greeting is too long ({len(str(initial_greeting))} characters). '
                              f'Maximum allowed is {max_greeting_length} characters.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            workflow = get_object_or_404(AgentWorkflow, workflow_id=workflow_id, project=project)
            
            # Get or create deployment
            deployment, created = WorkflowDeployment.objects.get_or_create(
                project=project,
                defaults={
                    'workflow': workflow,
                    'created_by': request.user,
                    'is_active': False,
                    'rate_limit_per_minute': request.data.get('rate_limit_per_minute', 10),
                    'initial_greeting': request.data.get('initial_greeting', 'Hi! I am your AI assistant.'),
                    # Chatbot branding customization
                    'chatbot_title': request.data.get('chatbot_title', 'AI Assistant'),
                    'chatbot_subtitle': request.data.get('chatbot_subtitle', 'Powered by AICC IntelliDoc'),
                    'primary_color': request.data.get('primary_color', '#ffffff'),
                    'secondary_color': request.data.get('secondary_color', '#ffffff'),
                    'font_color': request.data.get('font_color', '#000000'),
                    'logo_url': request.data.get('logo_url', None),
                    'file_uploads_enabled': request.data.get('file_uploads_enabled', False),
                    # Public Chat URL — password handled below after .save()
                    'public_url_enabled': request.data.get('public_url_enabled', False),
                    'public_url_auth_enabled': request.data.get('public_url_auth_enabled', False),
                    'public_url_username': request.data.get('public_url_username', ''),
                }
            )

            # If password is supplied on FIRST create, hash it and bump version.
            if created:
                _apply_public_url_password_if_provided(deployment, request.data.get('public_url_password', ''))
                deployment.save(update_fields=['public_url_password_hash', 'public_url_password_version'])

            # Update deployment if it already exists
            if not created:
                # If there's an active deployment for another workflow, deactivate it
                if deployment.is_active and deployment.workflow and deployment.workflow != workflow:
                    deployment.is_active = False

                deployment.workflow = workflow
                if 'rate_limit_per_minute' in request.data:
                    deployment.rate_limit_per_minute = request.data['rate_limit_per_minute']
                if 'initial_greeting' in request.data:
                    deployment.initial_greeting = request.data['initial_greeting']
                # Handle chatbot branding customization
                if 'chatbot_title' in request.data:
                    deployment.chatbot_title = request.data['chatbot_title']
                if 'chatbot_subtitle' in request.data:
                    deployment.chatbot_subtitle = request.data['chatbot_subtitle']
                if 'primary_color' in request.data:
                    deployment.primary_color = request.data['primary_color']
                if 'secondary_color' in request.data:
                    deployment.secondary_color = request.data['secondary_color']
                if 'font_color' in request.data:
                    deployment.font_color = request.data['font_color']
                if 'logo_url' in request.data:
                    deployment.logo_url = request.data['logo_url']
                if 'file_uploads_enabled' in request.data:
                    deployment.file_uploads_enabled = request.data['file_uploads_enabled']
                # Public Chat URL — snapshot prior state so we can invalidate
                # live sessions on any session-affecting change.
                _prev_public_url_enabled = deployment.public_url_enabled
                _prev_public_url_auth_enabled = deployment.public_url_auth_enabled
                _prev_public_url_username = deployment.public_url_username
                if 'public_url_enabled' in request.data:
                    deployment.public_url_enabled = bool(request.data['public_url_enabled'])
                if 'public_url_auth_enabled' in request.data:
                    deployment.public_url_auth_enabled = bool(request.data['public_url_auth_enabled'])
                if 'public_url_username' in request.data:
                    deployment.public_url_username = (request.data['public_url_username'] or '').strip()
                # Bump session generation if anything that would affect session
                # validity changed — kills existing cookies on the next request.
                # The password-apply helper bumps too; keep the two paths distinct
                # so we don't double-bump when admin changes both password and a flag.
                new_password_set = bool(request.data.get('public_url_password', ''))
                config_changed = (
                    _prev_public_url_enabled != deployment.public_url_enabled
                    or _prev_public_url_auth_enabled != deployment.public_url_auth_enabled
                    or _prev_public_url_username != deployment.public_url_username
                )
                if new_password_set:
                    # This helper bumps the version itself; don't double-bump.
                    _apply_public_url_password_if_provided(
                        deployment, request.data['public_url_password']
                    )
                elif config_changed:
                    deployment.public_url_password_version = (
                        deployment.public_url_password_version or 0
                    ) + 1
                deployment.save()
            
            logger.info(f"✅ DEPLOYMENT: {'Created' if created else 'Updated'} deployment for project {project.name}")
            
            return Response({
                'id': deployment.id,
                'workflow_id': str(deployment.workflow.workflow_id),
                'workflow_name': deployment.workflow.name,
                'is_active': deployment.is_active,
                'endpoint_path': deployment.endpoint_path,
                'endpoint_url': request.build_absolute_uri(deployment.endpoint_path),
                'rate_limit_per_minute': deployment.rate_limit_per_minute,
                'initial_greeting': getattr(deployment, 'initial_greeting', 'Hi! I am your AI assistant.'),
                # Chatbot branding customization
                'chatbot_title': getattr(deployment, 'chatbot_title', 'AI Assistant'),
                'chatbot_subtitle': getattr(deployment, 'chatbot_subtitle', 'Powered by AICC IntelliDoc'),
                'primary_color': getattr(deployment, 'primary_color', '#ffffff'),
                'secondary_color': getattr(deployment, 'secondary_color', '#ffffff'),
                'font_color': getattr(deployment, 'font_color', '#000000'),
                'logo_url': getattr(deployment, 'logo_url', None),
                'file_uploads_enabled': getattr(deployment, 'file_uploads_enabled', False),
                # Public Chat URL (password hash/version intentionally omitted).
                'public_url_enabled': getattr(deployment, 'public_url_enabled', False),
                'public_url_auth_enabled': getattr(deployment, 'public_url_auth_enabled', False),
                'public_url_username': getattr(deployment, 'public_url_username', ''),
                'public_url_password_set': bool(getattr(deployment, 'public_url_password_hash', '')),
                'public_chat_url': _build_public_chat_url(request, project),
                'message': 'Deployment created successfully' if created else 'Deployment updated successfully'
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
            
        except DataError as e:
            logger.warning(f"⚠️ DEPLOYMENT: Invalid deployment data: {e}")
            return Response(
                {'error': 'One of the provided values is too long for its field. '
                          'Please shorten it and try again.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error creating/updating deployment: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to create/update deployment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['patch'], url_path='toggle')
    def toggle(self, request, project_id=None):
        """Toggle deployment active status"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            deployment = get_object_or_404(WorkflowDeployment, project=project)
            
            # Toggle active status
            new_active_status = not deployment.is_active
            
            # Check if trying to activate without workflow
            if new_active_status and not deployment.workflow:
                return Response(
                    {'error': 'Cannot activate deployment without a workflow. Please select a workflow first.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            deployment.is_active = new_active_status
            
            # If activating, ensure no other active deployment exists
            if deployment.is_active:
                # Deactivate any other active deployments for this project (shouldn't happen due to constraint, but safety check)
                WorkflowDeployment.objects.filter(
                    project=project,
                    is_active=True
                ).exclude(id=deployment.id).update(is_active=False)
            
            deployment.save()
            
            logger.info(f"🔄 DEPLOYMENT: Toggled deployment to {'active' if deployment.is_active else 'inactive'} for project {project.name}")
            
            return Response({
                'id': deployment.id,
                'is_active': deployment.is_active,
                'message': f'Deployment {"activated" if deployment.is_active else "deactivated"} successfully'
            })
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error toggling deployment: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to toggle deployment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'], url_path='origins')
    def list_origins(self, request, project_id=None):
        """List allowed origins for deployment"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get deployment - return empty list if doesn't exist yet
            try:
                deployment = WorkflowDeployment.objects.get(project=project)
            except WorkflowDeployment.DoesNotExist:
                return Response({'origins': []})
            
            origins = WorkflowAllowedOrigin.objects.filter(
                deployment=deployment
            ).order_by('origin')
            
            origins_data = []
            for origin in origins:
                origins_data.append({
                    'id': origin.id,
                    'origin': origin.origin,
                    'rate_limit_per_minute': origin.rate_limit_per_minute,
                    'is_active': origin.is_active,
                    'created_at': origin.created_at.isoformat(),
                    'updated_at': origin.updated_at.isoformat()
                })
            
            return Response({'origins': origins_data})
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error listing origins: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to list origins'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['post'], url_path='origins')
    def add_origin(self, request, project_id=None):
        """Add allowed origin"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get deployment - must exist before adding origins
            try:
                deployment = WorkflowDeployment.objects.get(project=project)
            except WorkflowDeployment.DoesNotExist:
                return Response(
                    {'error': 'Deployment does not exist. Please save deployment configuration first by selecting a workflow.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            origin = request.data.get('origin', '').strip()
            if not origin:
                return Response(
                    {'error': 'origin is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate origin format (basic validation)
            if not (origin.startswith('http://') or origin.startswith('https://')):
                return Response(
                    {'error': 'Origin must start with http:// or https://'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Normalize origin (remove trailing slash and ensure no double protocol)
            origin = origin.rstrip('/')
            # Fix double https:// or http://
            if origin.startswith('https://https://') or origin.startswith('http://http://'):
                origin = origin.replace('https://https://', 'https://').replace('http://http://', 'http://')
            
            rate_limit = request.data.get('rate_limit_per_minute', deployment.rate_limit_per_minute)
            
            # Create or update origin
            allowed_origin, created = WorkflowAllowedOrigin.objects.get_or_create(
                deployment=deployment,
                origin=origin,
                defaults={
                    'rate_limit_per_minute': rate_limit,
                    'is_active': True
                }
            )
            
            if not created:
                # Update existing origin
                allowed_origin.rate_limit_per_minute = rate_limit
                allowed_origin.is_active = True
                allowed_origin.save()
            
            logger.info(f"✅ DEPLOYMENT: {'Added' if created else 'Updated'} origin {origin} for project {project.name}")
            
            return Response({
                'id': allowed_origin.id,
                'origin': allowed_origin.origin,
                'rate_limit_per_minute': allowed_origin.rate_limit_per_minute,
                'is_active': allowed_origin.is_active,
                'message': 'Origin added successfully' if created else 'Origin updated successfully'
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error adding origin: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to add origin'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['delete'], url_path='origins/(?P<origin_id>[^/.]+)')
    def remove_origin(self, request, project_id=None, origin_id=None):
        """Remove allowed origin"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get deployment (must exist to have origins)
            try:
                deployment = WorkflowDeployment.objects.get(project=project)
            except WorkflowDeployment.DoesNotExist:
                return Response(
                    {'error': 'No deployment found for this project'},
                    status=status.HTTP_404_NOT_FOUND
                )
            origin = get_object_or_404(WorkflowAllowedOrigin, id=origin_id, deployment=deployment)
            
            origin.delete()
            
            logger.info(f"🗑️ DEPLOYMENT: Removed origin {origin.origin} for project {project.name}")
            
            return Response({'message': 'Origin removed successfully'})
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error removing origin: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to remove origin'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['patch'], url_path='origins/(?P<origin_id>[^/.]+)')
    def update_origin(self, request, project_id=None, origin_id=None):
        """Update origin rate limit or active status"""
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            
            # Check project access
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get deployment (must exist to have origins)
            try:
                deployment = WorkflowDeployment.objects.get(project=project)
            except WorkflowDeployment.DoesNotExist:
                return Response(
                    {'error': 'No deployment found for this project'},
                    status=status.HTTP_404_NOT_FOUND
                )
            origin = get_object_or_404(WorkflowAllowedOrigin, id=origin_id, deployment=deployment)
            
            # Update fields
            if 'rate_limit_per_minute' in request.data:
                origin.rate_limit_per_minute = request.data['rate_limit_per_minute']
            if 'is_active' in request.data:
                origin.is_active = request.data['is_active']
            
            origin.save()
            
            logger.info(f"🔄 DEPLOYMENT: Updated origin {origin.origin} for project {project.name}")
            
            return Response({
                'id': origin.id,
                'origin': origin.origin,
                'rate_limit_per_minute': origin.rate_limit_per_minute,
                'is_active': origin.is_active,
                'message': 'Origin updated successfully'
            })
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error updating origin: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to update origin'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['post'], url_path='projects/(?P<project_id>[^/.]+)/summarize-urls')
    def summarize_urls(self, request, project_id=None):
        """
        Kick off LLM-summary generation for a list of web-search URLs.

        Runs in a detached daemon thread so the HTTP request returns
        immediately (~200 ms) — a 20-URL run can take 5-15 minutes at
        two LLM calls per URL and would otherwise hit gunicorn/nginx
        timeouts.

        POST /api/agent-orchestration/projects/{project_id}/summarize-urls/
        Body: {
            "urls": ["https://..."],
            "llm_provider": "openai",
            "llm_model":    "gpt-4o-mini",
            "cache_ttl":    2592000,
            "force":        false
        }

        Returns 202 Accepted with:
            { "status": "started", "urls_queued": N,
              "dropped_invalid": N, "dropped_over_cap": N }

        Returns 409 Conflict if a generation is already running for
        this project (Redis flag websearch_summary_gen_<pid>).

        Poll GET /url-summaries/ to observe progress and completion.
        """
        import asyncio
        import threading
        from asgiref.sync import async_to_sync
        from django.core.cache import cache as django_cache
        from .websearch_handler import WebSearchHandler
        from .websearch import WebSearchCacheService
        from .llm_provider_manager import LLMProviderManager

        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            body = request.data
            urls = body.get('urls', [])
            if not urls or not isinstance(urls, list):
                return Response(
                    {'error': 'urls must be a non-empty list'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Validate (protocol check + dedupe) WITHOUT applying the per-agent
            # URL cap.  MAX_URLS_PER_AGENT is a node-editor UI limit — applying
            # it here silently truncates the tail of large URL lists so those
            # URLs are never queued, even on repeated retries.
            seen_urls: set = set()
            cleaned_urls: list = []
            dropped_invalid = 0
            for item in urls:
                if not isinstance(item, str) or not item.strip():
                    dropped_invalid += 1
                    continue
                u = item.strip()
                if not (u.startswith('http://') or u.startswith('https://')):
                    dropped_invalid += 1
                    continue
                if u not in seen_urls:
                    seen_urls.add(u)
                    cleaned_urls.append(u)
            urls = cleaned_urls
            dropped_over_cap = 0
            if not urls:
                return Response(
                    {'error': 'No valid URLs provided'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            force = body.get('force', False)
            original_count = len(urls)
            if not force:
                from users.models import WebSearchUrlSummary
                existing = set(
                    WebSearchUrlSummary.objects.filter(
                        project=project, url__in=urls
                    ).values_list('url', flat=True)
                )
                urls = [u for u in urls if u not in existing]
                if not urls:
                    # Nothing to do — return immediately with the legacy
                    # shape so clients don't have to special-case it.
                    return Response(
                        {
                            'status': 'noop',
                            'summarized': 0,
                            'skipped': original_count,
                            'failed': 0,
                            'results': [],
                            'dropped_invalid': dropped_invalid,
                            'dropped_over_cap': dropped_over_cap,
                        },
                        status=status.HTTP_200_OK,
                    )

            provider_name = body.get('llm_provider', 'openai')
            model_name = body.get('llm_model', '')
            cache_ttl = int(body.get('cache_ttl', 2592000))  # default 30 days

            # --- Pre-flight: resolve the LLM provider synchronously so we
            # can return a helpful 400 BEFORE kicking off a background job.
            mgr = LLMProviderManager()
            agent_config = {
                'llm_provider': provider_name,
                'llm_model': model_name or 'gpt-4o-mini',
            }
            try:
                llm = async_to_sync(mgr.get_llm_provider)(agent_config, project=project)
            except Exception as e:
                logger.error(f"❌ SUMMARIZE URLS: provider resolution failed: {e}")
                return Response({'error': f'LLM provider error: {e}'}, status=status.HTTP_400_BAD_REQUEST)
            if not llm:
                return Response(
                    {
                        'error': f'No {provider_name} API key configured for this project. '
                                 'Add one in Project Settings → API Keys.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # --- Double-start guard via Redis flag.
            cache_svc = WebSearchCacheService()
            pid_for_key = str(project_id).replace('-', '_')
            flag_key = f"{cache_svc.SUMMARY_GEN_PREFIX}{pid_for_key}"
            if django_cache.get(flag_key):
                return Response(
                    {
                        'error': 'generation_in_progress',
                        'detail': 'A summary generation job is already running for this project.',
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # 15-minute TTL acts as a circuit breaker: if the thread dies
            # without clearing the flag (worker recycle, OOM, etc.) the
            # flag self-expires instead of blocking the endpoint forever.
            django_cache.set(flag_key, True, timeout=900)

            handler = WebSearchHandler()
            project_id_str = str(project_id)
            urls_snapshot = list(urls)  # defensive copy into the closure

            job_provider_name = provider_name
            job_model_name = model_name or 'gpt-4o-mini'

            def _run_in_background():
                import time as _time
                _t_start = _time.time()
                _result = None
                _error = None
                try:
                    _result = asyncio.run(handler.summarize_urls_for_project(
                        urls=urls_snapshot,
                        project_id=project_id_str,
                        llm_provider=llm,
                        cache_ttl=cache_ttl,
                    ))
                    logger.info(
                        f"✅ SUMMARIZE URLS (bg): finished {len(urls_snapshot)} URL(s) "
                        f"for project {project_id_str[:8]}"
                    )
                except Exception as e:
                    _error = str(e)
                    logger.error(f"❌ SUMMARIZE URLS (bg): {e}", exc_info=True)
                finally:
                    try:
                        django_cache.delete(flag_key)
                    except Exception:
                        pass
                # Emit a summary_job metric row so the Project Analytics
                # page can show job history + success rate. In-thread sync
                # write — we're already on a worker thread so no need for
                # sync_to_async.
                try:
                    from users.models import IntelliDocProject, ExperimentMetric
                    _duration_ms = round((_time.time() - _t_start) * 1000, 1)
                    _project = IntelliDocProject.objects.get(project_id=project_id_str)
                    ExperimentMetric.objects.create(
                        project=_project,
                        experiment_type='summary_job',
                        metric_data={
                            'experiment': 'summary_job',
                            'project_id': project_id_str,
                            'urls_queued': len(urls_snapshot),
                            'summarized': (_result or {}).get('summarized', 0) if isinstance(_result, dict) else 0,
                            'skipped': (_result or {}).get('skipped', 0) if isinstance(_result, dict) else 0,
                            'failed': (_result or {}).get('failed', 0) if isinstance(_result, dict) else len(urls_snapshot),
                            'duration_ms': _duration_ms,
                            'status': 'error' if _error else 'ok',
                            'error': _error,
                            'llm_provider': job_provider_name,
                            'llm_model': job_model_name,
                        },
                        configuration={
                            'llm_provider': job_provider_name,
                            'llm_model': job_model_name,
                            'cache_ttl': cache_ttl,
                        },
                        execution_id='',
                    )
                except Exception as metric_err:
                    logger.warning(f"⚠️ SUMMARIZE URLS (bg): metric logging failed: {metric_err}")

            threading.Thread(
                target=_run_in_background,
                daemon=True,
                name='websearch-summarize',
            ).start()

            return Response(
                {
                    'status': 'started',
                    'urls_queued': len(urls_snapshot),
                    'dropped_invalid': dropped_invalid,
                    'dropped_over_cap': dropped_over_cap,
                },
                status=status.HTTP_202_ACCEPTED,
            )

        except Exception as e:
            logger.error(f"❌ SUMMARIZE URLS: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=['get'], url_path='projects/(?P<project_id>[^/.]+)/url-summaries')
    def list_url_summaries(self, request, project_id=None):
        """
        List stored LLM summaries for the project's web-search URLs.

        GET /api/agent-orchestration/projects/{project_id}/url-summaries/
        Optional query: ?urls=url1,url2  → only return entries for these URLs.

        Returns: {
          "summaries": [
            {
              "url": "...",
              "short_summary": "...",
              "long_summary": "...",
              "llm_provider": "openai",
              "llm_model": "gpt-4o-mini",
              "updated_at": "ISO-8601",
            }, ...
          ]
        }
        """
        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            from users.models import WebSearchUrlSummary
            qs = WebSearchUrlSummary.objects.filter(project=project)

            raw_urls = request.query_params.get('urls', '').strip()
            if raw_urls:
                wanted = [u.strip() for u in raw_urls.split(',') if u.strip()]
                if wanted:
                    qs = qs.filter(url__in=wanted)

            summaries = [
                {
                    'url': s.url,
                    'short_summary': s.short_summary,
                    'long_summary': s.long_summary,
                    'llm_provider': s.llm_provider,
                    'llm_model': s.llm_model,
                    'updated_at': s.updated_at.isoformat() if s.updated_at else None,
                }
                for s in qs.order_by('url')
            ]

            # Expose the background-generation flag so the UI can poll
            # this endpoint and stop when the Redis key has cleared.
            from django.core.cache import cache as django_cache
            from .websearch import WebSearchCacheService
            cache_svc = WebSearchCacheService()
            pid_for_key = str(project_id).replace('-', '_')
            flag_key = f"{cache_svc.SUMMARY_GEN_PREFIX}{pid_for_key}"
            generation_in_progress = bool(django_cache.get(flag_key))

            return Response(
                {
                    'summaries': summaries,
                    'generation_in_progress': generation_in_progress,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error(f"❌ LIST URL SUMMARIES: {e}", exc_info=True)
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='projects/(?P<project_id>[^/.]+)/clear-websearch-cache')
    def clear_websearch_cache(self, request, project_id=None):
        """
        Clear all Redis-cached websearch content (URL fetches and search results) for a project.

        POST /api/agent-orchestration/projects/{project_id}/clear-websearch-cache/

        Returns: { "cleared": true }
        """
        from .websearch import WebSearchCacheService

        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            cache_service = WebSearchCacheService()
            success = cache_service.clear_all_websearch_cache(str(project_id))

            # Also drop the Milvus websearch RAG collection
            try:
                import asyncio
                from .websearch import WebRAGService
                rag_service = WebRAGService()
                asyncio.run(rag_service.clear_project(str(project_id)))
            except Exception as rag_err:
                logger.warning(f"⚠️ CLEAR WEBSEARCH CACHE: Milvus cleanup failed (non-fatal): {rag_err}")

            if success:
                logger.info(f"🗑️ CLEAR WEBSEARCH CACHE: Cleared cache for project {str(project_id)[:8]}")
                return Response({'cleared': True}, status=status.HTTP_200_OK)
            else:
                return Response(
                    {'error': 'Cache backend does not support pattern deletion'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        except Exception as e:
            logger.error(f"❌ CLEAR WEBSEARCH CACHE: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=['post'], url_path='projects/(?P<project_id>[^/.]+)/sync-websearch-index')
    def sync_websearch_index(self, request, project_id=None):
        """
        Sync the Milvus RAG index for a project's web search URLs.
        Indexes new URLs, removes URLs no longer in the list.

        POST /api/agent-orchestration/projects/{project_id}/sync-websearch-index/
        Body: { "urls": ["https://…", …], "cache_ttl": 2592000 }
        Returns: { "indexed": 3, "removed": 1, "already_indexed": 5, "failed": 0 }
        """
        import asyncio
        from .websearch import WebSearchCacheService, WebsiteFetcherService, WebRAGService

        try:
            project = get_object_or_404(IntelliDocProject, project_id=project_id)
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            urls = request.data.get('urls', [])
            cache_ttl = request.data.get('cache_ttl', 2592000)

            if not isinstance(urls, list):
                return Response({'error': '"urls" must be a list'}, status=status.HTTP_400_BAD_REQUEST)

            # Normalise, dedupe, and cap at MAX_URLS_PER_AGENT.
            from .websearch import clean_url_list
            urls, dropped_invalid, dropped_over_cap = clean_url_list(urls)
            if dropped_invalid or dropped_over_cap:
                logger.info(
                    f"🌐 SYNC INDEX: dropped {dropped_invalid} invalid, "
                    f"{dropped_over_cap} over-cap URLs (project {str(project_id)[:8]})"
                )

            pid = str(project_id)
            rag_service = WebRAGService()
            cache_service = WebSearchCacheService()
            fetcher_service = WebsiteFetcherService()

            async def _sync():
                # 1. Get currently indexed URLs
                indexed_urls = set(await rag_service.get_indexed_urls(pid))
                desired_urls = set(urls)

                # 2. Remove URLs no longer in the list
                to_remove = indexed_urls - desired_urls
                for url in to_remove:
                    await rag_service.remove_url(url, pid)
                logger.info(f"🌐 SYNC INDEX: Removed {len(to_remove)} stale URLs (project {pid[:8]})")

                # 3. Determine which URLs need indexing
                #    (not in Milvus OR index flag expired)
                to_index = []
                already_indexed = 0
                for url in desired_urls:
                    flag_key = f"websearch_milvus_idx_{pid.replace('-', '_')}_{__import__('hashlib').md5(url.encode()).hexdigest()}"
                    from django.core.cache import cache as django_cache
                    if django_cache.get(flag_key):
                        already_indexed += 1
                    else:
                        to_index.append(url)

                if not to_index:
                    return {
                        'indexed': 0,
                        'removed': len(to_remove),
                        'already_indexed': already_indexed,
                        'failed': 0,
                    }

                # 4. Fetch content for URLs that need indexing
                cached_results = cache_service.get_cached_urls_batch(to_index, pid)
                urls_to_fetch = [url for url, content in cached_results.items() if content is None]

                if urls_to_fetch:
                    fetch_results = await fetcher_service.fetch_urls_parallel(urls_to_fetch)
                    to_cache = {}
                    for result in fetch_results:
                        url = result.get('url')
                        if url:
                            to_cache[url] = result
                            cached_results[url] = result
                    if to_cache:
                        cache_service.cache_urls_batch(to_cache, pid, ttl=cache_ttl)

                # 5. Index in Milvus — ONE batched pipeline (one embed, one
                # delete, one insert, one flush) regardless of URL count.
                batch_items = [
                    (url, cached_results.get(url))
                    for url in to_index
                    if cached_results.get(url) and not cached_results[url].get('extraction_error')
                ]
                pre_fail = len(to_index) - len(batch_items)

                if batch_items:
                    # async_flush=True: the sync endpoint doesn't need to wait
                    # for Milvus fsync before returning — the next agent-turn
                    # path uses async_flush=False to preserve read-your-writes.
                    batch_result = await rag_service.index_urls_batch(
                        batch_items, pid, cache_ttl=cache_ttl, async_flush=True
                    )
                    indexed = batch_result['indexed']
                    already_indexed += batch_result['skipped']
                    failed = batch_result['failed'] + pre_fail

                    # Emit an analytics row recording which cache tiers
                    # saved work on this sync. Feeds the Project Analytics
                    # cache-tier breakdown chart.
                    try:
                        from .metrics_logger import log_experiment_metric
                        timings = batch_result.get('timings_ms', {}) or {}
                        await log_experiment_metric(
                            project_id=pid,
                            experiment_type='websearch_index_batch',
                            metric_data={
                                'experiment': 'websearch_index_batch',
                                'project_id': pid,
                                'n_urls': len(batch_items),
                                'indexed': indexed,
                                'skipped': batch_result.get('skipped', 0),
                                'failed': batch_result.get('failed', 0),
                                'flag_alive_hits': batch_result.get('flag_alive_hits', 0),
                                'content_hash_hits': batch_result.get('content_hash_hits', 0),
                                'embed_cache_hits': batch_result.get('embed_cache_hits', 0),
                                'cold_count': batch_result.get('cold_count', 0),
                                'duration_ms': timings.get('total', 0),
                                'embed_ms': timings.get('embed', 0),
                                'milvus_ms': timings.get('milvus', 0),
                                'flush_ms': timings.get('flush', 0),
                            },
                            configuration={'cache_ttl': cache_ttl},
                            log_tag='EXP_METRIC_WEBSEARCH_INDEX_BATCH',
                        )
                    except Exception as exc:
                        logger.warning(f"⚠️ SYNC INDEX: metric logging failed: {exc}")
                else:
                    indexed = 0
                    failed = pre_fail

                return {
                    'indexed': indexed,
                    'removed': len(to_remove),
                    'already_indexed': already_indexed,
                    'failed': failed,
                    'dropped_invalid': dropped_invalid,
                    'dropped_over_cap': dropped_over_cap,
                }

            result = asyncio.run(_sync())
            logger.info(f"🌐 SYNC INDEX: project {pid[:8]} — indexed={result['indexed']}, removed={result['removed']}, already={result['already_indexed']}, failed={result['failed']}")
            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"❌ SYNC WEBSEARCH INDEX: {e}", exc_info=True)
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='projects/(?P<project_id>[^/.]+)/deployment/activity')
    def get_deployment_activity(self, request, project_id=None):
        """
        Get all deployment sessions and their conversation history for Activity Tracker
        GET /api/agent-orchestration/projects/{project_id}/deployment/activity/
        """
        try:
            # Get project
            try:
                project = IntelliDocProject.objects.get(project_id=project_id)
            except IntelliDocProject.DoesNotExist:
                return Response(
                    {'error': 'Project not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check permissions
            if not project.has_user_access(request.user):
                return Response(
                    {'error': 'You do not have permission to access this project'},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get deployment
            try:
                deployment = WorkflowDeployment.objects.get(project=project)
            except WorkflowDeployment.DoesNotExist:
                return Response({
                    'sessions': [],
                    'total_sessions': 0,
                    'message': 'No deployment found for this project'
                })
            
            # Get query parameters
            session_id_filter = request.query_params.get('session_id', '').strip()
            limit = int(request.query_params.get('limit', 50))
            offset = int(request.query_params.get('offset', 0))
            
            # Query sessions
            sessions_query = DeploymentSession.objects.filter(deployment=deployment)
            
            if session_id_filter:
                # Exact match so preload / activity lookup returns the intended session (icontains can match wrong rows)
                sessions_query = sessions_query.filter(session_id=session_id_filter)
            
            total_sessions = sessions_query.count()
            sessions = sessions_query.order_by('-last_activity')[offset:offset + limit]
            
            # Build response
            sessions_data = []
            for session in sessions:
                # Get executions for this session
                executions = DeploymentExecution.objects.filter(
                    deployment_session=session
                ).order_by('created_at')[:100]  # Limit to recent 100 executions
                
                sessions_data.append({
                    'session_id': session.session_id,
                    'message_count': session.message_count,
                    'is_active': session.is_active,
                    'created_at': session.created_at.isoformat(),
                    'last_activity': session.last_activity.isoformat(),
                    'conversation_history': session.conversation_history or [],
                    'executions': [
                        {
                            'execution_id': exec.execution_id,
                            'user_query': exec.user_query,
                            'assistant_response': exec.assistant_response,
                            'execution_time_ms': exec.execution_time_ms,
                            'status': exec.status,
                            'created_at': exec.created_at.isoformat(),
                            'workflow_execution_id': exec.workflow_execution.execution_id if exec.workflow_execution else None
                        }
                        for exec in executions
                    ]
                })
            
            logger.info(f"📊 DEPLOYMENT: Retrieved {len(sessions_data)} sessions for project {project.name}")
            
            return Response({
                'sessions': sessions_data,
                'total_sessions': total_sessions,
                'limit': limit,
                'offset': offset
            })
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error getting deployment activity: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to retrieve deployment activity'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ────────────────────────────────────────────────────────────────────────
# Public Chat URL — config / auth / logout endpoints (no DRF permission
# class; credentials are scoped to this deployment only and cannot grant
# access to admin areas — see helpers at the top of the module).
# ────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@never_cache
def public_config_endpoint(request, project_id):
    """Small blob the Svelte /chat/{id} page uses to decide what to render.
    Returns 404 when the deployment or its public URL is not available so
    the endpoint doesn't leak the existence of deployments that aren't
    meant to be publicly discoverable."""
    try:
        project = IntelliDocProject.objects.filter(project_id=project_id).first()
        if not project:
            return JsonResponse({'error': 'Not available'}, status=404)
        deployment = WorkflowDeployment.objects.filter(project=project).first()
        if not deployment or not deployment.is_active or not deployment.public_url_enabled:
            return JsonResponse({'error': 'Not available'}, status=404)
        return JsonResponse({
            'is_active': deployment.is_active,
            'public_url_enabled': deployment.public_url_enabled,
            'auth_required': deployment.public_url_auth_enabled,
            'is_logged_in': _verify_public_chat_cookie(request, deployment),
            'deployment_id': deployment.id,
            'chatbot_title': deployment.chatbot_title or 'AI Assistant',
            'chatbot_subtitle': deployment.chatbot_subtitle or '',
            'primary_color': deployment.primary_color or '#002147',
            'secondary_color': deployment.secondary_color or '#ffffff',
            'font_color': deployment.font_color or '#000000',
            'logo_url': deployment.logo_url or '',
        })
    except Exception as e:
        logger.error(f"❌ PUBLIC-CONFIG: {e}", exc_info=True)
        return JsonResponse({'error': 'Not available'}, status=404)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def public_auth_endpoint(request, project_id):
    """Validate shared public-chat credentials and set the signed auth cookie.
    Rate-limited per IP. Every attempt is rowed in DeploymentPublicLoginAttempt
    for audit. On success returns {ok: true} with the cookie attached."""
    if request.method == 'OPTIONS':
        return _add_cors_headers(JsonResponse({'ok': True}), request)
    try:
        project = IntelliDocProject.objects.filter(project_id=project_id).first()
        if not project:
            return JsonResponse({'error': 'Not available'}, status=404)
        deployment = WorkflowDeployment.objects.filter(project=project).first()
        if not deployment or not deployment.is_active or not deployment.public_url_enabled:
            return JsonResponse({'error': 'Not available'}, status=404)
        # If auth isn't even enabled, there's nothing to log in to.
        if not deployment.public_url_auth_enabled:
            return JsonResponse({'error': 'Authentication not required'}, status=400)

        # Brute-force rate limit — per IP, per 15 min window.
        from django.core.cache import cache
        ip = _client_ip(request)
        rl_key = f'public_auth_rl:{deployment.id}:{ip}'
        attempts = cache.get(rl_key, 0)
        if attempts >= PUBLIC_CHAT_LOGIN_RL_ATTEMPTS:
            return JsonResponse(
                {'error': 'Too many attempts. Please wait a few minutes and try again.'},
                status=429,
            )

        # Parse credentials
        try:
            body = json.loads(request.body or b'{}')
        except json.JSONDecodeError:
            body = {}
        username = (body.get('username') or '').strip()
        password = body.get('password') or ''

        # Verify (username + password_hash). Constant-time via check_password.
        ok = (
            username == (deployment.public_url_username or '')
            and bool(deployment.public_url_password_hash)
            and check_password(password, deployment.public_url_password_hash)
        )

        # Audit row (every attempt, regardless of outcome)
        try:
            DeploymentPublicLoginAttempt.objects.create(
                deployment=deployment,
                ip_address=ip if ip else '0.0.0.0',
                username_attempted=username[:150],
                success=ok,
            )
        except Exception as audit_err:
            logger.warning(f"⚠️ PUBLIC-AUTH: failed to audit login attempt: {audit_err}")

        if not ok:
            # Count only failures against the rate limit window.
            cache.set(rl_key, attempts + 1, timeout=PUBLIC_CHAT_LOGIN_RL_WINDOW)
            return JsonResponse({'error': 'Invalid credentials'}, status=401)

        # Success — clear the per-IP failure counter so a user who fails a
        # few times then logs in correctly is not stuck behind their own
        # earlier failures for the rest of the 15-minute window.
        cache.delete(rl_key)
        # Attach the signed cookie.
        response = JsonResponse({'ok': True})
        _set_public_chat_cookie(response, deployment)
        return response
    except Exception as e:
        logger.error(f"❌ PUBLIC-AUTH: {e}", exc_info=True)
        return JsonResponse({'error': 'Authentication error'}, status=500)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def public_logout_endpoint(request, project_id):
    """Clear the public-chat cookie so the user has to log in again."""
    if request.method == 'OPTIONS':
        return _add_cors_headers(JsonResponse({'ok': True}), request)
    response = JsonResponse({'ok': True})
    try:
        project = IntelliDocProject.objects.filter(project_id=project_id).first()
        if project:
            deployment = WorkflowDeployment.objects.filter(project=project).first()
            if deployment:
                _clear_public_chat_cookie(response, deployment)
    except Exception as e:
        logger.warning(f"⚠️ PUBLIC-LOGOUT: {e}")
    return response


# Public endpoint (unauthenticated)
@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def public_chat_endpoint(request, project_id):
    """
    Public chat endpoint for deployed workflows
    
    POST /api/workflow-deploy/{project_id}/
    """
    # Handle CORS preflight (middleware should handle this, but safety check)
    if request.method == 'OPTIONS':
        response = JsonResponse({'status': 'ok'})
        return response
    
    # Generate unique request ID
    origin = request.META.get('HTTP_ORIGIN', '')
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    request_id = f"deploy_{timestamp}_{hash(origin) % 10000:04d}_{uuid.uuid4().hex[:8]}"
    
    # Initialize tracking
    deployment_request = None
    start_time = timezone.now()
    
    try:
        # Get project and deployment
        try:
            project = IntelliDocProject.objects.get(project_id=project_id)
        except IntelliDocProject.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'error': 'Project not found',
                'request_id': request_id
            }, status=404)
        
        deployment = WorkflowDeployment.objects.filter(
            project=project,
            is_active=True
        ).first()
        
        if not deployment:
            return JsonResponse({
                'status': 'error',
                'error': 'No active deployment found for this project',
                'request_id': request_id
            }, status=404)
        
        if not deployment.workflow:
            return JsonResponse({
                'status': 'error',
                'error': 'Deployment has no workflow configured',
                'request_id': request_id
            }, status=400)

        # Public Chat URL auth gate (legacy non-stream endpoint)
        auth_block = _enforce_public_chat_auth(request, deployment)
        if auth_block is not None:
            return auth_block

        # Parse request body
        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({
                'status': 'error',
                'error': 'Invalid JSON format',
                'request_id': request_id
            }, status=400)
        
        # Extract user query and session_id (client now sends only current query)
        user_query = data.get('user_query', '').strip()
        # Fallback to 'message' for backward compatibility
        if not user_query:
            user_query = data.get('message', '').strip()
        session_id = data.get('session_id', '').strip()
        # provider_file_ids tied to THIS turn (subset of session files); used to
        # persist attachment filenames on the user message so the file chip
        # re-renders when the conversation is reopened later.
        turn_file_ids = data.get('file_ids') or []
        
        if not user_query:
            return JsonResponse({
                'status': 'error',
                'error': 'User query is required',
                'request_id': request_id
            }, status=400)
        
        if not session_id:
            return JsonResponse({
                'status': 'error',
                'error': 'Session ID is required',
                'request_id': request_id
            }, status=400)
        
        # Validate message length
        if len(user_query) > 1000:
            return JsonResponse({
                'status': 'error',
                'error': 'Message too long (max 1000 characters)',
                'request_id': request_id
            }, status=400)
        
        # Get or create deployment session.
        # Note: No per-session locking; concurrent requests with same session_id can overwrite (last save wins).
        # Embed UI should disable send while a request is in flight (see DEPLOYMENT_CHAT_LIMITATIONS.md).
        try:
            deployment_session, created = DeploymentSession.objects.get_or_create(
                deployment=deployment,
                session_id=session_id,
                defaults={
                    'conversation_history': [],
                    'message_count': 0,
                    'is_active': True
                }
            )
            
            conversation_history = deployment_session.conversation_history or []
            
            if created:
                # Add initial greeting for new sessions
                initial_greeting = getattr(deployment, 'initial_greeting', 'Hi! I am your AI assistant.')
                conversation_history.append({
                    'role': 'assistant',
                    'content': initial_greeting,
                    'timestamp': timezone.now().isoformat()
                })
                logger.info(f"🆕 DEPLOYMENT: Created new session {session_id[:8]} for project {project.name} with initial greeting")
            else:
                logger.info(f"🔄 DEPLOYMENT: Retrieved existing session {session_id[:8]} with {deployment_session.message_count} messages")
            
            # Build file references and text attachments from ALL session files
            chat_file_references = []
            chat_text_attachments = []
            try:
                session_files = list(DeploymentSessionFile.objects.filter(session=deployment_session))
                for sf in session_files:
                    if sf.provider_file_id:
                        chat_file_references.append({
                            'file_id': sf.provider_file_id,
                            'filename': sf.filename,
                            'provider': sf.provider,
                            'file_type': sf.mime_type,
                            'file_size': sf.file_size,
                        })
                    if sf.extracted_text:
                        chat_text_attachments.append({
                            'filename': sf.filename,
                            'text': sf.extracted_text,
                        })
                if chat_file_references:
                    logger.info(f"📎 CHAT FILES: {len(chat_file_references)} file refs for {session_id[:8]}")
                if chat_text_attachments:
                    logger.info(f"📎 CHAT TEXT: {len(chat_text_attachments)} text attachments for {session_id[:8]}")
            except Exception as e:
                logger.warning(f"⚠️ CHAT FILES: Failed to load session files: {e}")

            # Attachments scoped to THIS turn (filenames only — file_ids are
            # provider-side and shouldn't leak to the client).
            turn_attachments = [
                {'filename': sf.filename}
                for sf in session_files
                if turn_file_ids and sf.provider_file_id in turn_file_ids
            ] if turn_file_ids else []

            # Add user query to conversation history
            user_turn = {
                'role': 'user',
                'content': user_query,
                'timestamp': timezone.now().isoformat(),
            }
            if turn_attachments:
                user_turn['attachments'] = turn_attachments
            conversation_history.append(user_turn)

            # Build full conversation history string for workflow execution
            # Format: "Assistant: greeting\nUser: query1\nAssistant: response1\nUser: query2..."
            conversation_text_parts = []
            for msg in conversation_history:
                role_label = 'User' if msg['role'] == 'user' else 'Assistant'
                conversation_text_parts.append(f"{role_label}: {msg['content']}")
            
            full_conversation = '\n'.join(conversation_text_parts)
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error managing session: {e}", exc_info=True)
            return JsonResponse({
                'status': 'error',
                'error': 'Failed to manage session',
                'request_id': request_id
            }, status=500)
        
        # Check rate limit
        rate_limiter = WorkflowDeploymentRateLimiter()
        is_allowed, retry_after = rate_limiter.check_rate_limit(deployment, origin)
        
        if not is_allowed:
            # Persist user message so it is not lost (session persistence on failure)
            _save_session_conversation_sync(deployment_session, conversation_history)
            # Create tracking record
            try:
                deployment_request = WorkflowDeploymentRequest.objects.create(
                    deployment=deployment,
                    origin=origin,
                    request_id=request_id,
                    session_id=session_id[:100] if session_id else None,
                    message_preview=user_query[:100],
                    status=WorkflowDeploymentRequestStatus.RATE_LIMITED,
                    response_generated=False
                )
            except Exception:
                pass
            
            return JsonResponse({
                'status': 'error',
                'error': 'Rate limit exceeded. Please try again later.',
                'retry_after': retry_after,
                'request_id': request_id
            }, status=429)
        
        # Create tracking record (will be updated after execution)
        try:
            deployment_request = WorkflowDeploymentRequest.objects.create(
                deployment=deployment,
                origin=origin,
                request_id=request_id,
                session_id=session_id[:100] if session_id else None,
                message_preview=user_query[:100],
                status=WorkflowDeploymentRequestStatus.SUCCESS,
                response_generated=False
            )
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Failed to create request record: {e}")
        
        # Execute workflow with full conversation history
        executor = WorkflowDeploymentExecutor()
        
        # Generate unique execution ID
        execution_id = f"deploy_exec_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        
        # Run async execution - use asyncio.run() for better efficiency
        try:
            execution_result = asyncio.run(
                executor.execute_deployment_workflow(
                    deployment,
                    full_conversation,
                    session_id,
                    execution_id,
                    current_user_query=user_query,
                    chat_file_references=chat_file_references,
                    chat_text_attachments=chat_text_attachments,
                )
            )
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT: Error executing workflow: {e}", exc_info=True)
            _exec_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)
            _fail_result = {'status': 'error', 'error': 'Workflow execution failed'}
            threading.Thread(
                target=_save_deployment_data_async,
                args=(
                    deployment_session,
                    conversation_history,
                    '',
                    execution_id,
                    deployment_request,
                    _fail_result,
                    _exec_time_ms,
                    None,
                    user_query
                ),
                daemon=True,
                name=f"deploy-save-fail-{execution_id[:8]}"
            ).start()
            return JsonResponse({
                'status': 'error',
                'error': 'Workflow execution failed',
                'request_id': request_id
            }, status=500)
        
        execution_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)

        # Extract assistant response
        assistant_response = execution_result.get('response', '') if execution_result.get('status') == 'success' else ''

        # Strip ---CITATIONS--- block and orphan trailing CITATIONS heading from
        # the assistant text, matching the SSE streaming path so API consumers
        # see the same clean response shape. Citation data is exposed separately
        # via execution_result['citations']; the inline block is only used to
        # transport JSON to the client.
        import re as _re_nonstream
        parsed_block_citations = None
        if assistant_response:
            _m = _re_nonstream.search(
                r'(?:---)?CITATIONS(?:---)?\s*([\s\S]*?)\s*---END_?CITATIONS---',
                assistant_response,
            )
            if _m:
                try:
                    parsed_block_citations = json.loads(_m.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass
                assistant_response = assistant_response[:_m.start()].rstrip()
            # Strip orphan trailing CITATIONS heading (markdown variations too).
            assistant_response = _re_nonstream.sub(
                r'[\n\r]+(?:#{1,6}\s*)?(?:\*{1,2})?CITATIONS(?:\*{1,2})?\s*$',
                '',
                assistant_response,
                flags=_re_nonstream.IGNORECASE,
            ).rstrip()

        # Add assistant response to conversation history for background save
        if assistant_response:
            conversation_history.append({
                'role': 'assistant',
                'content': assistant_response,
                'timestamp': timezone.now().isoformat()
            })
        
        # Get workflow_execution_id for background task
        workflow_execution_id = execution_result.get('execution_id')
        
        # Start background task to save all deployment data (non-blocking)
        # Session is persisted even on failure (awaiting_human_input / error) so user message is not lost
        background_thread = threading.Thread(
            target=_save_deployment_data_async,
            args=(
                deployment_session,
                conversation_history,
                assistant_response,
                execution_id,
                deployment_request,
                execution_result,
                execution_time_ms,
                workflow_execution_id,
                user_query
            ),
            daemon=True,
            name=f"deploy-save-{execution_id[:8]}"
        )
        background_thread.start()
        logger.debug(f"🚀 DEPLOYMENT: Started background save task for execution {execution_id[:8]}")
        
        # Return response immediately (don't wait for database writes)
        if execution_result.get('status') == 'success':
            # Prefer citations parsed out of the response text block (matches the
            # streaming endpoint's behavior) over execution_result.citations,
            # since the text-block form carries the LLM-emitted format the UI
            # expects. Fall back to execution_result.citations when no inline
            # block was present.
            response_citations = parsed_block_citations or execution_result.get('citations', [])
            return JsonResponse({
                'status': 'success',
                'response': assistant_response,
                'citations': response_citations,
                'metadata': {
                    'request_id': request_id,
                    'execution_time_ms': execution_time_ms,
                    'workflow_name': execution_result.get('workflow_name', ''),
                    'session_id': session_id
                }
            })
        elif execution_result.get('status') == 'awaiting_human_input':
            # UserProxyAgent requires human input - return special response
            return JsonResponse({
                'status': 'awaiting_human_input',
                'human_input_required': True,
                'title': execution_result.get('title', 'USER INPUT REQUIRED'),
                'last_conversation_message': execution_result.get('last_conversation_message', ''),
                'agent_name': execution_result.get('agent_name', ''),
                'execution_id': execution_result.get('execution_id', ''),
                'session_id': session_id,
                'metadata': {
                    'request_id': request_id,
                    'workflow_name': execution_result.get('workflow_name', '')
                }
            })
        else:
            return JsonResponse({
                'status': 'error',
                'error': execution_result.get('error', 'Workflow execution failed'),
                'request_id': request_id
            }, status=500)
        
    except Exception as e:
        logger.error(f"❌ DEPLOYMENT: Error in public endpoint: {e}", exc_info=True)
        
        # Update tracking record
        if deployment_request:
            try:
                deployment_request.status = WorkflowDeploymentRequestStatus.ERROR
                deployment_request.error_message = str(e)
                deployment_request.execution_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)
                deployment_request.save()
            except Exception:
                pass
        
        return JsonResponse({
            'status': 'error',
            'error': 'An error occurred while processing your request',
            'request_id': request_id
        }, status=500)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def public_chat_endpoint_stream(request, project_id):
    """
    Public streaming chat endpoint for deployed workflows
    
    POST /api/workflow-deploy/{project_id}/stream/
    Returns Server-Sent Events (SSE) stream
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = JsonResponse({'status': 'ok'})
        _add_cors_headers(response, request)
        return response
    
    def generate_stream():
        """Generator function for SSE streaming"""
        deployment_session = None
        conversation_history = None
        try:
            # Generate unique request ID
            origin = request.META.get('HTTP_ORIGIN', '')
            timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
            request_id = f"deploy_stream_{timestamp}_{hash(origin) % 10000:04d}_{uuid.uuid4().hex[:8]}"
            
            start_time = timezone.now()
            
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected', 'request_id': request_id})}\n\n"
            
            # Get project and deployment
            try:
                project = IntelliDocProject.objects.get(project_id=project_id)
            except IntelliDocProject.DoesNotExist:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Project not found', 'request_id': request_id})}\n\n"
                return
            
            deployment = WorkflowDeployment.objects.filter(
                project=project,
                is_active=True
            ).first()
            
            if not deployment:
                yield f"data: {json.dumps({'type': 'error', 'error': 'No active deployment found', 'request_id': request_id})}\n\n"
                return
            
            if not deployment.workflow:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Deployment has no workflow configured', 'request_id': request_id})}\n\n"
                return

            # Public Chat URL auth gate
            auth_block = _enforce_public_chat_auth(request, deployment)
            if auth_block is not None:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Authentication required', 'authRequired': True, 'request_id': request_id})}\n\n"
                return

            # Parse request body
            try:
                data = json.loads(request.body) if request.body else {}
            except json.JSONDecodeError:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Invalid JSON format', 'request_id': request_id})}\n\n"
                return
            
            # Extract user query and session_id
            user_query = data.get('user_query', '').strip()
            if not user_query:
                user_query = data.get('message', '').strip()
            session_id = data.get('session_id', '').strip()
            # provider_file_ids tied to THIS turn — used to persist attachment
            # filenames on the user message so the chip re-renders on reload.
            turn_file_ids = data.get('file_ids') or []

            if not user_query or not session_id:
                yield f"data: {json.dumps({'type': 'error', 'error': 'User query and session ID are required', 'request_id': request_id})}\n\n"
                return
            
            # Get or create deployment session
            try:
                deployment_session, created = DeploymentSession.objects.get_or_create(
                    deployment=deployment,
                    session_id=session_id,
                    defaults={
                        'conversation_history': [],
                        'message_count': 0,
                        'is_active': True
                    }
                )
                
                conversation_history = deployment_session.conversation_history or []
                
                if created:
                    initial_greeting = getattr(deployment, 'initial_greeting', 'Hi! I am your AI assistant.')
                    conversation_history.append({
                        'role': 'assistant',
                        'content': initial_greeting,
                        'timestamp': timezone.now().isoformat()
                    })
                
                # Build file references and text attachments from ALL session files
                chat_file_references = []
                chat_text_attachments = []
                turn_attachments = []
                try:
                    session_files = list(DeploymentSessionFile.objects.filter(session=deployment_session))
                    for sf in session_files:
                        if sf.provider_file_id:
                            chat_file_references.append({
                                'file_id': sf.provider_file_id,
                                'filename': sf.filename,
                                'provider': sf.provider,
                                'file_type': sf.mime_type,
                                'file_size': sf.file_size,
                            })
                        if sf.extracted_text:
                            chat_text_attachments.append({
                                'filename': sf.filename,
                                'text': sf.extracted_text,
                            })
                    if chat_file_references:
                        logger.info(f"📎 CHAT FILES (stream): {len(chat_file_references)} file refs for {session_id[:8]}")
                    if chat_text_attachments:
                        logger.info(f"📎 CHAT TEXT (stream): {len(chat_text_attachments)} text attachments for {session_id[:8]}")
                    if turn_file_ids:
                        turn_attachments = [
                            {'filename': sf.filename}
                            for sf in session_files
                            if sf.provider_file_id in turn_file_ids
                        ]
                except Exception as file_err:
                    logger.warning(f"⚠️ CHAT FILES: Failed to load session files: {file_err}")

                # Add user query to conversation history
                user_turn = {
                    'role': 'user',
                    'content': user_query,
                    'timestamp': timezone.now().isoformat()
                }
                if turn_attachments:
                    user_turn['attachments'] = turn_attachments
                conversation_history.append(user_turn)

                # Build conversation history string
                conversation_text_parts = []
                for msg in conversation_history:
                    role_label = 'User' if msg['role'] == 'user' else 'Assistant'
                    conversation_text_parts.append(f"{role_label}: {msg['content']}")
                
                full_conversation = '\n'.join(conversation_text_parts)
                
            except Exception as e:
                logger.error(f"❌ DEPLOYMENT STREAM: Error managing session: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'error': 'Failed to manage session', 'request_id': request_id})}\n\n"
                return
            
            # Check rate limit
            rate_limiter = WorkflowDeploymentRateLimiter()
            is_allowed, retry_after = rate_limiter.check_rate_limit(deployment, origin)
            
            if not is_allowed:
                # Persist user message so it is not lost (session persistence on failure)
                _save_session_conversation_sync(deployment_session, conversation_history)
                yield f"data: {json.dumps({'type': 'error', 'error': 'Rate limit exceeded', 'retry_after': retry_after, 'request_id': request_id})}\n\n"
                return
            
            # Send thinking indicator
            yield f"data: {json.dumps({'type': 'thinking'})}\n\n"
            
            # Execute workflow in background thread with event queue
            executor = WorkflowDeploymentExecutor()
            execution_id = f"deploy_exec_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            
            event_queue = queue_mod.Queue()

            def _emit_event(event_type, data):
                event_queue.put({"type": event_type, **data})

            result_holder = [None, None]  # [result, error]

            def _run_workflow():
                try:
                    result_holder[0] = asyncio.run(
                        executor.execute_deployment_workflow(
                            deployment,
                            full_conversation,
                            session_id,
                            execution_id,
                            current_user_query=user_query,
                            event_callback=_emit_event,
                            chat_file_references=chat_file_references,
                            chat_text_attachments=chat_text_attachments,
                        )
                    )
                except Exception as exc:
                    result_holder[1] = exc
                finally:
                    event_queue.put({"type": "_done"})

            wf_thread = threading.Thread(
                target=_run_workflow, daemon=True,
                name=f"deploy-wf-{execution_id[:8]}"
            )
            wf_thread.start()

            # Consume intermediate events from queue and yield as SSE.
            # "token" events are live chunks from workflow_executor.py's
            # real-token-streaming path (final-response node only) — translate
            # them into the same {'type': 'content', ...} shape the frontend
            # already renders, so no new client-side event handling is needed.
            # Track whether any arrived so the post-hoc word-chunking loop
            # below can skip re-sending content that was already streamed live.
            live_streamed = False
            while True:
                try:
                    evt = event_queue.get(timeout=0.5)
                except queue_mod.Empty:
                    continue
                if evt["type"] == "_done":
                    break
                if evt["type"] == "token":
                    live_streamed = True
                    yield f"data: {json.dumps({'type': 'content', 'content': evt.get('content', '')})}\n\n"
                else:
                    yield f"data: {json.dumps(evt)}\n\n"

            wf_thread.join(timeout=5)

            if result_holder[1] is not None:
                e = result_holder[1]
                logger.error(f"❌ DEPLOYMENT STREAM: Error executing workflow: {e}", exc_info=True)
                _exec_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)
                _fail_result = {'status': 'error', 'error': 'Workflow execution failed'}
                threading.Thread(
                    target=_save_deployment_data_async,
                    args=(
                        deployment_session,
                        conversation_history,
                        '',
                        execution_id,
                        None,
                        _fail_result,
                        _exec_time_ms,
                        None,
                        user_query
                    ),
                    daemon=True,
                    name=f"deploy-stream-save-fail-{execution_id[:8]}"
                ).start()
                yield f"data: {json.dumps({'type': 'error', 'error': 'Workflow execution failed', 'request_id': request_id})}\n\n"
                return

            execution_result = result_holder[0]
            if execution_result is None:
                err = result_holder[1] or 'Workflow execution did not complete'
                yield f"data: {json.dumps({'type': 'error', 'error': str(err), 'request_id': request_id})}\n\n"
                return
            execution_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)

            # Handle different execution results (persist session on each path so user message is not lost)
            if execution_result.get('status') == 'awaiting_human_input':
                threading.Thread(
                    target=_save_deployment_data_async,
                    args=(
                        deployment_session,
                        conversation_history,
                        '',
                        execution_id,
                        None,
                        execution_result,
                        execution_time_ms,
                        execution_result.get('execution_id'),
                        user_query
                    ),
                    daemon=True,
                    name=f"deploy-stream-save-await-{execution_id[:8]}"
                ).start()
                yield f"data: {json.dumps({'type': 'awaiting_human_input', 'title': execution_result.get('title', 'USER INPUT REQUIRED'), 'last_conversation_message': execution_result.get('last_conversation_message', ''), 'agent_name': execution_result.get('agent_name', ''), 'execution_id': execution_result.get('execution_id', ''), 'session_id': session_id, 'request_id': request_id})}\n\n"
                return
            
            if execution_result.get('status') != 'success':
                threading.Thread(
                    target=_save_deployment_data_async,
                    args=(
                        deployment_session,
                        conversation_history,
                        '',
                        execution_id,
                        None,
                        execution_result,
                        execution_time_ms,
                        execution_result.get('execution_id'),
                        user_query
                    ),
                    daemon=True,
                    name=f"deploy-stream-save-fail-{execution_id[:8]}"
                ).start()
                yield f"data: {json.dumps({'type': 'error', 'error': execution_result.get('error', 'Workflow execution failed'), 'request_id': request_id})}\n\n"
                return
            
            # Extract assistant response
            assistant_response = execution_result.get('response', '')
            
            if not assistant_response:
                _no_resp_result = {'status': 'error', 'error': 'No response generated'}
                threading.Thread(
                    target=_save_deployment_data_async,
                    args=(
                        deployment_session,
                        conversation_history,
                        '',
                        execution_id,
                        None,
                        _no_resp_result,
                        execution_time_ms,
                        execution_result.get('execution_id'),
                        user_query
                    ),
                    daemon=True,
                    name=f"deploy-stream-save-fail-{execution_id[:8]}"
                ).start()
                yield f"data: {json.dumps({'type': 'error', 'error': 'No response generated', 'request_id': request_id})}\n\n"
                return
            
            # Persist conversation (including full assistant response) BEFORE streaming
            # so the response survives even if the client disconnects mid-stream.
            stream_citations = execution_result.get('citations') or []
            logger.info(f"🔍 DEPLOYMENT STREAM: citations from execution_result: {len(stream_citations)} items, keys in result: {list(execution_result.keys())}")
            logger.info(
                f"🔗 CITE[7/OUTPUT]: {len(stream_citations)} citations reaching frontend stream "
                f"(refs: {[c.get('ref') for c in stream_citations]}, "
                f"urls: {[c.get('url','')[:60] for c in stream_citations]})"
            )
            assistant_history_entry = {
                'role': 'assistant',
                'content': assistant_response,
                'timestamp': timezone.now().isoformat(),
            }
            if stream_citations:
                assistant_history_entry['citations'] = stream_citations
            conversation_history.append(assistant_history_entry)
            _save_session_conversation_sync(deployment_session, conversation_history)

            # Strip ---CITATIONS--- block from the response before streaming.
            # Citations are sent as a separate SSE event after the content stream.
            # Tolerant opener: the LLM occasionally drops the leading `---` on
            # the opener (emitting bare `CITATIONS`) — accept both forms but
            # still require the full `---END_CITATIONS---` closer, so we don't
            # over-match when the body legitimately contains the word.
            import re as _re
            _citations_match = _re.search(
                r'(?:---)?CITATIONS(?:---)?\s*([\s\S]*?)\s*---END_?CITATIONS---',
                assistant_response,
            )
            parsed_citations_json = None
            if _citations_match:
                try:
                    parsed_citations_json = json.loads(_citations_match.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass
                assistant_response = assistant_response[:_citations_match.start()].rstrip()
            # Strip trailing "CITATIONS" header some models leave. Match the
            # same shapes the client-side regex handles so we don't leave an
            # orphan `##` / `**` after stripping the literal word:
            #   - "\nCITATIONS"          → bare
            #   - "\n## CITATIONS"       → markdown heading (any level 1-6)
            #   - "\n**CITATIONS**"      → bold markdown
            #   - "\n*CITATIONS*"        → italic markdown
            #   - any combination of the above prefixes + whitespace
            assistant_response = _re.sub(
                r'[\n\r]+(?:#{1,6}\s*)?(?:\*{1,2})?CITATIONS(?:\*{1,2})?\s*$',
                '',
                assistant_response,
                flags=_re.IGNORECASE,
            ).rstrip()

            # Stream the cleaned response word by word so the client can render
            # incrementally. The backend already spent real time computing this
            # response (workflow execution) — do NOT add an artificial per-word
            # delay here. An earlier version slept 20ms between words to fake a
            # "typing" effect, which added several extra seconds of pure latency
            # on top of actual computation for any non-trivial response length,
            # and blocked the worker thread for that whole duration. Any pacing
            # for a typing effect belongs client-side (CSS/JS), where it costs
            # no server latency or worker time.
            # If the final-response node already streamed its tokens live
            # (workflow_executor.py's _stream_or_call_llm, forwarded above as
            # "token" -> "content" SSE events), the client already has this
            # text on screen — sending it again here would duplicate it.
            if not live_streamed:
                words = assistant_response.split(' ')
                accumulated = ""

                for i, word in enumerate(words):
                    accumulated += word
                    if i < len(words) - 1:
                        accumulated += " "

                    # Send chunk
                    yield f"data: {json.dumps({'type': 'content', 'content': word + (' ' if i < len(words) - 1 else ''), 'request_id': request_id})}\n\n"

            # Send citations as a separate SSE event after content stream.
            # Use text-parsed citations if available, otherwise use structured
            # citations from the execution result (metadata path).
            _final_citations = parsed_citations_json or stream_citations
            if _final_citations:
                yield f"data: {json.dumps({'type': 'citations', 'citations': _final_citations, 'request_id': request_id})}\n\n"
                logger.info(f"🔍 DEPLOYMENT STREAM: Sent citations SSE event with {len(_final_citations)} citations (source={'text_block' if parsed_citations_json else 'execution_result'})")
            
            # Save execution metadata in background (conversation already persisted above)
            background_thread = threading.Thread(
                target=_save_deployment_data_async,
                args=(
                    deployment_session,
                    conversation_history,
                    assistant_response,
                    execution_id,
                    None,  # deployment_request (None for streaming)
                    execution_result,
                    execution_time_ms,
                    execution_result.get('execution_id'),
                    user_query
                ),
                daemon=True,
                name=f"deploy-stream-save-{execution_id[:8]}"
            )
            background_thread.start()
            
            # Send completion (stream_citations computed earlier, before streaming).
            # `content` carries the fully-processed authoritative text (citation
            # parse/backfill/reconcile already applied above) — the frontend's
            # `done` handler prefers this over what it accumulated from live
            # "content" events, since reconcile_citations can renumber [N]
            # markers after streaming already started (e.g. dropping an
            # unreferenced citation and shifting the rest down).
            logger.info(f"🔍 DEPLOYMENT STREAM: Sending done event with {len(stream_citations)} citations, parsed_from_text={parsed_citations_json is not None}, live_streamed={live_streamed}")
            yield f"data: {json.dumps({'type': 'done', 'request_id': request_id, 'execution_time_ms': execution_time_ms, 'citations': stream_citations, 'content': assistant_response})}\n\n"
            
        except Exception as e:
            logger.error(f"❌ DEPLOYMENT STREAM: Error in stream generation: {e}", exc_info=True)
            # Persist user message if we already had a session (so it is not lost)
            if deployment_session is not None and conversation_history is not None:
                try:
                    _save_session_conversation_sync(deployment_session, conversation_history)
                    logger.info(f"💾 DEPLOYMENT STREAM: Saved session on outer exception so user message is not lost")
                except Exception as save_err:
                    logger.warning(f"⚠️ DEPLOYMENT STREAM: Failed to save session on outer exception: {save_err}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    
    response = StreamingHttpResponse(generate_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # Disable buffering in nginx
    _add_cors_headers(response, request)

    # Rolling TTL: if this is a public-chat-authenticated request, refresh the
    # cookie so ongoing activity extends the 4h window. Pre-resolve deployment
    # here so we can set the cookie before streaming starts (set_cookie after
    # iteration begins has no effect because headers are already flushed).
    try:
        project = IntelliDocProject.objects.filter(project_id=project_id).first()
        deployment = WorkflowDeployment.objects.filter(project=project, is_active=True).first() if project else None
        if deployment:
            _maybe_refresh_public_chat_cookie(request, response, deployment)
    except Exception:
        pass

    return response


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def submit_deployment_human_input(request, project_id):
    """
    Submit human input for a paused deployment workflow execution.
    
    POST /api/workflow-deploy/{project_id}/submit-input/
    Body: { "session_id": "...", "user_input": "..." }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = JsonResponse({'status': 'ok'})
        return response
    
    origin = request.META.get('HTTP_ORIGIN', '')
    request_id = f"submit_input_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    start_time = timezone.now()

    try:
        # Get project and deployment
        try:
            project = IntelliDocProject.objects.get(project_id=project_id)
        except IntelliDocProject.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'error': 'Project not found',
                'request_id': request_id
            }, status=404)

        deployment = WorkflowDeployment.objects.filter(
            project=project,
            is_active=True
        ).first()

        if not deployment:
            return JsonResponse({
                'status': 'error',
                'error': 'No active deployment found for this project',
                'request_id': request_id
            }, status=404)

        # Public Chat URL auth gate (submit-human-input)
        auth_block = _enforce_public_chat_auth(request, deployment)
        if auth_block is not None:
            return auth_block


        # Parse request body
        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({
                'status': 'error',
                'error': 'Invalid JSON format',
                'request_id': request_id
            }, status=400)
        
        session_id = data.get('session_id', '').strip()
        user_input = data.get('user_input', '').strip()
        
        if not session_id:
            return JsonResponse({
                'status': 'error',
                'error': 'Session ID is required',
                'request_id': request_id
            }, status=400)
        
        if not user_input:
            return JsonResponse({
                'status': 'error',
                'error': 'User input is required',
                'request_id': request_id
            }, status=400)
        
        # Get deployment session
        try:
            deployment_session = DeploymentSession.objects.get(
                deployment=deployment,
                session_id=session_id
            )
        except DeploymentSession.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'error': 'Session not found',
                'request_id': request_id
            }, status=404)
        
        # Check if session is awaiting human input
        if not deployment_session.awaiting_human_input:
            return JsonResponse({
                'status': 'error',
                'error': 'Session is not awaiting human input',
                'request_id': request_id
            }, status=400)
        
        # Get the paused execution
        if not deployment_session.paused_execution_id:
            return JsonResponse({
                'status': 'error',
                'error': 'No paused execution found for this session',
                'request_id': request_id
            }, status=400)
        
        # Resume workflow execution asynchronously (non-blocking)
        import threading
        from .conversation_orchestrator import ConversationOrchestrator
        
        # Create a thread-safe result container (appended_input: True if thread already saved human message)
        result_container = {'result': None, 'error': None, 'completed': False, 'appended_input': False}
        
        def resume_workflow_async():
            """Resume workflow in background thread"""
            from django.db import close_old_connections
            close_old_connections()
            
            try:
                from users.models import WorkflowExecution
                # Get fresh session and execution records in the background thread
                session = DeploymentSession.objects.get(
                    deployment=deployment,
                    session_id=session_id
                )
                
                if not session.paused_execution_id:
                    raise ValueError(f"No paused execution ID found in session {session_id}")
                
                logger.info(f"🔍 DEPLOYMENT: Looking for execution_id: {session.paused_execution_id}")
                
                try:
                    execution_record = WorkflowExecution.objects.get(
                        execution_id=session.paused_execution_id
                    )
                    logger.info(f"✅ DEPLOYMENT: Found execution record {execution_record.execution_id[:8]}")
                    
                    # Verify execution is in a valid state for resuming
                    from users.models import WorkflowExecutionStatus
                    if execution_record.status not in [WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.PENDING]:
                        logger.error(f"❌ DEPLOYMENT: Execution {execution_record.execution_id[:8]} is in {execution_record.status} state, cannot resume")
                        raise ValueError(f"Execution {execution_record.execution_id} is in {execution_record.status} state, cannot resume")
                    
                except WorkflowExecution.DoesNotExist:
                    logger.error(f"❌ DEPLOYMENT: WorkflowExecution {session.paused_execution_id} not found")
                    logger.error(f"❌ DEPLOYMENT: Session paused_execution_id: {session.paused_execution_id}")
                    logger.error(f"❌ DEPLOYMENT: Session awaiting_human_input: {session.awaiting_human_input}")
                    
                    # Try to find the execution by checking recent executions for this workflow
                    workflow = deployment.workflow
                    if workflow:
                        recent_execution = WorkflowExecution.objects.filter(
                            workflow=workflow
                        ).order_by('-start_time').first()
                        
                        if recent_execution:
                            # Verify execution is in a valid state for resuming
                            from users.models import WorkflowExecutionStatus
                            if recent_execution.status not in [WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.PENDING]:
                                logger.error(f"❌ DEPLOYMENT: Recent execution {recent_execution.execution_id[:8]} is in {recent_execution.status} state, cannot resume")
                                raise ValueError(f"Execution {recent_execution.execution_id} is in {recent_execution.status} state, cannot resume")
                            
                            logger.warning(f"⚠️ DEPLOYMENT: Using recent execution {recent_execution.execution_id[:8]} instead")
                            execution_record = recent_execution
                            # Update session with correct execution_id
                            session.paused_execution_id = execution_record.execution_id
                            session.save()
                        else:
                            raise ValueError(f"WorkflowExecution {session.paused_execution_id} not found and no recent executions available")
                    else:
                        raise ValueError(f"WorkflowExecution {session.paused_execution_id} not found and no workflow available")
                
                # Add user input to conversation history immediately (before resuming workflow)
                # This ensures it appears in the chat UI right away
                session.conversation_history.append({
                    'role': 'user',
                    'content': user_input,
                    'timestamp': timezone.now().isoformat()
                })
                session.message_count = len(session.conversation_history)
                session.last_activity = timezone.now()
                session.save()
                result_container['appended_input'] = True
                logger.info(f"📝 DEPLOYMENT: Added user input to session conversation history: {user_input[:100]}...")
                
                # Resume workflow with user input
                orchestrator = ConversationOrchestrator()
                result = asyncio.run(
                    orchestrator.resume_workflow_with_human_input(
                        execution_record.execution_id,
                        user_input,
                        deployment.created_by
                    )
                )
                
                result_container['result'] = result
                result_container['completed'] = True
                
                # Clear pause state from session
                session.awaiting_human_input = False
                session.paused_execution_id = None
                session.human_input_prompt = ''
                session.human_input_agent_name = ''
                session.human_input_agent_id = ''
                session.save()
                
                logger.info(f"✅ DEPLOYMENT: Resumed workflow execution {execution_record.execution_id[:8]} with user input")
                
            except Exception as e:
                logger.error(f"❌ DEPLOYMENT: Failed to resume workflow: {e}", exc_info=True)
                result_container['error'] = str(e)
                result_container['completed'] = True
        
        # Start background thread
        resume_thread = threading.Thread(
            target=resume_workflow_async,
            daemon=True,
            name=f"deploy-resume-{deployment_session.paused_execution_id[:8]}"
        )
        resume_thread.start()
        
        # Wait for completion (with timeout)
        import time
        timeout = 60  # 60 seconds timeout
        elapsed = 0
        while not result_container['completed'] and elapsed < timeout:
            time.sleep(0.1)
            elapsed += 0.1
        
        if not result_container['completed']:
            # Timeout - return response indicating processing is ongoing
            return JsonResponse({
                'status': 'processing',
                'message': 'Workflow is being processed. Please check back shortly.',
                'session_id': session_id,
                'request_id': request_id
            })
        
        if result_container['error']:
            # Persist human input to session when resume failed and thread did not append it
            if not result_container.get('appended_input'):
                try:
                    deployment_session.conversation_history = deployment_session.conversation_history or []
                    deployment_session.conversation_history.append({
                        'role': 'user',
                        'content': user_input,
                        'timestamp': timezone.now().isoformat()
                    })
                    deployment_session.message_count = len(deployment_session.conversation_history)
                    deployment_session.last_activity = timezone.now()
                    deployment_session.save()
                    logger.info(f"📝 DEPLOYMENT: Persisted human input to session after resume failure: {user_input[:100]}...")
                except Exception as save_err:
                    logger.warning(f"⚠️ DEPLOYMENT: Failed to persist human input on resume failure: {save_err}")
            return JsonResponse({
                'status': 'error',
                'error': result_container['error'],
                'request_id': request_id
            }, status=500)
        
        # Extract response from result
        result = result_container['result']
        
        # Check if workflow is still awaiting human input (another UserProxyAgent)
        if result.get('status') == 'awaiting_human_input':
            # Another UserProxyAgent requires input - return the same format
            return JsonResponse({
                'status': 'awaiting_human_input',
                'human_input_required': True,
                'title': result.get('title', 'USER INPUT REQUIRED'),
                'last_conversation_message': result.get('last_conversation_message', ''),
                'agent_name': result.get('agent_name', ''),
                'execution_id': result.get('execution_id', ''),
                'session_id': session_id,
                'metadata': {
                    'request_id': request_id
                }
            })
        elif result.get('status') == 'success':
            # Extract response from execution result
            from .deployment_executor import WorkflowDeploymentExecutor
            executor = WorkflowDeploymentExecutor()
            
            # Get workflow graph for response extraction
            workflow = deployment.workflow
            if workflow:
                graph_json = workflow.graph_json
                assistant_response, response_citations = executor._extract_end_node_output(result, graph_json)
            else:
                # Fallback: try to get from result directly
                assistant_response = result.get('response', '') or result.get('conversation_history', '')
                response_citations = result.get('citations') or []
            
            # User input was already added to conversation history when submitted
            # Just add the assistant response now
            if assistant_response:
                deployment_session.conversation_history.append({
                    'role': 'assistant',
                    'content': assistant_response,
                    'timestamp': timezone.now().isoformat()
                })
            deployment_session.message_count = len(deployment_session.conversation_history)
            deployment_session.last_activity = timezone.now()
            deployment_session.save()
            
            execution_time_ms = int((timezone.now() - start_time).total_seconds() * 1000)
            
            return JsonResponse({
                'status': 'success',
                'response': assistant_response,
                'citations': response_citations or [],
                'metadata': {
                    'request_id': request_id,
                    'execution_time_ms': execution_time_ms,
                    'session_id': session_id
                }
            })
        else:
            return JsonResponse({
                'status': 'error',
                'error': result.get('error', 'Workflow execution failed'),
                'request_id': request_id
            }, status=500)
        
    except Exception as e:
        logger.error(f"❌ DEPLOYMENT: Error in submit-input endpoint: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'error': 'An error occurred while processing your input',
            'request_id': request_id
        }, status=500)


def _get_deployment_llm_provider(workflow):
    """Extract LLM provider from the first AssistantAgent node in a workflow."""
    for node in workflow.graph_json.get('nodes', []):
        if node.get('type') == 'AssistantAgent':
            return node.get('data', {}).get('llm_provider', 'openai').lower()
    return 'openai'


# Extensions supported by each provider's File API (uploaded as file references)
CHAT_UPLOAD_FILE_API_EXTENSIONS = {
    'openai': {'.pdf'},
    'anthropic': {'.pdf', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.webp'},
    'google': {'.pdf', '.txt', '.doc', '.docx', '.md', '.rtf', '.png', '.jpg', '.jpeg'},
}
CHAT_UPLOAD_FILE_API_DEFAULT = {'.pdf'}
# Extensions we can handle via server-side text extraction (fallback for unsupported File API types)
CHAT_UPLOAD_TEXT_EXTRACTABLE = {'.docx', '.doc', '.txt', '.md', '.rtf', '.csv', '.xlsx', '.html'}
CHAT_UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50 MB (OpenAI limit, strictest)
CHAT_UPLOAD_MAX_FILES_PER_SESSION = 10


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@never_cache
def upload_chat_file(request, project_id):
    """
    Upload a file for use in the deployed chatbot.
    POST /api/workflow-deploy/{project_id}/upload-file/
    Multipart: file, session_id
    """
    if request.method == 'OPTIONS':
        response = JsonResponse({'status': 'ok'})
        _add_cors_headers(response, request)
        return response

    try:
        # Validate project + deployment
        try:
            project = IntelliDocProject.objects.get(project_id=project_id)
        except IntelliDocProject.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)

        deployment = WorkflowDeployment.objects.filter(project=project, is_active=True).first()
        if not deployment:
            return JsonResponse({'error': 'No active deployment'}, status=404)

        if not deployment.file_uploads_enabled:
            return JsonResponse({'error': 'File uploads are not enabled for this chatbot'}, status=403)

        # Public Chat URL auth gate
        auth_block = _enforce_public_chat_auth(request, deployment)
        if auth_block is not None:
            return auth_block

        # Parse session_id
        session_id = request.POST.get('session_id', '').strip()
        if not session_id:
            return JsonResponse({'error': 'session_id is required'}, status=400)

        session, _ = DeploymentSession.objects.get_or_create(
            deployment=deployment,
            session_id=session_id,
            defaults={'conversation_history': [], 'message_count': 0, 'is_active': True}
        )

        # Check per-session file limit and deduplication
        from .models import DeploymentSessionFile

        # Deduplicate: if same filename already exists in session, return existing
        upload_peek = request.FILES.get('file')
        if upload_peek:
            existing_file = DeploymentSessionFile.objects.filter(
                session=session, filename__iexact=upload_peek.name
            ).first()
            if existing_file:
                response = JsonResponse({
                    'attachment_id': str(existing_file.id),
                    'file_id': existing_file.provider_file_id or 'text_extracted',
                    'filename': existing_file.filename,
                    'provider': existing_file.provider,
                    'size': existing_file.file_size,
                    'mime_type': existing_file.mime_type,
                    'mode': 'file_api' if existing_file.provider_file_id else 'text_extraction',
                    'deduplicated': True,
                }, status=200)
                _add_cors_headers(response, request)
                return response

        existing_count = DeploymentSessionFile.objects.filter(session=session).count()
        if existing_count >= CHAT_UPLOAD_MAX_FILES_PER_SESSION:
            return JsonResponse({
                'error': f'Maximum {CHAT_UPLOAD_MAX_FILES_PER_SESSION} files per session'
            }, status=400)

        # Determine LLM provider from workflow
        workflow = deployment.workflow
        provider = _get_deployment_llm_provider(workflow)

        # Validate file
        upload = request.FILES.get('file')
        if not upload:
            return JsonResponse({'error': 'No file provided'}, status=400)

        # Normalize filename extension to lowercase (OpenAI rejects .PDF but accepts .pdf)
        raw_name = upload.name
        name_base, name_ext = os.path.splitext(raw_name)
        filename = name_base + name_ext.lower()
        ext = name_ext.lower()
        mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        file_api_exts = CHAT_UPLOAD_FILE_API_EXTENSIONS.get(provider, CHAT_UPLOAD_FILE_API_DEFAULT)
        use_file_api = ext in file_api_exts
        use_text_extraction = ext in CHAT_UPLOAD_TEXT_EXTRACTABLE

        if not use_file_api and not use_text_extraction:
            all_supported = sorted(file_api_exts | CHAT_UPLOAD_TEXT_EXTRACTABLE)
            return JsonResponse({
                'error': f'Unsupported file type: {ext}. Allowed: {", ".join(all_supported)}'
            }, status=400)

        if upload.size > CHAT_UPLOAD_MAX_SIZE:
            return JsonResponse({
                'error': f'File too large ({upload.size // (1024*1024)}MB). Maximum: {CHAT_UPLOAD_MAX_SIZE // (1024*1024)}MB'
            }, status=400)

        provider_file_id = ''
        extracted_text = ''

        # Always extract text FIRST (before default_storage.save moves the temp file)
        from .document_processor import DocumentProcessor
        _dp = DocumentProcessor()
        _dp_method = _dp.SUPPORTED_FORMATS.get(ext)
        if _dp_method:
            try:
                upload.seek(0)
                extracted_text = getattr(_dp, _dp_method)(upload) or ''
                if extracted_text.strip():
                    logger.info(f"📎 CHAT UPLOAD (text): {filename} → {len(extracted_text)} chars extracted")
            except Exception as _te:
                logger.warning(f"⚠️ CHAT UPLOAD: Text extraction failed for {filename}: {_te}")
                extracted_text = ''

        # Save to storage (this moves the temp file — must happen after text extraction)
        upload.seek(0)
        storage_path = default_storage.save(
            os.path.join('chat_uploads', str(project_id), session_id, f'{uuid.uuid4().hex}_{filename}'),
            upload,
        )

        if use_file_api:
            # Also upload to LLM provider via File API (used when no tool-calling)
            # Reuse the storage_path from the initial save (don't save twice — temp file is already moved)
            from types import SimpleNamespace
            pseudo_doc = SimpleNamespace(
                file_path=storage_path,
                file_size=upload.size,
                file_extension=ext,
                file_type=mime_type,
                original_filename=filename,
                document_id=None,
            )

            from .llm_file_service import LLMFileUploadService
            from asgiref.sync import async_to_sync
            service = LLMFileUploadService(project)
            result = async_to_sync(service._upload_to_provider)(pseudo_doc, provider)

            if result.get('file_id'):
                provider_file_id = result['file_id']
                logger.info(f"📎 CHAT UPLOAD (File API): {filename} ({upload.size} bytes) → {provider} file_id={provider_file_id[:20]}... (session {session_id[:8]})")
            else:
                logger.warning(f"⚠️ CHAT UPLOAD: File API upload failed for {filename}, text extraction available as fallback")

        if not provider_file_id and not extracted_text:
            try:
                if default_storage.exists(storage_path):
                    default_storage.delete(storage_path)
            except Exception:
                pass
            return JsonResponse({
                'error': f'Could not process {filename} — no text could be extracted'
            }, status=400)

        # Clean up temp storage
        try:
            if default_storage.exists(storage_path):
                default_storage.delete(storage_path)
        except Exception:
            pass

        # Create session file record
        session_file = DeploymentSessionFile.objects.create(
            session=session,
            filename=filename,
            file_extension=ext,
            mime_type=mime_type,
            file_size=upload.size,
            storage_path='',
            provider=provider,
            provider_file_id=provider_file_id,
            extracted_text=extracted_text,
        )

        response = JsonResponse({
            'attachment_id': str(session_file.id),
            'file_id': provider_file_id or 'text_extracted',
            'filename': filename,
            'provider': provider,
            'size': upload.size,
            'mime_type': mime_type,
            'mode': 'file_api' if provider_file_id else 'text_extraction',
        }, status=201)
        _add_cors_headers(response, request)
        return response

    except Exception as e:
        logger.error(f"❌ CHAT UPLOAD: Error: {e}", exc_info=True)
        response = JsonResponse({'error': 'File upload failed'}, status=500)
        _add_cors_headers(response, request)
        return response


def _frame_ancestors_csp(deployment) -> str:
    """Build a CSP `frame-ancestors` directive from the deployment's origins.

    The embed view is xframe_options_exempt because third-party sites are
    *meant* to iframe it — that is the whole product. X-Frame-Options cannot
    express an allowlist (it is only SAMEORIGIN or DENY), so clickjacking
    protection for this endpoint is carried by frame-ancestors instead, built
    from the same WorkflowAllowedOrigin rows that govern the chat API's CORS.

    Origins are stored as the operator typed them and may carry a path
    (e.g. 'https://fairsharing.gitbook.io/fairsharing'), but a frame-ancestors
    host-source must not, so each is reduced to scheme://host[:port].
    """
    from urllib.parse import urlsplit

    sources = ["'self'"]
    seen = set()
    try:
        origins = list(deployment.allowed_origins.filter(is_active=True))
    except Exception as exc:
        logger.warning(
            '⚠️ EMBED CSP: could not read allowed origins for deployment %s: %s',
            getattr(deployment, 'id', '?'), exc,
        )
        origins = []
    for row in origins:
        raw = (getattr(row, 'origin', '') or '').strip()
        if not raw:
            continue
        parts = urlsplit(raw if '//' in raw else f'//{raw}', scheme='https')
        if not parts.hostname:
            continue
        source = f'{parts.scheme}://{parts.netloc}'
        if source not in seen:
            seen.add(source)
            sources.append(source)
    return 'frame-ancestors ' + ' '.join(sources)


@csrf_exempt
@xframe_options_exempt
def embed_chatbot_html(request, project_id):
    """
    Serve the chatbot HTML for iframe embedding.
    This endpoint returns a complete HTML page with the chatbot interface.
    Features a modern glassmorphism design with customizable branding.
    """
    from django.shortcuts import get_object_or_404
    from django.http import HttpResponse
    from users.models import IntelliDocProject
    
    try:
        project = get_object_or_404(IntelliDocProject, project_id=project_id)
        deployment = WorkflowDeployment.objects.filter(
            project=project,
            is_active=True
        ).first()
        
        if not deployment or not deployment.workflow:
            return HttpResponse(
                '<html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f7;"><p style="color:#6b7280;">Chatbot not available. Please ensure deployment is active and a workflow is configured.</p></body></html>',
                status=404,
                content_type='text/html'
            )

        # Public Chat URL auth gate — when auth is enabled, the iframe must
        # only render for requests that carry a valid public-chat cookie or
        # come from an allowed external origin. Prevents direct /embed/ URL
        # bypass of the /chat/<id> login form.
        auth_block = _enforce_public_chat_auth(request, deployment)
        if auth_block is not None:
            return HttpResponse(
                '<html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f7;"><p style="color:#6b7280;">Please sign in to use this chat.</p></body></html>',
                status=401,
                content_type='text/html'
            )

        # Absolute URL fallback (e.g. external embeds). In-app iframe must use the browser's
        # origin — build_absolute_uri often yields 127.0.0.1:8000 or internal Docker host behind
        # Vite/nginx proxy, which the user's browser cannot reach → fetch fails ("connection problem").
        # Use X-Forwarded-Proto to determine the scheme when behind a reverse proxy.
        base_url = request.build_absolute_uri('/').rstrip('/')
        if request.META.get('HTTP_X_FORWARDED_PROTO') == 'https' and base_url.startswith('http://'):
            base_url = 'https://' + base_url[len('http://'):]
        endpoint_url = f"{base_url}{deployment.endpoint_path}"
        initial_greeting = getattr(deployment, 'initial_greeting', 'Hi! I am your AI assistant.')

        # Hide header when embedded inside in-app wrapper (avoids double header)
        hide_header = request.GET.get('hide_header', '') == '1'

        # Server-side preload: inject conversation history so the iframe doesn't need auth
        preloaded_history = []
        session_id_param = request.GET.get('session_id', '').strip()
        if session_id_param:
            try:
                existing_session = DeploymentSession.objects.filter(
                    deployment=deployment, session_id=session_id_param
                ).first()
                if existing_session and existing_session.conversation_history:
                    preloaded_history = existing_session.conversation_history[-100:]
            except Exception:
                pass

        # Get branding customization with defaults
        chatbot_title = getattr(deployment, 'chatbot_title', 'AI Assistant')
        chatbot_subtitle = getattr(deployment, 'chatbot_subtitle', 'Powered by AICC IntelliDoc')
        primary_color = getattr(deployment, 'primary_color', '#ffffff')
        secondary_color = getattr(deployment, 'secondary_color', '#ffffff')
        font_color = getattr(deployment, 'font_color', '#000000')
        logo_url = getattr(deployment, 'logo_url', None) or ''
        file_uploads_enabled = getattr(deployment, 'file_uploads_enabled', False)

        # Generate the modern HTML with glassmorphism design
        html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>{chatbot_title}</title>
  <style>
    :root {{
      --primary-color: {primary_color};
      --secondary-color: {secondary_color};
      --font-color: {font_color};
      --primary-rgb: {_embed_hex_to_rgb_csv(primary_color, '255, 255, 255')};
      --secondary-rgb: {_embed_hex_to_rgb_csv(secondary_color, '255, 255, 255')};
    }}
    
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    
    html, body {{
      font-family: 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: transparent;
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
    }}
    
    .chat-container {{
      width: 100%;
      height: 100%;
      background: rgba(255, 255, 255, 0.95);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border-radius: 0;
      box-shadow: 
        0 25px 50px -12px rgba(0, 0, 0, 0.25),
        0 0 0 1px rgba(255, 255, 255, 0.1);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transition: transform 0.3s ease, box-shadow 0.3s ease;
    }}
    
    .chat-container:hover {{
      transform: translateY(-2px);
      box-shadow: 
        0 30px 60px -12px rgba(0, 0, 0, 0.3),
        0 0 0 1px rgba(255, 255, 255, 0.15);
    }}
    
    .chat-header {{
      padding: 20px 24px;
      background: linear-gradient(135deg, var(--primary-color) 0%, var(--secondary-color) 100%);
      color: var(--font-color, #000);
      display: {'none' if hide_header else 'flex'};
      align-items: center;
      gap: 14px;
      position: relative;
      overflow: hidden;
    }}
    
    .chat-header::before {{
      content: '';
      position: absolute;
      top: -50%;
      right: -50%;
      width: 100%;
      height: 200%;
      background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
      pointer-events: none;
    }}
    
    .header-logo {{
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.2);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      backdrop-filter: blur(10px);
      overflow: hidden;
    }}
    
    .header-logo img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      border-radius: 12px;
    }}
    
    .header-logo-placeholder {{
      font-size: 20px;
      font-weight: 700;
      color: var(--font-color, #000);
      text-transform: uppercase;
    }}
    
    .header-text {{
      flex: 1;
      min-width: 0;
    }}
    
    .chat-header-title {{
      font-weight: 700;
      font-size: 17px;
      letter-spacing: -0.3px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    
    .chat-header-sub {{
      font-size: 12px;
      opacity: 0.85;
      margin-top: 2px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    
    .online-indicator {{
      width: 10px;
      height: 10px;
      background: #22c55e;
      border-radius: 50%;
      box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.3);
      animation: pulse 2s infinite;
    }}
    
    @keyframes pulse {{
      0%, 100% {{ box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.3); }}
      50% {{ box-shadow: 0 0 0 6px rgba(34, 197, 94, 0.1); }}
    }}
    
    .chat-messages {{
      flex: 1;
      padding: 20px;
      overflow-y: auto;
      scrollbar-gutter: stable;
      background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
      font-size: 14px;
      scroll-behavior: smooth;
    }}
    
    .chat-messages::-webkit-scrollbar {{
      width: 7px;
    }}
    
    .chat-messages::-webkit-scrollbar-track {{
      background: rgba(0, 0, 0, 0.03);
    }}
    
    .chat-messages::-webkit-scrollbar-thumb {{
      background: rgba(0, 0, 0, 0.15);
      border-radius: 4px;
    }}
    
    .chat-messages::-webkit-scrollbar-thumb:hover {{
      background: rgba(0, 0, 0, 0.25);
    }}
    
    .msg {{
      margin-bottom: 16px;
      display: flex;
      animation: slideIn 0.3s ease-out;
    }}
    
    @keyframes slideIn {{
      from {{
        opacity: 0;
        transform: translateY(10px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}
    
    .msg.user {{
      justify-content: flex-end;
    }}
    
    .msg.assistant {{
      justify-content: flex-start;
    }}
    
    .bubble {{
      max-width: 85%;
      padding: 12px 16px;
      border-radius: 18px;
      line-height: 1.6;
      position: relative;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
    }}
    
    .msg.user .bubble {{
      background: var(--font-color, #000);
      color: #fff;
      border-bottom-right-radius: 6px;
    }}
    
    .msg.assistant .bubble {{
      background: #ffffff;
      color: var(--font-color, #1e293b);
      border: 1px solid rgba(0, 0, 0, 0.06);
      border-bottom-left-radius: 6px;
    }}
    
    .chat-input-container {{
      padding: 16px 20px 20px;
      background: #ffffff;
      border-top: 1px solid rgba(0, 0, 0, 0.06);
    }}
    
    .chat-input {{
      display: flex;
      align-items: flex-end;
      gap: 12px;
      background: #f1f5f9;
      border-radius: 16px;
      padding: 8px 8px 8px 16px;
      transition: box-shadow 0.2s ease, background 0.2s ease;
    }}
    
    .chat-input:focus-within {{
      background: #fff;
      box-shadow: 0 0 0 2px var(--primary-color), 0 4px 12px rgba(var(--primary-rgb), 0.15);
    }}
    
    .chat-input textarea {{
      flex: 1;
      resize: none;
      border: none;
      background: transparent;
      padding: 8px 0;
      font-size: 14px;
      line-height: 1.5;
      color: #1e293b;
      font-family: inherit;
      min-height: 24px;
      max-height: 310px;
      overflow-y: auto;
    }}
    
    .chat-input textarea::placeholder {{
      color: #94a3b8;
    }}
    
    .chat-input textarea:focus {{
      outline: none;
    }}
    
    .chat-input button {{
      width: 40px;
      height: 40px;
      background: var(--font-color, #000);
      color: #fff;
      border: none;
      border-radius: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      flex-shrink: 0;
    }}
    
    .chat-input button:hover:not(:disabled) {{
      transform: scale(1.05);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }}
    
    .chat-input button:active:not(:disabled) {{
      transform: scale(0.95);
    }}
    
    .chat-input button:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
    }}
    
    .chat-input button:focus-visible {{
      outline: 2px solid var(--primary-color);
      outline-offset: 2px;
    }}
    
    .chat-input button svg {{
      width: 20px;
      height: 20px;
      transition: transform 0.2s ease;
    }}
    
    .chat-input button:hover:not(:disabled) svg {{
      transform: translateX(2px);
    }}
    
    .status {{
      font-size: 11px;
      color: #64748b;
      padding: 8px 20px 0;
      text-align: center;
    }}
    
    .human-input-modal {{
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(4px);
      z-index: 1000;
      justify-content: center;
      align-items: center;
      padding: 16px;
    }}
    
    .human-input-modal.active {{
      display: flex;
    }}
    
    .human-input-box {{
      background: #fff;
      border-radius: 20px;
      padding: 28px;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.4);
      animation: modalSlideIn 0.3s ease-out;
    }}
    
    @keyframes modalSlideIn {{
      from {{
        opacity: 0;
        transform: scale(0.95) translateY(10px);
      }}
      to {{
        opacity: 1;
        transform: scale(1) translateY(0);
      }}
    }}
    
    .human-input-title {{
      font-size: 18px;
      font-weight: 700;
      color: var(--primary-color);
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    
    .human-input-title::before {{
      content: '';
      width: 4px;
      height: 20px;
      background: linear-gradient(135deg, var(--primary-color) 0%, var(--secondary-color) 100%);
      border-radius: 2px;
    }}
    
    .human-input-message {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
      font-size: 14px;
      color: #475569;
      line-height: 1.6;
    }}
    
    .human-input-textarea {{
      width: 100%;
      min-height: 100px;
      border: 2px solid #e2e8f0;
      border-radius: 12px;
      padding: 14px;
      font-size: 14px;
      resize: vertical;
      font-family: inherit;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }}
    
    .human-input-textarea:focus {{
      outline: none;
      border-color: var(--primary-color);
      box-shadow: 0 0 0 3px rgba(var(--primary-rgb), 0.1);
    }}
    
    .human-input-buttons {{
      display: flex;
      gap: 12px;
      justify-content: flex-end;
      margin-top: 20px;
    }}
    
    .human-input-buttons button {{
      padding: 12px 24px;
      border-radius: 12px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: all 0.2s ease;
    }}
    
    .human-input-buttons .submit-btn {{
      background: linear-gradient(135deg, var(--primary-color) 0%, var(--secondary-color) 100%);
      color: #fff;
    }}
    
    .human-input-buttons .submit-btn:hover:not(:disabled) {{
      box-shadow: 0 4px 12px rgba(var(--primary-rgb), 0.4);
      transform: translateY(-1px);
    }}
    
    .human-input-buttons .submit-btn:disabled {{
      opacity: 0.6;
      cursor: not-allowed;
    }}
    
    .human-input-buttons .cancel-btn {{
      background: #f1f5f9;
      color: #475569;
    }}
    
    .human-input-buttons .cancel-btn:hover {{
      background: #e2e8f0;
    }}
    
    .thinking-indicator {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px 16px;
    }}
    
    .thinking-dots {{
      display: flex;
      gap: 5px;
    }}
    
    .thinking-dot {{
      width: 8px;
      height: 8px;
      background: var(--primary-color);
      border-radius: 50%;
      animation: bounce 1.4s infinite ease-in-out both;
    }}
    
    .thinking-dot:nth-child(1) {{ animation-delay: -0.32s; }}
    .thinking-dot:nth-child(2) {{ animation-delay: -0.16s; }}
    .thinking-dot:nth-child(3) {{ animation-delay: 0s; }}
    
    @keyframes bounce {{
      0%, 80%, 100% {{
        transform: scale(0.6);
        opacity: 0.4;
      }}
      40% {{
        transform: scale(1);
        opacity: 1;
      }}
    }}
    
    /* Markdown Styles */
    .bubble markdown {{
      display: block;
    }}
    
    .bubble markdown p {{
      margin: 6px 0;
    }}
    
    .bubble markdown p:first-child {{
      margin-top: 0;
    }}
    
    .bubble markdown p:last-child {{
      margin-bottom: 0;
    }}
    
    .bubble markdown strong {{
      font-weight: 600;
    }}
    
    .bubble markdown em {{
      font-style: italic;
    }}
    
    .bubble markdown code {{
      background: rgba(0, 0, 0, 0.06);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: 'SF Mono', 'Consolas', monospace;
      font-size: 0.875em;
    }}
    
    .msg.user .bubble markdown code {{
      background: rgba(255, 255, 255, 0.2);
    }}
    
    .bubble markdown pre {{
      background: #1e293b;
      color: #e2e8f0;
      padding: 14px;
      border-radius: 10px;
      overflow-x: auto;
      margin: 10px 0;
    }}
    
    .bubble markdown pre code {{
      background: none;
      padding: 0;
      color: inherit;
    }}
    
    .bubble markdown ul, .bubble markdown ol {{
      margin: 4px 0 8px;
      padding-left: 22px;
    }}

    .bubble markdown li {{
      margin: 1px 0;
      line-height: 1.45;
    }}

    /* Kill the stacked margin when a list follows a paragraph — the
       paragraph's own margin-bottom is enough breathing room. */
    .bubble markdown p + ul,
    .bubble markdown p + ol {{
      margin-top: 0;
    }}

    /* Tighten heading → body paragraph spacing. */
    .bubble markdown h1 + p,
    .bubble markdown h2 + p,
    .bubble markdown h3 + p {{
      margin-top: 2px;
    }}

    /* Section labels auto-generated by renderMarkdown (a <p> whose only
       child is a <strong>, e.g. "(1) Feature summary") get extra top air
       so consecutive sections feel distinct. */
    .bubble markdown p:has(> strong:only-child) {{
      margin-top: 12px;
      margin-bottom: 4px;
    }}

    .bubble markdown p:first-child:has(> strong:only-child) {{
      margin-top: 0;
    }}
    
    .bubble markdown blockquote {{
      border-left: 3px solid var(--primary-color);
      padding-left: 12px;
      margin: 10px 0;
      color: #64748b;
      font-style: italic;
    }}
    
    .bubble markdown a {{
      color: var(--font-color, #000);
      text-decoration: underline;
    }}
    
    .msg.user .bubble markdown a {{
      color: #fff;
    }}
    
    .bubble markdown h1 {{ font-size: 1.4em; font-weight: 700; margin: 14px 0 8px; }}
    .bubble markdown h2 {{ font-size: 1.25em; font-weight: 700; margin: 12px 0 6px; }}
    .bubble markdown h3 {{ font-size: 1.1em; font-weight: 600; margin: 10px 0 4px; }}
    .bubble markdown hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 12px 0; }}

    .cite-chip {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 4px;
      border-radius: 4px;
      background: var(--primary-color, #0ea5e9);
      color: white;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      vertical-align: super;
      margin: 0 1px;
      line-height: 1;
      transition: background 0.15s;
    }}
    .cite-chip:hover {{
      filter: brightness(0.85);
    }}
    .cite-chip-secondary {{
      background: #e5e7eb;
      color: #374151;
      cursor: default;
    }}
    .cite-chip-secondary:hover {{
      filter: none;
    }}
    .cite-tooltip {{
      position: fixed;
      max-width: 340px;
      background: #1e293b;
      color: #e2e8f0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 13px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.3);
      z-index: 9999;
      animation: citeIn 0.15s ease-out;
    }}
    .cite-tooltip-title {{
      font-weight: 600;
      color: #38bdf8;
      margin-bottom: 6px;
      font-size: 12px;
    }}
    .cite-tooltip-link {{
      color: #38bdf8;
      text-decoration: underline;
      text-underline-offset: 2px;
      cursor: pointer;
      transition: color 0.15s;
    }}
    .cite-tooltip-link:hover {{
      color: #7dd3fc;
    }}
    .cite-tooltip-source {{
      font-size: 11px;
      color: #94a3b8;
      margin-bottom: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .cite-tooltip-quote {{
      font-style: italic;
      color: #cbd5e1;
      font-size: 12px;
      line-height: 1.5;
    }}
    @keyframes citeIn {{
      from {{ opacity: 0; transform: translateY(-4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    /* ── Copy button ─────────────────────────────────────────── */
    .bubble-actions {{
      display: flex;
      justify-content: flex-end;
      margin-top: 6px;
      padding-top: 4px;
      border-top: 1px solid rgba(0,0,0,0.04);
      opacity: 0;
      transition: opacity 0.15s;
    }}
    .msg.assistant:hover .bubble-actions {{
      opacity: 1;
    }}
    .copy-btn {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: none;
      border: none;
      color: #94a3b8;
      cursor: pointer;
      font-size: 12px;
      padding: 2px 6px;
      border-radius: 4px;
      transition: color 0.15s, background 0.15s;
    }}
    .copy-btn:hover {{
      color: #475569;
      background: rgba(0,0,0,0.04);
    }}
    .copy-btn svg {{
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }}
    .copy-btn.copied {{
      color: #16a34a;
    }}

    /* ── File upload ─────────────────────────────────────────── */
    .file-attach-btn {{
      background: none;
      border: none;
      color: var(--font-color, #000);
      cursor: pointer;
      padding: 6px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      transition: color 0.15s, background 0.15s;
      flex-shrink: 0;
    }}
    .file-attach-btn:hover {{
      color: var(--font-color, #000);
      background: rgba(0,0,0,0.04);
    }}
    .file-attach-btn svg {{
      width: 20px;
      height: 20px;
    }}
    .file-preview-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 8px 12px 4px;
      border-top: 1px solid #e2e8f0;
      background: #f8fafc;
    }}
    .file-chip {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #e2e8f0;
      color: #334155;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 12px;
      max-width: 200px;
    }}
    .file-chip-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .file-chip-remove {{
      background: none;
      border: none;
      color: #94a3b8;
      cursor: pointer;
      padding: 0 2px;
      font-size: 14px;
      line-height: 1;
    }}
    .file-chip-remove:hover {{
      color: #dc2626;
    }}
    .file-chip.uploading {{
      opacity: 0.6;
    }}
    .file-chip.uploading::after {{
      content: '';
      width: 12px;
      height: 12px;
      border: 2px solid #94a3b8;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .msg-file-indicator {{
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      opacity: 0.7;
      margin-top: 4px;
    }}
    .msg-file-indicator svg {{
      width: 12px;
      height: 12px;
    }}

    /* ── Activity / Planning panel ────────────────────────────── */
    .activity-panel {{
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      margin: 0 16px 6px 56px;
      overflow: hidden;
      transition: max-height 0.35s ease, opacity 0.25s ease;
      max-height: 320px;
      opacity: 1;
    }}
    .activity-panel.collapsed {{
      max-height: 32px;
      cursor: pointer;
    }}
    .activity-header {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 600;
      color: #64748b;
      user-select: none;
      cursor: pointer;
    }}
    .activity-header svg {{
      width: 14px; height: 14px; flex-shrink: 0;
      transition: transform 0.25s;
    }}
    .activity-panel.collapsed .activity-header svg {{
      transform: rotate(-90deg);
    }}
    .activity-items {{
      max-height: 260px;
      overflow-y: auto;
      padding: 0 12px 8px;
    }}
    .activity-panel.collapsed .activity-items {{
      display: none;
    }}
    .activity-item {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 5px 0;
      font-size: 12px;
      color: #475569;
      line-height: 1.45;
      border-bottom: 1px solid #e2e8f0;
      animation: actItemIn 0.2s ease-out;
    }}
    .activity-item:last-child {{ border-bottom: none; }}
    .activity-item-icon {{
      flex-shrink: 0;
      width: 18px; height: 18px;
      display: flex; align-items: center; justify-content: center;
      border-radius: 4px;
      font-size: 11px;
    }}
    .activity-item-body b {{ color: #334155; }}

    .activity-item.expandable {{
      cursor: pointer;
      flex-wrap: wrap;
    }}
    .activity-item.expandable:hover {{
      background: #e2e8f0;
      border-radius: 6px;
    }}
    .activity-item.expandable .activity-item-body::after {{
      content: ' ▸';
      font-size: 10px;
      color: #94a3b8;
      transition: transform 0.2s;
    }}
    .activity-item.expanded .activity-item-body::after {{
      content: ' ▾';
    }}
    .activity-detail {{
      display: none;
      width: 100%;
      margin-top: 4px;
      padding: 8px 10px;
      background: #e8ecf1;
      border-radius: 6px;
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
      font-size: 11px;
      line-height: 1.55;
      color: #334155;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 180px;
      overflow-y: auto;
      animation: detailSlide 0.2s ease-out;
    }}
    .activity-item.expanded .activity-detail {{
      display: block;
    }}
    @keyframes detailSlide {{
      from {{ opacity: 0; max-height: 0; }}
      to {{ opacity: 1; max-height: 180px; }}
    }}

    @keyframes actItemIn {{
      from {{ opacity: 0; transform: translateX(-6px); }}
      to {{ opacity: 1; transform: translateX(0); }}
    }}

    /* ──────────────────────────────────────────────────────────────────
       Mobile (≤640px) — touch-friendly UX
       Uses dynamic viewport units (`dvh`) so the chat fits cleanly when
       mobile browser chrome shows/hides and when the soft keyboard opens.
       Bumps tap targets to 44px (WCAG), 16px input font (prevents iOS
       auto-zoom), and lets citation tooltips fit narrow viewports.
       ────────────────────────────────────────────────────────────── */
    @media (max-width: 640px) {{
      html, body {{
        height: 100dvh;
        min-height: -webkit-fill-available; /* iOS Safari fallback */
      }}
      .chat-container {{
        height: 100dvh;
        max-height: 100dvh;
      }}
      .chat-header {{
        padding-top: max(16px, env(safe-area-inset-top));
      }}
      .chat-messages {{
        padding-left: 12px;
        padding-right: 12px;
      }}
      .bubble {{
        max-width: 92% !important;  /* override desktop's 85% — was clipping on 320px */
        font-size: 14px;
      }}
      .chat-input-container {{
        padding: 12px 12px max(12px, env(safe-area-inset-bottom));
      }}
      .chat-input textarea {{
        font-size: 16px;            /* iOS auto-zoom threshold — must be ≥16px */
        max-height: 30dvh;          /* never let the input dominate the screen */
      }}
      /* Bump every interactive button to a 44×44px tap target. Visual
         padding stays comfortable; min-* enforces the WCAG floor. */
      .chat-input button,
      .file-attach-btn,
      .copy-btn,
      .icon-btn,
      .send-btn {{
        min-width: 44px;
        min-height: 44px;
      }}
      /* Citation chips: keep visual size compact but expand the hit area. */
      .cite-chip,
      .cite-chip-secondary {{
        position: relative;
        min-height: 22px;
        padding: 2px 8px;
      }}
      .cite-chip::after,
      .cite-chip-secondary::after {{
        content: '';
        position: absolute;
        inset: -10px;               /* invisible 10px expansion of tap area */
      }}
      /* Citation tooltips: span the viewport instead of being a 340px box
         that overflows on phones. Anchored above the safe-area-bottom. */
      .cite-tooltip {{
        position: fixed !important;
        left: 12px !important;
        right: 12px !important;
        bottom: calc(12px + env(safe-area-inset-bottom)) !important;
        top: auto !important;
        max-width: unset !important;
        width: auto !important;
      }}
    }}
  </style>
</head>
<body>
<div class="chat-container">
  <div class="chat-header">
    <div class="header-logo">
      {f'<img src="{logo_url}" alt="Logo" />' if logo_url else f'<span class="header-logo-placeholder">{chatbot_title[0] if chatbot_title else "A"}</span>'}
    </div>
    <div class="header-text">
      <div class="chat-header-title">{chatbot_title}</div>
      <div class="chat-header-sub">{chatbot_subtitle}</div>
    </div>
    <div class="online-indicator" role="status" aria-label="Assistant online" title="Online"></div>
  </div>
  <div id="messages" class="chat-messages"></div>
  <div id="status" class="status"></div>
  <div class="chat-input-container">
    <div id="filePreviewBar" class="file-preview-bar" style="display:none;"></div>
    <div class="chat-input">
      <button id="attachBtn" class="file-attach-btn" title="Attach file" type="button" style="display:none;">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>
        </svg>
      </button>
      <textarea id="input" rows="1" placeholder="Type your message..."></textarea>
      <button id="sendBtn" title="Send message">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"></line>
          <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
        </svg>
      </button>
    </div>
    <input type="file" id="fileInput" accept=".pdf,.txt,.doc,.docx,.md,.rtf,.csv,.xlsx" multiple style="display:none;" />
  </div>
</div>

<!-- Human Input Modal -->
<div id="humanInputModal" class="human-input-modal">
  <div class="human-input-box">
    <div class="human-input-title" id="humanInputTitle">Input Required</div>
    <div class="human-input-message" id="humanInputMessage"></div>
    <textarea id="humanInputTextarea" class="human-input-textarea" placeholder="Enter your response..."></textarea>
    <div class="human-input-buttons">
      <button class="cancel-btn" id="humanInputCancel">Cancel</button>
      <button class="submit-btn" id="humanInputSubmit">Submit</button>
    </div>
  </div>
</div>

<script>
  // Build API endpoint URL
  // When served as iframe from the backend, use same-origin (window.location.origin).
  // When hosted standalone (copied Full HTML on different server), use the server-injected URL.
  const _SERVER_ENDPOINT = {json.dumps(endpoint_url)};
  const _isServedFromBackend = window.location.pathname.indexOf('/api/workflow-deploy/') >= 0;
  const ENDPOINT_URL = _isServedFromBackend
    ? (window.location.origin + '/api/workflow-deploy/{str(project_id)}/')
    : _SERVER_ENDPOINT;
  const STREAM_URL = ENDPOINT_URL.replace(/\\/$/, '') + '/stream/';
  const SUBMIT_INPUT_URL = ENDPOINT_URL.replace(/\\/$/, '') + '/submit-input/';
  const UPLOAD_URL = ENDPOINT_URL.replace(/\\/$/, '') + '/upload-file/';
  const FILE_UPLOADS_ENABLED = {json.dumps(file_uploads_enabled)};
  const INITIAL_GREETING = {json.dumps(initial_greeting)};
  const PRELOADED_HISTORY = {json.dumps(preloaded_history)};
  
  // Enhanced markdown renderer
  function renderMarkdown(text) {{
    if (!text) return '';
    let html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Auto-separate numbered sections that the LLM emits with single newlines.
    // Matches both "(N) Title" and "N) Title" forms, turning each into its
    // own bolded paragraph so the rendered response has real vertical rhythm
    // instead of one dense <p> with stacked <br>s.
    html = html.replace(/([^\\n])\\n(?=(?:\\(\\d+\\)|\\d+\\))\\s+\\S)/g, '$1\\n\\n');
    html = html.replace(/^((?:\\(\\d+\\)|\\d+\\))\\s+[^\\n]{{1,120}})$/gm, '**$1**');

    // Normalize Unicode bullets to "- " so the list detector recognises them.
    // LLMs emit many variants — •, ·, ●, ▪, ○, ◦, ▫, ■, en/em-dashes — and
    // sometimes prefix them with leading whitespace. If we don't normalise,
    // each bullet line is treated as a plain paragraph and <p> margins stack
    // between items (~16px per gap) instead of tight <li> spacing.
    // Leading whitespace is PRESERVED ($1) — an indented bullet means a
    // nested sub-item, and flattening it to column 0 would make the list
    // processor below close the parent <ol> instead of nesting.
    html = html.replace(
      /^(\\s*)[\\u2022\\u00B7\\u25CF\\u25AA\\u25CB\\u25E6\\u2043\\u2219\\u25AB\\u25A0\\u2013\\u2014]\\s+/gm,
      '$1- '
    );

    // Headers
    html = html.replace(/^######\\s+(.+)$/gm, '<h6>$1</h6>');
    html = html.replace(/^#####\\s+(.+)$/gm, '<h5>$1</h5>');
    html = html.replace(/^####\\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\\s+(.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^##\\s+(.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#\\s+(.+)$/gm, '<h1>$1</h1>');
    
    // Horizontal rules
    html = html.replace(/^\\s*[-*]{{3,}}\\s*$/gm, '<hr>');
    
    // Code blocks
    html = html.replace(/```(\\w+)?[\\n\\r]+([\\s\\S]*?)```/g, function(match, lang, code) {{
      return '<pre><code>' + code.trim() + '</code></pre>';
    }});
    
    // Blockquotes
    html = html.replace(/^>\\s+(.+)$/gm, '<blockquote>$1</blockquote>');
    
    // Process lists
    const lines = html.split('\\n');
    const processedLines = [];
    let inOrderedList = false;
    let inUnorderedList = false;
    // Closer for an in-progress nested <ul> under the current <ol> item.
    // Empty string when no nested list is open.
    let nestedCloser = '';

    function closeNested() {{
      if (nestedCloser) {{ processedLines.push(nestedCloser); nestedCloser = ''; }}
    }}

    for (let i = 0; i < lines.length; i++) {{
      const line = lines[i];
      const orderedMatch = line.match(/^(\\d+)\\.\\s+(.+)$/);
      const unorderedMatch = line.match(/^[-*]\\s+(.+)$/);
      // Indented bullet while an <ol> is open = sub-item of the current
      // numbered entry. Previously this fell through to the paragraph branch,
      // which CLOSED the <ol>; the next numbered item then opened a fresh
      // <ol> restarting at 1 — so "1. / 2. / 3." rendered as "1. / 1. / 1.".
      const nestedBulletMatch = inOrderedList ? line.match(/^\\s+[-*]\\s+(.+)$/) : null;

      if (orderedMatch) {{
        closeNested();
        if (!inOrderedList) {{
          if (inUnorderedList) {{ processedLines.push('</ul>'); inUnorderedList = false; }}
          // start="N" carries the item's REAL number from the markdown, so
          // even when intervening content (sub-bullets, explanatory lines)
          // legitimately splits one logical list into several <ol> fragments,
          // the visible numbering continues 1, 2, 3 instead of resetting.
          processedLines.push('<ol start="' + orderedMatch[1] + '">');
          inOrderedList = true;
        }}
        processedLines.push('<li>' + orderedMatch[2] + '</li>');
      }} else if (nestedBulletMatch) {{
        if (!nestedCloser) {{
          const last = processedLines.length - 1;
          if (last >= 0 && processedLines[last].endsWith('</li>')) {{
            // Re-open the parent <li> and start a nested <ul> inside it —
            // the only valid place for a sub-list in HTML.
            processedLines[last] = processedLines[last].slice(0, -5) + '<ul>';
            nestedCloser = '</ul></li>';
          }} else {{
            processedLines.push('<ul>');
            nestedCloser = '</ul>';
          }}
        }}
        processedLines.push('<li>' + nestedBulletMatch[1] + '</li>');
      }} else if (unorderedMatch) {{
        if (!inUnorderedList) {{
          closeNested();
          if (inOrderedList) {{ processedLines.push('</ol>'); inOrderedList = false; }}
          processedLines.push('<ul>');
          inUnorderedList = true;
        }}
        processedLines.push('<li>' + unorderedMatch[1] + '</li>');
      }} else if ((inOrderedList || inUnorderedList) && /^\\s*$/.test(line)) {{
        // Blank line inside a list — peek ahead. If the next non-blank line is
        // the same kind of bullet (or a nested sub-bullet of an open <ol>),
        // keep the list open and absorb this blank line. Otherwise close the
        // list as before. Without this, LLM responses with blank lines between
        // bullets render as N separate <ul>/<ol> elements.
        let j = i + 1;
        while (j < lines.length && /^\\s*$/.test(lines[j])) j++;
        if (j < lines.length) {{
          const nextOrdered = lines[j].match(/^(\\d+)\\.\\s+(.+)$/);
          const nextUnordered = lines[j].match(/^[-*]\\s+(.+)$/);
          const nextNested = lines[j].match(/^\\s+[-*]\\s+(.+)$/);
          if ((inOrderedList && (nextOrdered || nextNested)) || (inUnorderedList && nextUnordered)) {{
            continue;  // keep list open, drop the blank line
          }}
        }}
        closeNested();
        if (inOrderedList) {{ processedLines.push('</ol>'); inOrderedList = false; }}
        if (inUnorderedList) {{ processedLines.push('</ul>'); inUnorderedList = false; }}
        processedLines.push(line);
      }} else {{
        closeNested();
        if (inOrderedList) {{ processedLines.push('</ol>'); inOrderedList = false; }}
        if (inUnorderedList) {{ processedLines.push('</ul>'); inUnorderedList = false; }}
        processedLines.push(line);
      }}
    }}
    closeNested();
    if (inOrderedList) processedLines.push('</ol>');
    if (inUnorderedList) processedLines.push('</ul>');
    html = processedLines.join('\\n');
    
    // Links
    html = html.replace(/\\[([^\\]]+)\\]\\(([^\\)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    
    // Bold
    html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
    html = html.replace(/__(?!_)([^_]+)__/g, '<strong>$1</strong>');
    
    // Italic
    html = html.replace(/\\*(?!\\*)([^*]+)\\*(?!\\*)/g, '<em>$1</em>');
    html = html.replace(/_(?!_)([^_]+)_(?!_)/g, '<em>$1</em>');
    
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // Paragraphs
    const paragraphs = html.split(/\\n\\n+/);
    html = paragraphs.map(function(p) {{
      p = p.trim();
      if (!p) return '';
      if (/^<(pre|blockquote|ul|ol|hr|h[1-6])/i.test(p)) return p;
      p = p.replace(/\\n/g, '<br>');
      return '<p>' + p + '</p>';
    }}).filter(function(p) {{ return p; }}).join('');
    
    return html;
  }}

  let _chatCitations = [];
  let _activeCiteTooltip = null;

  function parseCitations(text) {{
    // Tolerant opener: the LLM occasionally drops the leading `---` on the
    // opener (emitting bare `CITATIONS`) — accept both forms but still require
    // the full `---END_CITATIONS---` closer so we don't over-match when the
    // body legitimately contains the word.
    const pattern = /(?:---)?CITATIONS(?:---)?\\s*([\\s\\S]*?)\\s*---END_CITATIONS---/;
    const m = text.match(pattern);
    if (!m) return {{ cleanText: text, citations: [] }};
    var cleanText = text.slice(0, m.index).trim();
    // Strip trailing "CITATIONS" headings the LLM sometimes emits before the block
    cleanText = cleanText.replace(/[\\n\\r]+(?:#{1,6}\\s*)?(?:\\*{{1,2}})?CITATIONS(?:\\*{{1,2}})?\\s*$/i, '').trim();
    let citations = [];
    try {{
      citations = JSON.parse(m[1].trim());
      if (!Array.isArray(citations)) citations = [];
      else citations = citations.filter(function(c) {{
        if (!c || typeof c !== 'object') return false;
        var r = parseInt(c.ref, 10);
        if (isNaN(r)) return false;
        c.ref = r;
        return true;
      }});
    }} catch(e) {{ citations = []; }}
    return {{ cleanText, citations }};
  }}

  function renderCitationChips(html, citations) {{
    if (!citations || citations.length === 0) return html;
    return html.replace(/\\[(\\d+)\\]/g, function(match, num) {{
      const ref = parseInt(num, 10);
      const cit = citations.find(function(c) {{ return Number(c.ref) === ref; }});
      if (!cit) return '<span class="cite-chip cite-chip-secondary" data-ref="' + ref + '">' + ref + '</span>';
      return '<span class="cite-chip" data-ref="' + ref + '">' + ref + '</span>';
    }});
  }}

  function _getCiteAuthHeaders() {{
    try {{
      var raw = localStorage.getItem('auth');
      if (raw) {{
        var parsed = JSON.parse(raw);
        if (parsed && parsed.token) return {{ 'Authorization': 'Bearer ' + parsed.token }};
      }}
    }} catch(_e) {{}}
    return {{}};
  }}

  function openCitationDocument(documentId) {{
    if (!projectId || !documentId) return;
    var url = '/api/projects/' + projectId + '/documents/' + documentId + '/download/';
    fetch(url, {{ headers: _getCiteAuthHeaders() }})
      .then(function(resp) {{
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.blob();
      }})
      .then(function(blob) {{
        var blobUrl = URL.createObjectURL(blob);
        window.open(blobUrl, '_blank');
        setTimeout(function() {{ URL.revokeObjectURL(blobUrl); }}, 60000);
      }})
      .catch(function(err) {{
        console.error('Citation document open failed:', err);
      }});
  }}

  function showCiteTooltip(chipEl, ref) {{
    if (_activeCiteTooltip) {{ _activeCiteTooltip.remove(); _activeCiteTooltip = null; }}
    const cit = _chatCitations.find(function(c) {{ return Number(c.ref) === ref; }});
    if (!cit) return;
    const tip = document.createElement('div');
    tip.className = 'cite-tooltip';
    var isWeb = cit.url || cit.source === 'web';
    var titleHtml;
    if (cit.document_id) {{
      var loc = '';
      if (cit.page) loc += 'p.' + cit.page;
      if (cit.section) loc += (loc ? ', ' : '') + cit.section;
      var titleText = (cit.document_title || 'Document') + (loc ? ' &mdash; ' + loc : '');
      titleHtml = '<a class="cite-tooltip-link" href="#" data-docid="' + cit.document_id + '">' + titleText + '</a>';
    }} else if (isWeb && cit.url) {{
      var safeUrl = cit.url.replace(/"/g, '&quot;').replace(/</g, '&lt;');
      titleHtml = '<a class="cite-tooltip-link" href="' + safeUrl + '" target="_blank" rel="noopener">' + (cit.document_title || cit.url).replace(/</g, '&lt;') + '</a>';
    }} else {{
      titleHtml = (cit.document_title || 'Source').replace(/</g, '&lt;');
    }}
    var sourceHtml = '';
    if (isWeb && cit.url) {{
      try {{
        var domain = new URL(cit.url).hostname.replace(/^www\\./, '');
        sourceHtml = '<div class="cite-tooltip-source">&#127760; ' + domain.replace(/</g, '&lt;') + '</div>';
      }} catch(_e) {{
        sourceHtml = '<div class="cite-tooltip-source">&#127760; Web</div>';
      }}
    }}
    tip.innerHTML = '<div class="cite-tooltip-title">' + titleHtml + '</div>' +
      sourceHtml +
      '<div class="cite-tooltip-quote">&ldquo;' +
      ((cit.quoted_text || '').slice(0, 300).replace(/</g,'&lt;')) +
      '&rdquo;</div>';
    const rect = chipEl.getBoundingClientRect();
    tip.style.left = rect.left + 'px';
    tip.style.top = (rect.bottom + 6) + 'px';
    document.body.appendChild(tip);
    _activeCiteTooltip = tip;
  }}

  document.addEventListener('click', function(e) {{
    var link = e.target.closest && e.target.closest('.cite-tooltip-link');
    if (link) {{
      if (link.dataset.docid) {{
        e.preventDefault();
        e.stopPropagation();
        openCitationDocument(link.dataset.docid);
        return;
      }}
      if (link.href && link.target === '_blank') {{
        return;
      }}
    }}
    const chip = e.target.closest && e.target.closest('.cite-chip');
    if (chip) {{
      const ref = parseInt(chip.dataset.ref, 10);
      if (_activeCiteTooltip && _activeCiteTooltip._ref === ref) {{
        _activeCiteTooltip.remove(); _activeCiteTooltip = null;
      }} else {{
        showCiteTooltip(chip, ref);
        if (_activeCiteTooltip) _activeCiteTooltip._ref = ref;
      }}
    }} else if (_activeCiteTooltip) {{
      _activeCiteTooltip.remove(); _activeCiteTooltip = null;
    }}
  }});

  // ── Copy to clipboard ──────────────────────────────────────
  function buildCopyText(plainText, citations) {{
    let out = plainText;
    if (citations && citations.length > 0) {{
      out += '\\n\\nReferences:\\n';
      citations.forEach(function(c) {{
        let line = '[' + c.ref + '] ';
        if (c.document_title) line += c.document_title;
        else if (c.url) line += c.url;
        if (c.page) line += ', p.' + c.page;
        if (c.section) line += ', ' + c.section;
        if (c.url && c.document_title) line += ' (' + c.url + ')';
        if (c.quoted_text) line += ' — "' + c.quoted_text.slice(0, 200) + '"';
        out += line + '\\n';
      }});
    }}
    return out;
  }}

  function addCopyButton(bubble, plainText, citations) {{
    const actions = document.createElement('div');
    actions.className = 'bubble-actions';
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.title = 'Copy to clipboard';
    btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span>Copy</span>';
    btn.addEventListener('click', function(e) {{
      e.stopPropagation();
      var text = buildCopyText(plainText, citations);
      navigator.clipboard.writeText(text).then(function() {{
        btn.classList.add('copied');
        btn.querySelector('span').textContent = 'Copied';
        btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg><span>Copied</span>';
        setTimeout(function() {{
          btn.classList.remove('copied');
          btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span>Copy</span>';
        }}, 2000);
      }});
    }});
    actions.appendChild(btn);
    bubble.appendChild(actions);
  }}

  // ── File upload ────────────────────────────────────────────
  const attachBtn = document.getElementById('attachBtn');
  const fileInput = document.getElementById('fileInput');
  const filePreviewBar = document.getElementById('filePreviewBar');
  let pendingFiles = [];

  if (FILE_UPLOADS_ENABLED && attachBtn) {{
    attachBtn.style.display = 'flex';
    attachBtn.addEventListener('click', function() {{ fileInput.click(); }});
    fileInput.addEventListener('change', function() {{
      if (fileInput.files && fileInput.files.length > 0) {{
        for (var i = 0; i < fileInput.files.length; i++) {{
          uploadFile(fileInput.files[i]);
        }}
        fileInput.value = '';
      }}
    }});
  }}

  function _updatePreviewBar() {{
    if (pendingFiles.length === 0) {{
      filePreviewBar.style.display = 'none';
      return;
    }}
    filePreviewBar.style.display = 'flex';
    filePreviewBar.innerHTML = '';
    pendingFiles.forEach(function(f) {{
      const chip = document.createElement('div');
      chip.className = 'file-chip' + (f.uploading ? ' uploading' : '');
      chip.id = 'fchip_' + f._cid;
      var nameSpan = '<span class="file-chip-name">' + f.filename.replace(/</g,'&lt;') + '</span>';
      var removeBtn = f.uploading ? '' : '<button class="file-chip-remove" data-cid="' + f._cid + '">&times;</button>';
      chip.innerHTML = nameSpan + removeBtn;
      filePreviewBar.appendChild(chip);
    }});
    filePreviewBar.querySelectorAll('.file-chip-remove').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var cid = btn.dataset.cid;
        pendingFiles = pendingFiles.filter(function(f) {{ return f._cid !== cid; }});
        _updatePreviewBar();
      }});
    }});
  }}

  async function uploadFile(file) {{
    var cid = 'f' + Date.now();
    var entry = {{ _cid: cid, filename: file.name, uploading: true }};
    pendingFiles.push(entry);
    _updatePreviewBar();

    try {{
      var formData = new FormData();
      formData.append('file', file);
      formData.append('session_id', sessionId);
      var resp = await fetch(UPLOAD_URL, {{ method: 'POST', body: formData }});
      if (!resp.ok) {{
        var errText = await resp.text();
        try {{ errText = JSON.parse(errText).error; }} catch(_) {{}}
        throw new Error(errText || 'Upload failed (HTTP ' + resp.status + ')');
      }}
      var data = await resp.json();
      entry.uploading = false;
      entry.attachment_id = data.attachment_id;
      entry.file_id = data.file_id;
      entry.provider = data.provider;
      _updatePreviewBar();
    }} catch (e) {{
      pendingFiles = pendingFiles.filter(function(f) {{ return f._cid !== cid; }});
      _updatePreviewBar();
      statusEl.textContent = 'Upload failed: ' + e.message;
      setTimeout(function() {{ statusEl.textContent = ''; }}, 4000);
    }}
  }}

  const messages = [];
  // Allow explicit session_id via URL for in-app chatbot, fallback to random for external embeds
  const urlParams = new URLSearchParams(window.location.search || '');
  const urlSessionId = (urlParams.get('session_id') || '').trim();
  const sessionId = urlSessionId || ('sess_' + Math.random().toString(36).slice(2));
  let currentExecutionId = null;
  let awaitingHumanInput = false;

  const messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('sendBtn');
  const statusEl = document.getElementById('status');
  const humanInputModal = document.getElementById('humanInputModal');
  const humanInputTitle = document.getElementById('humanInputTitle');
  const humanInputMessage = document.getElementById('humanInputMessage');
  const humanInputTextarea = document.getElementById('humanInputTextarea');
  const humanInputSubmit = document.getElementById('humanInputSubmit');
  const humanInputCancel = document.getElementById('humanInputCancel');
  
  // Forward Escape key to the parent window so fullscreen can be exited
  // even when the iframe has keyboard focus.
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{
      try {{ parent.postMessage({{ type: 'chatbot_escape' }}, '*'); }} catch(_) {{}}
    }}
  }});

  // Extract projectId from URL path: /api/workflow-deploy/<project_id>/embed/
  const _pathParts = window.location.pathname.split('/').filter(Boolean);
  const _pidIdx = _pathParts.indexOf('workflow-deploy') + 1;
  const projectId = _pidIdx > 0 && _pidIdx < _pathParts.length ? _pathParts[_pidIdx] : null;

  // Auto-resize textarea
  function autoResize() {{
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 310) + 'px';
  }}
  inputEl.addEventListener('input', autoResize);

  function appendMessage(role, text, isStreaming = false, apiCitations = undefined) {{
    const msg = document.createElement('div');
    msg.className = 'msg ' + (role === 'user' ? 'user' : 'assistant');
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    
    if (role === 'assistant' && !isStreaming) {{
      const parsed = parseCitations(text);
      let cleanText = parsed.cleanText;
      let citations = [];
      if (Array.isArray(apiCitations) && apiCitations.length > 0) {{
        citations = apiCitations;
      }} else {{
        citations = parsed.citations;
      }}
      if (citations.length > 0) _chatCitations = citations;
      const markdownEl = document.createElement('markdown');
      let rendered = renderMarkdown(cleanText);
      if (citations.length > 0) rendered = renderCitationChips(rendered, citations);
      markdownEl.innerHTML = rendered;
      bubble.appendChild(markdownEl);
      addCopyButton(bubble, cleanText, citations);
    }} else {{
      bubble.textContent = text;
    }}

    msg.appendChild(bubble);
    messagesEl.appendChild(msg);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  }}
  
  function showThinkingIndicator() {{
    const msg = document.createElement('div');
    msg.className = 'msg assistant';
    msg.id = 'thinking-indicator';
    const indicator = document.createElement('div');
    indicator.className = 'thinking-indicator';
    indicator.innerHTML = '<div class="thinking-dots"><div class="thinking-dot"></div><div class="thinking-dot"></div><div class="thinking-dot"></div></div>';
    msg.appendChild(indicator);
    messagesEl.appendChild(msg);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}
  
  function hideThinkingIndicator() {{
    const indicator = document.getElementById('thinking-indicator');
    if (indicator) indicator.remove();
  }}

  /* ── Activity / planning panel ─────────────────────────── */
  let _activityPanel = null;
  let _activityItems = null;
  let _activityHeader = null;
  let _activityStartTs = null;

  const _activityIcons = {{
    planning: '📋', delegate_start: '🤝', delegate_plan: '📝',
    tool_result: '🔍', delegate_done: '✅', synthesizing: '⚙️',
    agent_started: '▶️',
  }};

  function _ensureActivityPanel() {{
    if (_activityPanel) return;
    _activityStartTs = Date.now();

    _activityPanel = document.createElement('div');
    _activityPanel.className = 'activity-panel';

    _activityHeader = document.createElement('div');
    _activityHeader.className = 'activity-header';
    _activityHeader.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<polyline points="6 9 12 15 18 9"></polyline></svg>' +
      '<span>Processing…</span>';
    var panelRef = _activityPanel;
    _activityHeader.addEventListener('click', function() {{
      panelRef.classList.toggle('collapsed');
    }});

    _activityItems = document.createElement('div');
    _activityItems.className = 'activity-items';

    _activityPanel.appendChild(_activityHeader);
    _activityPanel.appendChild(_activityItems);

    const thinkingEl = document.getElementById('thinking-indicator');
    if (thinkingEl) {{
      thinkingEl.parentNode.insertBefore(_activityPanel, thinkingEl);
      thinkingEl.remove();
    }} else {{
      messagesEl.appendChild(_activityPanel);
    }}
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}

  function showActivityItem(data) {{
    _ensureActivityPanel();
    const icon = _activityIcons[data.type] || '•';
    let desc = '';
    switch (data.type) {{
      case 'planning':
        desc = '<b>' + (data.agent || '') + '</b> created a plan';
        break;
      case 'agent_started':
        desc = '<b>' + (data.agent || '') + '</b> started';
        break;
      case 'delegate_start':
        desc = '<b>' + (data.agent || '') + '</b> started — ' +
               (Array.isArray(data.tasks) ? data.tasks.length + ' task(s)' : '');
        break;
      case 'delegate_plan':
        desc = '<b>' + (data.agent || '') + '</b> created its plan';
        break;
      case 'tool_result':
        desc = '<b>' + (data.agent || '') + '</b> queried <i>' +
               (data.tool || '') + '</i> (' + (data.chars || 0) + ' chars)';
        break;
      case 'delegate_done':
        desc = '<b>' + (data.agent || '') + '</b> finished (' +
               (data.chars || 0) + ' chars)';
        break;
      case 'synthesizing':
        desc = '<b>' + (data.agent || '') + '</b> is synthesizing the final answer';
        break;
      default:
        desc = JSON.stringify(data);
    }}

    let detail = '';
    if ((data.type === 'planning' || data.type === 'delegate_plan') && data.content) {{
      detail = data.content;
    }} else if (data.type === 'delegate_start' && Array.isArray(data.tasks) && data.tasks.length) {{
      detail = data.tasks.map(function(t, i) {{ return (i + 1) + '. ' + t; }}).join('\\n');
    }} else if (data.type === 'tool_result' && data.content) {{
      detail = data.content;
    }} else if (data.type === 'delegate_done' && data.content) {{
      // Show the agent's actual output (truncated upstream) as expandable
      // detail. This was previously hidden — only the "X finished (N chars)"
      // summary line was visible — leaving the user with no way to inspect
      // what each agent produced.
      detail = data.content;
    }}

    const item = document.createElement('div');
    item.className = 'activity-item' + (detail ? ' expandable' : '');
    const bodyEl = document.createElement('span');
    bodyEl.className = 'activity-item-body';
    bodyEl.innerHTML = desc;

    if (detail) {{
      const detailEl = document.createElement('div');
      detailEl.className = 'activity-detail';
      detailEl.textContent = detail;
      bodyEl.appendChild(detailEl);
      item.addEventListener('click', function() {{
        item.classList.toggle('expanded');
      }});
    }}

    item.innerHTML = '<span class="activity-item-icon">' + icon + '</span>';
    item.appendChild(bodyEl);
    _activityItems.appendChild(item);
    _activityItems.scrollTop = _activityItems.scrollHeight;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}

  function collapseActivityPanel() {{
    if (!_activityPanel) return;
    const elapsed = _activityStartTs
      ? Math.round((Date.now() - _activityStartTs) / 1000)
      : 0;
    const hdr = _activityPanel.querySelector('.activity-header span');
    if (hdr) hdr.textContent = 'Processed in ' + elapsed + 's — click to expand';
    _activityPanel.classList.add('collapsed');
  }}

  function resetActivityPanel() {{
    _activityPanel = null;
    _activityItems = null;
    _activityHeader = null;
    _activityStartTs = null;
  }}

  function showHumanInputModal(title, message) {{
    humanInputTitle.textContent = title || 'Input Required';
    humanInputMessage.textContent = message || 'Please provide your input to continue.';
    humanInputTextarea.value = '';
    humanInputModal.classList.add('active');
    humanInputTextarea.focus();
    awaitingHumanInput = true;
    inputEl.disabled = true;
    sendBtn.disabled = true;
  }}

  function hideHumanInputModal() {{
    humanInputModal.classList.remove('active');
    awaitingHumanInput = false;
    inputEl.disabled = false;
    sendBtn.disabled = false;
  }}

  async function submitHumanInput() {{
    const userInput = humanInputTextarea.value.trim();
    if (!userInput) {{
      alert('Please enter your response');
      return;
    }}

    humanInputSubmit.disabled = true;
    statusEl.textContent = 'Submitting...';

    try {{
      const resp = await fetch(SUBMIT_INPUT_URL, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ session_id: sessionId, user_input: userInput }})
      }});

      if (!resp.ok) {{
        const err = await resp.json().catch(() => ({{}}));
        throw new Error(err.error || 'HTTP ' + resp.status);
      }}

      const data = await resp.json();
      appendMessage('user', userInput);
      messages.push({{ role: 'user', content: userInput }});
      hideHumanInputModal();

      if (data.status === 'awaiting_human_input') {{
        showHumanInputModal(data.title, data.last_conversation_message);
        currentExecutionId = data.execution_id;
      }} else if (data.status === 'success') {{
        const reply = data.response || '(No response)';
        appendMessage('assistant', reply, false, data.citations);
        messages.push({{ role: 'assistant', content: reply }});
        statusEl.textContent = '';
        currentExecutionId = null;
      }} else if (data.status === 'processing') {{
        statusEl.textContent = 'Processing...';
        setTimeout(() => {{ statusEl.textContent = ''; }}, 2000);
      }} else {{
        appendMessage('assistant', 'Error: ' + (data.error || 'Unexpected error'));
        statusEl.textContent = '';
      }}
    }} catch (e) {{
      console.error('Submit error:', e);
      appendMessage('assistant', 'Sorry, there was a problem.');
      statusEl.textContent = e.message || 'Error';
    }} finally {{
      humanInputSubmit.disabled = false;
    }}
  }}

  async function sendMessage() {{
    const text = inputEl.value.trim();
    if (!text || awaitingHumanInput) return;

    // Show file indicator in user message if files are pending
    var fileNames = pendingFiles.filter(function(f) {{ return !f.uploading; }}).map(function(f) {{ return f.filename; }});
    appendMessage('user', text);
    if (fileNames.length > 0) {{
      var lastMsg = messagesEl.querySelector('.msg.user:last-child .bubble');
      if (lastMsg) {{
        var ind = document.createElement('div');
        ind.className = 'msg-file-indicator';
        ind.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>' +
          fileNames.map(function(n) {{ return '<span>' + n.replace(/</g,'&lt;') + '</span>'; }}).join(', ');
        lastMsg.appendChild(ind);
      }}
    }}
    messages.push({{ role: 'user', content: text }});

    // Notify the embedding parent (public /chat page, in-app project page) that
    // this session just received a user message, so the parent can update its
    // conversation-list sidebar preview. Same try/'*' pattern as chatbot_escape.
    try {{
      parent.postMessage({{
        type: 'session_message_sent',
        sessionId: sessionId,
        userText: text,
        timestamp: new Date().toISOString()
      }}, '*');
    }} catch(_) {{}}

    // Collect file IDs and clear pending
    var fileIds = pendingFiles.filter(function(f) {{ return f.attachment_id; }}).map(function(f) {{ return f.attachment_id; }});
    pendingFiles = [];
    _updatePreviewBar();

    inputEl.value = '';
    inputEl.style.height = 'auto';
    sendBtn.disabled = true;
    statusEl.textContent = '';
    showThinkingIndicator();

    try {{
      var payload = {{ user_query: text, session_id: sessionId }};
      if (fileIds.length > 0) payload.file_ids = fileIds;
      const resp = await fetch(STREAM_URL, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});

      if (!resp.ok) throw new Error('HTTP ' + resp.status);

      let thinkingHidden = false;
      let msg = null, bubble = null, markdownEl = null;
      let accumulatedContent = '';
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let _sseBuffer = '';

      while (true) {{
        const {{ done, value }} = await reader.read();
        if (done) break;

        _sseBuffer += decoder.decode(value, {{ stream: true }});
        const parts = _sseBuffer.split('\\n\\n');
        _sseBuffer = parts.pop() || '';

        for (const part of parts) {{
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;
          try {{
              const data = JSON.parse(line.slice(6));
              
              if (data.type === 'planning' || data.type === 'delegate_start' ||
                  data.type === 'delegate_plan' || data.type === 'tool_result' ||
                  data.type === 'delegate_done' || data.type === 'synthesizing') {{
                showActivityItem(data);
              }} else if (data.type === 'content') {{
                if (!thinkingHidden) {{
                  collapseActivityPanel();
                  hideThinkingIndicator();
                  thinkingHidden = true;
                  msg = document.createElement('div');
                  msg.className = 'msg assistant';
                  bubble = document.createElement('div');
                  bubble.className = 'bubble';
                  markdownEl = document.createElement('markdown');
                  bubble.appendChild(markdownEl);
                  msg.appendChild(bubble);
                  messagesEl.appendChild(msg);
                }}
                accumulatedContent += data.content;
                markdownEl.innerHTML = renderMarkdown(accumulatedContent);
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }} else if (data.type === 'citations') {{
                // Render citation chips below the streamed message
                console.log('📎 Citations SSE received:', data.citations ? data.citations.length : 0, 'items');
                if (markdownEl && Array.isArray(data.citations) && data.citations.length > 0) {{
                  _chatCitations = data.citations;
                  markdownEl.innerHTML = renderCitationChips(renderMarkdown(accumulatedContent), data.citations);
                  messagesEl.scrollTop = messagesEl.scrollHeight;
                  console.log('📎 Citations rendered in markdownEl');
                }} else {{
                  console.warn('📎 Citations NOT rendered: markdownEl=', !!markdownEl, 'citations=', data.citations?.length);
                }}
              }} else if (data.type === 'awaiting_human_input') {{
                hideThinkingIndicator();
                showHumanInputModal(data.title, data.last_conversation_message);
                currentExecutionId = data.execution_id;
                statusEl.textContent = 'Waiting for input...';
                if (msg) msg.remove();
                return;
              }} else if (data.type === 'error') {{
                hideThinkingIndicator();
                if (msg) msg.remove();
                appendMessage('assistant', 'Error: ' + (data.error || 'Unexpected error'));
                return;
              }} else if (data.type === 'done') {{
                collapseActivityPanel();
                // Edge case: live-streamed prose was entirely held back (e.g. the
                // whole response was the citations marker with nothing before
                // it) — no bubble exists yet. Create one now, same as the
                // 'content' handler above, so the authoritative text still renders.
                if (!thinkingHidden) {{
                  hideThinkingIndicator();
                  thinkingHidden = true;
                  msg = document.createElement('div');
                  msg.className = 'msg assistant';
                  bubble = document.createElement('div');
                  bubble.className = 'bubble';
                  markdownEl = document.createElement('markdown');
                  bubble.appendChild(markdownEl);
                  msg.appendChild(bubble);
                  messagesEl.appendChild(msg);
                }}
                // Prefer the server's authoritative post-processing text over what
                // was accumulated from live "content" events: reconcile_citations
                // can renumber [N] markers (e.g. dropping an unreferenced citation
                // and shifting the rest down) AFTER streaming already started, so
                // the live-typed text can be stale by the time this event arrives.
                const authoritativeText = (typeof data.content === 'string' && data.content)
                  ? data.content
                  : accumulatedContent;
                const parsed = parseCitations(authoritativeText);
                let cleanContent = parsed.cleanText;
                let citations = [];
                if (Array.isArray(data.citations) && data.citations.length > 0) {{
                  citations = data.citations;
                  console.log('📎 Done: using data.citations:', citations.length);
                }} else if (_chatCitations.length > 0) {{
                  citations = _chatCitations;
                  console.log('📎 Done: using _chatCitations:', citations.length);
                }} else {{
                  citations = parsed.citations;
                  console.log('📎 Done: using parsed citations:', citations.length);
                }}
                _chatCitations = citations;
                console.log('📎 Done handler: total citations =', citations.length, 'markdownEl =', !!markdownEl, 'cleanContent has [1] =', cleanContent.includes('[1]'));
                if (markdownEl) {{
                  var newHtml = citations.length > 0
                    ? renderCitationChips(renderMarkdown(cleanContent), citations)
                    : renderMarkdown(cleanContent);
                  markdownEl.innerHTML = newHtml;
                  console.log('📎 Done: rendered, has cite-chip =', newHtml.includes('cite-chip'));
                }}
                if (bubble) addCopyButton(bubble, cleanContent, citations);
                messages.push({{ role: 'assistant', content: cleanContent }});
                statusEl.textContent = '';
                resetActivityPanel();
                return;
              }}
            }} catch (e) {{
              console.error('Parse error:', e);
            }}
          }}
        }}
    }} catch (e) {{
      console.error('Chat error:', e);
      hideThinkingIndicator();
      collapseActivityPanel();
      resetActivityPanel();
      appendMessage('assistant', 'Sorry, there was a connection problem.');
      statusEl.textContent = '';
    }} finally {{
      if (!awaitingHumanInput) sendBtn.disabled = false;
    }}
  }}

  sendBtn.addEventListener('click', sendMessage);
  inputEl.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter' && !e.shiftKey && !awaitingHumanInput) {{
      e.preventDefault();
      sendMessage();
    }}
  }});

  humanInputSubmit.addEventListener('click', submitHumanInput);
  humanInputCancel.addEventListener('click', () => {{
    hideHumanInputModal();
    statusEl.textContent = '';
  }});
  humanInputTextarea.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter' && e.ctrlKey) {{
      e.preventDefault();
      submitHumanInput();
    }}
  }});

  // Load server-side preloaded conversation history
  for (const msg of PRELOADED_HISTORY) {{
    if (!msg || !msg.role || typeof msg.content !== 'string') continue;
    const msgCitations = Array.isArray(msg.citations) ? msg.citations : undefined;
    appendMessage(msg.role === 'user' ? 'user' : 'assistant', msg.content, false, msgCitations);
    messages.push({{ role: msg.role, content: msg.content }});
    // Re-render the file attachment indicator on user turns that were sent
    // with files. Mirrors the chip created in sendMessage() so the bubble
    // looks identical to its original send-time appearance.
    if (msg.role === 'user' && Array.isArray(msg.attachments) && msg.attachments.length > 0) {{
      var fileNames = msg.attachments.map(function(a) {{ return a && a.filename ? a.filename : ''; }}).filter(Boolean);
      if (fileNames.length > 0) {{
        var lastBubble = messagesEl.querySelector('.msg.user:last-child .bubble');
        if (lastBubble) {{
          var ind = document.createElement('div');
          ind.className = 'msg-file-indicator';
          ind.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>' +
            fileNames.map(function(n) {{ return '<span>' + String(n).replace(/</g,'&lt;') + '</span>'; }}).join(', ');
          lastBubble.appendChild(ind);
        }}
      }}
    }}
  }}

  // Show default greeting if no preloaded history
  if (messages.length === 0) {{
    appendMessage('assistant', INITIAL_GREETING);
    messages.push({{ role: 'assistant', content: INITIAL_GREETING }});
  }}
</script>
</body>
</html>'''
        
        response = HttpResponse(html_content, content_type='text/html')
        # Remove restrictive headers that block fetch from cross-origin iframes
        response['Cross-Origin-Opener-Policy'] = 'unsafe-none'
        response['Cross-Origin-Embedder-Policy'] = 'unsafe-none'
        # Framing allowlist. @xframe_options_exempt only stops Django sending
        # X-Frame-Options; it grants nothing. Without this directive the embed
        # is frameable by *any* site, so state the permitted parents here from
        # the deployment's own allowed-origin rows. Note that nginx must not
        # add X-Frame-Options to this path — it would override the allowlist
        # and break every third-party embed (see nginx.prod.conf).
        response['Content-Security-Policy'] = _frame_ancestors_csp(deployment)
        # Make the no-cache posture explicit: this HTML can contain preloaded
        # per-session conversation_history (when ?session_id= is present), so
        # caching across users or even across reloads would surface stale or
        # leaked history. private + no-store is the safest contract.
        response['Cache-Control'] = 'private, no-store, no-cache, must-revalidate'
        response['Pragma'] = 'no-cache'
        # Rolling 4h TTL — refresh the cookie on every /embed/ load so keeping
        # the chat window open during activity extends the session.
        _maybe_refresh_public_chat_cookie(request, response, deployment)
        return response

    except Exception as e:
        logger.error(f"❌ DEPLOYMENT: Error serving embed HTML: {e}", exc_info=True)
        return HttpResponse(
            '<html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f7;"><p style="color:#ef4444;">Error loading chatbot. Please try again later.</p></body></html>',
            status=500,
            content_type='text/html'
        )

