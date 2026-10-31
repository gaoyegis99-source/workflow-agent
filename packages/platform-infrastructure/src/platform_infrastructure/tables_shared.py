"""Shared SQLAlchemy registry for Platform aggregates.

Every aggregate module imports this one registry.  Splitting declarations by
aggregate therefore does not create extra metadata instances or transactions.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

__all__ = ["ID", "JSONB", "TIME", "metadata", "sa", "table"]


metadata = sa.MetaData()
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def table(name, *columns, **kwargs):
    return sa.Table(name, metadata, *columns, **kwargs)
