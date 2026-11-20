import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, String, JSON, DateTime, ForeignKey, inspect, text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATA = Path(os.getenv('DATA_DIR', './data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
engine = create_engine(os.getenv('DATABASE_URL', 'sqlite:///./data/clips.db'))
Session = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Source(Base):
    __tablename__ = 'sources'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str]
    youtube_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    duration: Mapped[float] = mapped_column(default=0)
    prompt: Mapped[str] = mapped_column(default='wild chases and funny crashes')
    status: Mapped[str] = mapped_column(default='queued')
    error: Mapped[str | None] = mapped_column(nullable=True)
    words: Mapped[list] = mapped_column(JSON, default=list)
    analysis_cache: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    layout_pending: Mapped[bool] = mapped_column(default=False, server_default='false')
    use_browser_cookies: Mapped[bool] = mapped_column(default=False, server_default='false')
    dual_crop: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')

class Clip(Base):
    __tablename__ = 'clips'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.id'))
    title: Mapped[str]
    start: Mapped[float]
    end: Mapped[float]
    score: Mapped[float] = mapped_column(default=0)
    reason: Mapped[str] = mapped_column(default='Manual selection')
    captions: Mapped[list] = mapped_column(JSON, default=list)
    framing: Mapped[str] = mapped_column(default='fit')
    dual_crop: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    crop_x: Mapped[float] = mapped_column(default=0.5, server_default='0.5')
    crop_y: Mapped[float] = mapped_column(default=0.5, server_default='0.5')
    crop_segments: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    caption_style: Mapped[str] = mapped_column(default='highlight', server_default='highlight')
    caption_scale: Mapped[float] = mapped_column(default=1, server_default='1')
    caption_position: Mapped[str] = mapped_column(default='bottom', server_default='bottom')
    volume: Mapped[float] = mapped_column(default=1, server_default='1')
    audio_muted: Mapped[bool] = mapped_column(default=False, server_default='false')
    status: Mapped[str] = mapped_column(default='review')
    preview: Mapped[str | None] = mapped_column(nullable=True)
    export: Mapped[str | None] = mapped_column(nullable=True)

class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str]
    target: Mapped[str]
    status: Mapped[str] = mapped_column(default='queued', index=True)
    error: Mapped[str | None] = mapped_column(nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Publication(Base):
    __tablename__ = 'publications'
    clip_id: Mapped[str] = mapped_column(ForeignKey('clips.id'), primary_key=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(default='studio_export')
    youtube_id: Mapped[str | None] = mapped_column(nullable=True)

class Rejection(Base):
    __tablename__ = 'rejections'
    clip_id: Mapped[str] = mapped_column(ForeignKey('clips.id'), primary_key=True)
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

class SourceProgress(Base):
    __tablename__ = 'source_progress'
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.id'), primary_key=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

class IntegrationSecret(Base):
    __tablename__ = 'integration_secrets'
    name: Mapped[str] = mapped_column(String, primary_key=True)
    cipher: Mapped[str]

class CleanupPolicy(Base):
    __tablename__ = 'cleanup_policy'
    id: Mapped[int] = mapped_column(primary_key=True)
    clips: Mapped[bool] = mapped_column(default=False)
    sources: Mapped[bool] = mapped_column(default=False)

class PublishedHistory(Base):
    __tablename__ = 'published_history'
    clip_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str]
    title: Mapped[str]
    youtube_id: Mapped[str]
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class OAuthAttempt(Base):
    __tablename__ = 'oauth_attempts'
    state_hash: Mapped[str] = mapped_column(String, primary_key=True)
    browser_hash: Mapped[str]
    verifier_cipher: Mapped[str]
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class YouTubeUpload(Base):
    __tablename__ = 'youtube_uploads'
    clip_id: Mapped[str] = mapped_column(ForeignKey('clips.id'), primary_key=True)
    channel_id: Mapped[str]
    mode: Mapped[str] = mapped_column(default='scheduled')
    status: Mapped[str] = mapped_column(default='pending', index=True)
    session_cipher: Mapped[str | None] = mapped_column(nullable=True)
    offset: Mapped[int] = mapped_column(default=0)
    file_size: Mapped[int] = mapped_column(default=0)
    fingerprint: Mapped[str | None] = mapped_column(nullable=True)
    youtube_id: Mapped[str | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote: Mapped[dict] = mapped_column(JSON, default=dict)

QUERY_INDEXES = [Index('ix_jobs_claim', Job.status, Job.created),
                 Index('ix_jobs_target_status', Job.target, Job.status),
                 Index('ix_clips_source_id', Clip.source_id),
                 Index('ix_uploads_ready', YouTubeUpload.channel_id, YouTubeUpload.status, YouTubeUpload.next_attempt)]

def init():
    Base.metadata.create_all(engine)
    # Additive migration preserves existing clips and their original appearance.
    with engine.begin() as connection:
        for table in ('sources', 'clips'):
            existing = {c['name'] for c in inspect(connection).get_columns(table)}
            if 'dual_crop' not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN dual_crop JSON NOT NULL DEFAULT '{{}}'"))
            if table == 'sources' and 'layout_pending' not in existing:
                connection.execute(text('ALTER TABLE sources ADD COLUMN layout_pending BOOLEAN NOT NULL DEFAULT false'))
            if table == 'sources' and 'use_browser_cookies' not in existing:
                connection.execute(text('ALTER TABLE sources ADD COLUMN use_browser_cookies BOOLEAN NOT NULL DEFAULT false'))
            if table == 'sources' and 'analysis_cache' not in existing:
                connection.execute(text("ALTER TABLE sources ADD COLUMN analysis_cache JSON NOT NULL DEFAULT '{}'"))
        columns = {c['name'] for c in inspect(connection).get_columns('clips')}
        for name, definition in [('crop_x', 'FLOAT NOT NULL DEFAULT 0.5'), ('crop_y', 'FLOAT NOT NULL DEFAULT 0.5'), ('caption_style', "VARCHAR NOT NULL DEFAULT 'highlight'"), ('caption_scale','FLOAT NOT NULL DEFAULT 1'), ('caption_position',"VARCHAR NOT NULL DEFAULT 'bottom'"), ('volume','FLOAT NOT NULL DEFAULT 1'), ('audio_muted','BOOLEAN NOT NULL DEFAULT false')]:
            if name not in columns:
                connection.execute(text(f'ALTER TABLE clips ADD COLUMN {name} {definition}'))
        if 'crop_segments' not in columns:
            connection.execute(text("ALTER TABLE clips ADD COLUMN crop_segments JSON NOT NULL DEFAULT '[]'"))
        for index in QUERY_INDEXES:
            index.create(connection, checkfirst=True)
