"""受控下载会话测试：分阶段状态、断点续传、计数一致性、异常场景"""
import io
import os
import time


def _upload(client, content=b'controlled download content', name='controlled.txt'):
    """上传文件并返回 file_id"""
    data = {'file': (io.BytesIO(content), name)}
    resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert resp.status_code == 200
    return resp.get_json()['file_id']


def _make_share(client, auth_token, file_id, max_downloads=5):
    resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': max_downloads},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert resp.status_code == 200
    return resp.get_json()['share_id']


def _share_count(client, share_id):
    return client.get(f'/api/share/{share_id}').get_json()['download_count']


# ---------- 准备阶段 ----------

def test_prepare_requires_auth(client):
    """未登录不能创建下载会话"""
    resp = client.post('/api/download/some-file/prepare')
    assert resp.status_code == 401


def test_prepare_success(client, auth_token):
    """登录用户创建会话成功，初始状态为 prepared"""
    file_id = _upload(client)
    resp = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert resp.status_code == 200
    result = resp.get_json()
    assert result['status'] == 'prepared'
    assert result['size'] == len(b'controlled download content')
    assert result['filename'] == 'controlled.txt'
    assert result['session_id']
    assert result['expires_at'] > time.time()


def test_prepare_file_not_found(client, auth_token):
    resp = client.post(
        '/api/download/nonexistent/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert resp.status_code == 404


# ---------- 传输阶段（含 Range 断点续传） ----------

def test_content_full_download(client, auth_token):
    """全量传输"""
    file_id = _upload(client)
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 200
    assert resp.data == b'controlled download content'
    assert resp.headers.get('Accept-Ranges') == 'bytes'

    # 状态应进入 transferring
    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] == 'transferring'


def test_content_range_resume(client, auth_token):
    """Range 请求返回 206 与正确的分片，可从断点继续"""
    content = b'0123456789abcdef' * 64  # 1024 字节
    file_id = _upload(client, content, 'range.bin')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    resp = client.get(
        f"/api/downloads/{session['session_id']}/content",
        headers={'Range': 'bytes=512-'}
    )
    assert resp.status_code == 206
    assert resp.data == content[512:]
    assert resp.headers['Content-Range'] == f'bytes 512-1023/1024'


def test_content_unknown_session(client):
    resp = client.get('/api/downloads/nonexistent-session/content')
    assert resp.status_code == 404


# ---------- 完成确认与计数一致性 ----------

def test_complete_success_and_count_once(client, auth_token):
    """完整流程：准备→传输→确认，分享计数只在确认后 +1"""
    file_id = _upload(client, b'share session content', 'session_share.txt')
    share_id = _make_share(client, auth_token, file_id)

    session = client.post(f'/api/share/{share_id}/download/prepare').get_json()
    assert session['status'] == 'prepared'
    # 准备阶段不计数
    assert _share_count(client, share_id) == 0

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 200
    # 传输但未确认，仍不计数
    assert _share_count(client, share_id) == 0

    complete = client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': len(b'share session content')}
    )
    assert complete.status_code == 200
    assert complete.get_json()['success'] is True
    assert _share_count(client, share_id) == 1

    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] == 'completed'
    assert status['bytes_confirmed'] == len(b'share session content')


def test_complete_idempotent(client, auth_token):
    """重复确认（重复点击/重试）不会重复计数"""
    file_id = _upload(client, b'idempotent content', 'idem.txt')
    share_id = _make_share(client, auth_token, file_id)

    session = client.post(f'/api/share/{share_id}/download/prepare').get_json()
    client.get(f"/api/downloads/{session['session_id']}/content")

    for _ in range(3):
        resp = client.post(
            f"/api/downloads/{session['session_id']}/complete",
            json={'received': len(b'idempotent content')}
        )
        assert resp.status_code == 200

    assert _share_count(client, share_id) == 1


def test_complete_rejects_incomplete_transfer(client, auth_token):
    """确认字节数与文件大小不一致时拒绝完成"""
    file_id = _upload(client, b'partial content check', 'partial.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    resp = client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': 5}
    )
    assert resp.status_code == 400
    assert resp.get_json()['code'] == 'incomplete_transfer'

    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] != 'completed'


def test_completed_session_blocks_refetch(client, auth_token):
    """已完成会话不能再次取件（防止绕过次数限制）"""
    file_id = _upload(client, b'one shot', 'oneshot.txt')
    share_id = _make_share(client, auth_token, file_id, max_downloads=5)

    session = client.post(f'/api/share/{share_id}/download/prepare').get_json()
    client.get(f"/api/downloads/{session['session_id']}/content")
    client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': len(b'one shot')}
    )

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'session_completed'
    # 再次取件失败，计数不变
    assert _share_count(client, share_id) == 1


