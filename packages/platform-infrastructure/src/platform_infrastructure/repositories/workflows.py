"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from datetime import timedelta

import sqlalchemy as sa
from platform_core.domain.control import require

from platform_infrastructure import tables_workflows as t
from platform_infrastructure.repositories.base import Repository, one


class WorkflowRepository(Repository):
    def add_package(self, record):
        self.c.execute(t.workflow_packages.insert().values(**record))

    def package(self, project_id, package_id):
        return one(
            self.c,
            sa.select(t.workflow_packages).where(
                t.workflow_packages.c.project_id == project_id,
                t.workflow_packages.c.package_id == package_id,
            ),
        )

    def packages(self, project_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_packages)
            .where(
                t.workflow_packages.c.project_id == project_id,
                t.workflow_packages.c.package_id > after,
            )
            .order_by(t.workflow_packages.c.package_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def claim_package(self, owner, lease_seconds):
        now = self.now()
        row = one(
            self.c,
            sa.select(t.workflow_packages)
            .where(
                t.workflow_packages.c.status.in_(["QUEUED", "VALIDATING"]),
                sa.or_(
                    t.workflow_packages.c.lease_until.is_(None),
                    t.workflow_packages.c.lease_until <= now,
                ),
            )
            .order_by(t.workflow_packages.c.package_id)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if row is None:
            return None
        changes = {
            "status": "VALIDATING",
            "owner": owner,
            "generation": row["generation"] + 1,
            "lease_until": now + timedelta(seconds=lease_seconds),
        }
        self.c.execute(
            t.workflow_packages.update()
            .where(t.workflow_packages.c.package_id == row["package_id"])
            .values(**changes)
        )
        return row | changes

    def finish_package(self, package_id, owner, generation, *, status, compiled, error):
        require(status in {"READY", "REJECTED"}, "INVALID_BINDING", "Invalid package state")
        return (
            self.c.execute(
                t.workflow_packages.update()
                .where(
                    t.workflow_packages.c.package_id == package_id,
                    t.workflow_packages.c.owner == owner,
                    t.workflow_packages.c.generation == generation,
                    t.workflow_packages.c.status == "VALIDATING",
                    t.workflow_packages.c.lease_until > sa.func.clock_timestamp(),
                )
                .values(
                    status=status,
                    compiled=compiled,
                    error=error,
                    owner=None,
                    lease_until=None,
                )
            ).rowcount
            == 1
        )

    @staticmethod
    def _package_lease(package_id, owner, generation):
        """Return the single fenced predicate shared by renewal and completion.

        Package compilation happens outside a database transaction.  Every
        write after that work therefore has to prove that the exact worker
        generation still owns a live lease; checking only the generation would
        allow a slow validator to publish after a replacement worker claimed
        the package.
        """

        return sa.and_(
            t.workflow_packages.c.package_id == package_id,
            t.workflow_packages.c.owner == owner,
            t.workflow_packages.c.generation == generation,
            t.workflow_packages.c.status == "VALIDATING",
            t.workflow_packages.c.lease_until > sa.func.clock_timestamp(),
        )

    def package_owned(self, package_id, owner, generation):
        row = one(
            self.c,
            sa.select(t.workflow_packages)
            .where(t.workflow_packages.c.package_id == package_id)
            .with_for_update(),
            required=False,
        )
        return bool(
            row
            and row["owner"] == owner
            and row["generation"] == generation
            and row["status"] == "VALIDATING"
            and row["lease_until"] is not None
            and row["lease_until"] > self.now()
        )

    def renew_package(self, package_id, owner, generation, lease_seconds):
        return (
            self.c.execute(
                t.workflow_packages.update()
                .where(self._package_lease(package_id, owner, generation))
                .values(lease_until=self.now() + timedelta(seconds=lease_seconds))
            ).rowcount
            == 1
        )

    def release_package(self, package_id, owner, generation):
        """Return a transiently failed compilation to the queue if still owned."""

        return (
            self.c.execute(
                t.workflow_packages.update()
                .where(self._package_lease(package_id, owner, generation))
                .values(status="QUEUED", owner=None, lease_until=None)
            ).rowcount
            == 1
        )

    def add_envelope(self, action_id, amount):
        self.c.execute(t.workflow_envelopes.insert().values(action_id=action_id, amount=amount))

    def envelope(self, action_id, *, required=True):
        return one(
            self.c,
            sa.select(t.workflow_envelopes).where(t.workflow_envelopes.c.action_id == action_id),
            required=required,
        )

    def release_envelope(self, action_id):
        envelope = one(
            self.c,
            sa.select(t.workflow_envelopes)
            .where(t.workflow_envelopes.c.action_id == action_id)
            .with_for_update(),
        )
        if envelope["released"]:
            return 0
        rows = self.nodes(action_id)
        require(
            all(not row["slot_acquired"] or row["stopped"] for row in rows),
            "PRECONDITION_UNRESOLVED",
            "Workflow nodes have not converged",
        )
        used = sum(bool(row["executed"]) for row in rows)
        require(used <= envelope["amount"], "INVALID_BINDING", "Workflow envelope exceeded")
        self.c.execute(
            t.workflow_envelopes.update()
            .where(t.workflow_envelopes.c.action_id == action_id)
            .values(released=True)
        )
        return envelope["amount"] - used

    def node(self, node_id, *, lock=False):
        query = sa.select(t.workflow_nodes).where(t.workflow_nodes.c.node_id == node_id)
        if lock:
            query = query.with_for_update()
        return one(self.c, query, required=False)

    def add_node(self, record):
        self.c.execute(t.workflow_nodes.insert().values(**record))

    def nodes(self, action_id, after="", limit=1001):
        rows = self.c.execute(
            sa.select(t.workflow_nodes)
            .where(
                t.workflow_nodes.c.action_id == action_id,
                t.workflow_nodes.c.node_id > after,
            )
            .order_by(t.workflow_nodes.c.node_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def update_node(self, node_id, **changes):
        allowed = {"slot_acquired", "executed", "stopped", "revision", "observation"}
        if not set(changes) <= allowed:
            raise ValueError("Frozen workflow node fields cannot change")
        self.c.execute(
            t.workflow_nodes.update().where(t.workflow_nodes.c.node_id == node_id).values(**changes)
        )
