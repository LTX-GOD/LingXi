/**
 * Ling-Xi 控制台 — 前端逻辑
 * SSE 实时更新 + 任务管理
 */

let state = { status:'idle', start_time:0, zones:[], tasks:[], total_score:0, total_solved:0, knowledge:null };
let knowledgeRefreshTimer = null;

// ─── SSE 连接 ───
function connectSSE() {
    const es = new EventSource('/api/events');
    es.onmessage = e => {
        try { handleMsg(JSON.parse(e.data)); } catch(err) { console.error(err); }
    };
    es.onerror = () => { setStatus('disconnected'); setTimeout(() => { es.close(); connectSSE(); }, 3000); };
    es.onopen = () => setStatus(state.status || 'idle');
}

function handleMsg(msg) {
    switch(msg.type) {
        case 'init':
            state = { ...state, ...msg.data };
            renderAll();
            break;
        case 'agent_state':
            Object.assign(state, msg.data);
            if(msg.data.zones) state.zones = msg.data.zones;
            renderMetrics(); renderZones(); setStatus(state.status);
            break;
        case 'zones':
            state.zones = msg.data;
            renderZones();
            break;
        case 'task_update':
            updateTask(msg.data);
            break;
        case 'task_deleted':
            state.tasks = state.tasks.filter(t => t.task_id !== msg.data.task_id);
            renderTasks();
            break;
        case 'log':
            appendLog(msg.data);
            break;
        case 'knowledge_updated':
            scheduleKnowledgeRefresh();
            break;
        case 'ping':
            break;
    }
}

function updateTask(td) {
    const idx = state.tasks.findIndex(t => t.task_id === td.task_id);
    if (idx >= 0) state.tasks[idx] = td;
    else state.tasks.push(td);
    renderTasks(); renderMetrics();
}

// ─── 渲染 ───
function renderAll() { renderMetrics(); renderZones(); renderTasks(); renderKnowledge(); setStatus(state.status); }

function renderMetrics() {
    const tasks = state.tasks || [];
    const solved = tasks.filter(t => t.status === 'completed').length;
    const active = tasks.filter(t => t.status === 'running').length;
    document.getElementById('totalSolved').textContent = state.total_solved || solved;
    document.getElementById('totalScore').textContent = state.total_score || 0;
    document.getElementById('activeTasks').textContent = active;
    document.getElementById('totalTasks').textContent = tasks.length;
}

function renderZones() {
    const el = document.getElementById('zones');
    const zones = state.zones || [];
    if (!zones.length) { el.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">等待数据...</div>'; return; }
    el.innerHTML = zones.map(z => {
        const pct = z.total > 0 ? Math.round(z.solved / z.total * 100) : 0;
        const demoSkipped = Number(z.demo_skipped || z.excluded_total || 0);
        const extra = demoSkipped > 0
            ? `<div class="zone-stats" style="color:var(--text-dim);">已忽略 demo ${demoSkipped} 题</div>`
            : '';
        return `<div class="zone-item ${z.unlocked?'':'locked'}">
            <div class="zone-header">
                <span class="zone-name">${z.unlocked?'🔓':'🔒'} ${esc(z.name)}</span>
                <span class="zone-score">${z.score} 分</span>
            </div>
            <div class="zone-progress"><div class="zone-progress-fill" style="width:${pct}%"></div></div>
            <div class="zone-stats">${z.solved}/${z.total} 已攻克 (${pct}%)</div>
            ${extra}
        </div>`;
    }).join('');
}

const STATUS_CN = {
    running: '● 执行中', pending: '○ 等待中', completed: '✓ 已完成',
    failed: '✗ 失败', paused: '⏸ 已暂停', aborted: '⊘ 已中止',
};
const DIFF_CN = { easy: '简单', medium: '中等', hard: '困难' };

function renderTasks() {
    const tbody = document.getElementById('tasksBody');
    const tasks = state.tasks || [];
    if (!tasks.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="color:var(--text-dim);text-align:center;padding:20px;">暂无任务，点击 <b>+ 新建任务</b> 创建</td></tr>';
        return;
    }
    tbody.innerHTML = tasks.map(t => {
        const target = t.target || '—';
        const errorHint = t.error ? ` title="${esc(t.error)}"` : '';
        const flagCell = t.flag
            ? `<span class="flag-pill" title="${esc(t.flag)}">${esc(t.flag)}</span>`
            : '<span class="flag-empty">—</span>';
        return `<tr>
            <td>${esc(t.challenge_code)}</td>
            <td><span class="badge ${t.difficulty}">${DIFF_CN[t.difficulty]||t.difficulty}</span></td>
            <td>${t.points}</td>
            <td class="target-cell" title="${esc(target)}">${esc(target)}</td>
            <td><span class="badge ${t.status}"${errorHint}>${STATUS_CN[t.status]||t.status}</span></td>
            <td class="flag-cell">${flagCell}</td>
            <td>${t.attempts}</td>
            <td class="actions-cell">${getActions(t)}</td>
        </tr>`;
    }).join('');
}

function getActions(t) {
    const id = t.task_id;
    switch(t.status) {
        case 'running':
            return `<button class="btn btn-xs btn-pause" onclick="taskPause('${id}')">暂停</button>
                    <button class="btn btn-xs btn-abort" onclick="taskAbort('${id}')">中止</button>`;
        case 'paused':
            return `<button class="btn btn-xs btn-resume" onclick="taskResume('${id}')">恢复</button>
                    <button class="btn btn-xs btn-abort" onclick="taskAbort('${id}')">中止</button>`;
        case 'pending':
            return `<button class="btn btn-xs btn-abort" onclick="taskAbort('${id}')">取消</button>`;
        case 'completed': case 'failed': case 'aborted':
            return `<button class="btn btn-xs btn-dim" onclick="taskDelete('${id}')">删除</button>`;
        default:
            return '';
    }
}

// ─── 任务操作 ───
async function apiPost(url) {
    const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'} });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
        const msg = data.detail || data.error || `请求失败: ${r.status}`;
        throw new Error(msg);
    }
    return data;
}

