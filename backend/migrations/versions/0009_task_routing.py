"""Independent task-slot policies and additive audio/lip processing lineage."""
from alembic import op
import sqlalchemy as sa
revision='0009'
down_revision='0008'
branch_labels=None
depends_on=None

def timestamps():
    return [sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False)]

def upgrade():
    op.create_table('route_policies',sa.Column('id',sa.String(64),primary_key=True),sa.Column('scope',sa.String(20),nullable=False),
        sa.Column('target_id',sa.String(64),nullable=False),sa.Column('slot',sa.String(40),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),sa.Column('settings_json',sa.JSON(),nullable=False),*timestamps(),
        sa.UniqueConstraint('scope','target_id','slot',name='uq_route_scope_target_slot'))
    op.create_table('media_process_tasks',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),
        sa.Column('shot_id',sa.String(64),sa.ForeignKey('storyboard_shots.id')),sa.Column('kind',sa.String(32),nullable=False),
        sa.Column('slot',sa.String(40),nullable=False),sa.Column('fingerprint',sa.String(64)),sa.Column('status',sa.String(32),nullable=False),
        sa.Column('snapshot_json',sa.JSON(),nullable=False),sa.Column('result_json',sa.JSON(),nullable=False),sa.Column('error_message',sa.Text()),*timestamps())
    for field in ('project_id','shot_id','status'):op.create_index('ix_media_process_tasks_'+field,'media_process_tasks',[field])
    op.create_index('uq_media_live_fingerprint','media_process_tasks',['fingerprint'],unique=True,
        sqlite_where=sa.text("status IN ('queued','running','unknown','succeeded')"),
        postgresql_where=sa.text("status IN ('queued','running','unknown','succeeded')"))
    op.create_table('audio_assets',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),
        sa.Column('shot_id',sa.String(64),sa.ForeignKey('storyboard_shots.id')),
        sa.Column('participant_id',sa.String(64),sa.ForeignKey('shot_characters.id')),
        sa.Column('dialogue_line_id',sa.String(64),sa.ForeignKey('dialogue_lines.id')),
        sa.Column('character_version_id',sa.String(64),sa.ForeignKey('character_versions.id')),
        sa.Column('task_id',sa.String(64),sa.ForeignKey('media_process_tasks.id')),sa.Column('relative_path',sa.Text(),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),sa.Column('metadata_json',sa.JSON(),nullable=False),*timestamps())
    for field in ('project_id','shot_id'):op.create_index('ix_audio_assets_'+field,'audio_assets',[field])

def downgrade():
    op.drop_table('audio_assets');op.drop_table('media_process_tasks');op.drop_table('route_policies')
