"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from platform_core.domain.control import require

from platform_infrastructure import tables_workflows as t
from platform_infrastructure.repositories.base import Repository, one


class ServiceBindingRepository(Repository):
    def add(self, project_id, request, binding_digest):
        identity = request["service_binding_id"]
        self.c.execute(
            t.service_bindings.insert().values(
                service_binding_id=identity, project_id=project_id, current_revision=1, enabled=True
            )
        )
        self.c.execute(
            t.service_binding_revisions.insert().values(
                project_id=project_id,
                service_binding_id=identity,
                revision=1,
                protocol=request["protocol"],
                endpoint=request["endpoint"],
                credential_ref=request["credential_ref"],
                tls_ca_ref=request.get("tls_ca_ref"),
                targets=request["targets"],
                limits=request["limits"],
                binding_digest=binding_digest,
            )
        )
        return self.get(project_id, identity, 1)

    def revise(self, project_id, identity, request, binding_digest):
        head = one(
            self.c,
            sa.select(t.service_bindings)
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .with_for_update(),
        )
        revision = head["current_revision"] + 1
        self.c.execute(
            t.service_binding_revisions.insert().values(
                project_id=project_id,
                service_binding_id=identity,
                revision=revision,
                protocol=request["protocol"],
                endpoint=request["endpoint"],
                credential_ref=request["credential_ref"],
                tls_ca_ref=request.get("tls_ca_ref"),
                targets=request["targets"],
                limits=request["limits"],
                binding_digest=binding_digest,
            )
        )
        self.c.execute(
            t.service_bindings.update()
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .values(current_revision=revision)
        )
        return self.get(project_id, identity, revision)

    def set_enabled(self, project_id, identity, enabled):
        count = self.c.execute(
            t.service_bindings.update()
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .values(enabled=enabled)
        ).rowcount
        require(count == 1, "RESOURCE_NOT_FOUND", "Service binding not found", 404)

    def get(self, project_id, identity, revision=None):
        query = (
            sa.select(t.service_bindings, t.service_binding_revisions)
            .join(
                t.service_binding_revisions,
                t.service_binding_revisions.c.service_binding_id
                == t.service_bindings.c.service_binding_id,
            )
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_binding_revisions.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
        )
        if revision is None:
            query = query.where(
                t.service_binding_revisions.c.revision == t.service_bindings.c.current_revision
            )
        else:
            query = query.where(t.service_binding_revisions.c.revision == revision)
        return one(self.c, query)

    def list(self, project_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.service_bindings, t.service_binding_revisions)
                .join(
                    t.service_binding_revisions,
                    sa.and_(
                        t.service_binding_revisions.c.service_binding_id
                        == t.service_bindings.c.service_binding_id,
                        t.service_binding_revisions.c.project_id == t.service_bindings.c.project_id,
                        t.service_binding_revisions.c.revision
                        == t.service_bindings.c.current_revision,
                    ),
                )
                .where(t.service_bindings.c.project_id == project_id)
                .order_by(t.service_bindings.c.service_binding_id)
            ).mappings()
        ]
