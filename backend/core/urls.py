# backend/core/urls.py

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
from api.views import (
    RegisterView, 
    UserViewSet, 
    DashboardIconViewSet,
    UserIconPermissionViewSet,
    GroupViewSet,
    GroupIconPermissionViewSet,
    UserProjectPermissionViewSet,
    GroupProjectPermissionViewSet,
    password_reset_request, 
    password_reset_confirm,
    change_password
)

# Import template views
from templates.views import ProjectTemplateViewSet

# Import universal project views
from api.universal_project_views import UniversalProjectViewSet

# Import agent orchestration views
from agent_orchestration.workflow_views import AgentWorkflowViewSet
from agent_orchestration.debug_views import debug_workflows

# Import vector search views for main endpoints
from vector_search import consolidated_api_views

from llm_eval.views import (
    LLMProviderViewSet,
    APIKeyConfigViewSet,
    LLMComparisonViewSet
)

# Import dynamic template URL management
from templates.dynamic_urls import include_template_urls, TemplateURLManagementView

from core.health import health, health_ready

router = DefaultRouter()
router.register(r'users', UserViewSet)
router.register(r'dashboard-icons', DashboardIconViewSet)
router.register(r'icon-permissions', UserIconPermissionViewSet)
router.register(r'groups', GroupViewSet)
router.register(r'group-icon-permissions', GroupIconPermissionViewSet)
router.register(r'user-project-permissions', UserProjectPermissionViewSet, basename='user-project-permissions')
router.register(r'group-project-permissions', GroupProjectPermissionViewSet, basename='group-project-permissions')

# PHASE 3: Universal Project System (Single interface for all projects)
router.register(r'projects', UniversalProjectViewSet, basename='projects')

# ✅ PHASE 1 COMPLETE: ALL legacy template-specific project endpoints removed
# ALL projects now use universal /api/projects/ endpoints only

# Template system
router.register(r'project-templates', ProjectTemplateViewSet, basename='project-template')

# LLM Eval endpoints
router.register(r'llm-providers', LLMProviderViewSet)
router.register(r'api-keys', APIKeyConfigViewSet)
router.register(r'llm-comparisons', LLMComparisonViewSet)