async function taskPause(id)  { await safeAction(() => apiPost(`/api/tasks/${id}/pause`)); }
async function taskResume(id) { await safeAction(() => apiPost(`/api/tasks/${id}/resume`)); }
async function taskAbort(id)  { await safeAction(() => apiPost(`/api/tasks/${id}/abort`)); }
async function taskDelete(id) {
    await safeAction(async () => {
        const r = await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `删除失败: ${r.status}`);
        return data;
    });
}
async function agentStart()   { await safeAction(() => apiPost('/api/agent/start')); }
async function agentPause()   { await safeAction(() => apiPost('/api/agent/pause')); }

// ─── 新建弹窗 ───
function showCreateModal() { document.getElementById('createModal').style.display = 'flex'; document.getElementById('inputCode').focus(); }
function hideCreateModal() { document.getElementById('createModal').style.display = 'none'; }

async function createTask() {
    const body = {
        challenge_code: document.getElementById('inputCode').value.trim(),
        target: document.getElementById('inputTarget').value.trim(),
        difficulty: document.getElementById('inputDiff').value,
        points: parseInt(document.getElementById('inputPoints').value) || 100,
        zone: document.getElementById('inputZone').value,
    };
    if (!body.challenge_code) { alert('请输入题目编号'); return; }
    await safeAction(async () => {
        const r = await fetch('/api/tasks', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `创建失败: ${r.status}`);
        hideCreateModal();
        document.getElementById('inputCode').value = '';
        document.getElementById('inputTarget').value = '';
        return data;
    });
}

// ─── 知识库 ───
async function loadKnowledge() {
    try {
        const r = await fetch('/api/knowledge');
        const data = await r.json();
        state.knowledge = data;
        renderKnowledge();
    } catch(err) {
        console.error('Failed to load knowledge:', err);
    }
}

