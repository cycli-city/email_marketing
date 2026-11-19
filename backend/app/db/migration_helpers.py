"""Reusable DDL helpers for migrations (things Alembic autogenerate cannot express).

Every migration that creates a table MUST call ``enable_rls`` for it — ``tests/integration/test_schema.py``
fails the build otherwise (Supabase exposes tables through PostgREST; RLS-with-no-policies makes the
public anon key unable to read anything — ADR-004).
"""

from __future__ import annotations

from alembic import op


def enable_rls(*tables: str) -> None:
    for table in tables:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def enable_rls_everywhere() -> None:
    """Enable RLS on every table currently in ``public`` (used by the initial migration)."""
    op.execute(
        """
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename <> 'alembic_version'
            LOOP
                EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
            END LOOP;
        END $$;
        """
    )


def make_append_only(table: str) -> None:
    """Forbid UPDATE always, and DELETE unless a retention job opts in for its own transaction via
    ``SET LOCAL aoi.allow_audit_delete = 'on'``."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_append_only_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('aoi.allow_audit_delete', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_append_only
        BEFORE UPDATE OR DELETE ON public."{table}"
        FOR EACH ROW EXECUTE FUNCTION forbid_append_only_mutation()
        """
    )


def drop_append_only(table: str) -> None:
    op.execute(f'DROP TRIGGER IF EXISTS {table}_append_only ON public."{table}"')
    op.execute("DROP FUNCTION IF EXISTS forbid_append_only_mutation()")
