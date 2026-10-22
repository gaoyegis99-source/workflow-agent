"""Knowledge use cases: discovery, exact reads and immutable lifecycle changes."""

import hashlib
import unicodedata

from platform_contracts.pagination import decode_cursor, encode_cursor
from platform_contracts.service import ensure


class Knowledge:
    def __init__(self, repository, blobs):
        self.repo, self.blobs = repository, blobs

    def create_import(self, project, actor, key, request):
        source = request["source"]
        ensure(source["project_id"] == project, "SCOPE_DENIED", "Source project differs", 403)
        return self.repo.create_import(project, actor, key, request)

    def import_view(self, project, import_id):
        return self.repo.get_import(project, import_id)

    def documents(self, project, cursor=None, limit=100):
        scope = f"knowledge:documents:{project}"
        try:
            after = decode_cursor(cursor, scope, 2)
        except ValueError:
            ensure(False, "MALFORMED_REQUEST", "Invalid list cursor", 400)
        rows = self.repo.list_versions(project, after, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": rows,
            "next_cursor": (
                encode_cursor(scope, (rows[-1]["document_id"], rows[-1]["version_id"]))
                if more
                else None
            ),
        }

    def version(self, project, document, version):
        return self.repo.version_view(self.repo.version(project, document, version))

    def search(self, project, request):
        query = unicodedata.normalize("NFC", request["query"].strip())
        ensure(query, "MALFORMED_REQUEST", "Search query cannot be blank", 422)
        sources = request.get("sources")
        ensure(
            sources is None or all(source["project_id"] == project for source in sources),
            "SCOPE_DENIED",
            "Search source project differs",
            403,
        )
        count, rows = self.repo.search(project, query, request["limit"], sources=sources)
        items = []
        folded = query.casefold()
        for row in rows:
            text = row["text"]
            index = text.casefold().find(folded)
            center = index if index >= 0 else 0
            start = max(0, center - 120)
            snippet = text[start : start + 400]
            items.append(
                {
                    "document_id": row["document_id"],
                    "version_id": row["version_id"],
                    "unit_id": row["unit_id"],
                    "title": row["title"],
                    "snippet": snippet,
                    "start_char": row["start_char"],
                    "end_char": row["end_char"],
                    "score": float(row["score"]),
                    "availability": row["availability"],
                    "epistemic_status": row["epistemic_status"],
                    "review_state": row["review_state"],
                }
            )
        return {
            "status": "RESULTS" if items else "NO_RESULTS",
            "query_digest": "sha256:" + hashlib.sha256(query.encode()).hexdigest(),
            "index_version": "postgres-lexical-v1",
            "candidate_count": count,
            "items": items,
        }

    def read(self, project, document, version, unit, actor, receipt_id=None):
        version_row, row, receipt = self.repo.read(
            project, document, version, unit, actor, receipt_id
        )
        self.blobs.verify(version_row["content_digest"])
        exact = row["text"]
        ensure(
            "sha256:" + hashlib.sha256(exact.encode()).hexdigest() == row["exact_text_digest"],
            "INVALID_BINDING",
            "Evidence text digest changed",
        )
        return {
            "evidence_ref": {
                "project_id": project,
                "document_id": document,
                "version_id": version,
                "unit_id": unit,
                "content_digest": version_row["content_digest"],
                "start_char": row["start_char"],
                "end_char": row["end_char"],
                "exact_text_digest": row["exact_text_digest"],
                "normalization": "utf8-nfc-lf-v1",
            },
            "text": exact,
            "prefix": row["prefix"],
            "suffix": row["suffix"],
            "lifecycle_revision": version_row["lifecycle_revision"],
            "availability": version_row["availability"],
            "epistemic_status": version_row["epistemic_status"],
            "review_state": version_row["review_state"],
            "read_receipt_id": receipt,
        }

    def withdraw(self, project, document, version, actor, key, request):
        return self.repo.withdraw(project, document, version, actor, key, request)
