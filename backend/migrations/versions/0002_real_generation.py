"""Persist source provenance, workflows, reference IDs and final composition."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("storyboard_shots", sa.Column("reference_asset_ids_json", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table("source_documents", sa.Column("id", sa.String(64), primary_key=True), sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("filename", sa.Text()), sa.Column("source_uri", sa.Text()), sa.Column("original_text", sa.Text(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_source_documents_project_id", "source_documents", ["project_id"])
    op.create_table("generation_workflows", sa.Column("id", sa.String(64), primary_key=True), sa.Column("source_url", sa.Text(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("graph_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("final_compositions", sa.Column("id", sa.String(64), primary_key=True), sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("video_ids_json", sa.JSON(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("relative_path", sa.Text()), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_final_compositions_project_id", "final_compositions", ["project_id"])


def downgrade():
    op.drop_table("final_compositions")
    op.drop_table("generation_workflows")
    op.drop_table("source_documents")
    op.drop_column("storyboard_shots", "reference_asset_ids_json")
