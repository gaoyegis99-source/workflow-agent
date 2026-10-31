"""Knowledge persistence, including fenced imports and immutable evidence receipts."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa
from platform_contracts.service import digest, ensure
from sqlalchemy.dialects.postgresql import insert

from platform_knowledge import tables as t


def one(connection, query, required=True):
    row = connection.execute(query).mappings().first()
    if required:
        ensure(row is not None, "RESOURCE_NOT_FOUND", "Knowledge resource not found", 404)
    return dict(row) if row else None


class Repository:
    def __init__(self, database):
        self.db = database

    def write(self, scope, key, request, operation):
        fingerprint = digest(request)
        with self.db.engine.begin() as connection:
            connection.execute(
                insert(t.requests)
                .values(scope=scope, key=key, digest=fingerprint)
                .on_conflict_do_nothing()
            )
            where = (t.requests.c.scope == scope, t.requests.c.key == key)
            row = one(connection, sa.select(t.requests).where(*where).with_for_update())
            ensure(row["digest"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Key content changed")
            if row["result"] is not None:
                return row["result"]
            result = operation(connection)
            connection.execute(t.requests.update().where(*where).values(result=result))
            return result

    def create_import(self, project, actor, key, request):
        def operation(connection):
            import_id = uuid4().hex
            payload = request | {"actor_id": actor}
            connection.execute(
                t.imports.insert().values(
                    import_id=import_id, project_id=project, state="QUEUED", payload=payload
                )
            )
            return self.import_view(
                one(connection, sa.select(t.imports).where(t.imports.c.import_id == import_id))
            )

        return self.write(f"{actor}:{project}:knowledge-import", key, request, operation)

    @staticmethod
    def import_view(row):
        payload = row["payload"]
        return {
            "import_id": row["import_id"],
            "project_id": row["project_id"],
            "status": row["state"],
            "status_url": (
                f"/api/v1/projects/{row['project_id']}/knowledge/imports/{row['import_id']}"
            ),
            "actor_id": payload["actor_id"],
            "source": payload["source"],
            "representation_id": payload["representation_id"],
            "document_id": payload.get("document_id"),
            "version_id": payload.get("version_id"),
            "error": payload.get("error"),
        }

    def get_import(self, project, import_id):
        with self.db.engine.connect() as connection:
            return self.import_view(
                one(
                    connection,
                    sa.select(t.imports).where(
                        t.imports.c.project_id == project, t.imports.c.import_id == import_id
                    ),
                )
            )

    def claim(self, owner, lease_seconds=30):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            row = (
                connection.execute(
                    sa.select(t.imports)
                    .where(
                        t.imports.c.state.in_(["QUEUED", "IMPORTING"]),
                        sa.or_(t.imports.c.lease_until.is_(None), t.imports.c.lease_until < now),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            claim = dict(row)
            claim.update(owner=owner, generation=row["generation"] + 1, state="IMPORTING")
            connection.execute(
                t.imports.update()
                .where(t.imports.c.import_id == row["import_id"])
                .values(
                    owner=owner,
                    generation=claim["generation"],
                    state="IMPORTING",
                    lease_until=now + timedelta(seconds=lease_seconds),
                )
            )
            return claim

    def renew(self, claim, lease_seconds=30):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            return (
                connection.execute(
                    t.imports.update()
                    .where(
                        t.imports.c.import_id == claim["import_id"],
                        t.imports.c.owner == claim["owner"],
                        t.imports.c.generation == claim["generation"],
                        t.imports.c.state == "IMPORTING",
                        t.imports.c.lease_until > now,
                    )
                    .values(lease_until=now + timedelta(seconds=lease_seconds))
                ).rowcount
                == 1
            )

    def retry(self, claim):
        """Return a fenced import to the queue after a transient dependency failure."""
        with self.db.engine.begin() as connection:
            return (
                connection.execute(
                    t.imports.update()
                    .where(
                        t.imports.c.import_id == claim["import_id"],
                        t.imports.c.owner == claim["owner"],
                        t.imports.c.generation == claim["generation"],
                        t.imports.c.state == "IMPORTING",
                    )
                    .values(state="QUEUED", owner=None, lease_until=None)
                ).rowcount
                == 1
            )

    @staticmethod
    def identities(project, source):
        document_id = hashlib.sha256(f"{project}:{source['asset_id']}".encode()).hexdigest()[:32]
        version_id = source["version_id"]
        return document_id, version_id

    def import_result(self, claim, result=None, error=None):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            row = one(
                connection,
                sa.select(t.imports)
                .where(t.imports.c.import_id == claim["import_id"])
                .with_for_update(),
            )
            if (row["owner"], row["generation"]) != (claim["owner"], claim["generation"]) or row[
                "lease_until"
            ] <= now:
                return False
            payload = dict(row["payload"])
            if result:
                document_id, version_id = result["document_id"], result["version_id"]
                connection.execute(
                    insert(t.documents)
                    .values(document_id=document_id, project_id=row["project_id"], created_at=now)
                    .on_conflict_do_nothing()
                )
                existing = one(
                    connection,
                    sa.select(t.versions).where(
                        t.versions.c.document_id == document_id,
                        t.versions.c.version_id == version_id,
                    ),
                    required=False,
                )
                if existing:
                    ensure(
                        existing["content_digest"] == result["content_digest"]
                        and existing["source"] == result["source"],
                        "INVALID_BINDING",
                        "Existing knowledge version differs",
                    )
                else:
                    connection.execute(
                        t.versions.insert().values(
                            document_id=document_id,
                            version_id=version_id,
                            project_id=row["project_id"],
                            title=result["title"],
                            source=result["source"],
                            source_asset_id=result["source"]["asset_id"],
                            source_version_id=result["source"]["version_id"],
                            source_manifest_digest=result["source"]["version_manifest_digest"],
                            source_digest=result["source_digest"],
                            content_digest=result["content_digest"],
                            format=result["format"],
                            availability="ACTIVE",
                            epistemic_status="DECLARED",
                            review_state="UNREVIEWED",
                            lifecycle_revision=1,
                            unit_count=len(result["units"]),
                            created_at=now,
                        )
                    )
                    for unit in result["units"]:
                        connection.execute(
                            t.units.insert().values(
                                **unit,
                                document_id=document_id,
                                version_id=version_id,
                                project_id=row["project_id"],
                                search_vector=sa.func.to_tsvector("simple", unit["text"]),
                            )
                        )
                    # A profile indexes exact immutable units.  A newly
                    # imported document cannot be silently omitted from an
                    # existing semantic index, so discovery is fail-closed
                    # until its owner explicitly rebuilds that index.
                    connection.execute(
                        t.semantic_indexes.update()
                        .where(t.semantic_indexes.c.project_id == row["project_id"])
                        .values(status="STALE", updated_at=now)
                    )
                    self._event(
                        connection, row["project_id"], document_id, version_id, 1, "CREATED", {}
                    )
                payload.update(document_id=document_id, version_id=version_id)
                state = "READY"
            else:
                payload["error"] = error
                state = "FAILED"
            connection.execute(
                t.imports.update()
                .where(t.imports.c.import_id == row["import_id"])
                .values(state=state, payload=payload, owner=None, lease_until=None)
            )
            return True

    def _event(self, connection, project, document, version, revision, kind, payload):
        event_id = uuid4().hex
        now = connection.scalar(sa.select(sa.func.clock_timestamp()))
        event = {
            "event_id": event_id,
            "project_id": project,
            "document_id": document,
            "version_id": version,
            "revision": revision,
            "kind": kind,
        }
        connection.execute(
            t.lifecycle_events.insert().values(**event, payload=payload, created_at=now)
        )
        connection.execute(
            t.outbox.insert().values(event_id=event_id, payload=event | payload, created_at=now)
        )

    @staticmethod
    def version_view(row):
        return {
            "project_id": row["project_id"],
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "title": row["title"],
            "source": row["source"],
            "source_digest": row["source_digest"],
            "content_digest": row["content_digest"],
            "format": row["format"],
            "normalization": "utf8-nfc-lf-v1",
            "chunking": "paragraph-window-v1",
            "availability": row["availability"],
            "epistemic_status": row["epistemic_status"],
            "review_state": row["review_state"],
            "lifecycle_revision": row["lifecycle_revision"],
            "unit_count": row["unit_count"],
            "withdrawal_reason": row["withdrawal_reason"],
        }

    def version(self, project, document, version, lock=False, connection=None):
        owned = connection is None
        connection = connection or self.db.engine.connect()
        try:
            query = sa.select(t.versions).where(
                t.versions.c.project_id == project,
                t.versions.c.document_id == document,
                t.versions.c.version_id == version,
            )
            return one(connection, query.with_for_update() if lock else query)
        finally:
            if owned:
                connection.close()

    def list_versions(self, project, after=None, limit=100):
        conditions = [t.versions.c.project_id == project]
        if after is not None:
            conditions.append(sa.tuple_(t.versions.c.document_id, t.versions.c.version_id) > after)
        with self.db.engine.connect() as connection:
            rows = connection.execute(
                sa.select(t.versions)
                .where(*conditions)
                .order_by(t.versions.c.document_id, t.versions.c.version_id)
                .limit(limit)
            ).mappings()
            return [self.version_view(dict(row)) for row in rows]

    def semantic_index(self, project, index_id):
        with self.db.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(t.semantic_indexes).where(
                        t.semantic_indexes.c.project_id == project,
                        t.semantic_indexes.c.index_id == index_id,
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def begin_semantic_index(self, project, index_id, profile, profile_digest):
        """Create or re-open only the exact immutable profile requested."""

        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            existing = (
                connection.execute(
                    sa.select(t.semantic_indexes)
                    .where(t.semantic_indexes.c.index_id == index_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if existing is not None:
                ensure(
                    existing["project_id"] == project
                    and existing["profile_digest"] == profile_digest
                    and existing["profile"] == profile,
                    "INVALID_BINDING",
                    "Semantic index identity differs",
                    409,
                )
                connection.execute(
                    t.semantic_indexes.update()
                    .where(t.semantic_indexes.c.index_id == index_id)
                    .values(status="BUILDING", failure_category=None, updated_at=now)
                )
                return
            same_profile = connection.execute(
                sa.select(t.semantic_indexes.c.index_id).where(
                    t.semantic_indexes.c.project_id == project,
                    t.semantic_indexes.c.profile_digest == profile_digest,
                )
            ).scalar_one_or_none()
            ensure(
                same_profile is None,
                "INVALID_BINDING",
                "Semantic profile already belongs to another index identity",
                409,
            )
            connection.execute(
                t.semantic_indexes.insert().values(
                    index_id=index_id,
                    project_id=project,
                    profile=profile,
                    profile_digest=profile_digest,
                    status="BUILDING",
                    created_at=now,
                    updated_at=now,
                )
            )

    def fail_semantic_index(self, project, index_id, profile_digest, category):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                t.semantic_indexes.update()
                .where(
                    t.semantic_indexes.c.project_id == project,
                    t.semantic_indexes.c.index_id == index_id,
                    t.semantic_indexes.c.profile_digest == profile_digest,
                )
                .values(status="FAILED", failure_category=category[:64], updated_at=now)
            )

    def stale_semantic_index(self, project, index_id, profile_digest):
        """Retain an interrupted build as stale rather than falsely ready."""

        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                t.semantic_indexes.update()
                .where(
                    t.semantic_indexes.c.project_id == project,
                    t.semantic_indexes.c.index_id == index_id,
                    t.semantic_indexes.c.profile_digest == profile_digest,
                )
                .values(status="STALE", failure_category=None, updated_at=now)
            )

    def active_units_for_semantic_index(self, project):
        joined = t.units.join(
            t.versions,
            sa.and_(
                t.units.c.document_id == t.versions.c.document_id,
                t.units.c.version_id == t.versions.c.version_id,
            ),
        )
        with self.db.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    t.units.c.document_id,
                    t.units.c.version_id,
                    t.units.c.unit_id,
                    t.units.c.text,
                    t.versions.c.content_digest,
                    t.versions.c.lifecycle_revision,
                )
                .select_from(joined)
                .where(
                    t.units.c.project_id == project,
                    t.versions.c.project_id == project,
                    t.versions.c.availability == "ACTIVE",
                )
                .order_by(t.units.c.document_id, t.units.c.version_id, t.units.c.unit_id)
            ).mappings()
            return [dict(row) for row in rows]

    def replace_semantic_vectors(self, project, index_id, profile_digest, records):
        """Make one fully validated semantic build visible atomically."""

        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            index = one(
                connection,
                sa.select(t.semantic_indexes)
                .where(
                    t.semantic_indexes.c.project_id == project,
                    t.semantic_indexes.c.index_id == index_id,
                )
                .with_for_update(),
            )
            ensure(
                index["profile_digest"] == profile_digest,
                "REVISION_CONFLICT",
                "Semantic profile changed while indexing",
                409,
            )
            ensure(
                index["status"] == "BUILDING",
                "REVISION_CONFLICT",
                "Semantic index was invalidated while indexing",
                409,
            )
            received = {
                (
                    record["document_id"],
                    record["version_id"],
                    record["unit_id"],
                    record["content_digest"],
                    record["lifecycle_revision"],
                )
                for record in records
            }
            ensure(
                len(received) == len(records),
                "INVALID_BINDING",
                "Semantic build contains duplicate evidence units",
                422,
            )
            active = {
                (
                    row["document_id"],
                    row["version_id"],
                    row["unit_id"],
                    row["content_digest"],
                    row["lifecycle_revision"],
                )
                for row in connection.execute(
                    sa.select(
                        t.units.c.document_id,
                        t.units.c.version_id,
                        t.units.c.unit_id,
                        t.versions.c.content_digest,
                        t.versions.c.lifecycle_revision,
                    )
                    .select_from(
                        t.units.join(
                            t.versions,
                            sa.and_(
                                t.units.c.document_id == t.versions.c.document_id,
                                t.units.c.version_id == t.versions.c.version_id,
                            ),
                        )
                    )
                    .where(
                        t.units.c.project_id == project,
                        t.versions.c.project_id == project,
                        t.versions.c.availability == "ACTIVE",
                    )
                ).mappings()
            }
            ensure(
                received == active,
                "REVISION_CONFLICT",
                "Knowledge units changed while indexing",
                409,
            )
            for record in records:
                version = one(
                    connection,
                    sa.select(t.versions).where(
                        t.versions.c.project_id == project,
                        t.versions.c.document_id == record["document_id"],
                        t.versions.c.version_id == record["version_id"],
                    ),
                )
                ensure(
                    version["availability"] == "ACTIVE"
                    and version["content_digest"] == record["content_digest"]
                    and version["lifecycle_revision"] == record["lifecycle_revision"],
                    "REVISION_CONFLICT",
                    "Knowledge document changed while indexing",
                    409,
                )
            connection.execute(
                t.semantic_vectors.update()
                .where(
                    t.semantic_vectors.c.project_id == project,
                    t.semantic_vectors.c.index_id == index_id,
                    t.semantic_vectors.c.valid.is_(True),
                )
                .values(valid=False, invalidated_at=now)
            )
            for record in records:
                values = {
                    **record,
                    "index_id": index_id,
                    "project_id": project,
                    "valid": True,
                    "invalidated_at": None,
                    "created_at": now,
                }
                connection.execute(
                    insert(t.semantic_vectors)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[
                            t.semantic_vectors.c.index_id,
                            t.semantic_vectors.c.unit_id,
                        ],
                        set_={
                            key: values[key]
                            for key in (
                                "project_id",
                                "document_id",
                                "version_id",
                                "content_digest",
                                "lifecycle_revision",
                                "vector",
                                "valid",
                                "invalidated_at",
                                "created_at",
                            )
                        },
                    )
                )
            connection.execute(
                t.semantic_indexes.update()
                .where(t.semantic_indexes.c.index_id == index_id)
                .values(status="READY", failure_category=None, updated_at=now)
            )

    def semantic_candidates(self, project, index_id, profile_digest, *, sources=None):
        if sources == []:
            return []
        joined = (
            t.semantic_vectors.join(
                t.semantic_indexes,
                t.semantic_vectors.c.index_id == t.semantic_indexes.c.index_id,
            )
            .join(
                t.units,
                sa.and_(
                    t.semantic_vectors.c.document_id == t.units.c.document_id,
                    t.semantic_vectors.c.version_id == t.units.c.version_id,
                    t.semantic_vectors.c.unit_id == t.units.c.unit_id,
                ),
            )
            .join(
                t.versions,
                sa.and_(
                    t.units.c.document_id == t.versions.c.document_id,
                    t.units.c.version_id == t.versions.c.version_id,
                ),
            )
        )
        conditions = [
            t.semantic_vectors.c.project_id == project,
            t.semantic_vectors.c.index_id == index_id,
            t.semantic_vectors.c.valid.is_(True),
            t.semantic_indexes.c.profile_digest == profile_digest,
            t.versions.c.project_id == project,
            t.versions.c.availability == "ACTIVE",
            t.semantic_vectors.c.content_digest == t.versions.c.content_digest,
            t.semantic_vectors.c.lifecycle_revision == t.versions.c.lifecycle_revision,
        ]
        if sources is not None:
            identities = {
                (
                    source["asset_id"],
                    source["version_id"],
                    source["version_manifest_digest"],
                )
                for source in sources
            }
            conditions.append(
                sa.tuple_(
                    t.versions.c.source_asset_id,
                    t.versions.c.source_version_id,
                    t.versions.c.source_manifest_digest,
                ).in_(sorted(identities))
            )
        with self.db.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    t.semantic_vectors.c.vector,
                    t.units.c.document_id,
                    t.units.c.version_id,
                    t.units.c.unit_id,
                    t.units.c.text,
                    t.units.c.start_char,
                    t.units.c.end_char,
                    t.versions.c.title,
                    t.versions.c.availability,
                    t.versions.c.epistemic_status,
                    t.versions.c.review_state,
                )
                .select_from(joined)
                .where(*conditions)
                .order_by(t.units.c.unit_id)
            ).mappings()
            return [dict(row) for row in rows]

    def search(self, project, query, limit, *, sources=None):
        if sources == []:
            return 0, []
        lowered = query.casefold()
        tsquery = sa.func.plainto_tsquery("simple", query)
        exact = sa.case(
            (sa.func.strpos(sa.func.lower(t.units.c.text), lowered) > 0, 1.0), else_=0.0
        )
        rank = sa.func.ts_rank_cd(t.units.c.search_vector, tsquery)
        similar = sa.func.word_similarity(query, t.units.c.text)
        score = (exact * 10.0 + rank + similar).label("score")
        condition = sa.or_(
            t.units.c.search_vector.op("@@")(tsquery),
            sa.func.strpos(sa.func.lower(t.units.c.text), lowered) > 0,
            similar > 0.2,
        )
        joined = t.units.join(
            t.versions,
            sa.and_(
                t.units.c.document_id == t.versions.c.document_id,
                t.units.c.version_id == t.versions.c.version_id,
            ),
        )
        with self.db.engine.connect() as connection:
            base = (
                sa.select(
                    t.units,
                    t.versions.c.title,
                    t.versions.c.availability,
                    t.versions.c.epistemic_status,
                    t.versions.c.review_state,
                    score,
                )
                .select_from(joined)
                .where(
                    t.units.c.project_id == project,
                    t.versions.c.project_id == project,
                    t.versions.c.availability == "ACTIVE",
                    condition,
                )
            )
            if sources is not None:
                identities = {
                    (
                        source["asset_id"],
                        source["version_id"],
                        source["version_manifest_digest"],
                    )
                    for source in sources
                }
                base = base.where(
                    sa.tuple_(
                        t.versions.c.source_asset_id,
                        t.versions.c.source_version_id,
                        t.versions.c.source_manifest_digest,
                    ).in_(sorted(identities))
                )
            count = connection.scalar(sa.select(sa.func.count()).select_from(base.subquery()))
            rows = connection.execute(
                base.order_by(score.desc(), t.units.c.unit_id).limit(limit)
            ).mappings()
            return int(count or 0), [dict(row) for row in rows]

    def read(self, project, document, version, unit, actor, receipt_id=None):
        with self.db.engine.begin() as connection:
            version_row = self.version(project, document, version, connection=connection)
            unit_row = one(
                connection,
                sa.select(t.units).where(
                    t.units.c.project_id == project,
                    t.units.c.document_id == document,
                    t.units.c.version_id == version,
                    t.units.c.unit_id == unit,
                ),
            )
            receipt = receipt_id or uuid4().hex
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                insert(t.read_receipts)
                .values(
                    receipt_id=receipt,
                    project_id=project,
                    document_id=document,
                    version_id=version,
                    unit_id=unit,
                    actor_id=actor,
                    lifecycle_revision=version_row["lifecycle_revision"],
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[t.read_receipts.c.receipt_id])
            )
            saved = one(
                connection,
                sa.select(t.read_receipts).where(t.read_receipts.c.receipt_id == receipt),
            )
            ensure(
                saved["project_id"] == project
                and saved["document_id"] == document
                and saved["version_id"] == version
                and saved["unit_id"] == unit
                and saved["actor_id"] == actor,
                "IDEMPOTENCY_CONFLICT",
                "Read receipt identity changed",
            )
            return version_row, unit_row, receipt

    def withdraw(self, project, document, version, actor, key, request):
        def operation(connection):
            row = self.version(project, document, version, lock=True, connection=connection)
            ensure(
                row["lifecycle_revision"] == request["expected_lifecycle_revision"],
                "REVISION_CONFLICT",
                "Knowledge lifecycle changed",
            )
            ensure(
                row["availability"] == "ACTIVE",
                "PRECONDITION_UNRESOLVED",
                "Version is already withdrawn",
            )
            revision = row["lifecycle_revision"] + 1
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                t.versions.update()
                .where(t.versions.c.document_id == document, t.versions.c.version_id == version)
                .values(
                    availability="WITHDRAWN",
                    lifecycle_revision=revision,
                    withdrawal_reason=request["reason"],
                    withdrawn_at=now,
                )
            )
            connection.execute(
                t.semantic_vectors.update()
                .where(
                    t.semantic_vectors.c.project_id == project,
                    t.semantic_vectors.c.document_id == document,
                    t.semantic_vectors.c.version_id == version,
                    t.semantic_vectors.c.valid.is_(True),
                )
                .values(valid=False, invalidated_at=now)
            )
            connection.execute(
                t.semantic_indexes.update()
                .where(t.semantic_indexes.c.project_id == project)
                .values(status="STALE", updated_at=now)
            )
            self._event(
                connection,
                project,
                document,
                version,
                revision,
                "WITHDRAWN",
                {"reason": request["reason"]},
            )
            return self.version_view(
                self.version(project, document, version, connection=connection)
            )

        return self.write(
            f"{actor}:{project}:withdraw:{document}:{version}", key, request, operation
        )

    def claim_lifecycle(self, owner, lease_seconds=30):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            row = one(
                connection,
                sa.select(t.outbox)
                .where(
                    t.outbox.c.done.is_(False),
                    sa.or_(t.outbox.c.lease_until.is_(None), t.outbox.c.lease_until <= now),
                )
                .order_by(t.outbox.c.created_at, t.outbox.c.event_id)
                .with_for_update(skip_locked=True)
                .limit(1),
                required=False,
            )
            if row is None:
                return None
            generation = row["generation"] + 1
            connection.execute(
                t.outbox.update()
                .where(t.outbox.c.event_id == row["event_id"])
                .values(
                    owner=owner,
                    generation=generation,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempts=row["attempts"] + 1,
                )
            )
            return {
                "event_id": row["event_id"],
                "generation": generation,
                "payload": row["payload"],
            }

    def ack_lifecycle(self, event_id, owner, generation):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            changed = connection.execute(
                t.outbox.update()
                .where(
                    t.outbox.c.event_id == event_id,
                    t.outbox.c.owner == owner,
                    t.outbox.c.generation == generation,
                    t.outbox.c.done.is_(False),
                    # A late acknowledgement must never finish work claimed by
                    # a newer worker after the original lease expired.
                    t.outbox.c.lease_until > now,
                )
                .values(done=True, owner=None, lease_until=None)
            ).rowcount
            ensure(changed == 1, "REVISION_CONFLICT", "Lifecycle delivery lease changed", 409)
            return {"event_id": event_id, "status": "ACKNOWLEDGED"}

    def renew_lifecycle(self, event_id, owner, generation, lease_seconds=30):
        with self.db.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            changed = connection.execute(
                t.outbox.update()
                .where(
                    t.outbox.c.event_id == event_id,
                    t.outbox.c.owner == owner,
                    t.outbox.c.generation == generation,
                    t.outbox.c.done.is_(False),
                    t.outbox.c.lease_until > now,
                )
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            ).rowcount
            ensure(changed == 1, "REVISION_CONFLICT", "Lifecycle delivery lease changed", 409)
            return {"event_id": event_id, "status": "RENEWED"}
