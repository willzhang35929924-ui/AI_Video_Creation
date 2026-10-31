"""AI service settings, character generation and assembly timeline jobs."""
from alembic import op
import sqlalchemy as s

revision = '0003'
down_revision = '0002'
branch_labels = depends_on = None


def stamps():
    return [s.Column('created_at', s.DateTime(timezone=True), nullable=False), s.Column('updated_at', s.DateTime(timezone=True), nullable=False)]


def identifier():
    return s.Column('id', s.String(64), primary_key=True)


def upgrade():
    op.add_column('character_versions', s.Column('standard_face_asset_id', s.String(64)))
    op.add_column('character_versions', s.Column('anchor_locked_at', s.DateTime(timezone=True)))
    op.add_column('character_assets', s.Column('metadata_json', s.JSON(), nullable=False, server_default='{}'))
    op.add_column('character_assets', s.Column('review_reason', s.Text()))
    op.add_column('character_assets', s.Column('reviewed_by', s.String(100)))
    op.create_table('ai_services', identifier(), s.Column('module', s.String(40), nullable=False), s.Column('name', s.String(160), nullable=False), s.Column('provider', s.String(40), nullable=False), s.Column('base_url', s.Text(), nullable=False), s.Column('enabled', s.Boolean(), nullable=False), s.Column('is_default', s.Boolean(), nullable=False), s.Column('default_model', s.String(240), nullable=False), s.Column('models_json', s.JSON(), nullable=False), s.Column('settings_json', s.JSON(), nullable=False), s.Column('capabilities_json', s.JSON(), nullable=False), s.Column('last_test_json', s.JSON(), nullable=False), s.Column('last_success_at', s.DateTime(timezone=True)), s.Column('encrypted_secret', s.LargeBinary()), s.Column('revision', s.Integer(), nullable=False), *stamps())
    op.create_table('project_service_bindings', identifier(), s.Column('project_id', s.String(64), s.ForeignKey('projects.id'), nullable=False), s.Column('module', s.String(40), nullable=False), s.Column('service_id', s.String(64), s.ForeignKey('ai_services.id'), nullable=False), s.Column('model_id', s.String(240), nullable=False), *stamps(), s.UniqueConstraint('project_id', 'module'))
    op.create_table('character_batches', identifier(), s.Column('character_id', s.String(64), s.ForeignKey('characters.id'), nullable=False), s.Column('character_version_id', s.String(64), s.ForeignKey('character_versions.id'), nullable=False), s.Column('kind', s.String(32), nullable=False), s.Column('status', s.String(32), nullable=False), s.Column('paused', s.Boolean(), nullable=False), s.Column('snapshot_json', s.JSON(), nullable=False), s.Column('contact_sheet_path', s.Text()), *stamps())
    op.create_table('character_generation_jobs', identifier(), s.Column('batch_id', s.String(64), s.ForeignKey('character_batches.id'), nullable=False), s.Column('status', s.String(32), nullable=False), s.Column('asset_type', s.String(40), nullable=False), s.Column('prompt', s.Text(), nullable=False), s.Column('negative_prompt', s.Text(), nullable=False), s.Column('seed', s.Integer(), nullable=False), s.Column('snapshot_json', s.JSON(), nullable=False), s.Column('provider_job_id', s.String(200)), s.Column('result_asset_id', s.String(64)), s.Column('error_message', s.Text()), s.Column('log_path', s.Text()), s.Column('retry_of', s.String(64)), *stamps())
    op.create_table('assemblies', identifier(), s.Column('project_id', s.String(64), s.ForeignKey('projects.id'), nullable=False), s.Column('name', s.String(200), nullable=False), s.Column('timeline_json', s.JSON(), nullable=False), s.Column('revision', s.Integer(), nullable=False), *stamps())
    op.create_table('assembly_renders', identifier(), s.Column('assembly_id', s.String(64), s.ForeignKey('assemblies.id'), nullable=False), s.Column('version', s.Integer(), nullable=False), s.Column('kind', s.String(20), nullable=False), s.Column('status', s.String(32), nullable=False), s.Column('snapshot_json', s.JSON(), nullable=False), s.Column('metadata_json', s.JSON(), nullable=False), s.Column('relative_path', s.Text()), s.Column('error_message', s.Text()), s.Column('cancel_requested', s.Boolean(), nullable=False), s.Column('log_path', s.Text()), *stamps())
    for table, column in [('ai_services', 'module'), ('project_service_bindings', 'project_id'), ('character_batches', 'character_id'), ('character_generation_jobs', 'batch_id'), ('character_generation_jobs', 'status'), ('assemblies', 'project_id'), ('assembly_renders', 'assembly_id'), ('assembly_renders', 'status')]:
        op.create_index('ix_' + table + '_' + column, table, [column])


def downgrade():
    for table in ['assembly_renders', 'assemblies', 'character_generation_jobs', 'character_batches', 'project_service_bindings', 'ai_services']:
        op.drop_table(table)
    for column in ['reviewed_by', 'review_reason', 'metadata_json']:
        op.drop_column('character_assets', column)
    op.drop_column('character_versions', 'anchor_locked_at')
    op.drop_column('character_versions', 'standard_face_asset_id')
