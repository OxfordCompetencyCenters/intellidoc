# users/signals.py

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import DocumentVectorStatus, ProjectVectorCollection, VectorProcessingStatus, ProjectDocument
import logging
from django.utils import timezone
from django.db.models import Count

logger = logging.getLogger(__name__)

@receiver(post_save, sender=DocumentVectorStatus)
def update_collection_status_on_document_change(sender, instance, created, **kwargs):
    """
    Signal handler to automatically update collection status when document statuses change
    This ensures the overall collection status always reflects the actual state of documents
    """
    try:
        # Get the collection from the document status
        collection = instance.collection
        
        # Log that the signal was triggered
        logger.info(f"Signal triggered: Document status changed for {instance.document.original_filename} to {instance.status}")
        
        # Count document statuses
        doc_statuses = DocumentVectorStatus.objects.filter(collection=collection)
        total_docs = doc_statuses.count()
        completed_docs = doc_statuses.filter(status=VectorProcessingStatus.COMPLETED).count()
        failed_docs = doc_statuses.filter(status=VectorProcessingStatus.FAILED).count()
        processing_docs = doc_statuses.filter(status=VectorProcessingStatus.PROCESSING).count()
        pending_docs = doc_statuses.filter(status=VectorProcessingStatus.PENDING).count()
        
        # Log counts for debugging
        logger.info(f"Status counts: total={total_docs}, completed={completed_docs}, failed={failed_docs}, processing={processing_docs}, pending={pending_docs}")
        
        # Verify against project document count
        actual_doc_count = collection.project.documents.count()
        logger.info(f"Project document count: {actual_doc_count}")
        
        # Determine the correct status based on document statuses
        if total_docs == 0 or actual_doc_count == 0:
            # No documents to process
            collection_status = VectorProcessingStatus.PENDING
            logger.info(f"Setting collection status to PENDING (no documents)")
        elif completed_docs == actual_doc_count and completed_docs > 0:
            # All documents completed successfully
            collection_status = VectorProcessingStatus.COMPLETED
            logger.info(f"Setting collection status to COMPLETED (all documents processed)")
        elif failed_docs > 0 and (completed_docs + failed_docs) == actual_doc_count:
            # All documents processed, but some failed
            collection_status = VectorProcessingStatus.FAILED
            logger.info(f"Setting collection status to FAILED (some documents failed)")
        else:
            # Some documents still processing
            collection_status = VectorProcessingStatus.PROCESSING
            logger.info(f"Setting collection status to PROCESSING (processing in progress)")
        
        # Update the collection status if it's different
        if collection.status != collection_status:
            logger.info(f"Updating collection status from {collection.status} to {collection_status}")
            collection.status = collection_status
            collection.processed_documents = completed_docs
            collection.failed_documents = failed_docs
            collection.total_documents = actual_doc_count
            collection.last_processed_at = timezone.now()
            collection.save()
            
            # Verify the update was saved
            collection.refresh_from_db()
            logger.info(f"Collection status updated to {collection.status}")
        else:
            logger.info(f"Collection status already {collection_status}, no update needed")
    
    except Exception as e:
        logger.error(f"Error in signal handler: {e}")
        logger.exception(e)


@receiver(post_delete, sender=ProjectDocument)
def delete_document_file_from_storage(sender, instance, **kwargs):
    """
    Remove the uploaded file when its ProjectDocument row goes away.

    ProjectDocument.file_path is a plain CharField rather than a FileField, and
    nothing ever deleted the file behind it. Deleting a document - or a whole
    project, which cascades to its documents - removed the database rows and
    left the files on disk indefinitely. One such orphan was found during an
    audit: a PDF whose project no longer existed, still readable on disk.

    post_delete rather than an override of delete(), so this also fires for
    cascades, queryset deletes and deletions from the Django admin.

    Deliberately never raises: a failure to unlink must not roll back a
    deletion the user asked for, or break a cascade half way through. Failures
    are logged loudly enough to be found.
    """
    path = (instance.file_path or '').strip()
    if not path:
        return

    try:
        from django.core.files.storage import default_storage

        if default_storage.exists(path):
            default_storage.delete(path)
            logger.info(f"Deleted file from storage for document {instance.document_id}: {path}")
        else:
            # Already gone - previously cleaned up, or never written.
            logger.debug(f"No file to delete for document {instance.document_id}: {path}")
    except Exception as e:
        logger.error(
            f"Failed to delete file for document {instance.document_id} ({path}): {e}"
        )
