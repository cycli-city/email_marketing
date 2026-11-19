"""Custom column types."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def enum_col(enum_cls: type[StrEnum], *, name: str | None = None) -> SAEnum:
    """Store a ``StrEnum`` as VARCHAR with a named CHECK constraint (no native PG enum).

    Native PG enums make ``ADD VALUE`` migrations awkward inside transactions; VARCHAR + CHECK keeps
    integrity at the database level while staying easy to migrate. A test asserts the CHECK constraints
    always match the Python enums, so drift cannot go unnoticed.
    """
    return SAEnum(
        enum_cls,
        name=name or enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=40,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )
