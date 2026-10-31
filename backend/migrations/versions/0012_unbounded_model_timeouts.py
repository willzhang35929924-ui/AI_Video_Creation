"""Remove legacy 600-second caps from saved AI service profiles."""
from alembic import context, op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    if context.is_offline_mode():
        dialect = context.get_context().dialect.name
        if dialect == 'sqlite':
            op.execute("UPDATE ai_services SET settings_json = json_set(settings_json, '$.timeout', json('null'))")
        elif dialect == 'postgresql':
            op.execute("UPDATE ai_services SET settings_json = jsonb_set(settings_json::jsonb, '{timeout}', 'null'::jsonb, true)")
        else:
            return
        return

    services = sa.table('ai_services', sa.column('id', sa.String()), sa.column('settings_json', sa.JSON()))
    connection = op.get_bind()
    for row in connection.execute(sa.select(services.c.id, services.c.settings_json)).mappings():
        settings = dict(row['settings_json'] or {})
        settings['timeout'] = None
        connection.execute(services.update().where(services.c.id == row['id']).values(settings_json=settings))


def downgrade():
    # A previous timeout cannot be recovered after it has been cleared.
    pass
