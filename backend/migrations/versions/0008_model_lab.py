"""Persistent model inventory and isolated video comparison experiments."""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table('model_directories', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('path', sa.Text(), nullable=False, unique=True), sa.Column('label', sa.String(160), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False), sa.Column('scan_json', sa.JSON(), nullable=False), *timestamps())
    op.create_table('local_models', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('directory_id', sa.String(64), sa.ForeignKey('model_directories.id')),
        sa.Column('name', sa.String(200), nullable=False), sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('path', sa.Text(), nullable=False), sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('service_id', sa.String(64), sa.ForeignKey('ai_services.id')), sa.Column('workflow_path', sa.Text()),
        sa.Column('fingerprint', sa.String(64), nullable=False), sa.Column('scan_json', sa.JSON(), nullable=False),
        sa.Column('verification_json', sa.JSON(), nullable=False), *timestamps())
    op.create_index('ix_local_models_kind', 'local_models', ['kind'])
    op.create_table('model_experiments', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('name', sa.String(160), nullable=False), sa.Column('mode', sa.String(24), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('source_shot_id', sa.String(64), sa.ForeignKey('storyboard_shots.id'), nullable=False),
        sa.Column('test_project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('test_shot_id', sa.String(64), sa.ForeignKey('storyboard_shots.id'), nullable=False),
        sa.Column('snapshot_json', sa.JSON(), nullable=False), sa.Column('cases_json', sa.JSON(), nullable=False),
        sa.Column('result_json', sa.JSON(), nullable=False), sa.Column('error_message', sa.Text()), *timestamps())
    op.create_index('ix_model_experiments_status', 'model_experiments', ['status'])
    op.create_table('model_lab_defaults', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('configuration_json', sa.JSON(), nullable=False), *timestamps())


def downgrade():
    op.drop_table('model_lab_defaults')
    op.drop_table('model_experiments')
    op.drop_table('local_models')
    op.drop_table('model_directories')