def test_concurrent_complete_respects_max_downloads(client, auth_token):
    """max_downloads=1 时两个会话同时确认，只有一个成功"""
    file_id = _upload(client, b'race content', 'race.txt')
    share_id = _make_share(client, auth_token, file_id, max_downloads=1)

    s1 = client.post(f'/api/share/{share_id}/download/prepare').get_json()
    s2 = client.post(f'/api/share/{share_id}/download/prepare').get_json()

    r1 = client.post(f"/api/downloads/{s1['session_id']}/complete",
                     json={'received': len(b'race content')})
    r2 = client.post(f"/api/downloads/{s2['session_id']}/complete",
                     json={'received': len(b'race content')})

    results = sorted([r1.status_code, r2.status_code])
    assert results == [200, 409]
    assert _share_count(client, share_id) == 1


# ---------- 异常场景：过期 / 文件替换 / 中止 ----------

def test_expired_session(client, auth_token, db_conn):
    """过期会话不能传输也不能确认"""
    file_id = _upload(client, b'expiring', 'expire.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE download_sessions SET expires_at = ? WHERE id = ?',
        (time.time() - 10, session['session_id'])
    )
    db_conn.commit()

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 410
    assert resp.get_json()['code'] == 'session_expired'

    resp = client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': 8}
    )
    assert resp.status_code == 410

    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] == 'expired'


def test_file_replaced_blocks_transfer_and_complete(client, auth_token, db_conn):
    """文件被替换后，传输与确认都返回 409，会话标记失败"""
    file_id = _upload(client, b'original content', 'original.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    # 模拟文件被替换：改写磁盘文件并改变 mtime
    cursor = db_conn.cursor()
    cursor.execute('SELECT path FROM files WHERE id = ?', (file_id,))
    path = cursor.fetchone()['path']
    with open(path, 'wb') as f:
        f.write(b'replaced content with different size')
    os.utime(path, (time.time() + 100, time.time() + 100))

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'file_changed'

    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] == 'failed'

    # 失败后确认同样被拒绝
    resp = client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': len(b'original content')}
    )
    assert resp.status_code == 409


def test_file_deleted_blocks_transfer(client, auth_token, db_conn):
    """文件被删除后传输返回 404"""
    file_id = _upload(client, b'to be deleted', 'deleted.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    cursor = db_conn.cursor()
    cursor.execute('SELECT path FROM files WHERE id = ?', (file_id,))
    path = cursor.fetchone()['path']
    os.remove(path)

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 404
    assert resp.get_json()['code'] == 'file_missing'


def test_abort_session(client, auth_token):
    """中止后会话进入失败终态，不能再传输"""
    file_id = _upload(client, b'abort me', 'abort.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()

    resp = client.post(f"/api/downloads/{session['session_id']}/abort")
    assert resp.status_code == 200

    status = client.get(f"/api/downloads/{session['session_id']}").get_json()
    assert status['status'] == 'failed'

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 409


# ---------- 历史状态 ----------

def test_recent_downloads_history(client, auth_token):
    """最近下载历史反映真实状态，且需要登录"""
    resp = client.get('/api/downloads/recent')
    assert resp.status_code == 401

    file_id = _upload(client, b'history content', 'history.txt')
    session = client.post(
        f'/api/download/{file_id}/prepare',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()
    client.get(f"/api/downloads/{session['session_id']}/content")
    client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': len(b'history content')}
    )

    resp = client.get(
        '/api/downloads/recent',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert resp.status_code == 200
    history = resp.get_json()
    assert len(history) >= 1
    latest = history[0]
    assert latest['session_id'] == session['session_id']
    assert latest['status'] == 'completed'
    assert latest['filename'] == 'history.txt'


def test_share_prepare_public_and_no_auth_session(client, auth_token):
    """分享会话全流程无需登录，且访客会话不进入用户历史"""
    file_id = _upload(client, b'public session', 'public_session.txt')
    share_id = _make_share(client, auth_token, file_id)

    resp = client.post(f'/api/share/{share_id}/download/prepare')
    assert resp.status_code == 200
    session = resp.get_json()
    assert session['share_id'] == share_id

    resp = client.get(f"/api/downloads/{session['session_id']}/content")
    assert resp.status_code == 200

    resp = client.post(
        f"/api/downloads/{session['session_id']}/complete",
        json={'received': len(b'public session')}
    )
    assert resp.status_code == 200
    assert _share_count(client, share_id) == 1

    # 访客会话不计入 admin 的历史
    history = client.get(
        '/api/downloads/recent',
        headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()
    assert all(h['session_id'] != session['session_id'] for h in history)


def test_share_prepare_invalid_share(client):
    """无效分享链接不能创建会话"""
    resp = client.post('/api/share/nonexistent/download/prepare')
    assert resp.status_code == 404
