"""Data-owned transactional persistence. No Platform or Execution tables are visible."""

from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa
from platform_contracts.service import digest, ensure
from sqlalchemy.dialects.postgresql import insert

from platform_data.adapters import tables as t


def one(c, query):
    row = c.execute(query).mappings().first()
    ensure(row is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
    return dict(row)


class Repository:
    def __init__(self, database):
        self.db = database

    def write(self, scope, key, request, operation):
        fingerprint = digest(request)
        with self.db.engine.begin() as c:
            c.execute(
                insert(t.requests)
                .values(scope=scope, key=key, digest=fingerprint)
                .on_conflict_do_nothing()
            )
            where = (t.requests.c.scope == scope, t.requests.c.key == key)
            row = one(c, sa.select(t.requests).where(*where).with_for_update())
            ensure(row["digest"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Key content changed")
            if row["result"] is not None:
                return row["result"]
            result = operation(c)
            c.execute(t.requests.update().where(*where).values(result=result))
            return result

    def upload(self, c, project, id, lock=False):
        query = sa.select(t.uploads).where(
            t.uploads.c.project_id == project, t.uploads.c.upload_id == id
        )
        return one(c, query.with_for_update() if lock else query)

    def asset(self, c, project, id, version):
        return one(
            c,
            sa.select(t.assets).where(
                t.assets.c.project_id == project,
                t.assets.c.asset_id == id,
                t.assets.c.version_id == version,
            ),
        )

    def claim(self, owner, lease_seconds=30, formats=None):
        with self.db.engine.begin() as c:
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            conditions = [
                t.uploads.c.state.in_(["QUEUED", "IMPORTING"]),
                sa.or_(t.uploads.c.lease_until.is_(None), t.uploads.c.lease_until < now),
            ]
            if formats is not None:
                conditions.append(t.uploads.c.payload["format"].astext.in_(tuple(formats)))
            row = (
                c.execute(
                    sa.select(t.uploads)
                    .where(*conditions)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            record = dict(row)
            record.update(owner=owner, generation=row["generation"] + 1, state="IMPORTING")
            c.execute(
                t.uploads.update()
                .where(t.uploads.c.upload_id == row["upload_id"])
                .values(
                    owner=owner,
                    generation=record["generation"],
                    state="IMPORTING",
                    lease_until=now + timedelta(seconds=lease_seconds),
                )
            )
            return record

    def renew(self, claim, lease_seconds=30):
        with self.db.engine.begin() as c:
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            return (
                c.execute(
                    t.uploads.update()
                    .where(
                        t.uploads.c.upload_id == claim["upload_id"],
                        t.uploads.c.owner == claim["owner"],
                        t.uploads.c.generation == claim["generation"],
                        t.uploads.c.state == "IMPORTING",
                        t.uploads.c.lease_until > now,
                    )
                    .values(lease_until=now + timedelta(seconds=lease_seconds))
                ).rowcount
                == 1
            )

    def import_result(self, claim, result=None, error=None):
        with self.db.engine.begin() as c:
            row = self.upload(c, claim["project_id"], claim["upload_id"], True)
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            if (row["owner"], row["generation"]) != (claim["owner"], claim["generation"]) or row[
                "lease_until"
            ] <= now:
                return False
            payload = row["payload"].copy()
            if result:
                # Deterministic asset identity across import retries, not another public version.
                manifest = result
                c.execute(
                    insert(t.assets)
                    .values(
                        asset_id=manifest["asset_id"],
                        version_id=manifest["version_id"],
                        project_id=row["project_id"],
                        manifest=manifest,
                        manifest_digest="sha256:" + digest(manifest),
                    )
                    .on_conflict_do_nothing()
                )
                payload["asset"] = dict(
                    project_id=row["project_id"],
                    asset_id=manifest["asset_id"],
                    version_id=manifest["version_id"],
                    version_manifest_digest="sha256:" + digest(manifest),
                )
            else:
                payload["error"] = error
            c.execute(
                t.uploads.update()
                .where(t.uploads.c.upload_id == row["upload_id"])
                .values(
                    state="READY" if result else "FAILED",
                    payload=payload,
                    lease_until=None,
                    owner=None,
                )
            )
            return True

    def id(self):
        return uuid4().hex

    def transaction(self):
        return self.db.engine.begin()

    def add_upload(self, c, id, project, payload):
        c.execute(
            t.uploads.insert().values(
                upload_id=id, project_id=project, state="AWAITING_CONTENT", payload=payload
            )
        )

    def update_upload(self, c, id, **changes):
        c.execute(t.uploads.update().where(t.uploads.c.upload_id == id).values(**changes))

    def update_import_runtime(self, claim, **changes):
        with self.db.engine.begin() as c:
            row = self.upload(c, claim["project_id"], claim["upload_id"], True)
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            if (row["owner"], row["generation"]) != (claim["owner"], claim["generation"]) or row[
                "lease_until"
            ] <= now:
                return False
            runtime = dict(row.get("runtime") or {}) | changes
            c.execute(
                t.uploads.update()
                .where(t.uploads.c.upload_id == claim["upload_id"])
                .values(runtime=runtime)
            )
            claim["runtime"] = runtime
            return True

    def list_records(self, project, kind, after, limit):
        table = t.uploads if kind == "uploads" else t.assets
        pk = table.c.upload_id if kind == "uploads" else table.c.asset_id
        conditions = [table.c.project_id == project]
        if after is not None:
            if kind == "uploads":
                conditions.append(pk > after[0])
            else:
                conditions.append(sa.tuple_(table.c.asset_id, table.c.version_id) > after)
        ordering = (pk,) if kind == "uploads" else (table.c.asset_id, table.c.version_id)
        with self.db.engine.connect() as c:
            return [
                dict(row)
                for row in c.execute(
                    sa.select(table).where(*conditions).order_by(*ordering).limit(limit)
                ).mappings()
            ]

    def lock_asset(self, c, project, id, version):
        return one(
            c,
            sa.select(t.assets)
            .where(
                t.assets.c.project_id == project,
                t.assets.c.asset_id == id,
                t.assets.c.version_id == version,
            )
            .with_for_update(),
        )

    def semantic_revisions(self, c, id, version):
        return [
            dict(row)
            for row in c.execute(
                sa.select(t.semantics.c.revision, t.semantics.c.payload)
                .where(t.semantics.c.asset_id == id, t.semantics.c.version_id == version)
                .order_by(t.semantics.c.revision)
            ).mappings()
        ]

    def add_semantic(self, c, id, version, revision, payload):
        c.execute(
            t.semantics.insert().values(
                asset_id=id, version_id=version, revision=revision, payload=payload
            )
        )

    def publication(self, project, execution):
        with self.db.engine.connect() as c:
            return one(
                c,
                sa.select(t.publications).where(
                    t.publications.c.execution_id == execution,
                    t.publications.c.project_id == project,
                ),
            )

    def lock_publication(self, c, project, execution, generation):
        c.execute(
            insert(t.publications)
            .values(
                execution_id=execution,
                project_id=project,
                state="PENDING",
                generation=generation,
                payload={},
            )
            .on_conflict_do_nothing()
        )
        return one(
            c,
            sa.select(t.publications)
            .where(t.publications.c.execution_id == execution)
            .with_for_update(),
        )

    def update_publication(self, c, execution, **changes):
        c.execute(
            t.publications.update()
            .where(t.publications.c.execution_id == execution)
            .values(**changes)
        )

    def add_asset(self, c, record):
        c.execute(t.assets.insert().values(**record))

    def add_publication_member(self, c, execution, member, asset_ref):
        c.execute(
            insert(t.publication_members)
            .values(execution_id=execution, member_id=member, asset_ref=asset_ref)
            .on_conflict_do_nothing()
        )