urlpatterns = [
    # Container health probes. Kept first and unauthenticated: the
    # Dockerfile.prod HEALTHCHECK, docker-compose.prod.yml and
    # scripts/production.sh all poll /health/.
    path('health/', health, name='health'),
    path('health/ready/', health_ready, name='health_ready'),

    path('admin/', admin.site.urls),
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/register/', RegisterView.as_view(), name='register'),
    path('api/password-reset/', password_reset_request, name='password_reset'),
    path('api/password-reset/confirm/', password_reset_confirm, name='password_reset_confirm'),
    path('api/change-password/', change_password, name='change_password'),
    
    # PHASE 3: Universal Project System (Single interface for all project operations)
    # All project operations now use /api/projects/ endpoints with intelligent routing
    # This replaces the need for separate enhanced vs basic project endpoints
    
    # 🤖 Agent Orchestration Workflows (Nested under projects) - ENHANCED VERSION
    path('api/projects/<uuid:project_id>/workflows/', AgentWorkflowViewSet.as_view({
        'get': 'list',
        'post': 'create'
    }), name='project-workflows'),
    
    # 🔧 DEBUG: Temporary debug endpoint for workflow troubleshooting
    path('api/debug/projects/<uuid:project_id>/workflows/', debug_workflows, name='debug-workflows'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/', AgentWorkflowViewSet.as_view({
        'get': 'retrieve',
        'patch': 'partial_update',
        'put': 'update'
    }), name='project-workflow-detail'),
    # ✅ WORKFLOW ACTIONS: Execute, history, and validate endpoints now available
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/execute/', AgentWorkflowViewSet.as_view({
        'post': 'execute'
    }), name='project-workflow-execute'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/history/', AgentWorkflowViewSet.as_view({
        'get': 'history'
    }), name='project-workflow-history'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/validate/', AgentWorkflowViewSet.as_view({
        'post': 'validate'
    }), name='project-workflow-validate'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/conversation/', AgentWorkflowViewSet.as_view({
        'get': 'conversation'
    }), name='project-workflow-conversation'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/evaluate/', AgentWorkflowViewSet.as_view({
        'post': 'evaluate'
    }), name='project-workflow-evaluate'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/evaluation_history/', AgentWorkflowViewSet.as_view({
        'get': 'evaluation_history'
    }), name='project-workflow-evaluation-history'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/evaluation_results/', AgentWorkflowViewSet.as_view({
        'get': 'evaluation_results'
    }), name='project-workflow-evaluation-results'),
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/nodes/<str:node_id>/upload-file-attachment/', AgentWorkflowViewSet.as_view({
        'post': 'upload_file_attachment'
    }), name='project-workflow-upload-file-attachment'),

    # Export a workflow as a JSON bundle (download).
    path('api/projects/<uuid:project_id>/workflows/<uuid:workflow_id>/export/', AgentWorkflowViewSet.as_view({
        'get': 'export'
    }), name='project-workflow-export'),
    # Import a JSON bundle as a new workflow in this project.
    path('api/projects/<uuid:project_id>/workflows/import/', AgentWorkflowViewSet.as_view({
        'post': 'import_bundle'
    }), name='project-workflow-import'),

    # Document summary endpoint (explicit registration — router version not matched in live server)
    path('api/projects/<uuid:project_id>/documents/<uuid:document_id>/summary/', UniversalProjectViewSet.as_view({
        'get': 'document_summary'
    }), name='project-document-summary'),

    # PHASE 3: Universal Processing Endpoints (Consolidated via UniversalProjectViewSet)
    # All processing now handled through UniversalProjectViewSet actions:
    # - /api/projects/{project_id}/process_documents/ (POST)
    # - /api/projects/{project_id}/search/ (POST)
    # - /api/projects/{project_id}/vector_status/ (GET)
    # - /api/projects/{project_id}/capabilities/ (GET)
    # - /api/projects/{project_id}/documents/ (GET)
    # - /api/projects/{project_id}/upload_document/ (POST)
    # - /api/projects/{project_id}/delete_document/ (DELETE)
    
    # LEGACY: Maintained for backward compatibility only
    path('api/projects/<str:project_id>/digest/', consolidated_api_views.process_unified_consolidated, name='process_project_documents_legacy'),
    path('api/projects/<str:project_id>/vector-status/', consolidated_api_views.get_vector_status_consolidated, name='get_project_vector_status_legacy'),
    path('api/projects/<str:project_id>/search/', consolidated_api_views.search_unified_consolidated, name='search_project_documents_legacy'),
    path('api/projects/<str:project_id>/capabilities/', consolidated_api_views.get_project_capabilities_consolidated, name='get_project_capabilities_legacy'),
    
    # REMOVED: Legacy endpoints - ALL processing now uses consolidated endpoints
    
    # Enhanced Vector Search endpoints with stop capability
    path('api/vector-search/', include('vector_search.urls')),
    
    # Django Milvus Search endpoints
    path('api/milvus/', include('django_milvus_search.urls')),
    
    path('api/', include(router.urls)),
    
    # Template System API endpoints
    path('api/enhanced-project-templates/', ProjectTemplateViewSet.as_view({'get': 'list'}), name='enhanced-template-list'),
    path('api/enhanced-project-templates/discover/', ProjectTemplateViewSet.as_view({'get': 'discover'}), name='enhanced-template-discover'),
    path('api/enhanced-project-templates/duplicate/', ProjectTemplateViewSet.as_view({'post': 'duplicate'}), name='enhanced-template-duplicate'),
    path('api/enhanced-project-templates/cache-stats/', ProjectTemplateViewSet.as_view({'get': 'cache_stats'}), name='enhanced-template-cache-stats'),
    path('api/enhanced-project-templates/cache-management/', ProjectTemplateViewSet.as_view({'post': 'cache_management'}), name='enhanced-template-cache-management'),
    path('api/enhanced-project-templates/<str:pk>/', ProjectTemplateViewSet.as_view({'get': 'retrieve'}), name='enhanced-template-detail'),
    path('api/enhanced-project-templates/<str:pk>/configuration/', ProjectTemplateViewSet.as_view({'get': 'configuration'}), name='enhanced-template-configuration'),
    
    # ✅ PHASE 1 COMPLETE: Enhanced IntelliDoc endpoints removed - universal /api/projects/ only
    
    # Dynamic Template URL Management (Enhanced)
    path('api/templates/discover/', TemplateURLManagementView.discover_templates, name='template-discover-all'),
    path('api/templates/endpoints/', TemplateURLManagementView.get_template_endpoints, name='template-endpoints-all'),
    path('api/templates/refresh/', TemplateURLManagementView.refresh_urls, name='template-refresh-urls'),
    
    # EMERGENCY FIX: Direct AICC-IntelliDoc endpoint to bypass dynamic system
    path('api/templates/aicc-intellidoc/discover/', lambda request: JsonResponse({
        'status': 'success',
        'discovery': {
            'template_id': 'aicc-intellidoc',
            'template_name': 'AICC-IntelliDoc',
            'version': '2.0.0',
            'description': 'Advanced AI-powered document analysis with hierarchical processing',
            'capabilities': {
                'hierarchical_processing': True,
                'advanced_navigation': True,
                'ai_analysis': True,
                'multi_document_support': True,
                'category_classification': True
            },
            'configuration': {
                'total_pages': 4,
                'navigation_pages': ['Overview', 'Documents', 'Analysis', 'Summary'],
                'processing_modes': ['hierarchical', 'standard'],
                'supported_formats': ['pdf', 'docx', 'txt', 'md']
            },
            'independence_level': 'complete'
        }
    }), name='aicc-intellidoc-discover-emergency'),
    
    # Template-specific URL routing (Enhanced - supports ANY template name)
    # FIXED: Using emergency endpoint only for AICC-IntelliDoc due to dynamic loading issues
    # path('api/templates/aicc-intellidoc/', include('templates.template_definitions.aicc-intellidoc.urls')),
    path('api/templates/legal/', include(include_template_urls('legal'))),
    path('api/templates/medical/', include(include_template_urls('medical'))),
    path('api/templates/history/', include(include_template_urls('history'))),
    
    # 🤖 PHASE 3: Multi-Provider LLM Configuration API
    path('api/llm/', include('agent_orchestration.llm_urls')),
    
    # 🤖 PHASE 3: Agent Orchestration DocAware API
    path('api/agent-orchestration/', include('agent_orchestration.workflow_urls')),
    
    # 🚀 Workflow Deployment API
    path('', include('agent_orchestration.deployment_urls')),
    
    # 🔑 PROJECT-SPECIFIC API KEY MANAGEMENT
    path('api/project-api-keys/', include('project_api_keys.urls')),
    
    # 🔧 MCP SERVER MANAGEMENT
    path('api/mcp-servers/', include('mcp_servers.urls')),
    
    # The public chatbot API (/api/public-chatbot/) was retired on 2026-08-05
    # along with its ChromaDB backend. Data exported to
    # backup/chromadb-removal-2026-08-05/.

    # Root route handler - redirect to frontend or return basic response
    path('', lambda request: JsonResponse({
        'message': 'AI Catalogue Backend API',
        'version': '3.0.0',
        'status': 'running',
        'frontend_url': 'http://localhost:5173',
        'admin_url': '/admin/',
        'api_docs': '/api/'
    }), name='api-root'),
    
    # Login route handler
    path('login/', lambda request: JsonResponse({
        'message': 'Please use the API endpoints for authentication',
        'token_endpoint': '/api/token/',
        'register_endpoint': '/api/register/',
        'frontend_login': 'http://localhost:5173/login'
    }), name='login-info'),
]

# PHASE 2: Enhanced endpoints have been consolidated into the main system
# All enhanced functionality is now available through:
# - /api/projects/{id}/digest/ (consolidated processing)
# - /api/projects/{id}/search/ (consolidated search)
# - /api/projects/{id}/capabilities/ (consolidated capabilities)
# - /api/projects/{id}/vector-status/ (consolidated status)
# 
# The consolidated system automatically selects the optimal processing mode
# based on project template configuration and capabilities

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
