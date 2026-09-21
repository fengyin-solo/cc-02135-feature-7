"""文件路由"""
import os
import re
import uuid
import time
import logging
from flask import request, jsonify, send_file
from werkzeug.utils import secure_filename
from routes import files_bp
from database import get_db
from auth import verify_token, get_username_from_token, login_required
from config import UPLOAD_FOLDER, MAX_FILE_SIZE, BLOCKED_EXTENSIONS, SHARE_LINK_EXPIRE_HOURS, SHARE_LINK_MAX_DOWNLOADS
from download_sessions import (
    ACTIVE_STATUSES,
    create_session, get_session, effective_status, check_file_snapshot,
    mark_transferring, fail_session, complete_session,
    list_recent_sessions, cleanup_expired_sessions,
)

logger = logging.getLogger(__name__)


def allowed_file(filename):
    """检查文件扩展名是否被禁止"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext not in BLOCKED_EXTENSIONS


@files_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件类型'}), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_FILE_SIZE:
        return jsonify({'error': f'文件大小超过限制（最大{MAX_FILE_SIZE // 1024 // 1024}MB）'}), 400

    file_id = str(uuid.uuid4())
    # 保留原始文件名用于显示（去掉路径分隔符防止注入）
    original_name = re.sub(r'[/\\]', '_', file.filename).strip()
    if not original_name:
        original_name = file_id

    # 磁盘上用 UUID + 扩展名存储，避免文件名编码问题
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    safe_filename = f"{file_id}.{ext}" if ext else file_id
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    file_size = os.path.getsize(filepath)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO files (id, name, path, size) VALUES (?, ?, ?, ?)',
        (file_id, original_name, filepath, file_size)
    )
    conn.commit()
    conn.close()

    logger.info(f"文件上传成功: {original_name} (ID: {file_id}, 大小: {file_size} bytes)")
    return jsonify({'success': True, 'file_id': file_id, 'filename': original_name})


@files_bp.route('/api/files', methods=['GET'])
def list_files():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path, size FROM files')
    files = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(files)


@files_bp.route('/api/download/<file_id>', methods=['GET'])
def download_file(file_id):
    # 优先从 Authorization 头获取 token，兼容查询参数（已废弃）
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    else:
        token = request.args.get('token')  # 向后兼容，建议前端迁移到 Authorization 头

    if not token or not verify_token(token):
        return jsonify({'error': '未授权或token已过期'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info:
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    logger.info(f"文件下载: {file_info['name']} (ID: {file_id})")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


def generate_short_id():
    """生成短的分享链接ID"""
    return uuid.uuid4().hex[:12]


def get_share_link_info(share_id):
    """获取分享链接信息，包含文件信息和有效性检查"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               f.name as filename, f.size as filesize
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.id = ?
    ''', (share_id,))
    share = cursor.fetchone()
    conn.close()
    return share


def is_share_valid(share):
    """检查分享链接是否有效"""
    if not share:
        return False, '分享链接不存在'

    if share['expires_at'] is not None and share['expires_at'] < time.time():
        return False, '分享链接已过期'

    if share['max_downloads'] is not None and share['download_count'] >= share['max_downloads']:
        return False, '分享链接下载次数已用完'

    return True, None


def increment_download_count(share_id):
    """原子增加下载次数（不超过 max_downloads），返回是否成功"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE share_links SET download_count = download_count + 1
        WHERE id = ? AND (max_downloads IS NULL OR download_count < max_downloads)
    ''', (share_id,))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success


def get_token_from_request():
    """从请求中获取 token"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.args.get('token')


@files_bp.route('/api/share', methods=['POST'])
@login_required
def create_share():
    """创建分享链接"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400

    file_id = data.get('file_id', '').strip()
    expire_hours = data.get('expire_hours')
    max_downloads = data.get('max_downloads')

    if not file_id:
        return jsonify({'error': '文件ID不能为空'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()

    if not file_info:
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    if expire_hours is None:
        expire_hours = SHARE_LINK_EXPIRE_HOURS

    if expire_hours < 0:
        expire_hours = None

    if expire_hours is not None:
        expires_at = time.time() + expire_hours * 3600
    else:
        expires_at = None

    if max_downloads is None:
        max_downloads = SHARE_LINK_MAX_DOWNLOADS

    if max_downloads < 0:
        max_downloads = None

    token = get_token_from_request()
    username = get_username_from_token(token)

    share_id = generate_short_id()

    cursor.execute('''
        INSERT INTO share_links (id, file_id, created_by, expires_at, max_downloads)
        VALUES (?, ?, ?, ?, ?)
    ''', (share_id, file_id, username, expires_at, max_downloads))

    conn.commit()
    conn.close()

    logger.info(f"分享链接创建成功: 文件 {file_info['name']}, 分享ID {share_id}, 创建者 {username}")

    return jsonify({
        'success': True,
        'share_id': share_id,
        'expires_at': expires_at,
        'max_downloads': max_downloads,
        'filename': file_info['name']
    })


@files_bp.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    """获取分享链接信息（公开访问）"""
    share = get_share_link_info(share_id)
    valid, error_msg = is_share_valid(share)

    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    share_data = {
        'share_id': share['id'],
        'filename': share['filename'],
        'filesize': share['filesize'],
        'created_by': share['created_by'],
        'expires_at': share['expires_at'],
        'max_downloads': share['max_downloads'],
        'download_count': share['download_count'],
        'created_at': share['created_at'],
        'is_valid': valid,
        'error_msg': error_msg
    }

    return jsonify(share_data)


@files_bp.route('/api/share/<share_id>/download', methods=['GET'])
def download_by_share(share_id):
    """通过分享链接下载文件（公开访问）"""
    share = get_share_link_info(share_id)
    valid, error_msg = is_share_valid(share)

    if not valid:
        return jsonify({'error': error_msg}), 404

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (share['file_id'],))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info:
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    if not increment_download_count(share_id):
        return jsonify({'error': '分享链接下载次数已用完'}), 404

    logger.info(f"分享下载: 文件 {file_info['name']}, 分享ID {share_id}, 下载次数 {share['download_count'] + 1}")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


@files_bp.route('/api/shares', methods=['GET'])
@login_required
def list_shares():
    """获取当前用户的所有分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               f.name as filename, f.size as filesize
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.created_by = ?
        ORDER BY s.created_at DESC
    ''', (username,))
    shares = cursor.fetchall()
    conn.close()

    result = []
    for share in shares:
        valid, error_msg = is_share_valid(share)
        result.append({
            'share_id': share['id'],
            'file_id': share['file_id'],
            'filename': share['filename'],
            'filesize': share['filesize'],
            'expires_at': share['expires_at'],
            'max_downloads': share['max_downloads'],
            'download_count': share['download_count'],
            'created_at': share['created_at'],
            'is_valid': valid,
            'error_msg': error_msg
        })

    return jsonify(result)


@files_bp.route('/api/share/<share_id>', methods=['DELETE'])
@login_required
def delete_share(share_id):
    """删除分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT created_by, file_id FROM share_links WHERE id = ?', (share_id,))
    share = cursor.fetchone()

    if not share:
        conn.close()
        return jsonify({'error': '分享链接不存在'}), 404

    if share['created_by'] != username:
        conn.close()
        return jsonify({'error': '无权限删除此分享链接'}), 403

    cursor.execute('DELETE FROM share_links WHERE id = ?', (share_id,))
    conn.commit()
    conn.close()

    logger.info(f"分享链接删除: 分享ID {share_id}, 文件ID {share['file_id']}, 操作者 {username}")
    return jsonify({'success': True, 'message': '分享链接已删除'})


# ---------- 受控下载会话 ----------

def _get_file_record(file_id):
    """获取文件记录"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path, size FROM files WHERE id = ?', (file_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def _validate_file_on_disk(file_info):
    """校验文件路径合法且存在于磁盘"""
    if not file_info:
        return '文件不存在', 404
    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return '非法文件路径', 403
    if not os.path.exists(file_info['path']):
        return '文件不存在', 404
    return None, None


def _session_payload(session):
    """会话的对外表示"""
    return {
        'session_id': session['id'],
        'file_id': session['file_id'],
        'share_id': session['share_id'],
        'status': effective_status(session),
        'filename': session['file_name'],
        'size': session['file_size'],
        'mtime': session['file_mtime'],
        'bytes_confirmed': session['bytes_confirmed'],
        'failure_reason': session['failure_reason'],
        'created_at': session['created_at'],
        'expires_at': session['expires_at'],
        'completed_at': session['completed_at'],
    }


@files_bp.route('/api/download/<file_id>/prepare', methods=['POST'])
@login_required
def prepare_download(file_id):
    """创建受控下载会话（登录用户）"""
    cleanup_expired_sessions()

    file_info = _get_file_record(file_id)
    error, status = _validate_file_on_disk(file_info)
    if error:
        return jsonify({'error': error}), status

    token = get_token_from_request()
    username = get_username_from_token(token)

    stat = os.stat(file_info['path'])
    session = create_session(
        file_id=file_id,
        file_name=file_info['name'],
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
        username=username,
    )
    return jsonify(_session_payload(session))


@files_bp.route('/api/share/<share_id>/download/prepare', methods=['POST'])
def prepare_share_download(share_id):
    """创建分享下载会话（公开；此时不占用下载次数，确认完成后才计数）"""
    cleanup_expired_sessions()

    share = get_share_link_info(share_id)
    valid, error_msg = is_share_valid(share)
    if not valid:
        return jsonify({'error': error_msg}), 404

    file_info = _get_file_record(share['file_id'])
    error, status = _validate_file_on_disk(file_info)
    if error:
        return jsonify({'error': error}), status

    stat = os.stat(file_info['path'])
    session = create_session(
        file_id=share['file_id'],
        file_name=file_info['name'],
        file_size=stat.st_size,
        file_mtime=stat.st_mtime,
        share_id=share_id,
    )
    return jsonify(_session_payload(session))


@files_bp.route('/api/downloads/recent', methods=['GET'])
@login_required
def recent_downloads():
    """当前用户最近的下载会话（历史状态，返回列表页后仍可展示最近结果）"""
    token = get_token_from_request()
    username = get_username_from_token(token)
    sessions = list_recent_sessions(username)
    return jsonify([
        {
            'session_id': s['id'],
            'file_id': s['file_id'],
            'share_id': s['share_id'],
            'status': s['status'],
            'filename': s['file_name'],
            'size': s['file_size'],
            'bytes_confirmed': s['bytes_confirmed'],
            'failure_reason': s['failure_reason'],
            'created_at': s['created_at'],
            'updated_at': s['updated_at'],
            'completed_at': s['completed_at'],
        }
        for s in sessions
    ])


@files_bp.route('/api/downloads/<session_id>', methods=['GET'])
def get_download_session(session_id):
    """查询下载会话状态（页面刷新后据此恢复，避免重复取件或错误完成状态）"""
    session = get_session(session_id)
    if not session:
        return jsonify({'error': '下载会话不存在'}), 404
    return jsonify(_session_payload(session))


def _check_session_transferable(session):
    """校验会话当前是否允许传输，返回 (error_dict, status) 或 (None, None)"""
    status = effective_status(session)
    if status == 'expired':
        return {'error': '下载会话已过期，请重新发起下载', 'code': 'session_expired'}, 410
    if status == 'failed':
        return {'error': session['failure_reason'] or '下载会话已失败', 'code': 'session_failed'}, 409
    if status == 'completed':
        # 已完成会话不再提供内容，防止绕过下载次数限制反复取件
        return {'error': '下载会话已完成，如需再次下载请重新发起', 'code': 'session_completed'}, 409
    return None, None


def _check_session_file(session):
    """校验会话对应的文件记录与磁盘快照（检测文件被替换）"""
    file_info = _get_file_record(session['file_id'])
    if not file_info:
        fail_session(session['id'], '文件记录不存在')
        return None, {'error': '文件不存在', 'code': 'file_missing'}, 404
    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        fail_session(session['id'], '非法文件路径')
        return None, {'error': '非法文件路径', 'code': 'invalid_path'}, 403
    ok, code = check_file_snapshot(session, file_info['path'])
    if not ok:
        if code == 'file_missing':
            fail_session(session['id'], '文件已被删除')
            return None, {'error': '文件不存在', 'code': 'file_missing'}, 404
        fail_session(session['id'], '文件已被替换')
        return None, {'error': '文件已被替换，请重新确认后再下载', 'code': 'file_changed'}, 409
    return file_info, None, None


@files_bp.route('/api/downloads/<session_id>/content', methods=['GET'])
def download_session_content(session_id):
    """传输会话内容（支持 Range 断点续传，网络波动后可从已接收位置继续）"""
    session = get_session(session_id)
    if not session:
        return jsonify({'error': '下载会话不存在'}), 404

    error, status = _check_session_transferable(session)
    if error:
        return jsonify(error), status

    file_info, error, status = _check_session_file(session)
    if error:
        return jsonify(error), status

    mark_transferring(session_id)

    logger.info(f"会话传输: {session_id} 文件 {file_info['name']} "
                f"(Range: {request.headers.get('Range', '全量')})")
    # conditional=True 使 Flask 处理 Range 头并返回 206 / Accept-Ranges
    return send_file(
        file_info['path'],
        as_attachment=True,
        download_name=file_info['name'],
        conditional=True,
        max_age=0,
    )


@files_bp.route('/api/downloads/<session_id>/complete', methods=['POST'])
def complete_download_session_endpoint(session_id):
    """确认下载完成（幂等；分享下载在此刻才计入成功次数）"""
    session = get_session(session_id)
    if not session:
        return jsonify({'error': '下载会话不存在'}), 404

    was_completed = session['status'] == 'completed'

    # 活动状态的会话在确认前必须复核文件快照，防止文件被替换后产生错误完成状态
    if effective_status(session) in ACTIVE_STATUSES:
        _, error, status = _check_session_file(session)
        if error:
            return jsonify(error), status

    data = request.get_json(silent=True) or {}
    received = data.get('received')

    result, error, status = complete_session(session_id, received)
    if error:
        return jsonify(error), status

    return jsonify({
        'success': True,
        'session_id': result['id'],
        'status': 'completed',
        'completed_at': result['completed_at'],
        'already_completed': was_completed,
    })


@files_bp.route('/api/downloads/<session_id>/abort', methods=['POST'])
def abort_download_session(session_id):
    """中止下载会话（用户取消或放弃重试）"""
    session = get_session(session_id)
    if not session:
        return jsonify({'error': '下载会话不存在'}), 404

    if effective_status(session) == 'completed':
        return jsonify({'error': '下载会话已完成', 'code': 'session_completed'}), 409

    fail_session(session_id, '用户取消下载')
    return jsonify({'success': True, 'status': 'failed'})
