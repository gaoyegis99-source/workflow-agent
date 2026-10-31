"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from datetime import timedelta

import sqlalchemy as sa
from platform_core.domain.control import require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_access as t
from platform_infrastructure.repositories.base import Repository, one


class DeliveryRepository(Repository):
    @staticmethod
    def key(actor, scope, operation, key):
        return dict(principal_id=actor.principal_id, scope=scope, operation=operation, key=key)

    def replay(self, actor, scope, operation, key):
        identity = self.key(actor, scope, operation, key)
        record = one(
            self.c,
            sa.select(t.requests)
            .where(*[t.requests.c[name] == value for name, value in identity.items()])
            .with_for_update(),
            required=False,
        )
        return record["result"] if record else None

    def idempotency(self, actor, scope, operation, key, digest):
        identity = self.key(actor, scope, operation, key)
        self.c.execute(
            insert(t.requests).values(**identity, digest=digest).on_conflict_do_nothing()
        )
        query = (
            sa.select(t.requests)
            .where(*[t.requests.c[k] == v for k, v in identity.items()])
            .with_for_update()
        )
        record = one(self.c, query)
        require(
            record["digest"] == digest, "IDEMPOTENCY_CONFLICT", "Request key has different content"
        )
        return record["result"]

    def remember(self, actor, scope, operation, key, result):
        identity = self.key(actor, scope, operation, key)
        self.c.execute(
            t.requests.update()
            .where(*[t.requests.c[k] == v for k, v in identity.items()])
            .values(result=result)
        )

    def enqueue(self, event_id, task_id, kind, payload):
        self.c.execute(
            t.outbox.insert().values(event_id=event_id, task_id=task_id, kind=kind, payload=payload)
        )

    def claim(self, owner, lease_seconds):
        now = self.now()
        record = one(
            self.c,
            sa.select(t.outbox)
            .where(
                t.outbox.c.done.is_(False),
                t.outbox.c.available_at <= now,
                sa.or_(t.outbox.c.lease_until.is_(None), t.outbox.c.lease_until <= now),
            )
            .order_by(t.outbox.c.available_at, t.outbox.c.event_id)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if record is None:
            return None
        changes = dict(
            owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            generation=record["generation"] + 1,
            attempts=record["attempts"] + 1,
        )
        self.c.execute(
            t.outbox.update().where(t.outbox.c.event_id == record["event_id"]).values(**changes)
        )
        return record | changes

    def predicate(self, event_id, owner, generation):
        return sa.and_(
            t.outbox.c.event_id == event_id,
            t.outbox.c.owner == owner,
            t.outbox.c.generation == generation,
            t.outbox.c.lease_until > sa.func.clock_timestamp(),
            t.outbox.c.done.is_(False),
        )

    def owned(self, event_id, owner, generation):
        record = one(
            self.c,
            sa.select(t.outbox).where(t.outbox.c.event_id == event_id).with_for_update(),
            required=False,
        )
        # Read database time after the row lock is acquired: waiting for a lock
        # must not turn a pre-wait timestamp into permission to use an expired lease.
        return bool(
            record
            and not record["done"]
            and record["owner"] == owner
            and record["generation"] == generation
            and record["lease_until"] is not None
            and record["lease_until"] > self.now()
        )

    def renew(self, event_id, owner, generation, seconds):
        return (
            self.c.execute(
                t.outbox.update()
                .where(self.predicate(event_id, owner, generation))
                .values(lease_until=self.now() + timedelta(seconds=seconds))
            ).rowcount
            == 1
        )

    def finish(self, event_id, owner, generation, delay=None):
        self.c.execute(
            t.outbox.update()
            .where(self.predicate(event_id, owner, generation))
            .values(
                done=delay is None,
                owner=None,
                lease_until=None,
                available_at=self.now() + timedelta(seconds=delay or 0),
            )
        )

    def inbox(self, source, event_id, digest):
        inserted = self.c.execute(
            insert(t.inbox)
            .values(source=source, event_id=event_id, digest=digest)
            .on_conflict_do_nothing()
            .returning(t.inbox.c.event_id)
        ).scalar_one_or_none()
        if inserted is not None:
            return True
        row = one(
            self.c,
            sa.select(t.inbox).where(t.inbox.c.source == source, t.inbox.c.event_id == event_id),
        )
        require(
            row["digest"] == digest,
            "INVALID_BINDING",
            "Event identity has conflicting content",
            422,
        )
        return False

    def audit(self, event_id, task_id, payload):
        self.c.execute(
            insert(t.audit)
            .values(event_id=event_id, task_id=task_id, payload=payload)
            .on_conflict_do_nothing()
        )
