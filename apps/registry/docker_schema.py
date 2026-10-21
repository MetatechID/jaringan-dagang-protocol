"""Explicit fresh-schema creation and non-mutating readiness verification.

This is NOT an upgrade migration: create_all cannot repair existing tables.
"""
from sqlalchemy import Float, inspect, text

from database import engine
from database import Base
import models  # register the registry model


def _verify(connection):
    inspector = inspect(connection)
    problems = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name, schema=table.schema):
            problems.append(f"missing table {table.name}")
            continue
        actual = {c["name"]: c for c in inspector.get_columns(table.name, schema=table.schema)}
        for column in table.columns:
            found = actual.get(column.name)
            if found is None:
                problems.append(f"missing column {table.name}.{column.name}")
                continue
            expected_type = column.type.compile(dialect=connection.dialect)
            actual_type = found["type"].compile(dialect=connection.dialect)
            # PostgreSQL reflects unqualified FLOAT as DOUBLE PRECISION (and
            # FLOAT(p<=24) as REAL), not the original SQL spelling.
            if isinstance(column.type, Float) and connection.dialect.name == "postgresql":
                precision = column.type.precision
                expected_type = "REAL" if precision is not None and precision <= 24 else "DOUBLE PRECISION"
            if expected_type != actual_type or column.nullable != found["nullable"]:
                problems.append(f"column drift {table.name}.{column.name}: expected {expected_type} nullable={column.nullable}, got {actual_type} nullable={found['nullable']}")
        expected_pk = {c.name for c in table.primary_key.columns}
        actual_pk = set(inspector.get_pk_constraint(table.name, schema=table.schema)["constrained_columns"])
        if expected_pk != actual_pk:
            problems.append(f"primary key drift {table.name}")
    if problems:
        raise RuntimeError("Schema verification failed: " + "; ".join(problems))


async def verify_schema():
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        await connection.run_sync(_verify)


async def init_schema():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await verify_schema()
