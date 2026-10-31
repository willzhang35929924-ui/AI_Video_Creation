"""Versioned cinematic designs, visuals and orchestration; no legacy writes."""
from alembic import op
import sqlalchemy as s
revision='0005'
down_revision='0004'
branch_labels=depends_on=None


def common(name,*columns):
    op.create_table(name,s.Column('id',s.String(64),primary_key=True),*columns,s.Column('created_at',s.DateTime(timezone=True),nullable=False),s.Column('updated_at',s.DateTime(timezone=True),nullable=False))


def field(name,type=s.Text,nullable=False,fk=None):return s.Column(name,type,*([s.ForeignKey(fk)] if fk else []),nullable=nullable)


def upgrade():
    common('production_entities',field('project_id',s.String(64),fk='projects.id'),field('character_id',s.String(64),True,'characters.id'),field('scene_id',s.String(64),True,'scenes.id'),field('logical_id',s.String(100)),field('kind',s.String(32)),field('version',s.Integer),field('name',s.String(240)),field('status',s.String(32)),field('spec_json',s.JSON),field('source_json',s.JSON),s.UniqueConstraint('project_id','logical_id','version'))
    common('production_visuals',field('project_id',s.String(64),fk='projects.id'),field('scene_id',s.String(64),True,'scenes.id'),field('shot_id',s.String(64),True,'storyboard_shots.id'),field('entity_id',s.String(64),True,'production_entities.id'),field('kind',s.String(32)),field('status',s.String(32)),field('prompt'),field('snapshot_json',s.JSON),field('provider_job_id',s.String(200),True),field('relative_path',nullable=True),field('sha256',s.String(64),True),field('review_json',s.JSON),field('error_message',nullable=True))
    common('production_runs',field('project_id',s.String(64),fk='projects.id'),field('script_revision_id',s.String(64),fk='script_revisions.id'),field('status',s.String(40)),field('snapshot_json',s.JSON),field('steps_json',s.JSON),field('result_json',s.JSON),field('error_message',nullable=True))
    for table,fields in {'production_entities':['project_id'],'production_visuals':['project_id','status'],'production_runs':['project_id','status']}.items():
        for col in fields:op.create_index('ix_'+table+'_'+col,table,[col])


def downgrade():
    for name in ['production_runs','production_visuals','production_entities']:op.drop_table(name)
