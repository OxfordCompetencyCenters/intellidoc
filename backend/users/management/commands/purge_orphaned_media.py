"""
Find - and optionally delete - uploaded files with no database row behind them.

ProjectDocument.file_path was a plain CharField with nothing deleting the file
when the row went away, so every document or project deleted before the
post_delete handler in users/signals.py left its file on disk. Those files kept
being served for as long as /media/ was public.

Reports by default and deletes nothing. Pass --delete to remove.
"""

import os

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from users.models import IntelliDocProject, ProjectDocument


class Command(BaseCommand):
    help = "Report (or delete) uploaded files that no ProjectDocument row references."

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Actually delete the orphans. Without this nothing is removed.',
        )
        parser.add_argument(
            '--prefix',
            default='projects',
            help='Media subdirectory to scan (default: projects).',
        )

    def handle(self, *args, **options):
        do_delete = options['delete']
        prefix = options['prefix'].strip('/')

        referenced = {
            (p or '').strip().lstrip('/')
            for p in ProjectDocument.objects.values_list('file_path', flat=True)
            if p
        }
        known_projects = {
            str(pid) for pid in IntelliDocProject.objects.values_list('project_id', flat=True)
        }

        self.stdout.write(f"Referenced by a ProjectDocument row: {len(referenced)}")
        self.stdout.write(f"Projects in the database:            {len(known_projects)}")

        on_disk = self._walk(prefix)
        self.stdout.write(f"Files on disk under {prefix}/:          {len(on_disk)}")

        orphans = []
        for path in sorted(on_disk):
            if path.lstrip('/') in referenced:
                continue
            parts = path.split('/')
            project_uuid = parts[1] if len(parts) > 2 and parts[0] == prefix else None
            reason = (
                'project not in database'
                if project_uuid and project_uuid not in known_projects
                else 'no ProjectDocument row references this file'
            )
            orphans.append((path, reason))

        if not orphans:
            self.stdout.write(self.style.SUCCESS('\nNo orphaned files.'))
            return

        total_bytes = 0
        self.stdout.write(self.style.WARNING(f"\n{len(orphans)} orphaned file(s):\n"))
        for path, reason in orphans:
            try:
                size = default_storage.size(path)
            except Exception:
                size = 0
            total_bytes += size
            self.stdout.write(f"  {size:>12,} B  {path}")
            self.stdout.write(f"                 reason: {reason}")

        self.stdout.write(f"\nTotal: {total_bytes:,} bytes")

        if not do_delete:
            self.stdout.write(self.style.NOTICE(
                '\nNothing was deleted. Re-run with --delete to remove these files.'
            ))
            return

        deleted = failed = 0
        for path, _ in orphans:
            try:
                default_storage.delete(path)
                deleted += 1
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  could not delete {path}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"\nDeleted {deleted} file(s)."))
        if failed:
            self.stdout.write(self.style.ERROR(f"{failed} could not be deleted."))

        self._prune_empty_dirs(prefix)

    def _walk(self, prefix):
        """Every file under `prefix`, as storage-relative paths."""
        found = []
        stack = [prefix]
        while stack:
            current = stack.pop()
            try:
                dirs, files = default_storage.listdir(current)
            except Exception:
                continue
            for d in dirs:
                stack.append(f"{current}/{d}")
            for f in files:
                found.append(f"{current}/{f}")
        return found

    def _prune_empty_dirs(self, prefix):
        """Remove directories left empty, so deleted projects do not linger."""
        try:
            root = default_storage.path(prefix)
        except (NotImplementedError, AttributeError):
            return  # Non-filesystem storage: nothing to prune.

        removed = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root or dirnames or filenames:
                continue
            try:
                os.rmdir(dirpath)
                removed += 1
            except OSError:
                pass
        if removed:
            self.stdout.write(f"Removed {removed} empty directory/directories.")
