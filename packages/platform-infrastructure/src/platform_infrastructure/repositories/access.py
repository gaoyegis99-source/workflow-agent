"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from platform_core.domain.control import require

from platform_infrastructure import tables_access as t
from platform_infrastructure.repositories.base import Repository, one


class AccessRepository(Repository):
    def authorize(self, actor, project_id, role="VIEWER"):
        if actor.is_trusted_proxy:
            # Proxy-authenticated callers are resolved from a bound Principal by
            # the HTTP boundary.  Internal service continuations use
            # ``authorize_principal`` and never enter this user path.
            require(
                actor.token_id is None,
                "AUTHENTICATION_REQUIRED",
                "Valid authentication is required",
                401,
            )
            principal = one(
                self.c,
                sa.select(t.principals)
                .where(t.principals.c.principal_id == actor.principal_id)
                .with_for_update(read=True),
                required=False,
            )
            require(
                principal is not None,
                "AUTHENTICATION_REQUIRED",
                "Valid authentication is required",
                401,
            )
        elif actor.token_id is None:
            # A token-free Identity is only valid through
            # ``authorize_principal`` for an explicit service-side
            # continuation.  Do not allow arbitrary callers to turn an
            # omitted token into a user credential.
            require(False, "AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)
        else:
            token = one(
                self.c,
                sa.select(t.tokens)
                .where(
                    t.tokens.c.token_id == actor.token_id,
                    t.tokens.c.principal_id == actor.principal_id,
                )
                .with_for_update(read=True),
                required=False,
            )
            require(
                token is not None and not token["revoked"] and token["expires_at"] > self.now(),
                "AUTHENTICATION_REQUIRED",
                "Valid authentication is required",
                401,
            )
        if project_id is None:
            return
        member = one(
            self.c,
            sa.select(t.members)
            .where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == actor.principal_id,
            )
            .with_for_update(read=True),
            required=False,
        )
        require(member is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
        levels = {"VIEWER": 0, "EDITOR": 1, "OWNER": 2}
        require(
            levels[member["role"]] >= levels[role],
            "SCOPE_DENIED",
            "Project role is insufficient",
            403,
        )

    def authorize_principal(self, principal_id, project_id, role="VIEWER"):
        """Authorize a service-side continuation of an authenticated Agent task.

        Agent workers never receive or retain a user's bearer token.  The run
        freezes the initiating principal and every tool read rechecks current
        project membership through this service identity path.
        """
        member = one(
            self.c,
            sa.select(t.members)
            .where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == principal_id,
            )
            .with_for_update(read=True),
            required=False,
        )
        require(member is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
        levels = {"VIEWER": 0, "EDITOR": 1, "OWNER": 2}
        require(
            levels[member["role"]] >= levels[role],
            "SCOPE_DENIED",
            "Project role is insufficient",
            403,
        )

    def role(self, actor, project_id):
        return one(
            self.c,
            sa.select(t.members).where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == actor.principal_id,
            ),
        )["role"]

    def projects(self, actor, after, limit):
        rows = self.c.execute(
            sa.select(t.projects, t.members.c.role)
            .join(t.members)
            .where(
                t.members.c.principal_id == actor.principal_id,
                t.projects.c.project_id > after,
            )
            .order_by(t.projects.c.project_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def project(self, project_id):
        return one(self.c, sa.select(t.projects).where(t.projects.c.project_id == project_id))

    def create_project(self, project_id, name, actor):
        self.c.execute(t.projects.insert().values(project_id=project_id, name=name))
        self.c.execute(
            t.members.insert().values(
                project_id=project_id, principal_id=actor.principal_id, role="OWNER"
            )
        )
        return self.project(project_id) | {"role": "OWNER"}
