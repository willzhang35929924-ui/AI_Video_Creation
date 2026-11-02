"""Clean VideoFactory compose data; called only after explicit confirmation."""
import os, shutil, sqlite3, sys
from pathlib import Path

KEEP={'alembic_version','ai_services','generation_workflows','model_directories','local_models','model_lab_defaults','publishing_accounts','publishing_presets','content_templates','route_policies','quality_profiles'}
DELETE={'approvals','assemblies','assembly_renders','audio_assets','audio_events','audit_events','character_aliases','character_assets','character_batches','character_generation_jobs','character_reference_assets','character_reference_sets','character_versions','characters','content_notes','deletion_tasks','dialogue_lines','final_compositions','generation_jobs','managed_files','media_process_tasks','model_experiments','product_images','production_entities','production_runs','production_visuals','products','project_product_bindings','project_production_preferences','project_service_bindings','projects','publish_batches','quality_checks','quality_renders','reference_videos','scenes','screenplay_documents','screenplay_generation_jobs','screenplay_input_assets','screenplay_project_name_claims','script_revisions','shot_characters','shot_details','shot_product_bindings','source_documents','storyboard_shots','studio_decisions','studio_runs','style_profiles','video_analyses','video_revisions','voice_library_assets','word_imports'}
ROOT=Path('/app/data'); db_path=ROOT/'video_factory.db'
dry='--dry-run' in sys.argv
if not db_path.is_file(): sys.exit('未找到数据库，未执行清理。')
source=sqlite3.connect(str(db_path),timeout=15)
if dry:
    db=sqlite3.connect(':memory:'); source.backup(db); source.close()
else: db=source
tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
if tables != KEEP | DELETE: sys.exit('数据库结构与已验证版本不同，已停止，未删除。')
version=db.execute('SELECT version_num FROM alembic_version').fetchone()
if not version or version[0]!='0012': sys.exit('数据库版本不匹配，已停止，未删除。')
if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': sys.exit('数据库完整性异常，已停止，未删除。')
counts={t:db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in sorted(DELETE)}
print('拟删除记录总数:',sum(counts.values()),flush=True)
db.execute('PRAGMA foreign_keys=OFF'); db.execute('PRAGMA secure_delete=ON'); db.execute('BEGIN IMMEDIATE')
try:
    for t in sorted(DELETE): db.execute('DELETE FROM "'+t+'"')
    for t in ('route_policies','quality_profiles'): db.execute('DELETE FROM "'+t+'" WHERE scope != ?',('system',))
    bad=db.execute('PRAGMA foreign_key_check').fetchall()
    if bad: raise RuntimeError('保留配置引用了将被删除的记录，已回滚；请先人工检查。')
    if dry: db.rollback(); print('数据库模拟检查通过；没有删除任何内容。'); sys.exit(0)
    db.commit()
    for t in DELETE:
        if db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0]: raise RuntimeError('数据库清理验证失败：'+t)
    for t in ('route_policies','quality_profiles'):
        if db.execute('SELECT count(*) FROM "'+t+'" WHERE scope != ?',('system',)).fetchone()[0]: raise RuntimeError('项目配置清理验证失败：'+t)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('VACUUM')
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('清理后的数据库完整性检查失败')
    db.close()
except BaseException:
    db.rollback(); db.close(); raise
errors=[]
for directory in [ROOT/'media',ROOT/'tmp',ROOT/'home',ROOT/'schema_backups',Path('/app/logs'),Path('/app/reports')]:
    try:
        if directory.is_symlink(): raise RuntimeError('目录是符号链接')
        if not directory.exists(): continue
        for item in directory.iterdir():
            try:
                if item.is_symlink() or not item.is_dir(): item.unlink()
                else: shutil.rmtree(item)
            except Exception as exc: errors.append(str(item)+': '+str(exc))
        remaining=list(directory.iterdir())
        if remaining: errors.append(str(directory)+': 仍有 '+str(len(remaining))+' 个项目')
    except Exception as exc: errors.append(str(directory)+': '+str(exc))
if errors:
    print('数据库已清理，但以下文件清理失败：',*errors,sep='\n',file=sys.stderr); sys.exit(1)
print('项目记录和指定数据目录已清理；全局配置与密钥已保留。')
print('注意：SSD、快照、备份和远端文件无法通过此操作保证不可恢复。')
