"""Unified durable studio DAG and shot decisions, additive only."""
from alembic import op
import sqlalchemy as s
revision = '0006'
down_revision = '0005'
branch_labels = depends_on = None


def common(name, *fields):
    op.create_table(name, s.Column('id', s.String(64), primary_key=True), *fields,
                    s.Column('created_at', s.DateTime(timezone=True), nullable=False),
                    s.Column('updated_at', s.DateTime(timezone=True), nullable=False))


def field(name, kind=s.Text, nullable=False, fk=None):
    return s.Column(name, kind, *([s.ForeignKey(fk)] if fk else []), nullable=nullable)


def upgrade():
    common('studio_runs', field('project_id', s.String(64), fk='projects.id'),
           field('script_revision_id', s.String(64), True, 'script_revisions.id'),
           field('status', s.String(40)), field('snapshot_json', s.JSON),
           field('nodes_json', s.JSON), field('result_json', s.JSON), field('error_message', nullable=True))
    common('studio_decisions', field('project_id', s.String(64), fk='projects.id'),
           field('shot_id', s.String(64), fk='storyboard_shots.id'),
           field('video_id', s.String(64), True, 'video_revisions.id'),
           field('action', s.String(24)), field('position', s.Integer), field('trim_start', s.Float),
           field('trim_end', s.Float, True), field('instruction'), field('revision', s.Integer),
           s.UniqueConstraint('project_id', 'shot_id', name='uq_studio_project_shot'))
    for table, columns in {'studio_runs': ['project_id', 'status'], 'studio_decisions': ['project_id', 'shot_id']}.items():
        for col in columns: op.create_index('ix_'+table+'_'+col, table, [col])


def downgrade():
    op.drop_table('studio_decisions')
    op.drop_table('studio_runs')
