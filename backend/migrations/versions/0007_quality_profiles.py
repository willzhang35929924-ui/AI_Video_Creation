"""Add output quality profiles and immutable processing versions; retain existing tables."""
from alembic import op
import sqlalchemy as s
revision = '0007'
down_revision = '0006'
branch_labels = depends_on = None

def upgrade():
    timestamps = lambda: [s.Column('created_at', s.DateTime(timezone=True), nullable=False), s.Column('updated_at', s.DateTime(timezone=True), nullable=False)]
    op.create_table('quality_profiles', s.Column('id', s.String(64), primary_key=True),
        s.Column('scope', s.String(20), nullable=False), s.Column('target_id', s.String(64), nullable=False),
        s.Column('settings_json', s.JSON, nullable=False), s.Column('revision', s.Integer, nullable=False),
        *timestamps(), s.UniqueConstraint('scope','target_id',name='uq_quality_scope_target'))
    op.create_table('quality_renders', s.Column('id', s.String(64), primary_key=True),
        s.Column('source_video_id', s.String(64), s.ForeignKey('video_revisions.id'), nullable=False),
        s.Column('video_id', s.String(64), s.ForeignKey('video_revisions.id')),
        s.Column('status', s.String(32), nullable=False), s.Column('snapshot_json', s.JSON, nullable=False),
        s.Column('result_json', s.JSON, nullable=False), s.Column('relative_path', s.Text), s.Column('error_message', s.Text), *timestamps())
    for name in ['source_video_id','status']: op.create_index('ix_quality_renders_'+name,'quality_renders',[name])

def downgrade():
    op.drop_table('quality_renders'); op.drop_table('quality_profiles')
