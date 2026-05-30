"""insta_posts 테이블 생성 + 인덱스.

실행: python migrate_instagram.py

models.py 의 InstaPost 정의와 정합되도록 컬럼 추가. 멱등 — 이미 있으면 건너뜀.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/app.db")


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS insta_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME,

    content_type VARCHAR(20) NOT NULL,
    source_cluster_id INTEGER,
    topic_key VARCHAR(80),

    title VARCHAR(300) NOT NULL,
    slides JSON NOT NULL DEFAULT '[]',

    caption TEXT NOT NULL DEFAULT '',
    hashtags JSON NOT NULL DEFAULT '[]',

    image_paths JSON NOT NULL DEFAULT '[]',
    rendered_at DATETIME,

    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    scheduled_at DATETIME,
    posted_at DATETIME,
    approved BOOLEAN NOT NULL DEFAULT 0,

    ig_media_id VARCHAR(80),
    ig_permalink VARCHAR(500),

    error TEXT,

    FOREIGN KEY (source_cluster_id) REFERENCES clusters(id)
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_insta_posts_created_at ON insta_posts(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_insta_posts_content_type ON insta_posts(content_type)",
    "CREATE INDEX IF NOT EXISTS ix_insta_posts_status ON insta_posts(status)",
    "CREATE INDEX IF NOT EXISTS ix_insta_posts_scheduled_at ON insta_posts(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS ix_insta_posts_source_cluster_id ON insta_posts(source_cluster_id)",
]


def main():
    if not DB_PATH.exists():
        print(f"DB 파일이 없습니다: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='insta_posts'")
    if cur.fetchone():
        print("[OK] insta_posts 테이블 이미 존재. 누락 컬럼만 확인.")
        cur.execute("PRAGMA table_info(insta_posts)")
        existing = {row[1] for row in cur.fetchall()}
        # 미래 추가 컬럼이 있으면 여기에서 ALTER 처리
        wanted = {
            "ig_permalink": "VARCHAR(500)",
            "approved": "BOOLEAN NOT NULL DEFAULT 0",
            "topic_key": "VARCHAR(80)",
            "rendered_at": "DATETIME",
        }
        for col, ddl in wanted.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE insta_posts ADD COLUMN {col} {ddl}")
                print(f"  + col added: {col}")
    else:
        cur.execute(CREATE_SQL)
        print("[OK] insta_posts 테이블 생성.")

    for sql in INDEXES:
        cur.execute(sql)
    print("[OK] 인덱스 보강 완료.")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
