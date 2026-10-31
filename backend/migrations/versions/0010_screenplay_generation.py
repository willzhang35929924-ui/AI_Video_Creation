"""Screenplay authoring and optional project-scoped role references, additive only."""
from alembic import op
import sqlalchemy as sa

revision='0010'
down_revision='0009'
branch_labels=None
depends_on=None

def timestamps():
    return [sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False)]

def upgrade():
    op.create_table('screenplay_input_assets',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('input_mode',sa.String(12),nullable=False),sa.Column('filename',sa.String(240),nullable=False),
        sa.Column('relative_path',sa.Text(),nullable=False),sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('metadata_json',sa.JSON(),nullable=False),sa.Column('source_note',sa.Text(),nullable=False),*timestamps())
    op.create_table('screenplay_generation_jobs',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('idempotency_key',sa.String(120),nullable=False,unique=True),sa.Column('request_sha256',sa.String(64),nullable=False),
        sa.Column('project_name',sa.String(200),nullable=False),sa.Column('input_mode',sa.String(12),nullable=False),
        sa.Column('creative_prompt',sa.Text(),nullable=False),sa.Column('story_text',sa.Text(),nullable=False),
        sa.Column('input_asset_id',sa.String(64),sa.ForeignKey('screenplay_input_assets.id')),
        sa.Column('parent_job_id',sa.String(64),sa.ForeignKey('screenplay_generation_jobs.id')),
        sa.Column('scope',sa.String(12),nullable=False),sa.Column('target_key',sa.String(160)),sa.Column('instruction',sa.Text(),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('stage',sa.String(32),nullable=False),sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('capability_test',sa.Boolean(),nullable=False),sa.Column('snapshot_json',sa.JSON(),nullable=False),
        sa.Column('screenplay_json',sa.JSON(),nullable=False),sa.Column('receipt_json',sa.JSON(),nullable=False),
        sa.Column('error_message',sa.Text()),sa.Column('review_state',sa.String(24),nullable=False),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),unique=True),
        sa.Column('word_import_id',sa.String(64),sa.ForeignKey('word_imports.id'),unique=True),*timestamps())
    op.create_index('ix_screenplay_generation_jobs_status','screenplay_generation_jobs',['status'])
    op.create_table('screenplay_documents',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('job_id',sa.String(64),sa.ForeignKey('screenplay_generation_jobs.id'),nullable=False),sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('relative_path',sa.Text(),nullable=False),sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('screenplay_sha256',sa.String(64),nullable=False),sa.Column('versions_json',sa.JSON(),nullable=False),
        sa.Column('review_state',sa.String(24),nullable=False),*timestamps(),sa.UniqueConstraint('job_id','version'))
    op.create_index('ix_screenplay_documents_job_id','screenplay_documents',['job_id'])
    op.create_table('screenplay_project_name_claims',sa.Column('name_key',sa.String(64),primary_key=True),
        sa.Column('project_name',sa.String(200),nullable=False),sa.Column('job_id',sa.String(64),sa.ForeignKey('screenplay_generation_jobs.id'),nullable=False,unique=True),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),unique=True),*timestamps())
    op.create_table('character_reference_sets',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),
        sa.Column('character_id',sa.String(64),sa.ForeignKey('characters.id'),nullable=False),sa.Column('revision',sa.Integer(),nullable=False),
        *timestamps(),sa.UniqueConstraint('project_id','character_id',name='uq_character_reference_set'))
    for field in ('project_id','character_id'):op.create_index('ix_character_reference_sets_'+field,'character_reference_sets',[field])
    op.create_table('character_reference_assets',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),
        sa.Column('character_id',sa.String(64),sa.ForeignKey('characters.id'),nullable=False),
        sa.Column('relative_path',sa.Text(),nullable=False),sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('mime_type',sa.String(100),nullable=False),sa.Column('width',sa.Integer(),nullable=False),sa.Column('height',sa.Integer(),nullable=False),
        sa.Column('byte_size',sa.Integer(),nullable=False),sa.Column('purpose',sa.String(32),nullable=False),sa.Column('order',sa.Integer(),nullable=False),
        sa.Column('state',sa.String(24),nullable=False),sa.Column('source_note',sa.Text(),nullable=False),*timestamps())
    for field in ('project_id','character_id','sha256','state'):op.create_index('ix_character_reference_assets_'+field,'character_reference_assets',[field])

def downgrade():
    for name in ('character_reference_assets','character_reference_sets','screenplay_project_name_claims','screenplay_documents','screenplay_generation_jobs','screenplay_input_assets'):op.drop_table(name)
