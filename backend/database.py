"""数据库操作"""
import sqlite3
import hashlib
import logging
from config import DB_FILE

logger = logging.getLogger(__name__)


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    # 并发占用分享下载配额时避免立即报 database is locked
    conn.execute('PRAGMA busy_timeout = 5000')
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _column_exists(cursor, table, column):
    cursor.execute(f'PRAGMA table_info({table})')
    return any(row['name'] == column for row in cursor.fetchall())


def init_db():
    """初始化数据库表"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 兼容旧库：新增校验和列，用于检测文件是否被替换
    if not _column_exists(cursor, 'files', 'checksum'):
        cursor.execute('ALTER TABLE files ADD COLUMN checksum TEXT')
    if not _column_exists(cursor, 'files', 'mtime'):
        cursor.execute('ALTER TABLE files ADD COLUMN mtime REAL')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_links (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            expires_at REAL,
            max_downloads INTEGER,
            download_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    ''')

    # 受控下载票：prepare → fetch（可多次 Range 续传）→ confirm
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_tickets (
            ticket TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            username TEXT,
            share_id TEXT,
            status TEXT NOT NULL DEFAULT 'prepared',
            size INTEGER NOT NULL,
            checksum TEXT,
            claimed INTEGER NOT NULL DEFAULT 0,
            downloaded INTEGER NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_tickets_expires ON download_tickets (expires_at)'
    )

    default_users = [
        ('admin', hashlib.sha256('admin123'.encode()).hexdigest()),
        ('user', hashlib.sha256('user123'.encode()).hexdigest()),
        ('test', hashlib.sha256('test123'.encode()).hexdigest())
    ]
    for username, password_hash in default_users:
        cursor.execute(
            'INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?)',
            (username, password_hash)
        )

    conn.commit()
    conn.close()


def compute_file_checksum(path, chunk_size=1024 * 1024):
    """计算文件 SHA-256 校验和"""
    digest = hashlib.sha256()
    with open(path, 'rb') as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