function renderKnowledge() {
    const statsEl = document.getElementById('knowledgeStats');
    const statusEl = document.getElementById('knowledgeStatusLine');

    if (!state.knowledge || state.knowledge.error) {
        statsEl.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">知识库不可用</div>';
        statusEl.innerHTML = '';
        return;
    }

    const buckets = state.knowledge.buckets || [];
    const service = state.knowledge.service || {};
    if (!buckets.length) {
        statsEl.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">暂无知识记录</div>';
        statusEl.innerHTML = '';
        return;
    }

    const serviceLabel = service.status || 'unknown';
    const backendLabel = service.health?.backend || (service.assets?.db_exists ? 'milvus' : 'snapshot');
    const updatedAt = service.updated_at ? service.updated_at.split('T')[0] : '未记录';
    statusEl.innerHTML = `
        <div class="knowledge-home-pill ${esc(serviceLabel)}">外部 WP: ${esc(serviceLabel)}</div>
        <div class="knowledge-home-meta">后端: ${esc(backendLabel)} · 资产更新: ${esc(updatedAt)}</div>
    `;

    statsEl.innerHTML = buckets.map(b => {
        const categoryEntries = Object.entries(b.categories || {}).slice(0, 3);
        const categoryText = categoryEntries.length
            ? categoryEntries.map(([name, count]) => `${name} ${count}`).join(' · ')
            : '暂无分类统计';
        const updated = b.updated_at ? b.updated_at.split('T')[0] : '未记录';
        return `<a class="kb-summary-card" href="/knowledge?bucket=${encodeURIComponent(b.bucket)}">
            <div class="kb-summary-top">
                <div class="kb-bucket-name">${esc(b.display_name)}</div>
                <span class="knowledge-home-pill ${esc(b.status || 'idle')}">${esc(b.status || 'idle')}</span>
            </div>
            <div class="kb-summary-count">${b.total || 0}</div>
            <div class="kb-summary-label">${b.bucket === 'ctf_writeups' ? '已导入题解' : '结构化记录'}</div>
            <div class="kb-summary-meta">${esc(categoryText)}</div>
            <div class="kb-summary-meta">最近更新: ${esc(updated)}</div>
        </a>`;
    }).join('');
}

async function refreshKnowledge() {
    await loadKnowledge();
}

function scheduleKnowledgeRefresh() {
    if (knowledgeRefreshTimer) return;
    knowledgeRefreshTimer = setTimeout(async () => {
        knowledgeRefreshTimer = null;
        await loadKnowledge();
    }, 250);
}

// ─── 日志 ───
function appendLog(entry) {
    const c = document.getElementById('logContainer');
    const cls = entry.level==='error'?'error': entry.level==='warn'?'warn': entry.level==='success'?'success':'';
    const div = document.createElement('div');
    div.className = `log-line ${cls}`;
    div.innerHTML = `<span class="log-time">${entry.time||now()}</span><span class="log-source">${esc(entry.source||'sys')}</span><span class="log-msg">${esc(entry.message||'')}</span>`;
    c.appendChild(div);
    if (c.children.length > 500) c.removeChild(c.firstChild);
    c.scrollTop = c.scrollHeight;
}
function clearLogs() { document.getElementById('logContainer').innerHTML = ''; }

// ─── 状态 ───
const STATUS_TEXT = { running:'运行中', paused:'已暂停', idle:'空闲', disconnected:'已断开' };
function setStatus(s) {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    dot.className = 'status-dot ' + (s === 'running' ? 'running' : s === 'paused' ? 'paused' : 'idle');
    text.textContent = STATUS_TEXT[s] || s;
    document.getElementById('btnStartAll').style.display = s === 'running' ? 'none' : '';
    document.getElementById('btnPauseAll').style.display = s === 'running' ? '' : 'none';
}

// ─── 计时器 ───
setInterval(() => {
    if (state.start_time > 0) {
        const e = Math.floor(Date.now()/1000) - state.start_time;
        document.getElementById('runtime').textContent =
            `${String(Math.floor(e/3600)).padStart(2,'0')}:${String(Math.floor((e%3600)/60)).padStart(2,'0')}:${String(e%60).padStart(2,'0')}`;
    }
}, 1000);

// ─── 工具函数 ───
function now() { const d=new Date(); return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`; }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
async function safeAction(fn) {
    try {
        return await fn();
    } catch (err) {
        console.error(err);
        alert(err.message || '操作失败');
        return null;
    }
}

// ─── 初始化 ───
document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/state').then(r=>r.json()).then(d=>{ state={...state,...d}; renderAll(); }).catch(()=>{});
    fetch('/api/logs').then(r=>r.json()).then(l=>{ l.forEach(appendLog); }).catch(()=>{});
    loadKnowledge();
    connectSSE();
});
document.addEventListener('click', e => { if (e.target.classList.contains('modal-overlay')) hideCreateModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideCreateModal(); });

Object.assign(window, {
    taskPause,
    taskResume,
    taskAbort,
    taskDelete,
    agentStart,
    agentPause,
    showCreateModal,
    hideCreateModal,
    createTask,
    clearLogs,
    refreshKnowledge,
});
