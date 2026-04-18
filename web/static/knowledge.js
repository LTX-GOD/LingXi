const DEFAULT_BUCKET = 'ctf_writeups';

const pageState = {
    overview: null,
    service: null,
    selectedBucket: DEFAULT_BUCKET,
    mode: 'browse',
    results: [],
    backend: '',
    query: '',
    category: '',
};

function esc(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatDate(value) {
    if (!value) return '未记录';
    return String(value).split('T')[0];
}

function getBucketFromQuery() {
    const params = new URLSearchParams(window.location.search);
    return params.get('bucket') || DEFAULT_BUCKET;
}

function updateUrlBucket(bucket) {
    const url = new URL(window.location.href);
    url.searchParams.set('bucket', bucket);
    history.replaceState({}, '', url);
}

async function fetchJson(url) {
    const response = await fetch(url);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || data.error || `请求失败: ${response.status}`);
    }
    return data;
}

async function loadOverview() {
    const [overview, service] = await Promise.all([
        fetchJson('/api/knowledge'),
        fetchJson('/api/knowledge/status'),
    ]);
    pageState.overview = overview;
    pageState.service = service;
    const buckets = overview?.buckets || [];
    if (!buckets.some(bucket => bucket.bucket === pageState.selectedBucket)) {
        pageState.selectedBucket = buckets.find(bucket => bucket.bucket === DEFAULT_BUCKET)?.bucket || buckets[0]?.bucket || DEFAULT_BUCKET;
    }
    renderOverview();
}

function selectedBucketSummary() {
    const buckets = pageState.overview?.buckets || [];
    return buckets.find(bucket => bucket.bucket === pageState.selectedBucket) || null;
}

function renderOverview() {
    const badgeEl = document.getElementById('knowledgeServiceBadge');
    const summaryEl = document.getElementById('knowledgeServiceSummary');
    const bucketsEl = document.getElementById('knowledgeBuckets');
    const service = pageState.service || {};
    const health = service.health || {};
    const buckets = pageState.overview?.buckets || [];

    badgeEl.className = `knowledge-home-pill ${service.status || 'idle'}`;
    badgeEl.textContent = service.status || 'unknown';

    const assetHints = [];
    if (service.assets?.index_exists) assetHints.push('索引快照已就绪');
    if (service.assets?.db_exists) assetHints.push('Milvus 已就绪');
    if (service.assets?.raw_exists) assetHints.push('原始归档已在仓库中');
    summaryEl.innerHTML = `
        <div class="knowledge-service-row">服务地址: <span>${esc(service.base_url || '未配置')}</span></div>
        <div class="knowledge-service-row">检索状态: <span>${esc(service.status || 'unknown')}</span></div>
        <div class="knowledge-service-row">向量后端: <span>${esc(health.backend || (health.collection ? 'milvus' : '未启动'))}</span></div>
        <div class="knowledge-service-row">资产更新: <span>${esc(formatDate(service.updated_at))}</span></div>
        <div class="knowledge-service-note">${esc(assetHints.join(' · ') || '当前仅显示资产状态')}</div>
    `;

    bucketsEl.innerHTML = buckets.map(bucket => {
        const active = bucket.bucket === pageState.selectedBucket ? 'active' : '';
        const categories = Object.entries(bucket.categories || {}).slice(0, 3)
            .map(([name, count]) => `${name} ${count}`)
            .join(' · ') || '暂无分类统计';
        return `<button class="knowledge-bucket-card ${active}" type="button" onclick="selectKnowledgeBucket('${esc(bucket.bucket)}')">
            <div class="knowledge-bucket-title">${esc(bucket.display_name)}</div>
            <div class="knowledge-bucket-count">${bucket.total || 0}</div>
            <div class="knowledge-bucket-meta">${esc(bucket.status || 'idle')} · ${esc(formatDate(bucket.updated_at))}</div>
            <div class="knowledge-bucket-desc">${esc(categories)}</div>
        </button>`;
    }).join('');

    renderCategoryOptions();
}

function renderCategoryOptions() {
    const categoryEl = document.getElementById('knowledgeCategory');
    const summary = selectedBucketSummary();
    const entries = Object.entries(summary?.categories || {});
    const current = pageState.category || '';
    categoryEl.innerHTML = '<option value="">全部类别</option>' + entries.map(([name, count]) => {
        const selected = current === name ? 'selected' : '';
        return `<option value="${esc(name)}" ${selected}>${esc(name)} (${count})</option>`;
    }).join('');
}

