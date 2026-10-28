# Universal Project API System - Phase 3 CLEAN VERSION (AutoGen Removed)
# backend/api/universal_project_views.py

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction, connection
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404
from asgiref.sync import async_to_sync
import mimetypes
from users.models import (
    IntelliDocProject,
    ProjectDocument,
    ProjectDocumentSummary,
    AgentWorkflow,
    SimulationRun,
    AgentMessage,
)
from templates.discovery import TemplateDiscoverySystem
from .serializers import IntelliDocProjectSerializer, ProjectDocumentSerializer
from .template_cloning_utils import clone_template_configuration
from .template_schema_validator import validate_template_config_schema
from agent_orchestration.serializers import (
    AgentWorkflowSerializer, SimulationRunSerializer, AgentMessageSerializer,
    WorkflowValidationSerializer, WorkflowExecutionSerializer
)
import logging
import uuid
import os
import json
import zipfile
import tempfile
from typing import Dict, Any, List, Tuple

# Try to import our custom schema validator, but don't fail if jsonschema isn't installed
try:
    from schemas.workflow_validator import AgentWorkflowValidator
    SCHEMA_VALIDATOR_AVAILABLE = True
except ImportError as e:
    SCHEMA_VALIDATOR_AVAILABLE = False
    print(f"⚠️  Schema validator not available: {e}")

logger = logging.getLogger(__name__)


def _cascade_disable_doc_aware(project) -> Tuple[int, int]:
    """Flip doc_aware=False on every AssistantAgent node across every
    AgentWorkflow in the given project. Returns (agents_affected,
    workflows_affected). Persists each workflow whose graph was
    mutated; other workflows are left untouched. Idempotent — running
    it twice when no agents remain doc_aware-enabled is a no-op."""
    agents_affected = 0
    workflows_affected = 0
    for wf in AgentWorkflow.objects.filter(project=project):
        graph = wf.graph_json or {}
        nodes = graph.get('nodes') or []
        changed = False
        for node in nodes:
            if node.get('type') != 'AssistantAgent':
                continue
            data = node.get('data') or {}
            if data.get('doc_aware'):
                data['doc_aware'] = False
                node['data'] = data
                changed = True
                agents_affected += 1
        if changed:
            wf.graph_json = graph
            wf.save(update_fields=['graph_json', 'updated_at'])
            workflows_affected += 1
    return agents_affected, workflows_affected

