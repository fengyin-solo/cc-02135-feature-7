/**
 * 受控下载管理器
 *
 * 分阶段状态机：preparing(准备) → verifying(校验) → transferring(传输) → completed(完成)
 *                                                                   ↘ failed(失败)
 *
 * 特性：
 * - 网络波动后通过 Range 请求从已接收位置断点续传，指数退避重试
 * - 文件被替换 / 会话过期时不会产出错误完成状态，提示重新确认
 * - 重复点击防重入；页面刷新后可恢复会话状态，不重复取件
 * - 下载记录持久化到 localStorage，返回列表后仍能看到最近结果
 * - 只有服务器确认完成（complete）后才保存文件，保证成功次数与状态一致
 */
const DownloadStages = {
    PREPARING: 'preparing',
    VERIFYING: 'verifying',
    TRANSFERRING: 'transferring',
    COMPLETED: 'completed',
    FAILED: 'failed'
};

// 下载记录本地持久化（仅元数据，不含文件内容）
const DownloadStore = {
    KEY: 'download_records_v1',

    all() {
        try {
            return JSON.parse(localStorage.getItem(this.KEY)) || {};
        } catch {
            return {};
        }
    },

    get(key) {
        return this.all()[key] || null;
    },

    put(key, record) {
        const all = this.all();
        all[key] = record;
        // 只保留最近 50 条，避免无限增长
        const keys = Object.keys(all);
        if (keys.length > 50) {
            keys.sort((a, b) => (all[a].updated_at || 0) - (all[b].updated_at || 0));
            keys.slice(0, keys.length - 50).forEach(k => delete all[k]);
        }
        try {
            localStorage.setItem(this.KEY, JSON.stringify(all));
        } catch { /* 存储满时忽略，不影响下载主流程 */ }
    },

    remove(key) {
        const all = this.all();
        delete all[key];
        try {
            localStorage.setItem(this.KEY, JSON.stringify(all));
        } catch { /* ignore */ }
    }
};