function renderResults() {
    const titleEl = document.getElementById('knowledgeResultsTitle');
    const metaEl = document.getElementById('knowledgeResultsMeta');
    const resultsEl = document.getElementById('knowledgeResults');
    const summary = selectedBucketSummary();
    const title = summary?.display_name || '知识记录';
    titleEl.textContent = title;

    const modeLabel = pageState.mode === 'search' ? `搜索结果 · ${pageState.results.length} 条` : `最近记录 · ${pageState.results.length} 条`;
    const backendLabel = pageState.backend ? `后端: ${pageState.backend}` : '';
    metaEl.textContent = [modeLabel, backendLabel].filter(Boolean).join(' · ');

    if (!pageState.results.length) {
        resultsEl.innerHTML = '<div class="knowledge-empty">当前没有可展示的知识记录。</div>';
        return;
    }

    resultsEl.innerHTML = pageState.results.map(item => {
        const isExternal = item.bucket === 'ctf_writeups' || item.source === 'ctf_writeups';
        const tags = isExternal
            ? [
                item.category && item.category !== 'unknown' ? item.category : '',
                item.event || '',
                item.difficulty && item.difficulty !== 'unknown' ? item.difficulty : '',
                item.year ? String(item.year) : '',
            ].filter(Boolean)
            : [
                item.category && item.category !== 'unknown' ? item.category : '',
                item.outcome_type && item.outcome_type !== 'reference' ? item.outcome_type : '',
                item.source_type && item.source_type !== 'external_writeup' ? item.source_type : '',
                item.event || '',
                item.year ? String(item.year) : '',
            ].filter(Boolean);
        const footer = isExternal
            ? [
                pageState.mode === 'search' && item.quality_score ? `命中 ${Number(item.quality_score).toFixed(1)}` : '',
                formatDate(item.created_at),
            ].filter(Boolean)
            : [
                item.confidence ? `置信 ${(Number(item.confidence) * 100).toFixed(0)}%` : '',
                item.quality_score ? `质量 ${Number(item.quality_score).toFixed(2)}` : '',
                item.discoveries_count ? `发现 ${item.discoveries_count}` : '',
                item.credentials_count ? `凭据 ${item.credentials_count}` : '',
                item.verified_flags_count ? `Flag ${item.verified_flags_count}` : '',
                formatDate(item.created_at),
            ].filter(Boolean);
        return `<article class="knowledge-result-card">
            <div class="knowledge-result-head">
                <div>
                    <div class="knowledge-result-title">${esc(item.title || item.challenge_code || '未命名记录')}</div>
                    <div class="knowledge-result-subtitle">${esc(tags.join(' · '))}</div>
                </div>
                ${item.url ? `<a class="knowledge-result-link" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">来源</a>` : ''}
            </div>
            <div class="knowledge-result-summary">${esc(item.summary || item.content || '暂无摘要')}</div>
            <div class="knowledge-result-footer">${esc(footer.join(' · '))}</div>
        </article>`;
    }).join('');
}

async function loadBrowse() {
    const params = new URLSearchParams({
        limit: document.getElementById('knowledgeTopK').value || '10',
    });
    if (pageState.category) {
        params.set('category', pageState.category);
    }
    const data = await fetchJson(`/api/knowledge/${encodeURIComponent(pageState.selectedBucket)}?${params.toString()}`);
    pageState.mode = 'browse';
    pageState.backend = data.backend || 'knowledge_store';
    pageState.results = data.records || [];
    renderResults();
}

async function runSearch() {
    const params = new URLSearchParams({
        bucket: pageState.selectedBucket,
        q: pageState.query,
        top_k: document.getElementById('knowledgeTopK').value || '10',
    });
    if (pageState.category) {
        params.set('category', pageState.category);
    }
    const data = await fetchJson(`/api/knowledge/search?${params.toString()}`);
    pageState.mode = 'search';
    pageState.backend = data.backend || 'unknown';
    pageState.results = data.results || [];
    renderResults();
}

async function refreshKnowledgePage() {
    await loadOverview();
    if (pageState.query) {
        await runSearch();
    } else {
        await loadBrowse();
    }
}

async function selectKnowledgeBucket(bucket) {
    pageState.selectedBucket = bucket;
    pageState.category = '';
    updateUrlBucket(bucket);
    renderOverview();
    if (pageState.query) {
        await runSearch();
    } else {
        await loadBrowse();
    }
}

async function onSubmitSearch(event) {
    event.preventDefault();
    pageState.query = document.getElementById('knowledgeQuery').value.trim();
    pageState.category = document.getElementById('knowledgeCategory').value;
    if (!pageState.query) {
        await loadBrowse();
        return;
    }
    await runSearch();
}

async function switchToBrowse() {
    pageState.query = '';
    document.getElementById('knowledgeQuery').value = '';
    pageState.category = document.getElementById('knowledgeCategory').value;
    await loadBrowse();
}

document.addEventListener('DOMContentLoaded', async () => {
    pageState.selectedBucket = getBucketFromQuery();
    document.getElementById('knowledgeSearchForm').addEventListener('submit', onSubmitSearch);
    document.getElementById('btnKnowledgeRefresh').addEventListener('click', refreshKnowledgePage);
    document.getElementById('btnKnowledgeBrowse').addEventListener('click', switchToBrowse);
    document.getElementById('knowledgeCategory').addEventListener('change', async (event) => {
        pageState.category = event.target.value;
        if (pageState.query) {
            await runSearch();
        } else {
            await loadBrowse();
        }
    });

    try {
        await loadOverview();
        await loadBrowse();
    } catch (error) {
        console.error(error);
        document.getElementById('knowledgeResults').innerHTML = `<div class="knowledge-empty">${esc(error.message || '知识库加载失败')}</div>`;
    }
});

Object.assign(window, {
    selectKnowledgeBucket,
    refreshKnowledgePage,
});
