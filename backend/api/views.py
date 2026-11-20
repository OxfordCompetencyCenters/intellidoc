# backend/api/views.py

import logging
import os
import mimetypes
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.http import Http404
from rest_framework import status, permissions, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.parsers import MultiPartParser, FormParser

from users.models import (
    DashboardIcon, UserIconPermission, GroupIconPermission, IntelliDocProject, ProjectDocument,
    UserProjectPermission, GroupProjectPermission
)
from .permissions import IsAdminUser
from .serializers import (
    UserSerializer, 
    UserCreateSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    PasswordChangeSerializer,
    AdminUserUpdateSerializer,
    DashboardIconSerializer,
    IconChoicesSerializer,
    UserIconPermissionSerializer,
    IconPermissionBulkUpdateSerializer,
    GroupSerializer,
    GroupIconPermissionSerializer,
    GroupIconPermissionBulkUpdateSerializer,
    IntelliDocProjectSerializer,
    IntelliDocProjectCreateSerializer,
    ProjectDocumentSerializer,
    UserProjectPermissionSerializer,
    GroupProjectPermissionSerializer,
    ProjectPermissionBulkUpdateSerializer
)

User = get_user_model()
logger = logging.getLogger(__name__)

class RegisterView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = UserCreateSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            refresh = RefreshToken.for_user(user)
            
            return Response({
                "user": UserSerializer(user).data,
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def get_serializer_class(self):
        if self.action == 'update' or self.action == 'partial_update':
            return AdminUserUpdateSerializer
        return UserSerializer
        
    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def me(self, request):
        """
        Get the current authenticated user's information
        """
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_request(request):
    """Start a password reset.

    SECURITY: this endpoint is unauthenticated, so the reset material must never
    appear in the response — returning `uid`/`token` here previously let anyone
    who knew an email address take over that account. The token is delivered out
    of band (email) and the response is always the same generic message, so the
    endpoint also cannot be used to enumerate registered addresses.
    """
    serializer = PasswordResetRequestSerializer(data=request.data)
    generic_response = Response(
        {"detail": "If an account exists for that address, a password reset link has been sent."}
    )

    if not serializer.is_valid():
        # Still generic: an invalid-email 400 would distinguish inputs.
        return generic_response

    email = serializer.validated_data['email']
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return generic_response

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_path = f"/reset-password/{uid}/{token}/"
    frontend_base = (getattr(settings, 'PASSWORD_RESET_BASE_URL', '') or '').rstrip('/')
    reset_url = f"{frontend_base}{reset_path}" if frontend_base else reset_path

    subject = "Reset your IntelliDoc password"
    body = (
        f"A password reset was requested for {email}.\n\n"
        f"Use this link to choose a new password:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this message and your "
        "password will remain unchanged.\n"
    )
    try:
        send_mail(
            subject,
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@localhost',
            [email],
            fail_silently=False,
        )
        logger.info(f"🔑 PASSWORD RESET: reset link dispatched for user id={user.pk}")
    except Exception as exc:
        # Never surface the token to the caller, even when delivery fails. With
        # the default console email backend this simply prints to the server log,
        # which is what local development relies on.
        logger.error(
            f"❌ PASSWORD RESET: could not send reset email for user id={user.pk}: {exc}"
        )

    return generic_response

@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_confirm(request):
    serializer = PasswordResetConfirmSerializer(data=request.data)
    if serializer.is_valid():
        try:
            uid = force_str(urlsafe_base64_decode(serializer.validated_data['uid']))
            user = User.objects.get(pk=uid)
            
            # Check if the token is valid
            if default_token_generator.check_token(user, serializer.validated_data['token']):
                user.set_password(serializer.validated_data['new_password'])
                user.save()
                return Response({"detail": "Password has been reset successfully."})
            else:
                return Response({"detail": "Invalid reset link."}, status=status.HTTP_400_BAD_REQUEST)
        except (User.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Invalid reset link."}, status=status.HTTP_400_BAD_REQUEST)
            
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    """
    Change password for authenticated users
    POST /api/change-password/
    """
    serializer = PasswordChangeSerializer(data=request.data, context={'request': request})
    
    if serializer.is_valid():
        try:
            # Save the new password
            serializer.save()
            
            return Response({
                "detail": "Password changed successfully.",
                "message": "Your password has been updated successfully."
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response({
                "error": "Password change failed",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Return validation errors
    return Response({
        "error": "Invalid data provided",
        "errors": serializer.errors
    }, status=status.HTTP_400_BAD_REQUEST)

# Dashboard Icon Views
class DashboardIconViewSet(viewsets.ModelViewSet):
    queryset = DashboardIcon.objects.all()
    serializer_class = DashboardIconSerializer
    # Default permissions for regular CRUD operations
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        # This method will be used for standard actions (list, retrieve, create, etc.)
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [permissions.IsAuthenticated(), IsAdminUser()]
        return [permissions.IsAuthenticated()]
    
    def get_queryset(self):
        # Regular users only see icons they have access to
        # Admins see all icons
        user = self.request.user
        if user.is_admin:
            return DashboardIcon.objects.all()
        
        if self.action == 'my_icons':
            return DashboardIcon.objects.filter(is_active=True)
        
        # Regular users see icons from both direct permissions and group permissions
        user_icons = user.accessible_icons.filter(is_active=True)
        
        # Get groups this user belongs to
        user_groups = user.groups.all()
        
        # Get icons accessible via group permissions
        group_icons = DashboardIcon.objects.filter(
            groupiconpermission__group__in=user_groups,
            is_active=True
        )
        
        # Combine both querysets and remove duplicates
        return (user_icons | group_icons).distinct()
    
    # CRITICAL FIX: Use only IsAuthenticated here with no other permission classes
    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def my_icons(self, request):
        """
        Get icons the current user has access to
        """
        # Debug information
        print(f"User: {request.user.email}, Is admin: {getattr(request.user, 'is_admin', False)}")
        
        # For admin users, show all active icons
        # For regular users, only show icons they have explicit permissions for
        user = request.user
        if hasattr(user, 'is_admin') and user.is_admin:
            icons = DashboardIcon.objects.filter(is_active=True)
            print(f"Admin user, showing all icons: {icons.count()}")
        else:
            # Get icons user has direct access to
            user_icons = user.accessible_icons.filter(is_active=True)
            
            # Get groups this user belongs to
            user_groups = user.groups.all()
            
            # Get icons accessible via group permissions
            group_icons = DashboardIcon.objects.filter(
                groupiconpermission__group__in=user_groups,
                is_active=True
            )
            
            # Combine both querysets and remove duplicates
            icons = (user_icons | group_icons).distinct()
            print(f"Regular user, showing permitted icons: {icons.count()}")
        
        serializer = self.get_serializer(icons, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated, IsAdminUser])
    def choices(self, request):
        """
        Get available icon and color choices for creating/editing dashboard icons
        """
        # Return empty object since IconChoicesSerializer uses SerializerMethodField
        serializer = IconChoicesSerializer({})
        return Response(serializer.data)

class UserIconPermissionViewSet(viewsets.ModelViewSet):
    queryset = UserIconPermission.objects.all()
    serializer_class = UserIconPermissionSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """
        Update all icon permissions for a user in one request
        """
        serializer = IconPermissionBulkUpdateSerializer(data=request.data)
        if serializer.is_valid():
            user_id = serializer.validated_data['user_id']
            icon_ids = serializer.validated_data['icon_ids']
            
            try:
                user = User.objects.get(id=user_id)
                
                # Clear existing permissions for this user
                UserIconPermission.objects.filter(user=user).delete()
                
                # Create new permissions
                new_permissions = []
                for icon_id in icon_ids:
                    try:
                        icon = DashboardIcon.objects.get(id=icon_id)
                        new_permissions.append(UserIconPermission(
                            user=user,
                            icon=icon,
                            granted_by=request.user
                        ))
                    except DashboardIcon.DoesNotExist:
                        pass
                
                # Bulk create new permissions
                UserIconPermission.objects.bulk_create(new_permissions)
                
                return Response({
                    "detail": f"Updated icon permissions for {user.email}",
                    "count": len(new_permissions)
                })
                
            except User.DoesNotExist:
                return Response({
                    "detail": "User not found"
                }, status=status.HTTP_404_NOT_FOUND)
                
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get'])
    def by_user(self, request):
        """
        Get all icon permissions for a specific user
        """
        user_id = request.query_params.get('user_id')
        if not user_id:
            return Response({"detail": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            user = User.objects.get(id=user_id)
            permissions = UserIconPermission.objects.filter(user=user)
            serializer = self.get_serializer(permissions, many=True)
            return Response(serializer.data)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)

class GroupViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint that allows groups to be viewed.
    Read-only since group creation/deletion should be done through the Django admin.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

class GroupIconPermissionViewSet(viewsets.ModelViewSet):
    queryset = GroupIconPermission.objects.all()
    serializer_class = GroupIconPermissionSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """
        Update all icon permissions for a group in one request
        """
        serializer = GroupIconPermissionBulkUpdateSerializer(data=request.data)
        if serializer.is_valid():
            group_id = serializer.validated_data['group_id']
            icon_ids = serializer.validated_data['icon_ids']
            
            try:
                group = Group.objects.get(id=group_id)
                
                # Clear existing permissions for this group
                GroupIconPermission.objects.filter(group=group).delete()
                
                # Create new permissions
                new_permissions = []
                for icon_id in icon_ids:
                    try:
                        icon = DashboardIcon.objects.get(id=icon_id)
                        new_permissions.append(GroupIconPermission(
                            group=group,
                            icon=icon,
                            granted_by=request.user
                        ))
                    except DashboardIcon.DoesNotExist:
                        pass
                
                # Bulk create new permissions
                GroupIconPermission.objects.bulk_create(new_permissions)
                
                return Response({
                    "detail": f"Updated icon permissions for group '{group.name}'",
                    "count": len(new_permissions)
                })
                
            except Group.DoesNotExist:
                return Response({
                    "detail": "Group not found"
                }, status=status.HTTP_404_NOT_FOUND)
                
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get'])
    def by_group(self, request):
        """
        Get all icon permissions for a specific group
        """
        group_id = request.query_params.get('group_id')
        if not group_id:
            return Response({"detail": "group_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            group = Group.objects.get(id=group_id)
            permissions = GroupIconPermission.objects.filter(group=group)
            serializer = self.get_serializer(permissions, many=True)
            return Response(serializer.data)
        except Group.DoesNotExist:
            return Response({"detail": "Group not found"}, status=status.HTTP_404_NOT_FOUND)


class IntelliDocProjectViewSet(viewsets.ModelViewSet):
    """ViewSet for managing IntelliDoc projects"""
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Return projects accessible to current user based on permissions"""
        from django.db.models import Q
        user = self.request.user
        
        # Superusers and admin users can see all projects
        if user.is_superuser or user.is_admin:
            return IntelliDocProject.objects.all()
        
        # Regular users see projects they created + projects they have permission to access
        return IntelliDocProject.objects.filter(
            Q(created_by=user) | 
            Q(user_permissions__user=user) | 
            Q(group_permissions__group__in=user.groups.all())
        ).distinct()
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'create':
            return IntelliDocProjectCreateSerializer
        return IntelliDocProjectSerializer
    
    def perform_create(self, serializer):
        """Set the created_by field when creating a project"""
        serializer.save(created_by=self.request.user)
    
    def list(self, request, *args, **kwargs):
        """List all projects for the current user"""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    def create(self, request, *args, **kwargs):
        """Create a new IntelliDoc project"""
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            project = serializer.save(created_by=request.user)
            response_serializer = IntelliDocProjectSerializer(project)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def documents(self, request, pk=None):
        """Get all documents for a specific project"""
        project = self.get_object()
        documents = project.documents.all()
        serializer = ProjectDocumentSerializer(documents, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def upload_document(self, request, pk=None):
        """Upload a document to a specific project"""
        project = self.get_object()
        
        if 'file' not in request.FILES:
            return Response({
                "detail": "No file provided"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        uploaded_file = request.FILES['file']
        
        # Validate file size (10MB limit)
        max_size = 10 * 1024 * 1024  # 10MB
        if uploaded_file.size > max_size:
            return Response({
                "detail": "File size too large. Maximum size is 10MB."
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate file type
        allowed_types = [
            'application/pdf',
            'application/msword',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'text/plain',
            'text/markdown',
            'application/rtf',
            'application/vnd.oasis.opendocument.text'
        ]
        
        file_type = mimetypes.guess_type(uploaded_file.name)[0]
        if file_type not in allowed_types:
            return Response({
                "detail": "File type not supported. Allowed types: PDF, DOC, DOCX, TXT, MD, RTF, ODT"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if file with same name exists in project
        if ProjectDocument.objects.filter(project=project, original_filename=uploaded_file.name).exists():
            return Response({
                "detail": f"File '{uploaded_file.name}' already exists in this project"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Create document record
            document = ProjectDocument.objects.create(
                project=project,
                original_filename=uploaded_file.name,
                file_size=uploaded_file.size,
                file_type=file_type or 'application/octet-stream',
                file_extension=os.path.splitext(uploaded_file.name)[1].lower(),
                uploaded_by=request.user,
                upload_status='processing'
            )
            
            # Generate storage path
            storage_path = document.get_storage_path()
            
            # Ensure directory exists
            directory = os.path.dirname(storage_path)
            if not os.path.exists(os.path.join(settings.MEDIA_ROOT, directory)):
                os.makedirs(os.path.join(settings.MEDIA_ROOT, directory), exist_ok=True)
            
            # Save file
            file_path = default_storage.save(storage_path, uploaded_file)
            document.file_path = file_path
            document.upload_status = 'ready'
            document.save()
            
            serializer = ProjectDocumentSerializer(document)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            # Clean up if document was created but file save failed
            if 'document' in locals():
                document.delete()
            
            return Response({
                "detail": f"Upload failed: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['delete'])
    def delete_document(self, request, pk=None):
        """Delete a specific document from a project"""
        project = self.get_object()
        document_id = request.data.get('document_id')
        
        if not document_id:
            return Response({
                "detail": "document_id is required"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            document = ProjectDocument.objects.get(
                id=document_id,
                project=project
            )
            
            # Delete physical file
            if document.file_path and default_storage.exists(document.file_path):
                default_storage.delete(document.file_path)
            
            # Delete database record
            document.delete()
            
            return Response({
                "detail": "Document deleted successfully"
            }, status=status.HTTP_204_NO_CONTENT)
            
        except ProjectDocument.DoesNotExist:
            return Response({
                "detail": "Document not found"
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                "detail": f"Delete failed: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def destroy(self, request, *args, **kwargs):
        """
        Delete a project - ADMIN ONLY with password confirmation
        Enhanced security with detailed logging and cleanup
        """
        # SECURITY: Only admin users can delete projects
        if not hasattr(request.user, 'is_admin') or not request.user.is_admin:
            return Response({
                "error": "Permission denied",
                "detail": "Only administrators can delete projects",
                "user_role": getattr(request.user, 'role', 'unknown'),
                "required_role": "ADMIN"
            }, status=status.HTTP_403_FORBIDDEN)
        
        # SECURITY: Require password confirmation for project deletion
        password = request.data.get('password')
        if not password:
            return Response({
                "error": "Password confirmation required",
                "detail": "Project deletion requires password confirmation for security",
                "required_field": "password"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # SECURITY: Verify admin password
        if not request.user.check_password(password):
            return Response({
                "error": "Authentication failed", 
                "detail": "Invalid password provided"
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        # Get the project
        project = self.get_object()
        project_name = project.name
        project_id = project.project_id if hasattr(project, 'project_id') else project.id
        documents_count = project.documents.count()
        
        try:
            from django.db import transaction
            with transaction.atomic():
                # Delete all associated files from storage
                deleted_files = 0
                for document in project.documents.all():
                    if document.file_path and default_storage.exists(document.file_path):
                        try:
                            default_storage.delete(document.file_path)
                            deleted_files += 1
                        except Exception as e:
                            # Log but continue with deletion
                            pass
                
                # Drop the project's Milvus collections, then its bookkeeping row.
                #
                # Deleting project.vector_collection only removes the Postgres
                # record -- the vectors themselves used to survive in Milvus
                # forever, one orphaned collection per deleted project.
                #
                # Collections are matched by the project UUID suffix rather than
                # rebuilt from the project name: a project carries more than one
                # collection (documents plus "websearch_<uuid>"), and the
                # name-sanitising logic has drifted between call sites before, so
                # a reconstructed name can miss the collection that actually
                # exists. The UUID suffix is stable regardless of naming.
                dropped_collections = []
                try:
                    from django.conf import settings
                    from pymilvus import connections, utility

                    uuid_suffix = str(project_id).replace('-', '_')
                    alias = f'delete_{uuid_suffix[:8]}'
                    connections.connect(
                        alias=alias,
                        host=settings.MILVUS_HOST,
                        port=str(settings.MILVUS_PORT),
                        user=getattr(settings, 'MILVUS_USER', 'root'),
                        password=settings.MILVUS_PASSWORD,
                    )
                    try:
                        for name in utility.list_collections(using=alias):
                            if name.endswith(uuid_suffix):
                                utility.drop_collection(name, using=alias)
                                dropped_collections.append(name)
                    finally:
                        connections.disconnect(alias)
                except Exception as exc:
                    # Never block the delete on Milvus being unreachable, but do
                    # not hide it either -- a silent failure here is what let the
                    # orphans accumulate unnoticed.
                    logger.warning(
                        'Project %s: Milvus cleanup failed, collections may be '
                        'orphaned: %s', project_id, exc
                    )

                try:
                    if hasattr(project, 'vector_collection'):
                        project.vector_collection.delete()
                except Exception as exc:
                    logger.warning(
                        'Project %s: could not delete vector_collection row: %s',
                        project_id, exc
                    )
                
                # Delete all agent workflows if they exist
                workflows_count = 0
                if hasattr(project, 'agent_workflows'):
                    workflows_count = project.agent_workflows.count()
                    if workflows_count > 0:
                        project.agent_workflows.all().delete()
                
                # Delete the project itself (will cascade delete documents, permissions, etc.)
                project.delete()
                
                return Response({
                    "message": f'Project "{project_name}" deleted successfully',
                    "project_id": str(project_id),
                    "project_name": project_name,
                    "deleted_documents": documents_count,
                    "deleted_files": deleted_files,
                    "deleted_workflows": workflows_count,
                    "dropped_collections": dropped_collections,
                    "deleted_by": request.user.email,
                    "deleted_at": timezone.now().isoformat()
                }, status=status.HTTP_200_OK)
                
        except Exception as e:
            return Response({
                "error": "Deletion failed",
                "detail": str(e),
                "project_id": str(project_id),
                "project_name": project_name
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# Vector Search Views
from vector_search.enhanced_hierarchical_services import EnhancedHierarchicalVectorSearchManager
from users.models import ProjectVectorCollection, DocumentVectorStatus, VectorProcessingStatus
from django.utils import timezone

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def process_project_documents(request, project_id):
    """Process and vectorize all documents in a project (Digest functionality)"""
    try:
        # Get the project using either integer ID or UUID
        try:
            # First try as integer ID (pk)
            project = IntelliDocProject.objects.get(id=int(project_id))
        except (ValueError, IntelliDocProject.DoesNotExist):
            # If that fails, try as UUID
            project = IntelliDocProject.objects.get(project_id=project_id)
        
        # Verify user has access using proper permission system
        if not project.has_user_access(request.user):
            return Response({
                "detail": "You do not have access to this project"
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Get or create vector collection record
        collection, created = ProjectVectorCollection.objects.get_or_create(
            project=project,
            defaults={
                'collection_name': f"project_{str(project.project_id).replace('-', '_')}",
                'status': VectorProcessingStatus.PENDING
            }
        )
        
        # Check if already processing
        if collection.status == VectorProcessingStatus.PROCESSING:
            return Response({
                "detail": "Documents are already being processed",
                "status": collection.status,
                "progress": collection.processing_progress
            }, status=status.HTTP_409_CONFLICT)
        
        # Update status to processing
        collection.status = VectorProcessingStatus.PROCESSING
        collection.error_message = ""
        collection.save()
        
        # Count total documents
        total_docs = project.documents.count()
        collection.total_documents = total_docs
        collection.save()
        
        # Process documents using UUID project_id
        try:
            # Create instance and call correct method
            chunking_manager = ChunkingVectorSearchManager(str(project.project_id))
            result = chunking_manager.process_documents()
            
            # Update collection status based on results
            collection.processed_documents = result.get('processed_documents', 0)
            collection.failed_documents = result.get('failed_documents', 0)
            collection.last_processed_at = timezone.now()
            
            if result.get('status') == 'completed':
                if collection.failed_documents == 0:
                    collection.status = VectorProcessingStatus.COMPLETED
                else:
                    collection.status = VectorProcessingStatus.FAILED
                    collection.error_message = f"{collection.failed_documents} documents failed to process"
            else:
                collection.status = VectorProcessingStatus.FAILED
                collection.error_message = result.get('error', 'Unknown error occurred')
            
            collection.save()
            
            return Response({
                "message": "Document processing completed",
                "project_id": project_id,
                "results": result,
                "collection_status": {
                    "status": collection.status,
                    "total_documents": collection.total_documents,
                    "processed_documents": collection.processed_documents,
                    "failed_documents": collection.failed_documents,
                    "progress": collection.processing_progress
                }
            })
            
        except Exception as e:
            # Update collection status on error
            collection.status = VectorProcessingStatus.FAILED
            collection.error_message = str(e)
            collection.save()
            
            return Response({
                "detail": f"Document processing failed: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
    except IntelliDocProject.DoesNotExist:
        return Response({
            "detail": "Project not found or access denied"
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({
            "detail": f"Request failed: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_project_vector_status(request, project_id):
    """Get vector processing status for a project"""
    try:
        # Get the project using either integer ID or UUID
        try:
            # First try as integer ID (pk)
            project = IntelliDocProject.objects.get(id=int(project_id))
        except (ValueError, IntelliDocProject.DoesNotExist):
            # If that fails, try as UUID
            project = IntelliDocProject.objects.get(project_id=project_id)
        
        # Verify user has access using proper permission system
        if not project.has_user_access(request.user):
            return Response({
                "detail": "You do not have access to this project"
            }, status=status.HTTP_403_FORBIDDEN)
        
        try:
            collection = project.vector_collection
            
            # Ensure collection has correct document count
            actual_doc_count = project.documents.count()
            if collection.total_documents != actual_doc_count:
                collection.total_documents = actual_doc_count
                collection.save()
            
            return Response({
                "project_id": project_id,
                "collection_name": collection.collection_name,
                "status": collection.status,
                "total_documents": collection.total_documents,
                "processed_documents": collection.processed_documents,
                "failed_documents": collection.failed_documents,
                "progress": collection.processing_progress,
                "last_processed_at": collection.last_processed_at,
                "error_message": collection.error_message
            })
        except ProjectVectorCollection.DoesNotExist:
            # Auto-create vector collection if it doesn't exist
            actual_doc_count = project.documents.count()
            collection = ProjectVectorCollection.objects.create(
                project=project,
                collection_name=f"project_{str(project.project_id).replace('-', '_')}",
                status='PENDING',
                total_documents=actual_doc_count,
                processed_documents=0,
                failed_documents=0
            )
            
            return Response({
                "project_id": project_id,
                "collection_name": collection.collection_name,
                "status": collection.status,
                "total_documents": collection.total_documents,
                "processed_documents": collection.processed_documents,
                "failed_documents": collection.failed_documents,
                "progress": collection.processing_progress,
                "last_processed_at": collection.last_processed_at,
                "error_message": collection.error_message,
                "message": "Vector collection initialized"
            })
            
    except IntelliDocProject.DoesNotExist:
        return Response({
            "detail": "Project not found or access denied"
        }, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def search_project_documents(request, project_id):
    """Search documents within a project using vector similarity"""
    try:
        # Get the project using either integer ID or UUID
        try:
            # First try as integer ID (pk)
            project = IntelliDocProject.objects.get(id=int(project_id))
        except (ValueError, IntelliDocProject.DoesNotExist):
            # If that fails, try as UUID
            project = IntelliDocProject.objects.get(project_id=project_id)
        
        # Verify user has access using proper permission system
        if not project.has_user_access(request.user):
            return Response({
                "detail": "You do not have access to this project"
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Check if project has been processed
        try:
            collection = project.vector_collection
            if collection.status != VectorProcessingStatus.COMPLETED:
                return Response({
                    "detail": "Project documents have not been processed yet. Please run 'Digest' first.",
                    "status": collection.status
                }, status=status.HTTP_400_BAD_REQUEST)
        except ProjectVectorCollection.DoesNotExist:
            return Response({
                "detail": "Project documents have not been processed yet. Please run 'Digest' first."
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get search parameters
        query = request.data.get('query', '').strip()
        limit = request.data.get('limit', 5)
        filters = request.data.get('filters', {})
        
        if not query:
            return Response({
                "detail": "Search query is required"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Perform search using UUID project_id
        chunking_manager = ChunkingVectorSearchManager(str(project.project_id))
        result = chunking_manager.search_documents(query, limit, filters)
        
        return Response(result)
        
    except IntelliDocProject.DoesNotExist:
        return Response({
            "detail": "Project not found or access denied"
        }, status=status.HTTP_404_NOT_FOUND)


# Template Discovery API Views
from templates.discovery import TemplateDiscoverySystem, TemplateValidator
from .serializers import ProjectTemplateSerializer, TemplateConfigurationSerializer, TemplateDuplicationSerializer
from django.core.management import call_command
from pathlib import Path
import json
import shutil


class ProjectTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    """API for folder-based template discovery"""
    permission_classes = [IsAuthenticated]
    serializer_class = ProjectTemplateSerializer
    
    def get_queryset(self):
        """Return available templates from filesystem"""
        templates = TemplateDiscoverySystem.list_available_templates()
        return templates
    
    def list(self, request, *args, **kwargs):
        """List all available templates"""
        templates = self.get_queryset()
        serializer = self.get_serializer(templates, many=True)
        return Response({
            'count': len(templates),
            'templates': serializer.data
        })
    
    def retrieve(self, request, pk=None, *args, **kwargs):
        """Get specific template details"""
        template_config = TemplateDiscoverySystem.get_template_configuration(pk)
        if not template_config:
            return Response({
                'detail': 'Template not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        template_config['template_id'] = pk
        serializer = TemplateConfigurationSerializer(template_config)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def discover(self, request):
        """Force discovery and sync templates"""
        try:
            templates = TemplateDiscoverySystem.discover_templates(force_refresh=True)
            
            return Response({
                'discovered_count': len(templates),
                'templates': list(templates.keys()),
                'message': 'Template discovery completed successfully'
            })
        except Exception as e:
            return Response({
                'detail': f'Template discovery failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated, IsAdminUser])
    def duplicate(self, request):
        """Duplicate an existing template (admin only)"""
        serializer = TemplateDuplicationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        source_template = serializer.validated_data['source_template']
        new_template_id = serializer.validated_data['new_template_id']
        new_name = serializer.validated_data.get('new_name')
        new_description = serializer.validated_data.get('new_description')
        new_author = serializer.validated_data.get('new_author')
        
        try:
            # Get template definitions path
            template_dir = TemplateDiscoverySystem.get_template_definitions_path()
            source_dir = template_dir / source_template
            target_dir = template_dir / new_template_id
            
            # Validate source template
            if not source_dir.exists():
                return Response({
                    'detail': f'Source template not found: {source_template}'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            validation_result = TemplateValidator.validate_template_directory(source_dir)
            if not validation_result['valid']:
                return Response({
                    'detail': f'Source template is invalid: {validation_result["errors"]}'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Check target doesn't exist
            if target_dir.exists():
                return Response({
                    'detail': f'Target template already exists: {new_template_id}'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Copy template directory
            shutil.copytree(source_dir, target_dir)
            
            # Update metadata
            metadata_file = target_dir / 'metadata.json'
            
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            
            # Update metadata fields
            metadata['template_id'] = new_template_id
            if new_name:
                metadata['name'] = new_name
            if new_description:
                metadata['description'] = new_description
            if new_author:
                metadata['author'] = new_author
            
            # Update version for duplicated template
            metadata['version'] = '1.0.0'
            metadata['created_date'] = '2025-07-16'
            
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            # Validate duplicated template
            validation_result = TemplateValidator.validate_template_directory(target_dir)
            
            if not validation_result['valid']:
                # Clean up if validation fails
                shutil.rmtree(target_dir)
                return Response({
                    'detail': f'Duplicated template is invalid: {validation_result["errors"]}'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # Force refresh template cache
            TemplateDiscoverySystem.discover_templates(force_refresh=True)
            
            return Response({
                'message': f'Template duplicated successfully: {new_template_id}',
                'template_id': new_template_id,
                'location': str(target_dir)
            })
            
        except Exception as e:
            # Clean up on error
            if target_dir.exists():
                shutil.rmtree(target_dir)
            return Response({
                'detail': f'Error duplicating template: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=True, methods=['get'])
    def configuration(self, request, pk=None):
        """Get complete template configuration"""
        template_config = TemplateDiscoverySystem.get_template_configuration(pk)
        if not template_config:
            return Response({
                'detail': 'Template not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        template_config['template_id'] = pk
        return Response({
            'template_id': pk,
            'configuration': template_config
        })


# ============================================================================
# PROJECT PERMISSION VIEWSETS
# ============================================================================

class UserProjectPermissionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing user project permissions"""
    serializer_class = UserProjectPermissionSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def get_queryset(self):
        return UserProjectPermission.objects.all()
    
    def perform_create(self, serializer):
        """Set the granted_by field when creating a permission"""
        serializer.save(granted_by=self.request.user)
    
    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """Update all project permissions for a user"""
        serializer = ProjectPermissionBulkUpdateSerializer(data=request.data)
        if serializer.is_valid():
            project_id = serializer.validated_data['project_id']
            user_ids = serializer.validated_data.get('user_ids', [])
            
            try:
                project = IntelliDocProject.objects.get(project_id=project_id)
                
                # Clear existing user permissions for this project
                UserProjectPermission.objects.filter(project=project).delete()
                
                # Create new permissions
                permissions = []
                for user_id in user_ids:
                    try:
                        user = User.objects.get(id=user_id)
                        permissions.append(UserProjectPermission(
                            user=user,
                            project=project,
                            granted_by=request.user
                        ))
                    except User.DoesNotExist:
                        continue
                
                UserProjectPermission.objects.bulk_create(permissions)
                
                return Response({
                    'detail': f'Updated permissions for {len(permissions)} users',
                    'project_name': project.name,
                    'users_granted': len(permissions)
                })
            except IntelliDocProject.DoesNotExist:
                return Response({
                    'detail': 'Project not found'
                }, status=status.HTTP_404_NOT_FOUND)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get'])
    def by_project(self, request):
        """Get all user permissions for a specific project"""
        project_id = request.query_params.get('project_id')
        if not project_id:
            return Response({
                'detail': 'project_id parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            project = IntelliDocProject.objects.get(project_id=project_id)
            permissions = UserProjectPermission.objects.filter(project=project)
            serializer = self.get_serializer(permissions, many=True)
            return Response(serializer.data)
        except IntelliDocProject.DoesNotExist:
            return Response({
                'detail': 'Project not found'
            }, status=status.HTTP_404_NOT_FOUND)

class GroupProjectPermissionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing group project permissions"""
    serializer_class = GroupProjectPermissionSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def get_queryset(self):
        return GroupProjectPermission.objects.all()
    
    def perform_create(self, serializer):
        """Set the granted_by field when creating a permission"""
        serializer.save(granted_by=self.request.user)
    
    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        """Update all project permissions for groups"""
        serializer = ProjectPermissionBulkUpdateSerializer(data=request.data)
        if serializer.is_valid():
            project_id = serializer.validated_data['project_id']
            group_ids = serializer.validated_data.get('group_ids', [])
            
            try:
                project = IntelliDocProject.objects.get(project_id=project_id)
                
                # Clear existing group permissions for this project
                GroupProjectPermission.objects.filter(project=project).delete()
                
                # Create new permissions
                permissions = []
                for group_id in group_ids:
                    try:
                        group = Group.objects.get(id=group_id)
                        permissions.append(GroupProjectPermission(
                            group=group,
                            project=project,
                            granted_by=request.user
                        ))
                    except Group.DoesNotExist:
                        continue
                
                GroupProjectPermission.objects.bulk_create(permissions)
                
                return Response({
                    'detail': f'Updated permissions for {len(permissions)} groups',
                    'project_name': project.name,
                    'groups_granted': len(permissions)
                })
            except IntelliDocProject.DoesNotExist:
                return Response({
                    'detail': 'Project not found'
                }, status=status.HTTP_404_NOT_FOUND)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# ✅ PHASE 3 COMPLETE: Legacy IntelliDocProjectViewSet REMOVED
# All project operations now use UniversalProjectViewSet from api.universal_project_views
# This provides complete template independence and unified project management
