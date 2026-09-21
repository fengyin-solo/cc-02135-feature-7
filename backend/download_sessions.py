"""受控下载会话管理

会话状态机：
    prepared(已准备) -> transferring(传输中) -> completed(已完成)
                     -> failed(已失败)
    非终态会话超过 expires_at 后视为 expired(已过期，惰性判定)

设计要点：
- 文件快照(size + mtime)在会话创建时记录，传输/确认前重新比对，
  文件被替换时会话失效，避免基于旧内容产生错误的完成状态。
- 分享下载的成功计数只在 complete_session 确认后原子增加，
  且重复确认幂等，保证"成功下载次数与状态一致"。
"""
import os
import time
import uuid
import logging
from database import get_db
from config import DOWNLOAD_SESSION_EXPIRE_SECONDS, DOWNLOAD_HISTORY_LIMIT

logger = logging.getLogger(__name__)

# 会话状态
STATUS_PREPARED = 'prepared'
STATUS_TRANSFERRING = 'transferring'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
STATUS_EXPIRED = 'expired'  # 惰性判定，不落库

ACTIVE_STATUSES = (STATUS_PREPARED, STATUS_TRANSFERRING)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED)


def _now():
    return time.time()


def cleanup_expired_sessions():
    """清理过期且未完成的会话（惰性触发，避免表无限增长）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'DELETE FROM download_sessions WHERE expires_at < ? AND status NOT IN (?, ?)',
        (_now(), STATUS_COMPLETED, STATUS_FAILED)
    )
    conn.commit()
    conn.close()


def create_session(file_id, file_name, file_size, file_mtime, share_id=None, username=None):
    """创建下载会话，返回会话字典"""
    session_id = uuid.uuid4().hex
    now = _now()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO download_sessions
        (id, file_id, share_id, username, status, file_name, file_size, file_mtime,
         bytes_confirmed, created_at, updated_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
    ''', (session_id, file_id, share_id, username, STATUS_PREPARED,
          file_name, file_size, file_mtime, now, now, now + DOWNLOAD_SESSION_EXPIRE_SECONDS))
    conn.commit()
    conn.close()

    logger.info(f"下载会话创建: {session_id} 文件 {file_name} (ID: {file_id}, "
                f"分享: {share_id or '-'}, 用户: {username or '访客'})")
    return get_session(session_id)


def get_session(session_id):
    """按 ID 获取会话（不存在返回 None）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM download_sessions WHERE id = ?', (session_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def effective_status(session):
    """计算会话的有效状态（非终态且超时视为 expired）"""
    if session['status'] in ACTIVE_STATUSES and session['expires_at'] < _now():
        return STATUS_EXPIRED
    return session['status']


def check_file_snapshot(session, file_path):
    """比对文件快照，检测文件是否被替换

    返回 (ok, error_code)。ok 为 False 时 error_code 为 'file_changed' 或 'file_missing'。
    """
    if not os.path.exists(file_path):
        return False, 'file_missing'
    stat = os.stat(file_path)
    if stat.st_size != session['file_size'] or abs(stat.st_mtime - session['file_mtime']) > 1e-6:
        return False, 'file_changed'
    return True, None


def mark_transferring(session_id):
    """会话进入传输状态（幂等：仅 prepared -> transferring）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE download_sessions SET status = ?, updated_at = ?
        WHERE id = ? AND status = ?
    ''', (STATUS_TRANSFERRING, _now(), session_id, STATUS_PREPARED))
    conn.commit()
    conn.close()


def fail_session(session_id, reason):
    """标记会话失败（仅活动状态可转失败）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE download_sessions SET status = ?, failure_reason = ?, updated_at = ?
        WHERE id = ? AND status IN (?, ?)
    ''', (STATUS_FAILED, reason, _now(), session_id, STATUS_PREPARED, STATUS_TRANSFERRING))
    conn.commit()
    conn.close()


def complete_session(session_id, received_bytes):
    """确认会话完成（幂等）

    返回 (result, error_dict, http_status)。
    成功时 result 为最新会话字典，error_dict 为 None；
    失败时 result 为 None。重复确认同一会话不会重复计数。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM download_sessions WHERE id = ?', (session_id,))
        row = cursor.fetchone()
        if not row:
            return None, {'error': '下载会话不存在'}, 404

        session = dict(row)

        # 幂等：已完成的会话直接返回成功，不重复计数
        if session['status'] == STATUS_COMPLETED:
            return session, None, None

        if session['status'] == STATUS_FAILED:
            return None, {'error': '下载会话已失败', 'code': 'session_failed'}, 409

        if session['expires_at'] < _now():
            return None, {'error': '下载会话已过期，请重新发起下载', 'code': 'session_expired'}, 410

        # 确认的字节数必须与文件大小一致，否则说明传输不完整
        if received_bytes is None or received_bytes != session['file_size']:
            return None, {
                'error': '接收字节数与文件大小不一致，传输不完整',
                'code': 'incomplete_transfer',
                'expected': session['file_size'],
                'received': received_bytes
            }, 400

        # 分享下载：原子占用下载名额，并发下也不会超过 max_downloads
        if session['share_id']:
            cursor.execute('''
                UPDATE share_links SET download_count = download_count + 1
                WHERE id = ? AND (max_downloads IS NULL OR download_count < max_downloads)
            ''', (session['share_id'],))
            if cursor.rowcount == 0:
                cursor.execute('''
                    UPDATE download_sessions SET status = ?, failure_reason = ?, updated_at = ?
                    WHERE id = ?
                ''', (STATUS_FAILED, '分享链接下载次数已用完', _now(), session_id))
                conn.commit()
                return None, {'error': '分享链接下载次数已用完', 'code': 'quota_exceeded'}, 409

        now = _now()
        cursor.execute('''
            UPDATE download_sessions
            SET status = ?, bytes_confirmed = ?, completed_at = ?, updated_at = ?
            WHERE id = ? AND status IN (?, ?)
        ''', (STATUS_COMPLETED, received_bytes, now, now, session_id,
              STATUS_PREPARED, STATUS_TRANSFERRING))
        conn.commit()

        session['status'] = STATUS_COMPLETED
        session['bytes_confirmed'] = received_bytes
        session['completed_at'] = now
        logger.info(f"下载会话完成: {session_id} (文件: {session['file_name']}, "
                    f"分享: {session['share_id'] or '-'})")
        return session, None, None
    finally:
        conn.close()


def list_recent_sessions(username, limit=None):
    """获取用户最近的下载会话（用于历史状态展示）"""
    if limit is None:
        limit = DOWNLOAD_HISTORY_LIMIT
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, file_id, share_id, status, file_name, file_size,
               bytes_confirmed, failure_reason, created_at, updated_at,
               expires_at, completed_at
        FROM download_sessions
        WHERE username = ?
        ORDER BY updated_at DESC
        LIMIT ?
    ''', (username, limit))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    for row in rows:
        row['status'] = effective_status(row)
    return rows