const DownloadManager = {
    MAX_RETRIES: 5,
    // 进行中的下载（防重复点击）
    active: {},
    _lastPersist: {},

    _sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    },

    _formatSize(bytes) {
        if (!bytes) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },

    _error(code, message, fatal = false) {
        const err = new Error(message);
        err.code = code;
        // 会话/文件类错误不可通过网络重试恢复，直接失败
        err.fatal = fatal || [
            'file_changed', 'file_missing', 'invalid_path',
            'session_expired', 'session_failed', 'session_completed',
            'quota_exceeded', 'prepare_failed', 'complete_failed'
        ].includes(code);
        return err;
    },

    /**
     * 开始（或恢复）一次受控下载
     * @param kind  'file'（登录用户）| 'share'（分享访客）
     * @param id    file_id 或 share_id
     * @param hooks {
     *   onStage(stage, message, info),   // 阶段变化
     *   onProgress(received, total),     // 传输进度
     *   getAuthHeaders(),                // prepare 所需的认证头
     *   onAuthRequired()                 // 401 时调用，返回 Promise<boolean>（用户是否完成登录）
     * }
     */
    async start(kind, id, hooks) {
        const key = `${kind}:${id}`;
        if (this.active[key]) return;  // 重复点击直接忽略
        this.active[key] = true;
        try {
            await this._run(key, kind, id, hooks, true);
        } catch (err) {
            hooks.onStage(DownloadStages.FAILED, err.message || '下载失败', { retryable: true });
        } finally {
            delete this.active[key];
        }
    },

    isActive(kind, id) {
        return !!this.active[`${kind}:${id}`];
    },

    async _run(key, kind, id, hooks, allowReprepare) {
        let record = DownloadStore.get(key);

        // 1. 尝试恢复本地未完成的会话（页面刷新场景）
        if (record && record.session_id &&
            (record.status === DownloadStages.PREPARING || record.status === DownloadStages.TRANSFERRING)) {
            hooks.onStage(DownloadStages.VERIFYING, '正在恢复上次的下载会话…');
            let remote = null;
            try {
                remote = await this._fetchSession(record.session_id);
            } catch {
                throw this._error('network', '网络异常，无法恢复下载会话，请稍后重试');
            }
            if (remote && (remote.status === 'prepared' || remote.status === 'transferring')) {
                // 会话仍有效：复用同一会话继续，不重复取件
                record = this._recordFromRemote(key, kind, id, remote);
            } else if (remote && remote.status === 'completed') {
                // 服务器端已确认完成：直接展示完成状态，不重复下载
                record.status = DownloadStages.COMPLETED;
                record.updated_at = Date.now() / 1000;
                DownloadStore.put(key, record);
                hooks.onStage(DownloadStages.COMPLETED, '该文件此前已下载完成');
                return;
            } else {
                // 会话已失效（过期/失败/文件变更）：丢弃，重新准备
                record = null;
            }
        } else if (record && record.status === DownloadStages.COMPLETED) {
            // 本地记录已完成：展示状态，不自动重复下载
            hooks.onStage(DownloadStages.COMPLETED, '该文件此前已下载完成');
            return;
        } else {
            record = null;
        }

        // 2. 准备阶段：创建新会话
        if (!record) {
            hooks.onStage(DownloadStages.PREPARING, '正在申请下载授权…');
            let prep;
            try {
                prep = await this._prepare(kind, id, hooks);
            } catch (err) {
                this._markFailed(key, record, err);
                hooks.onStage(DownloadStages.FAILED, err.message, { code: err.code, retryable: err.code !== 'prepare_failed' });
                return;
            }
            if (!prep) return;  // 用户取消登录，保持原状
            record = this._recordFromRemote(key, kind, id, prep);
            DownloadStore.put(key, record);
        }

        // 3. 校验阶段：会话快照已由服务器校验，这里确认元信息一致
        hooks.onStage(DownloadStages.VERIFYING, '校验文件状态…');
        if (typeof record.size !== 'number' || record.size < 0) {
            this._markFailed(key, record, this._error('prepare_failed', '服务器返回的文件信息无效'));
            hooks.onStage(DownloadStages.FAILED, '服务器返回的文件信息无效', { retryable: true });
            return;
        }

        // 4. 传输阶段：断点续传（刷新后内存分片丢失，从 0 重新拉取但复用会话）
        const chunks = [];
        record.received = 0;
        try {
            await this._transfer(record, chunks, hooks);
        } catch (err) {
            if (err.code === 'session_expired' && allowReprepare) {
                // 会话过期：重新准备后自动重试一次（重新确认）
                DownloadStore.remove(key);
                return this._run(key, kind, id, hooks, false);
            }
            this._markFailed(key, record, err);
            hooks.onStage(DownloadStages.FAILED, err.message, {
                code: err.code,
                retryable: err.code !== 'file_changed'
            });
            return;
        }

        // 5. 完成确认：只有服务器确认后才算成功（计数与状态一致）
        hooks.onStage(DownloadStages.VERIFYING, '确认文件完整性…');
        try {
            await this._complete(record);
        } catch (err) {
            this._markFailed(key, record, err);
            hooks.onStage(DownloadStages.FAILED, err.message, {
                code: err.code,
                retryable: err.code !== 'file_changed' && err.code !== 'quota_exceeded'
            });
            return;
        }

        record.status = DownloadStages.COMPLETED;
        record.completed_at = Date.now() / 1000;
        record.updated_at = Date.now() / 1000;
        DownloadStore.put(key, record);

        // 6. 确认成功后才保存文件到本地
        this._saveBlob(record, chunks);
        hooks.onStage(DownloadStages.COMPLETED, '下载完成');
    },

    _markFailed(key, record, err) {
        if (record) {
            record.status = DownloadStages.FAILED;
            record.error = err.message;
            record.updated_at = Date.now() / 1000;
            DownloadStore.put(key, record);
        }
    },

    _recordFromRemote(key, kind, id, remote) {
        return {
            key,
            kind,
            id,
            session_id: remote.session_id,
            status: remote.status === 'prepared' ? DownloadStages.PREPARING : remote.status,
            filename: remote.filename,
            size: remote.size,
            mtime: remote.mtime,
            received: 0,
            updated_at: Date.now() / 1000
        };
    },

    async _fetchSession(sessionId) {
        const resp = await fetch(`${API_BASE}/downloads/${sessionId}`);
        if (resp.status === 404) return null;
        if (!resp.ok) throw this._error('http', `服务器错误 (${resp.status})`);
        return await resp.json();
    },

    async _prepare(kind, id, hooks) {
        const url = kind === 'share'
            ? `${API_BASE}/share/${id}/download/prepare`
            : `${API_BASE}/download/${id}/prepare`;
        let authRetried = false;
        while (true) {
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...(hooks.getAuthHeaders ? hooks.getAuthHeaders() : {}) }
            });
            if (resp.status === 401 && !authRetried && hooks.onAuthRequired) {
                // 权限过期/未登录：引导用户重新确认身份后自动继续
                authRetried = true;
                const authed = await hooks.onAuthRequired();
                if (authed) continue;
                return null;
            }
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw this._error('prepare_failed', body.error || '无法创建下载会话');
            }
            return await resp.json();
        }
    },

    async _transfer(record, chunks, hooks) {
        const total = record.size;
        let attempts = 0;
        hooks.onStage(DownloadStages.TRANSFERRING, null, { received: record.received, total });

        while (record.received < total) {
            try {
                const resp = await fetch(`${API_BASE}/downloads/${record.session_id}/content`, {
                    headers: { 'Range': `bytes=${record.received}-` }
                });

                if (resp.status === 409 || resp.status === 410) {
                    const body = await resp.json().catch(() => ({}));
                    throw this._error(body.code || 'session_failed', body.error || '下载会话已失效');
                }
                if (!resp.ok && resp.status !== 206) {
                    throw this._error('http', `服务器错误 (${resp.status})`);
                }
                if (resp.status === 200 && record.received > 0) {
                    // 服务器未接受 Range：丢弃已收分片，从头传输
                    chunks.length = 0;
                    record.received = 0;
                }
                if (!resp.body) {
                    // 不支持流式读取：退化为一次性接收
                    const buf = await resp.arrayBuffer();
                    chunks.push(new Uint8Array(buf));
                    record.received = buf.byteLength;
                    break;
                }

                const reader = resp.body.getReader();
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    chunks.push(value);
                    record.received += value.length;
                    if (hooks.onProgress) hooks.onProgress(record.received, total);
                    this._persistThrottled(record);
                }
                attempts = 0;  // 一次成功传输后重置重试计数
            } catch (err) {
                if (err.fatal) throw err;
                attempts += 1;
                if (attempts > this.MAX_RETRIES) {
                    throw this._error('network', '网络不稳定，下载中断，请稍后重试', true);
                }
                hooks.onStage(
                    DownloadStages.TRANSFERRING,
                    `网络波动，${attempts} 秒后从 ${this._formatSize(record.received)} 处继续…`,
                    { received: record.received, total }
                );
                await this._sleep(1000 * attempts);
            }
        }
        DownloadStore.put(record.key, record);
    },

    async _complete(record) {
        const resp = await fetch(`${API_BASE}/downloads/${record.session_id}/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ received: record.received })
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw this._error(body.code || 'complete_failed', body.error || '完成确认失败');
        }
        return body;
    },

    _saveBlob(record, chunks) {
        const blob = new Blob(chunks);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = record.filename || 'download';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
    },

    _persistThrottled(record) {
        const now = Date.now();
        if (now - (this._lastPersist[record.key] || 0) < 500) return;
        this._lastPersist[record.key] = now;
        record.updated_at = now / 1000;
        DownloadStore.put(record.key, record);
    },

    /**
     * 查询某对象最近一次下载记录（用于“返回列表后仍能看到最近结果”）
     */
    lastRecord(kind, id) {
        return DownloadStore.get(`${kind}:${id}`);
    },

    /**
     * 页面加载时校验本地记录与服务器状态是否一致（刷新恢复）
     * 返回修正后的记录（可能为 null）
     */
    async reconcile(kind, id) {
        const key = `${kind}:${id}`;
        const record = DownloadStore.get(key);
        if (!record || !record.session_id) return record;
        if (record.status !== DownloadStages.PREPARING && record.status !== DownloadStages.TRANSFERRING) {
            return record;  // 终态记录直接展示
        }
        try {
            const remote = await this._fetchSession(record.session_id);
            if (!remote) {
                DownloadStore.remove(key);
                return null;
            }
            record.status = remote.status === 'prepared' ? DownloadStages.PREPARING
                : remote.status === 'transferring' ? DownloadStages.TRANSFERRING
                : remote.status;
            record.updated_at = Date.now() / 1000;
            DownloadStore.put(key, record);
            return record;
        } catch {
            return record;  // 网络异常时保留本地状态展示
        }
    }
};
