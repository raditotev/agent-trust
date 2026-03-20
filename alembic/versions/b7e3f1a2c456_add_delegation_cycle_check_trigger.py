"""Add delegation cycle check trigger.

Revision ID: b7e3f1a2c456
Revises: a13d97533c44
Create Date: 2026-03-20 12:00:00.000000+00:00

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e3f1a2c456"
down_revision = "a13d97533c44"
branch_labels = None
depends_on = None

MAX_DELEGATION_DEPTH = 5


def upgrade() -> None:
    # Create a PL/pgSQL function that walks the delegation chain upward
    # and rejects the INSERT/UPDATE if it would create a cycle or exceed depth.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION check_delegation_cycle()
        RETURNS TRIGGER AS $$
        DECLARE
            current_id UUID;
            depth INT := 0;
            visited UUID[] := ARRAY[NEW.agent_id];
        BEGIN
            IF NEW.delegated_by IS NULL THEN
                RETURN NEW;
            END IF;

            current_id := NEW.delegated_by;

            WHILE current_id IS NOT NULL AND depth < {MAX_DELEGATION_DEPTH + 1} LOOP
                -- Check for cycle
                IF current_id = ANY(visited) THEN
                    RAISE EXCEPTION 'Delegation cycle detected: agent % would create a cycle',
                        NEW.agent_id;
                END IF;

                visited := array_append(visited, current_id);
                depth := depth + 1;

                -- Check depth limit
                IF depth > {MAX_DELEGATION_DEPTH} THEN
                    RAISE EXCEPTION 'Delegation chain too deep: maximum depth is {MAX_DELEGATION_DEPTH}';
                END IF;

                SELECT delegated_by INTO current_id
                FROM agents
                WHERE agent_id = current_id;
            END LOOP;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER trg_check_delegation_cycle
        BEFORE INSERT OR UPDATE OF delegated_by ON agents
        FOR EACH ROW
        WHEN (NEW.delegated_by IS NOT NULL)
        EXECUTE FUNCTION check_delegation_cycle();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_check_delegation_cycle ON agents;")
    op.execute("DROP FUNCTION IF EXISTS check_delegation_cycle();")