class UniversalProjectViewSet(viewsets.ModelViewSet):
    """
    Universal Project API - Phase 3 (CUSTOM Agent Implementation)
    
    Single API for ALL project operations regardless of template type.
    Uses CUSTOM agent orchestration system with JSON schema validation.
    AutoGen has been completely removed and replaced with our own implementation.
    """
    serializer_class = IntelliDocProjectSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'project_id'

    def _mark_vector_collection_stale(self, project):
        """
        Mark vector status as stale after document corpus changes (upload/delete).
        This prevents old COMPLETED status from being shown for a changed corpus.
        """
        try:
            from users.models import ProjectVectorCollection, VectorProcessingStatus

            collection = ProjectVectorCollection.objects.filter(project=project).first()
            if not collection:
                return

            collection.status = VectorProcessingStatus.PENDING
            collection.error_message = ''
            collection.total_documents = project.documents.count()
            collection.processed_documents = 0
            collection.failed_documents = 0
            collection.save(
                update_fields=[
                    'status',
                    'error_message',
                    'total_documents',
                    'processed_documents',
                    'failed_documents',
                    'updated_at',
                ]
            )
        except Exception as e:
            logger.warning(
                "⚠️ UNIVERSAL: Failed to mark vector collection stale for project %s: %s",
                getattr(project, 'project_id', None),
                e,
            )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if SCHEMA_VALIDATOR_AVAILABLE:
            self.workflow_validator = AgentWorkflowValidator()
        else:
            self.workflow_validator = None
    
    def get_queryset(self):
        """Get projects accessible to current user based on permissions"""
        user = self.request.user
        
        # Superusers and admin users can see all projects
        if user.is_superuser or user.is_admin:
            return IntelliDocProject.objects.all()
        
        # Regular users see projects they created + projects they have permission to access
        from django.db.models import Q
        
        # Get projects where user is creator
        created_projects = Q(created_by=user)
        
        # Get projects with direct user permissions
        user_permissions = Q(user_permissions__user=user)
        
        # Get projects with group permissions
        user_groups = user.groups.all()
        group_permissions = Q(group_permissions__group__in=user_groups)
        
        # Combine all conditions with OR
        return IntelliDocProject.objects.filter(
            created_projects | user_permissions | group_permissions
        ).distinct()
    
    def list(self, request, *args, **kwargs):
        """
        List all projects for current user
        GET /api/projects/
        """
        logger.info(f"📋 UNIVERSAL: Listing projects for user {request.user.email}")
        
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        # Enhanced response with metadata
        response_data = {
            'projects': serializer.data,
            'total_count': queryset.count(),
            'user_email': request.user.email,
            'templates_used': list(queryset.values_list('template_name', flat=True).distinct()),
            'project_types': list(queryset.values_list('template_type', flat=True).distinct()),
            'api_version': 'universal_v1',
            'agent_system': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic',
            'schema_validator_available': SCHEMA_VALIDATOR_AVAILABLE,
            'timestamp': timezone.now().isoformat()
        }
        
        logger.info(f"📊 UNIVERSAL: Returning {len(serializer.data)} projects")
        return Response(response_data)
    
    def retrieve(self, request, *args, **kwargs):
        """
        Get single project details
        GET /api/projects/{project_id}/
        """
        project = self.get_object()
        logger.info(f"📄 UNIVERSAL: Retrieving project {project.name} ({project.project_id})")
        
        serializer = self.get_serializer(project)
        
        # Enhanced response with additional metadata
        response_data = {
            **serializer.data,
            'documents_count': project.documents.count(),
            'ready_documents_count': project.documents.filter(upload_status='ready').count(),
            'processing_documents_count': project.documents.filter(upload_status='processing').count(),
            'api_version': 'universal_v1',
            'agent_system': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic',
            'schema_validator_available': SCHEMA_VALIDATOR_AVAILABLE,
            'retrieved_at': timezone.now().isoformat(),
            'template_independence': True,
            'universal_interface': True
        }
        
        return Response(response_data)
    
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """
        Create new project from template
        POST /api/projects/
        """
        logger.info(f"🏗️ UNIVERSAL: Creating project for user {request.user.email}")
        
        # Only admin users can create projects
        if not request.user.is_admin:
            logger.warning(f"🚫 UNIVERSAL: Non-admin user {request.user.email} attempted to create project")
            return Response({
                'error': 'Permission denied. Only admin users can create projects.',
                'detail': 'Project creation is restricted to administrators only.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        template_id = request.data.get('template_id')
        if not template_id:
            return Response({
                'error': 'template_id is required',
                'message': 'Please specify which template to use for project creation'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            template_data = TemplateDiscoverySystem.get_template_configuration(template_id)
            if not template_data:
                return Response({
                    'error': 'Template not found',
                    'message': f'Template with ID {template_id} does not exist'
                }, status=status.HTTP_404_NOT_FOUND)
            
            template_config = template_data.get('configuration', {})
            template_metadata = template_data.get('metadata', {})
            
            # Validate template configuration before cloning
            from .template_cloning_utils import validate_template_config
            missing_fields = validate_template_config(template_config, template_id)
            if missing_fields:
                logger.warning(f"Template {template_id} missing fields: {missing_fields}, proceeding with defaults")
            
            # Schema validation (non-strict - logs warnings but doesn't fail)
            schema_valid, schema_errors = validate_template_config_schema(template_config, template_id, strict=False)
            if not schema_valid:
                logger.warning(f"Template {template_id} schema validation found issues: {schema_errors}")
            
            # Clone template configuration with deep copy and audit trail
            cloned_config, audit_trail = clone_template_configuration(
                template_config, 
                template_metadata, 
                include_audit_trail=True
            )
            
            # Log audit trail
            if audit_trail:
                logger.info(f"📋 Template cloning audit for {template_id}: {json.dumps(audit_trail, indent=2)}")
            
            template_name = cloned_config.get('name') or template_metadata.get('name') or template_id.title()
            template_type = cloned_config.get('template_type') or template_metadata.get('template_type') or template_id
            
            # Handle icon_class fallback from metadata
            icon_class = cloned_config.get('icon_class') or template_metadata.get('ui_assets', {}).get('icon', 'fa-file-alt')
            
            # Extract template version
            template_version = template_metadata.get('version', '1.0.0')
            
            project_data = {
                **request.data,
                'project_id': str(uuid.uuid4()),
                'template_name': template_name,
                'template_type': template_type,
                'template_version': template_version,
                'instructions': cloned_config.get('instructions', ''),
                'suggested_questions': cloned_config.get('suggested_questions', []),
                'analysis_focus': cloned_config.get('analysis_focus', 'Document analysis'),
                'icon_class': icon_class,
                'color_theme': cloned_config.get('color_theme', 'oxford-blue'),
                'has_navigation': cloned_config.get('has_navigation', False),
                'total_pages': cloned_config.get('total_pages', 1),
                'navigation_pages': cloned_config.get('navigation_pages', []),
                'processing_capabilities': cloned_config.get('processing_capabilities', {}),
                'validation_rules': cloned_config.get('validation_rules', {}),
                'ui_configuration': cloned_config.get('ui_configuration', {})
            }
            
            serializer = self.get_serializer(data=project_data)
            serializer.is_valid(raise_exception=True)
            project = serializer.save(created_by=request.user)
            
            response_data = {
                **serializer.data,
                'template_info': {
                    'template_id': template_id,
                    'template_name': template_name,
                    'template_type': template_type,
                    'template_version': template_metadata.get('version', '1.0.0'),
                    'cloned_at': timezone.now().isoformat()
                },
                'cloning_audit': audit_trail if audit_trail else None,
                'project_capabilities': {
                    'processing_mode': 'enhanced_hierarchical' if template_type == 'aicc-intellidoc' else 'enhanced',
                    'supports_navigation': project.has_navigation,
                    'supports_multi_page': project.total_pages > 1,
                    'processing_capabilities': project.processing_capabilities,
                    'agent_orchestration': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic'
                },
                'api_version': 'universal_v1',
                'agent_system': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic',
                'schema_validator_available': SCHEMA_VALIDATOR_AVAILABLE,
                'created_at': timezone.now().isoformat(),
                'template_independence': True,
                'universal_interface': True
            }
            
            logger.info(f"✅ UNIVERSAL: Created project {project.name} ({project.project_id})")
            return Response(response_data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            import traceback
            from django.conf import settings
            
            # Determine where the failure occurred
            failed_at = 'unknown'
            template_version = 'unknown'
            if 'template_config' in locals():
                failed_at = 'cloning'
                template_version = template_metadata.get('version', 'unknown') if 'template_metadata' in locals() else 'unknown'
            elif 'template_data' in locals():
                failed_at = 'template_loading'
            else:
                failed_at = 'initialization'
            
            error_details = {
                'error': str(e),
                'error_type': type(e).__name__,
                'template_id': template_id if 'template_id' in locals() else 'unknown',
                'template_version': template_version,
                'failed_at': failed_at,
                'traceback': traceback.format_exc() if settings.DEBUG else None
            }
            
            logger.error(f"❌ UNIVERSAL: Project creation failed: {json.dumps(error_details, indent=2)}")
            
            return Response({
                'error': str(e),
                'message': 'Failed to create project',
                'template_id': template_id if 'template_id' in locals() else None,
                'details': error_details if settings.DEBUG else None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def preview_template_clone(self, request):
        """
        Preview what will be cloned from a template before creating a project.
        POST /api/projects/preview_template_clone/
        """
        template_id = request.data.get('template_id')
        if not template_id:
            return Response({
                'error': 'template_id is required',
                'message': 'Please specify which template to preview'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            template_data = TemplateDiscoverySystem.get_template_configuration(template_id)
            if not template_data:
                return Response({
                    'error': 'Template not found',
                    'message': f'Template with ID {template_id} does not exist'
                }, status=status.HTTP_404_NOT_FOUND)
            
            template_config = template_data.get('configuration', {})
            template_metadata = template_data.get('metadata', {})
            
            # Get cloning preview with audit trail
            cloned_config, audit_trail = clone_template_configuration(
                template_config,
                template_metadata,
                include_audit_trail=True
            )
            
            # Get schema validation summary
            from .template_schema_validator import get_schema_validation_summary
            schema_summary = get_schema_validation_summary(template_config)
            
            # Build preview response
            template_name = cloned_config.get('name') or template_metadata.get('name') or template_id.title()
            template_type = cloned_config.get('template_type') or template_metadata.get('template_type') or template_id
            icon_class = cloned_config.get('icon_class') or template_metadata.get('ui_assets', {}).get('icon', 'fa-file-alt')
            
            preview = {
                'template_id': template_id,
                'template_name': template_name,
                'template_type': template_type,
                'template_version': template_metadata.get('version', '1.0.0'),
                'icon_class': icon_class,
                'will_clone': {
                    'navigation_pages_count': len(cloned_config.get('navigation_pages', [])),
                    'navigation_pages': cloned_config.get('navigation_pages', [])[:3],  # First 3 pages as sample
                    'processing_capabilities_keys': list(cloned_config.get('processing_capabilities', {}).keys()),
                    'validation_rules_keys': list(cloned_config.get('validation_rules', {}).keys()),
                    'ui_configuration_keys': list(cloned_config.get('ui_configuration', {}).keys()),
                    'suggested_questions_count': len(cloned_config.get('suggested_questions', [])),
                    'has_navigation': cloned_config.get('has_navigation', False),
                    'total_pages': cloned_config.get('total_pages', 1)
                },
                'cloning_audit': audit_trail,
                'schema_validation': schema_summary,
                'preview_generated_at': timezone.now().isoformat()
            }
            
            return Response(preview, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Preview template clone failed: {e}")
            return Response({
                'error': str(e),
                'message': 'Failed to generate preview',
                'template_id': template_id
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['get'], url_path='experiment-metrics')
    def experiment_metrics(self, request, project_id=None):
        """Get experiment metrics for System Performance Analysis.

        Supports filtering via query params:
          - ?evaluation_id=<uuid>  → scope to one evaluation (legacy)
          - ?execution_id=<str>    → scope to one workflow run (new)
        The new sub-tab "Workflow Performance" drives the latter; the
        general "Project Analytics" tab leaves both empty.
        """
        from users.models import WorkflowEvaluation, WorkflowEvaluationResult, ExperimentMetric
        from django.db.models import Avg, Count, Q, Sum, Min, Max

        project = self.get_object()

        try:
            # Read metrics from database
            try:
                experiment_metrics = ExperimentMetric.objects.filter(project=project).order_by('-created_at')

                # Optional per-run filter (used by the Workflow Performance tab).
                execution_id_filter = request.query_params.get('execution_id', None)
                if execution_id_filter:
                    experiment_metrics = experiment_metrics.filter(execution_id=execution_id_filter)
                    logger.info(f"📊 PERFORMANCE: Filtering all metrics by execution_id={execution_id_filter}")

                total_count = experiment_metrics.count()
                logger.info(f"📊 PERFORMANCE: Found {total_count} experiment metrics for project {project.project_id}")
                
                # Log breakdown by experiment type for debugging
                if total_count > 0:
                    type_counts = {}
                    for exp_type in ['workflow_execution', 'docaware_single', 'docaware_context']:
                        count = experiment_metrics.filter(experiment_type=exp_type).count()
                        if count > 0:
                            type_counts[exp_type] = count
                    logger.info(f"📊 PERFORMANCE: Metric breakdown by type: {type_counts}")
            except Exception as e:
                logger.error(f"❌ PERFORMANCE: Failed to query ExperimentMetric table: {e}", exc_info=True)
                # Table might not exist if migration hasn't run
                return Response({
                    'error': 'ExperimentMetric table not found. Please run migrations: python manage.py migrate',
                    'metrics_count': 0
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            result_data = {}
            configurations = {}
            
            # Optionally filter by evaluation_id if provided in query params
            evaluation_id_filter = request.query_params.get('evaluation_id', None)
            if evaluation_id_filter:
                experiment_metrics = experiment_metrics.filter(evaluation_id=evaluation_id_filter)
                logger.info(f"📊 PERFORMANCE: Filtering all metrics by evaluation_id={evaluation_id_filter}")
            
            # Count deployment vs evaluation metrics
            deployment_metrics_count = experiment_metrics.filter(
                experiment_type='workflow_execution',
                configuration__is_deployment=True
            ).count()
            evaluation_metrics_count = experiment_metrics.filter(
                experiment_type='workflow_execution'
            ).exclude(evaluation_id__isnull=True).exclude(evaluation_id='').count()
            logger.info(f"📊 PERFORMANCE: Found {deployment_metrics_count} deployment metrics, {evaluation_metrics_count} evaluation metrics")
            
            # 1. Sequential vs Parallel Execution
            workflow_metrics = experiment_metrics.filter(experiment_type='workflow_execution')
            
            # Log deployment vs evaluation breakdown
            if workflow_metrics.exists():
                deployment_workflow_metrics = workflow_metrics.filter(configuration__is_deployment=True)
                evaluation_workflow_metrics = workflow_metrics.exclude(evaluation_id__isnull=True).exclude(evaluation_id='')
                logger.info(f"📊 PERFORMANCE: Sequential/Parallel analysis - {deployment_workflow_metrics.count()} deployment, {evaluation_workflow_metrics.count()} evaluation metrics")
            
            if workflow_metrics.exists():
                sequential = [m for m in workflow_metrics if m.metric_data.get('parallel_batches', 0) == 0]
                parallel = [m for m in workflow_metrics if m.metric_data.get('parallel_batches', 0) > 0]
                
                if sequential and parallel:
                    avg_sequential = sum(m.metric_data.get('duration_s', 0) for m in sequential) / len(sequential)
                    avg_parallel = sum(m.metric_data.get('duration_s', 0) for m in parallel) / len(parallel)
                    
                    # Get configuration from most recent metric
                    latest_config = workflow_metrics.first().configuration
                    configurations['sequential_vs_parallel'] = latest_config
                    
                    result_data['sequential_vs_parallel'] = {
                        'sequential_time_s': avg_sequential,
                        'parallel_time_s': avg_parallel,
                        'speedup_factor': avg_sequential / avg_parallel if avg_parallel > 0 else None
                    }
                elif sequential:
                    # Only sequential executions available
                    avg_sequential = sum(m.metric_data.get('duration_s', 0) for m in sequential) / len(sequential)
                    latest_config = workflow_metrics.first().configuration
                    configurations['sequential_vs_parallel'] = latest_config
                    
                    result_data['sequential_vs_parallel'] = {
                        'sequential_time_s': avg_sequential,
                        'parallel_time_s': None,
                        'speedup_factor': None
                    }
                elif parallel:
                    # Only parallel executions available
                    avg_parallel = sum(m.metric_data.get('duration_s', 0) for m in parallel) / len(parallel)
                    latest_config = workflow_metrics.first().configuration
                    configurations['sequential_vs_parallel'] = latest_config
                    
                    result_data['sequential_vs_parallel'] = {
                        'sequential_time_s': None,
                        'parallel_time_s': avg_parallel,
                        'speedup_factor': None
                    }
            
            # 2. DocAware Impact - Check workflow configuration for RAG status
            # Filter metrics by evaluation_id if available to get evaluation-specific metrics
            docaware_metrics = experiment_metrics.filter(experiment_type__in=['docaware_single', 'docaware_context'])
            
            # Optionally filter by evaluation_id if provided in query params
            evaluation_id_filter = request.query_params.get('evaluation_id', None)
            if evaluation_id_filter:
                docaware_metrics = docaware_metrics.filter(evaluation_id=evaluation_id_filter)
                logger.info(f"📊 PERFORMANCE: Filtering DocAware metrics by evaluation_id={evaluation_id_filter}")
            
            if docaware_metrics.exists():
                # Get evaluations and check their workflow configurations for RAG
                evaluations = WorkflowEvaluation.objects.filter(workflow__project=project).order_by('-created_at')
                
                # If evaluation_id filter is provided, only use that evaluation
                if evaluation_id_filter:
                    try:
                        target_eval = evaluations.get(evaluation_id=evaluation_id_filter)
                        evaluations = [target_eval]
                    except WorkflowEvaluation.DoesNotExist:
                        logger.warning(f"⚠️ PERFORMANCE: Evaluation {evaluation_id_filter} not found")
                        evaluations = []
                
                with_rag_eval = None
                without_rag_eval = None
                
                for eval_obj in evaluations:
                    workflow = eval_obj.workflow
                    # Check if workflow has RAG enabled
                    nodes = workflow.graph_json.get('nodes', [])
                    has_rag = any(
                        node.get('data', {}).get('doc_aware', False)
                        for node in nodes
                        if node.get('type') == 'AssistantAgent'
                    )
                    
                    if has_rag and with_rag_eval is None:
                        with_rag_eval = eval_obj
                    elif not has_rag and without_rag_eval is None:
                        without_rag_eval = eval_obj
                    
                    if with_rag_eval and without_rag_eval:
                        break
                
                # Initialize retrieval_times before the if/else block
                retrieval_times = []
                
                if with_rag_eval and without_rag_eval:
                    with_rag_results = WorkflowEvaluationResult.objects.filter(evaluation=with_rag_eval)
                    without_rag_results = WorkflowEvaluationResult.objects.filter(evaluation=without_rag_eval)
                    
                    avg_bert_with = with_rag_results.aggregate(avg=Avg('bert_score'))['avg'] or 0
                    avg_bert_without = without_rag_results.aggregate(avg=Avg('bert_score'))['avg'] or 0
                    
                    # Filter DocAware metrics by evaluation_id if we have specific evaluations
                    if with_rag_eval:
                        # Get metrics linked to with_rag_eval
                        with_rag_metrics = docaware_metrics.filter(evaluation_id=str(with_rag_eval.evaluation_id))
                        retrieval_times.extend([
                            m.metric_data.get('search_duration_ms', 0) 
                            for m in with_rag_metrics 
                            if m.metric_data.get('search_duration_ms')
                        ])
                    if without_rag_eval:
                        # Get metrics linked to without_rag_eval
                        without_rag_metrics = docaware_metrics.filter(evaluation_id=str(without_rag_eval.evaluation_id))
                        retrieval_times.extend([
                            m.metric_data.get('search_duration_ms', 0) 
                            for m in without_rag_metrics 
                            if m.metric_data.get('search_duration_ms')
                        ])
                    
                    # Get configuration from most recent metric
                    latest_config = docaware_metrics.first().configuration
                    configurations['docaware_impact'] = latest_config
                    
                    result_data['docaware_impact'] = {
                        'bertscore_delta': avg_bert_with - avg_bert_without,
                        'retrieval_overhead_ms': sum(retrieval_times) / len(retrieval_times) if retrieval_times else None,
                        'configuration': latest_config
                    }
                else:
                    # No comparison data available - need both with and without RAG evaluations
                    # Collect retrieval times from all docaware metrics if available
                    retrieval_times = [
                        m.metric_data.get('search_duration_ms', 0) 
                        for m in docaware_metrics 
                        if m.metric_data.get('search_duration_ms')
                    ]
                    
                    latest_config = docaware_metrics.first().configuration if docaware_metrics.exists() else {}
                    configurations['docaware_impact'] = latest_config
                    result_data['docaware_impact'] = {
                        'bertscore_delta': None,
                        'retrieval_overhead_ms': sum(retrieval_times) / len(retrieval_times) if retrieval_times else None,
                        'configuration': latest_config,
                        'message': 'Requires two evaluations: one with RAG enabled and one without'
                    }
            
            # 3. Agent Statistics - Per-agent performance metrics
            from users.models import WorkflowExecutionMessage, WorkflowExecution

            # Get all workflow executions for this project
            project_workflows = AgentWorkflow.objects.filter(project=project)
            project_executions = WorkflowExecution.objects.filter(workflow__in=project_workflows)
            # If the caller is scoping to a single run, limit agent_statistics
            # to that execution too — otherwise the drill-down view would
            # mix project-wide numbers into a per-run view.
            if execution_id_filter:
                project_executions = project_executions.filter(execution_id=execution_id_filter)

            # Aggregate agent statistics from WorkflowExecutionMessage
            agent_stats_query = WorkflowExecutionMessage.objects.filter(
                execution__in=project_executions
            ).exclude(
                agent_name__in=['Start', 'End']  # Exclude workflow control nodes
            ).values('agent_name', 'agent_type').annotate(
                total_executions=Count('execution', distinct=True),
                total_messages=Count('id'),
                avg_response_time_ms=Avg('response_time_ms'),
                total_tokens=Sum('token_count'),
                min_response_time_ms=Min('response_time_ms'),
                max_response_time_ms=Max('response_time_ms')
            ).order_by('-total_messages')
            
            agent_statistics = []
            for agent_stat in agent_stats_query:
                agent_statistics.append({
                    'agent_name': agent_stat['agent_name'],
                    'agent_type': agent_stat['agent_type'],
                    'total_executions': agent_stat['total_executions'],
                    'total_messages': agent_stat['total_messages'],
                    'avg_response_time_ms': round(agent_stat['avg_response_time_ms'] or 0, 2),
                    'total_tokens': agent_stat['total_tokens'] or 0,
                    'min_response_time_ms': agent_stat['min_response_time_ms'] or 0,
                    'max_response_time_ms': agent_stat['max_response_time_ms'] or 0,
                })
            
            result_data['agent_statistics'] = agent_statistics
            logger.info(f"📊 PERFORMANCE: Found {len(agent_statistics)} agents with statistics")

            # 4. Splitter decisions (one row per execute_splitter call)
            splitter_metrics = list(experiment_metrics.filter(experiment_type='splitter'))
            if splitter_metrics:
                split_durations = [m.metric_data.get('duration_ms') for m in splitter_metrics if m.metric_data.get('duration_ms') is not None]
                allocated_counts = [m.metric_data.get('allocated_count') or 0 for m in splitter_metrics]
                pruned_counts = [m.metric_data.get('pruned_count') or 0 for m in splitter_metrics]
                # Per-agent allocation tally for a simple top-list.
                allocation_tally: dict = {}
                for m in splitter_metrics:
                    for name in (m.metric_data.get('allocated_agent_names') or []):
                        if name:
                            allocation_tally[name] = allocation_tally.get(name, 0) + 1
                result_data['splitter_stats'] = {
                    'total_decisions': len(splitter_metrics),
                    'avg_duration_ms': round(sum(split_durations) / len(split_durations), 1) if split_durations else None,
                    'avg_allocated': round(sum(allocated_counts) / len(allocated_counts), 2) if allocated_counts else None,
                    'avg_pruned': round(sum(pruned_counts) / len(pruned_counts), 2) if pruned_counts else None,
                    'top_targets': sorted(
                        [{'agent_name': k, 'allocations': v} for k, v in allocation_tally.items()],
                        key=lambda x: -x['allocations'],
                    )[:10],
                }

            # 5. Classifier decisions
            classifier_metrics = list(experiment_metrics.filter(experiment_type='classifier'))
            if classifier_metrics:
                cls_durations = [m.metric_data.get('duration_ms') for m in classifier_metrics if m.metric_data.get('duration_ms') is not None]
                category_tally: dict = {}
                for m in classifier_metrics:
                    name = m.metric_data.get('category_name')
                    if name:
                        category_tally[name] = category_tally.get(name, 0) + 1
                result_data['classifier_stats'] = {
                    'total_decisions': len(classifier_metrics),
                    'avg_duration_ms': round(sum(cls_durations) / len(cls_durations), 1) if cls_durations else None,
                    'category_distribution': sorted(
                        [{'category_name': k, 'count': v} for k, v in category_tally.items()],
                        key=lambda x: -x['count'],
                    ),
                }

            # Add configurations to response
            result_data['configurations'] = configurations

            return Response(result_data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Failed to retrieve experiment metrics: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return Response({
                'error': f'Failed to retrieve experiment metrics: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['get'], url_path='recent-executions')
    def recent_executions(self, request, project_id=None):
        """
        Return the most recent workflow executions for this project —
        powers the "Workflow Performance" sub-tab's dropdown.

        Query params:
          - ?limit=<int>   default 50, capped at 200

        Response shape:
          { "executions": [
              { "execution_id": str, "workflow_id": str|null,
                "workflow_name": str, "started_at": iso8601,
                "duration_s": float|null, "status": str,
                "total_nodes": int, "parallel_batches": int }
            , …] }
        """
        from users.models import WorkflowExecution, AgentWorkflow

        project = self.get_object()
        try:
            limit = int(request.query_params.get('limit') or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        qs = (
            WorkflowExecution.objects
            .filter(workflow__project=project)
            .select_related('workflow')
            .order_by('-start_time')[:limit]
        )

        executions = []
        for ex in qs:
            wf = ex.workflow
            # duration_seconds is authoritative when set; otherwise compute
            # from timestamps so still-running executions show an estimate.
            duration_s = ex.duration_seconds
            if duration_s is None and ex.start_time and ex.end_time:
                try:
                    duration_s = round((ex.end_time - ex.start_time).total_seconds(), 2)
                except Exception:
                    duration_s = None
            executions.append({
                'execution_id': ex.execution_id,
                'workflow_id': str(wf.workflow_id) if wf else None,
                'workflow_name': (wf.name if wf else '(deleted workflow)'),
                'started_at': ex.start_time.isoformat() if ex.start_time else None,
                'duration_s': duration_s,
                'status': ex.status,
                'total_agents_involved': ex.total_agents_involved,
                'total_messages': ex.total_messages,
                'executed_nodes_count': len(ex.executed_nodes or {}),
            })
        return Response({'executions': executions}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='analytics')
    def analytics(self, request, project_id=None):
        """
        Agent timing analytics for the dedicated Analytics page.

        Returns four sections built from existing models (no new DB tables):
          planning_time   — from tool_plan messages
          tool_breakdown  — per-tool-type avg/count from tool_notebook messages
          websearch       — from ExperimentMetric websearch records
          execution_volume — daily execution counts + status breakdown
        """
        from users.models import WorkflowExecutionMessage, WorkflowExecution, ExperimentMetric
        from django.db.models import Avg, Count, Sum, Min, Max, Q
        from django.db.models.functions import TruncDate

        project = self.get_object()
        project_workflows = AgentWorkflow.objects.filter(project=project)
        project_executions = WorkflowExecution.objects.filter(workflow__in=project_workflows)

        try:
            # ── 1. Planning time ──────────────────────────────────────
            plan_qs = WorkflowExecutionMessage.objects.filter(
                execution__in=project_executions,
                message_type='tool_plan',
            )
            plan_agg = plan_qs.aggregate(
                avg_ms=Avg('response_time_ms'),
                max_ms=Max('response_time_ms'),
                min_ms=Min('response_time_ms'),
                total_count=Count('id'),
            )

            # Daily trend (last 30 days)
            plan_daily = list(
                plan_qs.annotate(day=TruncDate('timestamp'))
                .values('day')
                .annotate(avg_ms=Avg('response_time_ms'), count=Count('id'))
                .order_by('day')[:30]
            )
            plan_trend = [
                {'date': str(r['day']), 'avg_ms': round(r['avg_ms'] or 0, 1), 'count': r['count']}
                for r in plan_daily
            ]

            planning_time = {
                'avg_ms': round(plan_agg['avg_ms'] or 0, 1),
                'max_ms': plan_agg['max_ms'] or 0,
                'min_ms': plan_agg['min_ms'] or 0,
                'total_count': plan_agg['total_count'],
                'trend': plan_trend,
            }

            # ── 2. Tool call breakdown ────────────────────────────────
            notebook_qs = WorkflowExecutionMessage.objects.filter(
                execution__in=project_executions,
                message_type='tool_notebook',
            )

            # Group by metadata tool_type (PostgreSQL JSONB field)
            try:
                tool_rows = list(
                    notebook_qs
                    .exclude(metadata__tool_type=None)
                    .values('metadata__tool_type')
                    .annotate(
                        avg_ms=Avg('response_time_ms'),
                        total_count=Count('id'),
                        max_ms=Max('response_time_ms'),
                    )
                    .order_by('-total_count')
                )
                tool_breakdown = [
                    {
                        'tool_type': r['metadata__tool_type'] or 'unknown',
                        'avg_ms': round(r['avg_ms'] or 0, 1),
                        'max_ms': r['max_ms'] or 0,
                        'count': r['total_count'],
                    }
                    for r in tool_rows
                ]
                # Fallback totals for rows without tool_type metadata (legacy 0-ms records)
                legacy_count = notebook_qs.filter(metadata__tool_type=None).count()
                if legacy_count:
                    tool_breakdown.append({
                        'tool_type': 'legacy (no timing)',
                        'avg_ms': 0,
                        'max_ms': 0,
                        'count': legacy_count,
                    })
            except Exception:
                tool_breakdown = []

            # ── 3. WebSearch latency ──────────────────────────────────
            # Fix from previous version: filter was using the wrong
            # experiment_type values ('websearch_performance', 'web_search')
            # that are never written — the actual writer emits 'websearch'.
            # That's why cache_hit_rate stayed at None for everyone.
            ws_metrics = list(
                ExperimentMetric.objects.filter(
                    project=project,
                    experiment_type='websearch',
                ).order_by('-created_at')[:50]
            )

            ws_durations = []
            ws_cache_hits = 0
            ws_cache_total = 0
            for m in ws_metrics:
                md = m.metric_data or {}
                dur = md.get('duration_ms') or md.get('total_duration_ms')
                if dur is not None:
                    ws_durations.append(float(dur))
                cache_hit = md.get('cache_hit')
                if cache_hit is not None:
                    ws_cache_total += 1
                    if cache_hit:
                        ws_cache_hits += 1

            websearch = {
                'avg_ms': round(sum(ws_durations) / len(ws_durations), 1) if ws_durations else None,
                'max_ms': max(ws_durations) if ws_durations else None,
                'min_ms': min(ws_durations) if ws_durations else None,
                'sample_count': len(ws_durations),
                'cache_hit_rate': round(ws_cache_hits / ws_cache_total * 100, 1) if ws_cache_total else None,
                'recent': [
                    {
                        'date': str(m.created_at.date()),
                        'duration_ms': (m.metric_data or {}).get('duration_ms'),
                        'cache_hit': (m.metric_data or {}).get('cache_hit'),
                        'cache_tier': (m.metric_data or {}).get('cache_tier'),
                        'mode': (m.metric_data or {}).get('mode'),
                        'agent_name': (m.metric_data or {}).get('agent_name'),
                        'query': (m.metric_data or {}).get('query', '')[:80],
                    }
                    for m in ws_metrics[:20]
                ],
            }

            # ── 3b. Cache-tier breakdown ─────────────────────────────
            # Count websearch calls by cache_tier across the whole sample
            # plus aggregate the websearch_index_batch per-tier URL hits
            # from sync-index runs. These two together explain how much
            # work the caches are saving.
            tier_counts: dict = {}
            for m in ws_metrics:
                tier = (m.metric_data or {}).get('cache_tier') or 'unknown'
                tier_counts[tier] = tier_counts.get(tier, 0) + 1

            batch_metrics = list(
                ExperimentMetric.objects.filter(
                    project=project,
                    experiment_type='websearch_index_batch',
                ).order_by('-created_at')[:50]
            )
            batch_totals = {
                'flag_alive_hits': 0,
                'content_hash_hits': 0,
                'embed_cache_hits': 0,
                'cold_count': 0,
                'n_urls': 0,
                'sync_runs': len(batch_metrics),
            }
            for m in batch_metrics:
                md = m.metric_data or {}
                batch_totals['flag_alive_hits'] += int(md.get('flag_alive_hits') or 0)
                batch_totals['content_hash_hits'] += int(md.get('content_hash_hits') or 0)
                batch_totals['embed_cache_hits'] += int(md.get('embed_cache_hits') or 0)
                batch_totals['cold_count'] += int(md.get('cold_count') or 0)
                batch_totals['n_urls'] += int(md.get('n_urls') or 0)
            cache_tier_breakdown = {
                'call_tier_counts': tier_counts,      # { tier: N websearch calls at this tier }
                'url_tier_totals': batch_totals,       # cumulative URL hits across sync-index runs
            }

            # ── 3c. Summary-job health ───────────────────────────────
            summary_jobs_qs = list(
                ExperimentMetric.objects.filter(
                    project=project,
                    experiment_type='summary_job',
                ).order_by('-created_at')[:20]
            )
            summary_jobs = {
                'total_jobs': len(summary_jobs_qs),
                'ok_count': sum(1 for m in summary_jobs_qs if (m.metric_data or {}).get('status') == 'ok'),
                'error_count': sum(1 for m in summary_jobs_qs if (m.metric_data or {}).get('status') == 'error'),
                'recent': [
                    {
                        'date': m.created_at.isoformat(),
                        'urls_queued': (m.metric_data or {}).get('urls_queued'),
                        'summarized': (m.metric_data or {}).get('summarized'),
                        'failed': (m.metric_data or {}).get('failed'),
                        'duration_ms': (m.metric_data or {}).get('duration_ms'),
                        'status': (m.metric_data or {}).get('status'),
                        'error': (m.metric_data or {}).get('error'),
                        'llm_model': (m.metric_data or {}).get('llm_model'),
                    }
                    for m in summary_jobs_qs
                ],
            }

            # ── 4. Execution volume ───────────────────────────────────
            exec_daily = list(
                project_executions
                .annotate(day=TruncDate('start_time'))
                .values('day')
                .annotate(
                    total=Count('id'),
                    completed=Count('id', filter=Q(status='completed')),
                    failed=Count('id', filter=Q(status='failed')),
                )
                .order_by('day')[:60]
            )
            execution_volume = {
                'total_executions': project_executions.count(),
                'daily': [
                    {
                        'date': str(r['day']),
                        'total': r['total'],
                        'completed': r['completed'],
                        'failed': r['failed'],
                    }
                    for r in exec_daily
                ],
            }

            # ── 5. Query word cloud ───────────────────────────────────
            import re as _re
            from users.models import HumanInputInteraction

            STOPWORDS = {
                'the','a','an','and','or','but','in','on','at','to','for','of','with',
                'is','it','its','be','are','was','were','has','have','had','do','does',
                'did','will','would','could','should','may','might','can','i','you',
                'he','she','we','they','me','him','her','us','them','my','your','his',
                'our','their','this','that','these','those','what','how','why','when',
                'where','who','which','so','if','then','than','as','by','from','into',
                'about','up','out','no','not','all','more','also','just','get','like',
                'use','used','using','please','need','want','make','know','tell',
                'user','assistant',
            }

            raw_texts = list(
                WorkflowExecutionMessage.objects.filter(
                    execution__in=project_executions,
                    message_type__in=['workflow_start', 'human_input'],
                ).values_list('content', flat=True)[:2000]
            )

            # Extract only user-authored text.
            # Deployment workflow_start messages contain a full conversation string
            # like "Assistant: ...\nUser: query\nAssistant: ...". Extract only
            # the User: lines. Plain StartNode prompts (admin workflows) have no
            # role prefix and are included as-is.
            user_only_texts = []
            for text in raw_texts:
                if text and ('User:' in text or 'Assistant:' in text):
                    for line in text.split('\n'):
                        if line.startswith('User:'):
                            user_only_texts.append(line[5:].strip())
                elif text:
                    user_only_texts.append(text)

            human_texts = list(
                HumanInputInteraction.objects.filter(
                    execution__in=project_executions,
                ).values_list('human_response', flat=True)[:500]
            )

            all_text = ' '.join(t for t in (user_only_texts + human_texts) if t)
            raw_words = _re.findall(r"[a-zA-Z']{3,}", all_text.lower())
            word_freq: dict = {}
            for w in raw_words:
                w = w.strip("'")
                if len(w) >= 3 and w not in STOPWORDS:
                    word_freq[w] = word_freq.get(w, 0) + 1

            top_words = sorted(word_freq.items(), key=lambda x: -x[1])[:80]
            query_wordcloud = [{'word': w, 'count': c} for w, c in top_words]

            return Response({
                'planning_time': planning_time,
                'tool_breakdown': tool_breakdown,
                'websearch': websearch,
                'cache_tier_breakdown': cache_tier_breakdown,
                'summary_jobs': summary_jobs,
                'execution_volume': execution_volume,
                'query_wordcloud': query_wordcloud,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Failed to retrieve analytics: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return Response(
                {'error': f'Failed to retrieve analytics: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=['get'])
    def documents(self, request, project_id=None):
        """Get project documents"""
        project = self.get_object()
        documents = project.documents.all().order_by('-uploaded_at')
        serializer = ProjectDocumentSerializer(documents, many=True)
        
        return Response({
            'documents': serializer.data,
            'total_count': documents.count(),
            'project_id': project.project_id,
            'api_version': 'universal_v1'
        })
    
    @action(detail=True, methods=['post'])
    def upload_document(self, request, project_id=None):
        """Upload document to project"""
        project = self.get_object()

        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        uploaded_file = request.FILES['file']

        # Check for duplicate filename
        if ProjectDocument.objects.filter(project=project, original_filename=uploaded_file.name).exists():
            return Response(
                {'error': f'File "{uploaded_file.name}" already exists in this project'},
                status=status.HTTP_409_CONFLICT
            )

        try:
            with transaction.atomic():
                document = ProjectDocument.objects.create(
                    project=project,
                    original_filename=uploaded_file.name,
                    file_size=uploaded_file.size,
                    file_type=uploaded_file.content_type,
                    file_extension=os.path.splitext(uploaded_file.name)[1].lower(),
                    upload_status='processing',
                    uploaded_by=request.user
                )
                
                file_path = document.get_storage_path()
                saved_path = default_storage.save(file_path, ContentFile(uploaded_file.read()))
                document.file_path = saved_path
                document.upload_status = 'ready'
                document.save()
                self._mark_vector_collection_stale(project)
                
                serializer = ProjectDocumentSerializer(document)
                return Response({
                    **serializer.data,
                    'message': 'Document uploaded successfully',
                    'api_version': 'universal_v1'
                }, status=status.HTTP_201_CREATED)
                
        except Exception as e:
            return Response({'error': 'Upload failed', 'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def upload_bulk_files(self, request, project_id=None):
        """Upload multiple files in a single request"""
        project = self.get_object()

        if not request.FILES:
            return Response({'error': 'No files provided'}, status=status.HTTP_400_BAD_REQUEST)

        uploaded_documents = []
        failed_uploads = []
        skipped_duplicates = []

        # Pre-fetch existing filenames for fast duplicate checking
        existing_filenames = set(
            ProjectDocument.objects.filter(project=project)
            .values_list('original_filename', flat=True)
        )

        for file_key, uploaded_file in request.FILES.items():
            # Skip duplicates with a clear message
            if uploaded_file.name in existing_filenames:
                skipped_duplicates.append(uploaded_file.name)
                failed_uploads.append({
                    'filename': uploaded_file.name,
                    'error': 'File already exists in this project'
                })
                continue

            try:
                with transaction.atomic():
                    document = ProjectDocument.objects.create(
                        project=project,
                        original_filename=uploaded_file.name,
                        file_size=uploaded_file.size,
                        file_type=uploaded_file.content_type,
                        file_extension=os.path.splitext(uploaded_file.name)[1].lower(),
                        upload_status='processing',
                        uploaded_by=request.user
                    )

                    file_path = document.get_storage_path()
                    saved_path = default_storage.save(file_path, ContentFile(uploaded_file.read()))
                    document.file_path = saved_path
                    document.upload_status = 'ready'
                    document.save()
                    self._mark_vector_collection_stale(project)

                    uploaded_documents.append(ProjectDocumentSerializer(document).data)
                    existing_filenames.add(uploaded_file.name)

            except Exception as e:
                failed_uploads.append({
                    'filename': uploaded_file.name,
                    'error': str(e)
                })
                logger.error(f"Failed to upload {uploaded_file.name}: {e}")

        return Response({
            'uploaded_documents': uploaded_documents,
            'failed_uploads': failed_uploads,
            'skipped_duplicates': skipped_duplicates,
            'total_attempted': len(request.FILES),
            'total_successful': len(uploaded_documents),
            'total_failed': len(failed_uploads),
            'message': f'Bulk upload completed: {len(uploaded_documents)} successful, {len(failed_uploads)} failed',
            'api_version': 'universal_v1'
        }, status=status.HTTP_201_CREATED if uploaded_documents else status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def upload_zip_file(self, request, project_id=None):
        """Upload and extract a zip file containing documents"""
        project = self.get_object()
        
        if 'file' not in request.FILES:
            return Response({'error': 'No zip file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        uploaded_file = request.FILES['file']
        
        # Validate it's a zip file
        if not uploaded_file.name.lower().endswith('.zip'):
            return Response({'error': 'File must be a zip archive'}, status=status.HTTP_400_BAD_REQUEST)
        
        uploaded_documents = []
        failed_extractions = []
        extracted_files_info = []
        
        try:
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                # Save uploaded zip to temporary file
                for chunk in uploaded_file.chunks():
                    temp_file.write(chunk)
                temp_file_path = temp_file.name
            
            try:
                with zipfile.ZipFile(temp_file_path, 'r') as zip_ref:
                    # Get list of all files in zip
                    file_list = zip_ref.namelist()
                    
                    # Filter out directories and system files more comprehensively
                    valid_files = []
                    for f in file_list:
                        # Skip directories
                        if f.endswith('/'):
                            continue
                        # Skip system files and directories
                        if (f.startswith('__MACOSX/') or 
                            f.startswith('.DS_Store') or
                            '/.DS_Store' in f or
                            f.endswith('.tmp') or
                            '/.git/' in f or
                            f.startswith('.git/')):
                            continue
                        # Clean up path separators
                        clean_path = f.replace('\\', '/').strip('/')
                        if clean_path:  # Make sure we have a valid path
                            valid_files.append(f)
                    
                    if not valid_files:
                        return Response({
                            'error': 'No valid files found in zip archive',
                            'message': 'Zip file contains no extractable documents'
                        }, status=status.HTTP_400_BAD_REQUEST)
                    
                    # Check for supported file types
                    supported_extensions = ['.pdf', '.doc', '.docx', '.txt', '.md', '.rtf']
                    
                    # Pre-fetch existing filenames for duplicate checking
                    existing_filenames = set(
                        ProjectDocument.objects.filter(project=project)
                        .values_list('original_filename', flat=True)
                    )

                    for file_path in valid_files:
                        try:
                            # Get file extension
                            _, ext = os.path.splitext(file_path.lower())

                            if ext not in supported_extensions:
                                failed_extractions.append({
                                    'filename': file_path,
                                    'error': f'Unsupported file type: {ext}'
                                })
                                continue

                            # Extract file data
                            with zip_ref.open(file_path) as extracted_file:
                                file_data = extracted_file.read()

                            # Preserve the full relative path (like folder upload)
                            filename = file_path
                            filename = filename.replace('\\', '/').strip('/')

                            # Skip system files
                            if (filename.startswith('__MACOSX/') or
                                filename.startswith('.DS_Store') or
                                '/.DS_Store' in filename):
                                failed_extractions.append({
                                    'filename': filename,
                                    'error': 'System file or directory'
                                })
                                continue

                            # Skip duplicates with a clear message
                            if filename in existing_filenames:
                                failed_extractions.append({
                                    'filename': filename,
                                    'error': 'File already exists in this project'
                                })
                                continue

                            with transaction.atomic():
                                document = ProjectDocument.objects.create(
                                    project=project,
                                    original_filename=filename,
                                    file_size=len(file_data),
                                    file_type=self._get_mime_type_from_extension(ext),
                                    file_extension=ext,
                                    upload_status='processing',
                                    uploaded_by=request.user
                                )

                                doc_file_path = document.get_storage_path()
                                saved_path = default_storage.save(doc_file_path, ContentFile(file_data))
                                document.file_path = saved_path
                                document.upload_status = 'ready'
                                document.save()
                                self._mark_vector_collection_stale(project)

                            uploaded_documents.append(ProjectDocumentSerializer(document).data)
                            extracted_files_info.append({
                                'original_path': file_path,
                                'filename': filename,
                                'size': len(file_data),
                                'extension': ext
                            })
                            existing_filenames.add(filename)

                        except Exception as e:
                            failed_extractions.append({
                                'filename': file_path,
                                'error': str(e)
                            })
                            logger.error(f"Failed to extract {file_path}: {e}")
                
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            
            return Response({
                'uploaded_documents': uploaded_documents,
                'failed_extractions': failed_extractions,
                'extracted_files_info': extracted_files_info,
                'zip_filename': uploaded_file.name,
                'total_files_in_zip': len(file_list),
                'total_valid_files': len(valid_files),
                'total_extracted': len(uploaded_documents),
                'total_failed': len(failed_extractions),
                'message': f'Zip extraction completed: {len(uploaded_documents)} files extracted, {len(failed_extractions)} failed',
                'api_version': 'universal_v1'
            }, status=status.HTTP_201_CREATED if uploaded_documents else status.HTTP_400_BAD_REQUEST)
            
        except zipfile.BadZipFile:
            return Response({
                'error': 'Invalid zip file', 
                'message': 'The uploaded file is not a valid zip archive'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': 'Zip upload failed', 'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _get_mime_type_from_extension(self, ext: str) -> str:
        """Get MIME type from file extension"""
        mime_types = {
            '.pdf': 'application/pdf',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.txt': 'text/plain',
            '.md': 'text/markdown',
            '.rtf': 'application/rtf'
        }
        return mime_types.get(ext.lower(), 'application/octet-stream')
    
    @action(detail=True, methods=['get'], url_path='documents/(?P<document_id>[^/.]+)/download')
    def download_document(self, request, project_id=None, document_id=None):
        """Download a document from the project"""
        project = self.get_object()
        
        try:
            # Get document with security check - must belong to this project
            document = ProjectDocument.objects.get(
                document_id=document_id,
                project=project
            )
            
            # Check if file exists in storage
            if not document.file_path or not default_storage.exists(document.file_path):
                return Response({
                    'error': 'File not found',
                    'message': 'The document file could not be found in storage'
                }, status=status.HTTP_404_NOT_FOUND)
            
            # Open file from storage
            file_handle = default_storage.open(document.file_path, 'rb')
            
            # Determine content type
            content_type = document.file_type or mimetypes.guess_type(document.original_filename)[0] or 'application/octet-stream'
            
            # Create response with file
            response = FileResponse(
                file_handle,
                content_type=content_type,
                as_attachment=False  # Allow browser to display PDFs inline
            )
            
            # Set Content-Disposition header
            filename = document.original_filename
            # Handle filenames with special characters
            response['Content-Disposition'] = f'inline; filename="{filename}"'
            response['Content-Length'] = document.file_size
            
            # Add cache control headers
            response['Cache-Control'] = 'private, max-age=3600'
            
            logger.info(f"📥 DOWNLOAD: Serving document {document_id} ({filename}) for project {project_id}")
            
            return response
            
        except ProjectDocument.DoesNotExist:
            return Response({
                'error': 'Document not found',
                'message': f'No document found with ID {document_id} in this project'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"❌ DOWNLOAD: Error downloading document {document_id}: {e}")
            return Response({
                'error': 'Download failed',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['get'], url_path='documents/(?P<document_id>[^/.]+)/summary')
    def document_summary(self, request, project_id=None, document_id=None):
        """Return AI-generated long/short summaries for a project document."""
        project = self.get_object()
        try:
            document = ProjectDocument.objects.get(
                document_id=document_id,
                project=project,
            )
        except ProjectDocument.DoesNotExist:
            return Response(
                {
                    'error': 'Document not found',
                    'message': f'No document found with ID {document_id} in this project',
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        summary_row = (
            ProjectDocumentSummary.objects.filter(document=document).first()
        )
        long_text = ""
        short_text = ""
        generated_at = None
        updated_at = None
        llm_provider = ""
        llm_model = ""

        if summary_row:
            long_text = (summary_row.long_summary or "").strip()
            short_text = (summary_row.short_summary or "").strip()
            generated_at = (
                summary_row.generated_at.isoformat()
                if summary_row.generated_at
                else None
            )
            updated_at = (
                summary_row.updated_at.isoformat()
                if summary_row.updated_at
                else None
            )
            llm_provider = summary_row.llm_provider or ""
            llm_model = summary_row.llm_model or ""

        has_summary = bool(long_text or short_text)
        message = ""
        if not has_summary:
            message = (
                "No AI summary yet. Run processing with Enable Summary turned on "
                "in Document Processing."
            )

        return Response(
            {
                'document_id': str(document.document_id),
                'original_filename': document.original_filename,
                'long_summary': long_text,
                'short_summary': short_text,
                'has_summary': has_summary,
                'generated_at': generated_at,
                'updated_at': updated_at,
                'llm_provider': llm_provider,
                'llm_model': llm_model,
                'message': message,
                'api_version': 'universal_v1',
            },
            status=status.HTTP_200_OK,
        )
    
    @action(detail=True, methods=['post'], url_path='documents/upload-to-llm')
    def upload_documents_to_llm(self, request, project_id=None):
        """
        Upload project documents to LLM provider File APIs.
        
        This stores the provider-specific file_id on each document so it can be
        referenced later via the File Attachments feature without re-uploading.
        
        Request body:
        {
            "document_ids": ["uuid1", "uuid2"],  // optional, defaults to all ready docs
            "providers": ["openai", "anthropic"]  // optional, defaults to all with API keys
        }
        """
        project = self.get_object()
        
        document_ids = request.data.get('document_ids', [])
        providers = request.data.get('providers', [])
        
        try:
            # Get documents to upload
            if document_ids:
                documents = ProjectDocument.objects.filter(
                    project=project,
                    document_id__in=document_ids
                )
            else:
                documents = ProjectDocument.objects.filter(
                    project=project,
                    upload_status='ready'
                )
            
            if not documents.exists():
                return Response({
                    'error': 'No documents found',
                    'message': 'No matching documents found for upload'
                }, status=status.HTTP_404_NOT_FOUND)
            
            # Default providers to all that have API keys
            if not providers:
                providers = ['openai', 'anthropic', 'google']
            
            # Upload using LLMFileUploadService
            from agent_orchestration.llm_file_service import LLMFileUploadService
            
            service = LLMFileUploadService(project)
            
            results = {}
            for doc in documents:
                doc_key = str(doc.document_id)
                doc_results = {}
                
                for provider in providers:
                    # Skip if already uploaded (deduplication)
                    existing_id = LLMFileUploadService.get_file_id_for_provider(doc, provider)
                    if existing_id:
                        doc_results[provider] = {
                            'file_id': existing_id,
                            'status': 'already_uploaded'
                        }
                        continue
                    
                    # Check provider support before uploading
                    supported, reason = LLMFileUploadService.check_provider_support(doc, provider)
                    if not supported:
                        doc_results[provider] = {
                            'error': reason,
                            'status': 'unsupported'
                        }
                        continue
                    
                    # Perform the upload (sync wrapper around async method)
                    try:
                        result = async_to_sync(service._upload_to_provider)(doc, provider)
                        if result.get('file_id'):
                            doc_results[provider] = {
                                'file_id': result['file_id'],
                                'status': result.get('status', 'uploaded')
                            }
                        else:
                            doc_results[provider] = {
                                'error': result.get('error', 'Unknown error'),
                                'status': result.get('status', 'failed'),
                                'reason': result.get('reason')
                            }
                    except Exception as e:
                        doc_results[provider] = {
                            'error': str(e),
                            'status': 'failed'
                        }
                
                results[doc_key] = {
                    'filename': doc.original_filename,
                    'providers': doc_results
                }
            
            # Count successes
            total_uploaded = sum(
                1 for doc_result in results.values()
                for prov_result in doc_result['providers'].values()
                if prov_result.get('status') == 'uploaded'
            )
            total_already = sum(
                1 for doc_result in results.values()
                for prov_result in doc_result['providers'].values()
                if prov_result.get('status') == 'already_uploaded'
            )
            
            logger.info(
                f"📎 LLM UPLOAD: Project {project_id} - "
                f"{total_uploaded} new uploads, {total_already} already uploaded"
            )
            
            return Response({
                'status': 'completed',
                'documents': results,
                'summary': {
                    'total_documents': len(results),
                    'new_uploads': total_uploaded,
                    'already_uploaded': total_already,
                    'providers_targeted': providers
                }
            })
            
        except Exception as e:
            logger.error(f"❌ LLM UPLOAD: Error uploading documents for project {project_id}: {e}")
            import traceback
            logger.error(f"❌ LLM UPLOAD: Traceback: {traceback.format_exc()}")
            return Response({
                'error': 'Upload failed',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['post'])
    def process_documents(self, request, project_id=None):
        """Process project documents using consolidated processing - FIXED"""
        from vector_search.consolidated_api_views import process_unified_consolidated

        # Enforce project access: get_object() uses get_queryset() and returns 404 for inaccessible projects
        project = self.get_object()

        # Extract LLM configuration from request
        llm_provider = request.data.get('llm_provider', 'openai')
        llm_model = request.data.get('llm_model', 'gpt-5.3-chat-latest')
        enable_summary = request.data.get('enable_summary', True)

        logger.info(f"🚀 UNIVERSAL: Delegating document processing for project {project.project_id}")
        logger.info(f"📋 UNIVERSAL: LLM Config - Provider: {llm_provider}, Model: {llm_model}, Enable Summary: {enable_summary}")

        # Prepare LLM config dict
        llm_config = {
            'llm_provider': llm_provider,
            'llm_model': llm_model,
            'enable_summary': enable_summary
        }

        # Pass the DRF request object and LLM config as parameter
        return process_unified_consolidated(request, str(project.project_id), llm_config=llm_config)
    
    @action(detail=True, methods=['post'])
    def search(self, request, project_id=None):
        """Search project documents using consolidated search"""
        from vector_search.consolidated_api_views import search_unified_consolidated

        # Enforce project access: get_object() uses get_queryset() and returns 404 for inaccessible projects
        project = self.get_object()
        logger.info(f"🔍 UNIVERSAL: Delegating document search for project {project.project_id}")
        return search_unified_consolidated(request._request, str(project.project_id))
    
    @action(detail=True, methods=['get'])
    def vector_status(self, request, project_id=None):
        """Get vector status using consolidated status endpoint"""
        from vector_search.consolidated_api_views import get_vector_status_consolidated

        # Enforce project access: get_object() uses get_queryset() and returns 404 for inaccessible projects
        project = self.get_object()
        logger.info(f"📊 UNIVERSAL: Delegating vector status for project {project.project_id}")
        return get_vector_status_consolidated(request._request, str(project.project_id))
    
    @action(detail=True, methods=['get'])
    def capabilities(self, request, project_id=None):
        """Get project capabilities using consolidated capabilities endpoint"""
        from vector_search.consolidated_api_views import get_project_capabilities_consolidated

        # Enforce project access: get_object() uses get_queryset() and returns 404 for inaccessible projects
        project = self.get_object()
        logger.info(f"🎯 UNIVERSAL: Delegating capabilities check for project {project.project_id}")
        return get_project_capabilities_consolidated(request._request, str(project.project_id))
    
    @action(detail=True, methods=['patch'], url_path='rag-setting')
    def rag_setting(self, request, project_id=None):
        """
        Update the RAG (semantic search) toggle for a project.

        When disabled, Start Processing skips Milvus indexing — agents can
        still use tool-calling with summaries but DocAware queries return
        empty. When flipping from enabled→disabled, every AssistantAgent
        with doc_aware=True across every workflow in the project is
        auto-flipped to doc_aware=False. Vectors in Milvus are kept so
        re-enabling later skips re-embedding the same docs.

        PATCH /api/projects/{project_id}/rag-setting/
        Body: {"rag_enabled": true/false}
        """
        project = self.get_object()

        rag_enabled = request.data.get('rag_enabled')
        if rag_enabled is None:
            return Response({
                'error': 'rag_enabled field is required',
                'api_version': 'universal_v1'
            }, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(rag_enabled, str):
            rag_enabled = rag_enabled.lower() in ('true', '1', 'yes')
        rag_enabled = bool(rag_enabled)

        old_value = bool(getattr(project, 'rag_enabled', True))
        disabled_agents = 0
        affected_workflows = 0

        if old_value and not rag_enabled:
            # Cascade — flip doc_aware=False on every AssistantAgent across
            # every workflow in this project before flipping the toggle.
            disabled_agents, affected_workflows = _cascade_disable_doc_aware(project)
            logger.info(
                "🛑 UNIVERSAL: RAG disabled for project %s — disabled doc_aware on "
                "%d agents across %d workflows",
                project.name, disabled_agents, affected_workflows,
            )

        project.rag_enabled = rag_enabled
        project.save(update_fields=['rag_enabled', 'updated_at'])

        logger.info(
            "🔁 UNIVERSAL: RAG toggle %s -> %s for project %s (%s)",
            old_value, rag_enabled, project.name, project_id,
        )

        if rag_enabled:
            msg = 'RAG enabled. Existing summary-only documents will be indexed on the next Start Processing.'
        else:
            msg = 'RAG disabled. Start Processing will generate summaries only.'
            if disabled_agents:
                msg += f' Auto-disabled DocAware on {disabled_agents} agent(s) across {affected_workflows} workflow(s).'

        return Response({
            'project_id': str(project.project_id),
            'rag_enabled': project.rag_enabled,
            'doc_aware_agents_disabled': disabled_agents,
            'workflows_updated': affected_workflows,
            'message': msg,
            'api_version': 'universal_v1',
        })

    @action(detail=True, methods=['patch'], url_path='folder-structure-setting')
    def folder_structure_setting(self, request, project_id=None):
        """
        Update the preserve_original_folder_structure setting for a project.
        
        When enabled, folder organization is driven by LLM-based decisions
        (using per-document short summaries) instead of filename/path auto-classification.
        
        PATCH /api/projects/{project_id}/folder-structure-setting/
        Body: {"preserve_original_folder_structure": true/false}
        """
        project = self.get_object()
        
        # Get the new value from request data
        preserve_original = request.data.get('preserve_original_folder_structure')
        
        if preserve_original is None:
            return Response({
                "error": "preserve_original_folder_structure field is required",
                "api_version": "universal_v1"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Convert string to boolean if needed
        if isinstance(preserve_original, str):
            preserve_original = preserve_original.lower() in ('true', '1', 'yes')
        
        old_value = project.preserve_original_folder_structure

        # Update the project setting
        project.preserve_original_folder_structure = preserve_original
        project.save(update_fields=['preserve_original_folder_structure', 'updated_at'])
        
        logger.info(
            "📁 UNIVERSAL: Folder structure setting changed %s -> %s for project %s (%s)",
            old_value, preserve_original, project.name, project_id,
        )
        
        return Response({
            "project_id": str(project.project_id),
            "preserve_original_folder_structure": project.preserve_original_folder_structure,
            "message": f"Folder structure setting updated. {'LLM-based folder organization will be used' if preserve_original else 'Filename/path auto-classification will be used'}.",
            "api_version": "universal_v1"
        })
    
    def _cleanup_llm_files(self, document, project):
        """Best-effort deletion of files uploaded to LLM provider APIs."""
        for provider, field in [('openai', 'llm_file_id_openai'), ('anthropic', 'llm_file_id_anthropic'), ('google', 'llm_file_id_google')]:
            file_id = getattr(document, field, None)
            if not file_id:
                continue
            try:
                from agent_orchestration.llm_file_service import LLMFileUploadService
                service = LLMFileUploadService(project)
                from asgiref.sync import async_to_sync
                async_to_sync(service.delete_file)(provider, file_id)
                logger.info(f"🗑️ LLM FILE: Deleted {provider} file {file_id[:20]}... for {document.original_filename}")
            except Exception as e:
                logger.warning(f"⚠️ LLM FILE: Failed to delete {provider} file for {document.original_filename}: {e}")

    def _cleanup_workflow_references(self, project, deleted_filenames):
        """Remove references to deleted documents from workflow graph_json nodes."""
        from users.models import AgentWorkflow
        workflows_cleaned = 0
        for workflow in AgentWorkflow.objects.filter(project=project):
            graph = workflow.graph_json
            if not graph or not graph.get('nodes'):
                continue
            changed = False
            for node in graph['nodes']:
                data = node.get('data', {})
                # Clean file_attachment_documents
                fa_docs = data.get('file_attachment_documents')
                if isinstance(fa_docs, list):
                    filtered = [f for f in fa_docs if f not in deleted_filenames]
                    if len(filtered) != len(fa_docs):
                        data['file_attachment_documents'] = filtered
                        changed = True
                # Clean inline_file_attachments
                inline = data.get('inline_file_attachments')
                if isinstance(inline, list):
                    filtered = [a for a in inline if a.get('filename') not in deleted_filenames]
                    if len(filtered) != len(inline):
                        data['inline_file_attachments'] = filtered
                        changed = True
            if changed:
                workflow.graph_json = graph
                workflow.save(update_fields=['graph_json'])
                workflows_cleaned += 1
                logger.info(f"🗑️ WORKFLOW: Cleaned references in workflow {workflow.name} ({workflow.workflow_id})")
        return workflows_cleaned

    def _delete_single_document(self, document, project):
        """Full cleanup and deletion of a single document. Returns document name on success."""
        from users.models import DocumentVectorStatus, ProjectDocumentSummary, ProjectDocumentFolderOrganization
        doc_name = document.original_filename
        doc_uuid = str(document.document_id)

        # 1. Delete LLM provider files
        self._cleanup_llm_files(document, project)

        # 2. Clean up related DB records
        DocumentVectorStatus.objects.filter(document=document).delete()
        ProjectDocumentSummary.objects.filter(document=document).delete()
        ProjectDocumentFolderOrganization.objects.filter(document=document).delete()

        # 3. Delete Milvus vectors
        try:
            from vector_search.enhanced_hierarchical_database import EnhancedHierarchicalVectorDatabase
            vector_db = EnhancedHierarchicalVectorDatabase(str(project.project_id))
            vector_db.delete_document_and_chunks(doc_uuid)
        except Exception as vec_err:
            logger.warning(f"⚠️ UNIVERSAL: Failed to delete Milvus vectors for {doc_name}: {vec_err}")

        # 4. Delete physical file
        if document.file_path and default_storage.exists(document.file_path):
            default_storage.delete(document.file_path)

        # 5. Delete DB record
        document.delete()
        return doc_name

    @action(detail=True, methods=['delete'], url_path='bulk_delete_documents')
    def bulk_delete_documents(self, request, project_id=None):
        """Delete multiple documents from the project with full cleanup."""
        project = self.get_object()
        document_ids = request.data.get('document_ids', [])

        if not document_ids:
            return Response({'error': 'document_ids list is required'}, status=status.HTTP_400_BAD_REQUEST)

        deleted = []
        failed = []
        deleted_filenames = set()

        documents = list(project.documents.filter(document_id__in=document_ids))
        found_ids = {str(d.document_id) for d in documents}
        for doc_id in document_ids:
            if doc_id not in found_ids:
                failed.append({'document_id': doc_id, 'error': 'Document not found'})

        for document in documents:
            try:
                doc_name = self._delete_single_document(document, project)
                deleted.append({'document_id': str(document.document_id), 'filename': doc_name})
                deleted_filenames.add(doc_name)
            except Exception as e:
                failed.append({'document_id': str(document.document_id), 'error': str(e)})
                logger.error(f"❌ BULK DELETE: Failed to delete {document.original_filename}: {e}")

        # Clean workflow references for all deleted filenames at once
        workflows_cleaned = 0
        if deleted_filenames:
            workflows_cleaned = self._cleanup_workflow_references(project, deleted_filenames)
            self._mark_vector_collection_stale(project)

        logger.info(f"🗑️ BULK DELETE: {len(deleted)} deleted, {len(failed)} failed, {workflows_cleaned} workflows cleaned")

        return Response({
            'deleted': deleted,
            'failed': failed,
            'total_deleted': len(deleted),
            'total_failed': len(failed),
            'workflows_cleaned': workflows_cleaned,
            'message': f'{len(deleted)} document(s) deleted successfully',
            'api_version': 'universal_v1'
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['delete'])
    def delete_document(self, request, project_id=None):
        """Delete a specific document from the project with full cleanup."""
        project = self.get_object()
        document_id = request.data.get('document_id') or request.query_params.get('document_id')

        if not document_id:
            return Response({
                'error': 'document_id is required',
                'message': 'Please specify which document to delete'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            try:
                document = project.documents.get(document_id=document_id)
            except (ValueError, ProjectDocument.DoesNotExist):
                document = project.documents.get(id=document_id)

            document_name = self._delete_single_document(document, project)
            self._cleanup_workflow_references(project, {document_name})
            self._mark_vector_collection_stale(project)

            logger.info(f"🗑️ UNIVERSAL: Deleted document {document_name} from project {project_id}")

            return Response({
                'message': f'Document "{document_name}" deleted successfully',
                'document_id': document_id,
                'project_id': project_id,
                'api_version': 'universal_v1'
            }, status=status.HTTP_200_OK)

        except ProjectDocument.DoesNotExist:
            return Response({
                'error': 'Document not found',
                'message': f'Document with ID {document_id} does not exist in this project'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"❌ UNIVERSAL: Failed to delete document {document_id}: {e}")
            return Response({
                'error': 'Delete failed',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # ============================================================================
    # CUSTOM AGENT ORCHESTRATION ENDPOINTS (JSON Schema Based - When Available)
    # ============================================================================
    
    @action(detail=True, methods=['get', 'post'])
    def agent_workflows(self, request, project_id=None):
        """
        Universal agent workflow management with CUSTOM JSON schema validation
        """
        project = self.get_object()
        logger.info(f"🤖 UNIVERSAL: Agent workflows request for project {project.name} ({project_id})")
        
        if not SCHEMA_VALIDATOR_AVAILABLE:
            return Response({
                'error': 'Schema validator not available',
                'message': 'Please install jsonschema package: pip install jsonschema>=4.0.0',
                'workflows': [],
                'total_count': 0,
                'api_version': 'universal_v1',
                'agent_system': 'basic'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        
        if request.method == 'GET':
            workflows = project.agent_workflows.all().order_by('-updated_at')
            serializer = AgentWorkflowSerializer(workflows, many=True)
            
            response_data = {
                'workflows': serializer.data,
                'total_count': workflows.count(),
                'project_id': project_id,
                'project_name': project.name,
                'template_type': project.template_type,
                'agent_capabilities': {
                    'schema_version': '1.0.0',
                    'system_type': 'custom_aicc_schema',
                    'supports_json_validation': True,
                    'supports_flow_analysis': True,
                    'autogen_removed': True
                },
                'api_version': 'universal_v1',
                'agent_system': 'custom_aicc_schema',
                'retrieved_at': timezone.now().isoformat()
            }
            
            return Response(response_data, status=status.HTTP_200_OK)
        
        elif request.method == 'POST':
            workflow_data = request.data.copy()
            
            # Validate workflow using our custom JSON schema
            if 'graph_json' in workflow_data and self.workflow_validator:
                is_valid, validation_errors, analysis = self.workflow_validator.validate_workflow(
                    workflow_data['graph_json']
                )
                
                if not is_valid:
                    return Response({
                        'error': 'Workflow validation failed',
                        'validation_errors': validation_errors,
                        'schema_analysis': analysis,
                        'api_version': 'universal_v1',
                        'agent_system': 'custom_aicc_schema'
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            workflow_data['project'] = project.id
            workflow_data['created_by'] = request.user.id
            
            serializer = AgentWorkflowSerializer(data=workflow_data)
            if serializer.is_valid():
                workflow = serializer.save(project=project, created_by=request.user)
                
                response_data = {
                    **serializer.data,
                    'message': 'Workflow created successfully with custom JSON schema validation',
                    'project_id': project_id,
                    'api_version': 'universal_v1',
                    'agent_system': 'custom_aicc_schema',
                    'created_at': timezone.now().isoformat()
                }
                
                return Response(response_data, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'error': 'Invalid workflow data',
                    'validation_errors': serializer.errors,
                    'api_version': 'universal_v1'
                }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get', 'put', 'delete'])
    def agent_workflow(self, request, project_id=None):
        """Single agent workflow management"""
        project = self.get_object()
        workflow_id = request.query_params.get('workflow_id')
        
        if not workflow_id:
            return Response({
                'error': 'workflow_id parameter required',
                'api_version': 'universal_v1'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            workflow = project.agent_workflows.get(workflow_id=workflow_id)
        except AgentWorkflow.DoesNotExist:
            return Response({
                'error': 'Workflow not found',
                'api_version': 'universal_v1'
            }, status=status.HTTP_404_NOT_FOUND)
        
        if request.method == 'GET':
            serializer = AgentWorkflowSerializer(workflow)
            return Response({
                **serializer.data,
                'project_id': project_id,
                'api_version': 'universal_v1',
                'agent_system': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic'
            })
        
        elif request.method == 'PUT':
            serializer = AgentWorkflowSerializer(workflow, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response({
                    **serializer.data,
                    'message': 'Workflow updated successfully',
                    'api_version': 'universal_v1'
                })
            else:
                return Response({
                    'error': 'Invalid update data',
                    'validation_errors': serializer.errors,
                    'api_version': 'universal_v1'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        elif request.method == 'DELETE':
            workflow_name = workflow.name
            workflow.delete()
            return Response({
                'message': f'Workflow "{workflow_name}" deleted successfully',
                'api_version': 'universal_v1'
            }, status=status.HTTP_204_NO_CONTENT)
    
    def destroy(self, request, *args, **kwargs):
        """
        Delete a project - ADMIN ONLY with password confirmation
        DELETE /api/projects/{project_id}/
        """
        # SECURITY: Only admin users can delete projects
        if not hasattr(request.user, 'is_admin') or not request.user.is_admin:
            logger.warning(f"🚫 UNIVERSAL: Non-admin user {request.user.email} attempted to delete project")
            return Response({
                'error': 'Permission denied',
                'detail': 'Only administrators can delete projects',
                'user_role': getattr(request.user, 'role', 'unknown'),
                'required_role': 'ADMIN',
                'api_version': 'universal_v1'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # SECURITY: Require password confirmation for project deletion
        password = request.data.get('password')
        if not password:
            return Response({
                'error': 'Password confirmation required',
                'detail': 'Project deletion requires password confirmation for security',
                'required_field': 'password',
                'api_version': 'universal_v1'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # SECURITY: Verify admin password
        if not request.user.check_password(password):
            logger.warning(f"🚫 UNIVERSAL: Invalid password for project deletion by {request.user.email}")
            return Response({
                'error': 'Authentication failed',
                'detail': 'Invalid password provided',
                'api_version': 'universal_v1'
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        # Get the project
        project = self.get_object()
        project_name = project.name
        project_id = project.project_id
        documents_count = project.documents.count()
        
        logger.warning(f"🗑️ UNIVERSAL: Admin {request.user.email} deleting project {project_name} ({project_id}) with {documents_count} documents")
        
        # Store project info before deletion (in case we need it for manual deletion)
        project_id_to_delete = project.project_id
        project_name_to_delete = project.name
        
        try:
            with transaction.atomic():
                # Delete all associated files from storage
                deleted_files = 0
                for document in project.documents.all():
                    if document.file_path and default_storage.exists(document.file_path):
                        try:
                            default_storage.delete(document.file_path)
                            deleted_files += 1
                        except Exception as e:
                            logger.error(f"❌ UNIVERSAL: Failed to delete file {document.file_path}: {e}")
                
                # Delete vector collections if they exist
                try:
                    if hasattr(project, 'vector_collection'):
                        project.vector_collection.delete()
                        logger.info(f"🗑️ UNIVERSAL: Deleted vector collection for project {project_name}")
                except Exception as e:
                    logger.error(f"❌ UNIVERSAL: Failed to delete vector collection: {e}")
                
                # Delete deployment records through the ORM, so Django's collector
                # walks the foreign keys itself.
                #
                # This used to be hand-written SQL that deleted each child table in
                # a fixed order. That list fell behind the models: it never deleted
                # DeploymentSessionFile (chat uploads) or DeploymentPublicLoginAttempt,
                # both of which reference rows it did delete. Deleting any project
                # whose chatbot had a file upload or a public login attempt therefore
                # failed with a foreign key violation and surfaced in the UI as
                # "Failed to delete the project".
                #
                # Every relation under WorkflowDeployment is on_delete=CASCADE, and
                # DeploymentExecution.workflow_execution is SET_NULL, so one ORM
                # delete removes the whole subtree and stays correct as models change.
                try:
                    from agent_orchestration.models import WorkflowDeployment

                    deployments = WorkflowDeployment.objects.filter(project=project)
                    deployments_count = deployments.count()
                    if deployments_count > 0:
                        _, per_model = deployments.delete()
                        logger.info(
                            f"🗑️ UNIVERSAL: Deleted {deployments_count} workflow deployments "
                            f"and their dependants: {per_model}"
                        )
                except Exception as deployment_error:
                    logger.error(
                        f"❌ UNIVERSAL: Error deleting deployment records: {deployment_error}"
                    )
                    raise
                
                # Delete all agent workflows (this will cascade delete evaluations, execution history, etc.)
                workflows_count = project.agent_workflows.count()
                if workflows_count > 0:
                    project.agent_workflows.all().delete()
                    logger.info(f"🗑️ UNIVERSAL: Deleted {workflows_count} agent workflows")
                
                # Delete all project API keys explicitly (CASCADE should handle it, but explicit is safer)
                api_keys_count = project.api_keys.count()
                if api_keys_count > 0:
                    project.api_keys.all().delete()
                    logger.info(f"🗑️ UNIVERSAL: Deleted {api_keys_count} API keys")
                
                # Delete MCP server credentials if they exist (table may not exist if migrations haven't been run)
                # Check if table exists first using raw SQL to avoid transaction abort
                mcp_table_exists = False
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            SELECT EXISTS (
                                SELECT FROM information_schema.tables 
                                WHERE table_schema = 'public' 
                                AND table_name = 'users_mcpservercredential'
                            );
                        """)
                        mcp_table_exists = cursor.fetchone()[0]
                except Exception as check_error:
                    logger.warning(f"⚠️ UNIVERSAL: Could not check MCP credentials table existence: {check_error}")
                
                if mcp_table_exists:
                    try:
                        mcp_credentials_count = project.mcp_server_credentials.count()
                        if mcp_credentials_count > 0:
                            project.mcp_server_credentials.all().delete()
                            logger.info(f"🗑️ UNIVERSAL: Deleted {mcp_credentials_count} MCP server credentials")
                    except Exception as mcp_error:
                        logger.warning(f"⚠️ UNIVERSAL: Error deleting MCP credentials (table exists but query failed): {mcp_error}")
                else:
                    logger.info(f"ℹ️ UNIVERSAL: MCP server credentials table does not exist, skipping deletion (migrations may not have been run)")
                
                # Delete the project itself (will cascade delete documents, permissions, etc.)
                # Try to delete normally first
                try:
                    project.delete()
                except Exception as delete_error:
                    # Check if error is due to missing MCP credentials table
                    error_str = str(delete_error).lower()
                    if 'mcpservercredential' in error_str and ('does not exist' in error_str or 'relation' in error_str):
                        # Re-raise to exit the atomic block, then handle manually in outer exception handler
                        raise
                    else:
                        # Re-raise if it's a different error
                        logger.error(f"❌ UNIVERSAL: Unexpected error during project deletion: {delete_error}")
                        raise
                
                logger.info(f"✅ UNIVERSAL: Successfully deleted project {project_name} ({project_id})")
                
                return Response({
                    'message': f'Project "{project_name}" deleted successfully',
                    'project_id': str(project_id),
                    'project_name': project_name,
                    'deleted_documents': documents_count,
                    'deleted_files': deleted_files,
                    'deleted_workflows': workflows_count,
                    'deleted_by': request.user.email,
                    'deleted_at': timezone.now().isoformat(),
                    'api_version': 'universal_v1'
                }, status=status.HTTP_200_OK)
                
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            error_str = str(e).lower()
            
            # Check if error is due to missing MCP credentials table
            if 'mcpservercredential' in error_str and ('does not exist' in error_str or 'relation' in error_str):
                logger.warning(f"⚠️ UNIVERSAL: MCP server credentials table not found during cascade delete, attempting manual deletion")
                # The atomic block will rollback automatically, now do manual deletion in a new transaction
                try:
                    # Store project info before attempting manual deletion
                    project_id_to_delete = project_id
                    project_name_to_delete = project_name
                    
                    # Re-fetch the project in the new transaction (since the old one rolled back)
                    try:
                        project_to_delete = IntelliDocProject.objects.get(project_id=project_id_to_delete)
                    except IntelliDocProject.DoesNotExist:
                        logger.info(f"ℹ️ UNIVERSAL: Project {project_name_to_delete} ({project_id_to_delete}) already deleted")
                        return Response({
                            'message': f'Project "{project_name_to_delete}" was already deleted',
                            'project_id': str(project_id_to_delete),
                            'project_name': project_name_to_delete,
                            'api_version': 'universal_v1'
                        }, status=status.HTTP_200_OK)
                    
                    with transaction.atomic():
                        # Delete all related objects first (workflows, API keys, etc.)
                        # IMPORTANT: Delete workflow dependencies in correct order to avoid foreign key violations
                        
                        with connection.cursor() as cursor:
                            # STEP 0: Delete agent_orchestration deployment-related records FIRST
                            # These must be deleted before WorkflowExecution and AgentWorkflow
                            
                            # 0.1. Delete DeploymentExecution records (references WorkflowExecution and DeploymentSession)
                            cursor.execute("""
                                DELETE FROM agent_orchestration_deploymentexecution 
                                WHERE workflow_execution_id IN (
                                    SELECT id FROM users_workflowexecution 
                                    WHERE workflow_id IN (
                                        SELECT id FROM users_agentworkflow 
                                        WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                    )
                                )
                            """, [project_id_to_delete])
                            deployment_executions_count = cursor.rowcount
                            if deployment_executions_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {deployment_executions_count} deployment executions (raw SQL)")
                            
                            # 0.2. Delete DeploymentSession records (references WorkflowDeployment)
                            cursor.execute("""
                                DELETE FROM agent_orchestration_deploymentsession 
                                WHERE deployment_id IN (
                                    SELECT id FROM agent_orchestration_workflowdeployment 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            deployment_sessions_count = cursor.rowcount
                            if deployment_sessions_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {deployment_sessions_count} deployment sessions (raw SQL)")
                            
                            # 0.3. Delete WorkflowDeploymentRequest records (references WorkflowDeployment)
                            cursor.execute("""
                                DELETE FROM agent_orchestration_workflowdeploymentrequest 
                                WHERE deployment_id IN (
                                    SELECT id FROM agent_orchestration_workflowdeployment 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            deployment_requests_count = cursor.rowcount
                            if deployment_requests_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {deployment_requests_count} deployment requests (raw SQL)")
                            
                            # 0.4. Delete WorkflowAllowedOrigin records (references WorkflowDeployment)
                            cursor.execute("""
                                DELETE FROM agent_orchestration_workflowallowedorigin 
                                WHERE deployment_id IN (
                                    SELECT id FROM agent_orchestration_workflowdeployment 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            allowed_origins_count = cursor.rowcount
                            if allowed_origins_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {allowed_origins_count} allowed origins (raw SQL)")
                            
                            # 0.5. Delete WorkflowDeployment records (references AgentWorkflow)
                            cursor.execute("""
                                DELETE FROM agent_orchestration_workflowdeployment 
                                WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                            """, [project_id_to_delete])
                            workflow_deployments_count = cursor.rowcount
                            if workflow_deployments_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {workflow_deployments_count} workflow deployments (raw SQL)")
                            
                            # 1. Delete WorkflowExecutionMessage records (depends on WorkflowExecution)
                            cursor.execute("""
                                DELETE FROM users_workflowexecutionmessage 
                                WHERE execution_id IN (
                                    SELECT id FROM users_workflowexecution 
                                    WHERE workflow_id IN (
                                        SELECT id FROM users_agentworkflow 
                                        WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                    )
                                )
                            """, [project_id_to_delete])
                            messages_count = cursor.rowcount
                            if messages_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {messages_count} workflow execution messages (raw SQL)")
                            
                            # 2. Delete HumanInputInteraction records (depends on WorkflowExecution)
                            cursor.execute("""
                                DELETE FROM users_humaninputinteraction 
                                WHERE execution_id IN (
                                    SELECT id FROM users_workflowexecution 
                                    WHERE workflow_id IN (
                                        SELECT id FROM users_agentworkflow 
                                        WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                    )
                                )
                            """, [project_id_to_delete])
                            human_inputs_count = cursor.rowcount
                            if human_inputs_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {human_inputs_count} human input interactions (raw SQL)")
                            
                            # 3. Delete WorkflowExecution records (depends on AgentWorkflow)
                            cursor.execute("""
                                DELETE FROM users_workflowexecution 
                                WHERE workflow_id IN (
                                    SELECT id FROM users_agentworkflow 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            executions_count = cursor.rowcount
                            if executions_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {executions_count} workflow executions (raw SQL)")
                            
                            # 4. Delete WorkflowEvaluationResult records (depends on WorkflowEvaluation)
                            cursor.execute("""
                                DELETE FROM users_workflowevaluationresult 
                                WHERE evaluation_id IN (
                                    SELECT id FROM users_workflowevaluation 
                                    WHERE workflow_id IN (
                                        SELECT id FROM users_agentworkflow 
                                        WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                    )
                                )
                            """, [project_id_to_delete])
                            eval_results_count = cursor.rowcount
                            if eval_results_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {eval_results_count} workflow evaluation results (raw SQL)")
                            
                            # 5. Delete WorkflowEvaluation records (depends on AgentWorkflow)
                            cursor.execute("""
                                DELETE FROM users_workflowevaluation 
                                WHERE workflow_id IN (
                                    SELECT id FROM users_agentworkflow 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            evaluations_count = cursor.rowcount
                            if evaluations_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {evaluations_count} workflow evaluations (raw SQL)")
                            
                            # 6. Delete SimulationRun records (depends on AgentWorkflow)
                            cursor.execute("""
                                DELETE FROM users_simulationrun 
                                WHERE workflow_id IN (
                                    SELECT id FROM users_agentworkflow 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                )
                            """, [project_id_to_delete])
                            simulation_runs_count = cursor.rowcount
                            if simulation_runs_count > 0:
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {simulation_runs_count} simulation runs (raw SQL)")
                            
                            # 7. Finally, delete workflows themselves
                            cursor.execute("""
                                SELECT COUNT(*) FROM users_agentworkflow 
                                WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                            """, [project_id_to_delete])
                            workflows_count = cursor.fetchone()[0]
                            
                            if workflows_count > 0:
                                cursor.execute("""
                                    DELETE FROM users_agentworkflow 
                                    WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                                """, [project_id_to_delete])
                                logger.info(f"🗑️ UNIVERSAL: Manually deleted {workflows_count} agent workflows (raw SQL)")
                        
                        # Delete user permissions
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                DELETE FROM users_userprojectpermission 
                                WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                            """, [project_id_to_delete])
                            logger.info(f"🗑️ UNIVERSAL: Deleted user project permissions")
                        
                        # Delete group permissions
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                DELETE FROM users_groupprojectpermission 
                                WHERE project_id = (SELECT id FROM users_intellidocproject WHERE project_id = %s)
                            """, [project_id_to_delete])
                            logger.info(f"🗑️ UNIVERSAL: Deleted group project permissions")
                        
                        # Delete API keys
                        api_keys_count = project_to_delete.api_keys.count()
                        if api_keys_count > 0:
                            project_to_delete.api_keys.all().delete()
                            logger.info(f"🗑️ UNIVERSAL: Manually deleted {api_keys_count} API keys")
                        
                        # Delete documents
                        documents_count = project_to_delete.documents.count()
                        if documents_count > 0:
                            project_to_delete.documents.all().delete()
                            logger.info(f"🗑️ UNIVERSAL: Manually deleted {documents_count} documents")
                        
                        # Now delete the project itself using raw SQL to bypass ORM cascade
                        with connection.cursor() as cursor:
                            cursor.execute("DELETE FROM users_intellidocproject WHERE project_id = %s", [project_id_to_delete])
                        logger.info(f"🗑️ UNIVERSAL: Manually deleted project {project_name_to_delete} ({project_id_to_delete})")
                    
                    return Response({
                        'message': f'Project "{project_name_to_delete}" deleted successfully (manual deletion due to missing MCP table)',
                        'project_id': str(project_id_to_delete),
                        'project_name': project_name_to_delete,
                        'deleted_documents': documents_count,
                        'deleted_files': 0,  # Files were deleted in the previous transaction
                        'deleted_workflows': workflows_count if 'workflows_count' in locals() else 0,
                        'deleted_by': request.user.email,
                        'deleted_at': timezone.now().isoformat(),
                        'api_version': 'universal_v1',
                        'note': 'Manual deletion due to missing MCP server credentials table'
                    }, status=status.HTTP_200_OK)
                except Exception as manual_error:
                    logger.error(f"❌ UNIVERSAL: Failed to manually delete project {project_name}: {manual_error}")
                    import traceback
                    logger.error(f"❌ UNIVERSAL: Manual deletion error traceback:\n{traceback.format_exc()}")
                    return Response({
                        'error': 'Deletion failed',
                        'detail': f'Failed to delete project: {str(manual_error)}',
                        'project_id': str(project_id),
                        'project_name': project_name,
                        'api_version': 'universal_v1'
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                # Re-raise if it's a different error
                logger.error(f"❌ UNIVERSAL: Failed to delete project {project_name}: {e}")
                logger.error(f"❌ UNIVERSAL: Delete error traceback:\n{error_traceback}")
                return Response({
                    'error': 'Deletion failed',
                    'detail': str(e),
                    'project_id': str(project_id),
                    'project_name': project_name,
                    'api_version': 'universal_v1'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def health(self, request):
        """Health check for universal project API"""
        return Response({
            'status': 'healthy',
            'api_version': 'universal_v1',
            'agent_system': 'custom_aicc_schema' if SCHEMA_VALIDATOR_AVAILABLE else 'basic',
            'schema_validator_available': SCHEMA_VALIDATOR_AVAILABLE,
            'timestamp': timezone.now().isoformat(),
            'message': 'Universal Project API is operational (AutoGen completely removed)',
            'features': {
                'template_independence': True,
                'universal_interface': True,
                'autogen_removed': True,
                'custom_implementation_ready': SCHEMA_VALIDATOR_AVAILABLE
            }
        })
