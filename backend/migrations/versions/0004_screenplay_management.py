"""Additive screenplay relations and content lifecycle. No old column is removed."""
from alembic import op
import sqlalchemy as s

revision = '0004'
down_revision = '0003'
branch_labels = depends_on = None


def table(name, *columns, constraints=()):
    op.create_table(name, s.Column('id', s.String(64), primary_key=True), *columns,
                    s.Column('created_at', s.DateTime(timezone=True), nullable=False),
                    s.Column('updated_at', s.DateTime(timezone=True), nullable=False), *constraints)


def col(name, kind=s.Text, nullable=False, fk=None):
    return s.Column(name, kind, *([s.ForeignKey(fk)] if fk else []), nullable=nullable)


def upgrade():
    table('scenes', col('project_id', s.String(64), fk='projects.id'), col('script_revision_id', s.String(64), True, 'script_revisions.id'), col('order', s.Integer), col('name', s.String(240)), col('environment_json', s.JSON), col('source_json', s.JSON))
    table('shot_details', col('shot_id', s.String(64), fk='storyboard_shots.id'), col('scene_id', s.String(64), True, 'scenes.id'), col('photography_json', s.JSON), col('source_json', s.JSON), constraints=[s.UniqueConstraint('shot_id')])
    table('shot_characters', col('shot_id', s.String(64), fk='storyboard_shots.id'), col('character_id', s.String(64), fk='characters.id'), col('character_version_id', s.String(64), True, 'character_versions.id'), col('order', s.Integer), col('reference_asset_ids_json', s.JSON), col('performance_json', s.JSON), col('source_json', s.JSON), constraints=[s.UniqueConstraint('shot_id', 'character_id')])
    table('dialogue_lines', col('shot_id', s.String(64), fk='storyboard_shots.id'), col('participant_id', s.String(64), fk='shot_characters.id'), col('order', s.Integer), col('text'), col('delivery_json', s.JSON), col('source_json', s.JSON))
    table('audio_events', col('scene_id', s.String(64), True, 'scenes.id'), col('shot_id', s.String(64), True, 'storyboard_shots.id'), col('kind', s.String(30)), col('description'), col('start_seconds', s.Float), col('end_seconds', s.Float, True), col('volume', s.Float), col('relative_path', nullable=True), col('source_json', s.JSON))
    table('character_aliases', col('character_id', s.String(64), fk='characters.id'), col('name', s.String(160)))
    table('word_imports', col('filename', s.String(240)), col('sha256', s.String(64)), col('status', s.String(32)), col('relative_path'), col('blocks_json', s.JSON), col('preview_json', s.JSON), col('result_json', s.JSON), col('error_message', nullable=True), col('revision', s.Integer))
    table('content_templates', col('name', s.String(200)), col('kind', s.String(40)), col('body'))
    table('managed_files', col('relative_path'), col('state', s.String(32)), col('ownership_json', s.JSON), col('metadata_json', s.JSON), constraints=[s.UniqueConstraint('relative_path')])
    table('deletion_tasks', col('status', s.String(40)), col('mode', s.String(20)), col('fingerprint', s.String(64)), col('plan_json', s.JSON), col('result_json', s.JSON), col('error_message', nullable=True), constraints=[s.UniqueConstraint('fingerprint')])
    table('content_notes', col('resource', s.String(64)), col('record_id', s.String(64)), col('label', s.String(240)), col('notes'), constraints=[s.UniqueConstraint('resource', 'record_id')])
    for name, columns in {'scenes':['project_id'], 'shot_details':['scene_id'], 'shot_characters':['shot_id','character_id'], 'dialogue_lines':['shot_id','participant_id'], 'audio_events':['scene_id','shot_id'], 'character_aliases':['character_id','name'], 'word_imports':['sha256','status'], 'deletion_tasks':['status']}.items():
        for column in columns:
            op.create_index('ix_'+name+'_'+column, name, [column])
    # Stable IDs make the backfill inspectable and idempotent. Source text and
    # approvals stay untouched; speaker attribution is only made for old bound shots.
    op.execute("""INSERT INTO scenes (id,project_id,script_revision_id,"order",name,environment_json,source_json,created_at,updated_at)
       SELECT 'scene_legacy_'||id, project_id, NULL, "order", CASE WHEN location='' THEN scene_title ELSE location END, json_object('location',location,'lighting',lighting), '{"migration":"0004","inferred": true}', CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM storyboard_shots""")
    op.execute("""INSERT INTO shot_details (id,shot_id,scene_id,photography_json,source_json,created_at,updated_at)
       SELECT 'detail_legacy_'||id,id,'scene_legacy_'||id,'{}','{"migration":"0004"}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM storyboard_shots""")
    op.execute("""INSERT INTO shot_characters (id,shot_id,character_id,character_version_id,"order",reference_asset_ids_json,performance_json,source_json,created_at,updated_at)
       SELECT 'part_legacy_'||id,id,character_id,character_asset_version_id,1,reference_asset_ids_json,json_object('action',action),'{"migration":"0004","legacy_action_on_shot": true}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM storyboard_shots WHERE character_id IS NOT NULL""")
    op.execute("""INSERT INTO dialogue_lines (id,shot_id,participant_id,"order",text,delivery_json,source_json,created_at,updated_at)
       SELECT 'line_legacy_'||id,id,'part_legacy_'||id,1,dialogue,'{}','{"migration":"0004","speaker_inferred": true}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM storyboard_shots WHERE character_id IS NOT NULL AND dialogue<>''""")


def downgrade():
    # Only new tables are removed. Back up before downgrade to retain new content.
    for name in ['content_notes','deletion_tasks','managed_files','content_templates','word_imports','character_aliases','audio_events','dialogue_lines','shot_characters','shot_details','scenes']:
        op.drop_table(name)
