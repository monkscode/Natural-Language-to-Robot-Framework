/* learning.js — SPA logic for /learning dashboard
 *
 * Hash routing:
 *   #            → Hints Browser
 *   #/hints/{id} → open hint detail drawer over Hints Browser
 *   #/triggers   → Trigger Events list
 *   #/triggers/{id} → Trigger detail
 *   #/stats      → Stats dashboard
 *
 * Actor for write operations: localStorage.getItem("feedback_user_name") || "dhruvil"
 */

// ============================================================
// State
// ============================================================

let hintsOffset = 0;
const HINTS_LIMIT = 20;
let hintsTotal = 0;
let hintsFilters = { status: '', scope: '', domain: '', search: '' };

let triggersOffset = 0;
const TRIGGERS_LIMIT = 30;
let triggersTotal = 0;

// ============================================================
// Utilities
// ============================================================

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function reltime(isoStr) {
    if (!isoStr) return '—';
    const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
    if (diff < 5)      return 'just now';
    if (diff < 3600)   return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400)  return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return new Date(isoStr).toLocaleDateString();
}

function fmtNum(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString();
}

function truncate(s, n = 120) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n) + '…' : s;
}

function getActor() {
    return localStorage.getItem('feedback_user_name') || 'dhruvil';
}

function hintStatusInfo(hint) {
    if (hint.is_active && hint.conflict_flagged) {
        return { label: 'FLAGGED', cls: 'lrn-badge-flagged' };
    }
    if (hint.is_active) {
        const label = hint.created_via === 'admin' ? 'ACTIVE (admin)' : 'ACTIVE';
        return { label, cls: 'lrn-badge-active' };
    }
    // sort_priority=4 → retracted (has retract audit row); =3 → auto-disabled or llm_review_disabled
    if (hint.sort_priority === 4) {
        return { label: 'RETRACTED', cls: 'lrn-badge-retracted' };
    }
    if (hint.llm_review_disabled) {
        return { label: 'LLM-DISABLED', cls: 'lrn-badge-llm-disabled' };
    }
    return { label: 'AUTO-DISABLED', cls: 'lrn-badge-disabled' };
}

function badgeHtml(hint) {
    const { label, cls } = hintStatusInfo(hint);
    return `<span class="lrn-badge ${cls}">${esc(label)}</span>`;
}

function infoIcon(tip) {
    return `<i class="lrn-info" data-tip="${esc(tip)}">i</i>`;
}

function pct(num, denom) {
    if (!denom) return '—';
    return (num / denom * 100).toFixed(1) + '%';
}

function fmtPctRate(r) {
    // r is a 0–1 fraction (a pass rate).
    if (r == null) return '—';
    return (r * 100).toFixed(1) + '%';
}

function fmtLift(v) {
    // v is a signed 0–1 fraction (a lift); render with an explicit sign.
    if (v == null) return '—';
    return (v >= 0 ? '+' : '−') + Math.abs(v * 100).toFixed(1) + '%';
}

// ============================================================
// API helpers
// ============================================================

async function apiFetch(path, opts = {}) {
    const resp = await fetch('/api/learning' + path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
    });
    if (resp.status === 503) {
        throw new Error('Learning system is disabled (OPTIMIZATION_ENABLED=false)');
    }
    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    return resp.json();
}

// ============================================================
// Router
// ============================================================

function navigate(hash) {
    location.hash = hash;
}

function setActiveTab(view) {
    document.querySelectorAll('.lrn-tab').forEach(t => t.classList.remove('active'));
    const tabMap = { hints: 'tab-hints', triggers: 'tab-triggers', 'trigger-detail': 'tab-triggers', stats: 'tab-stats', review: 'tab-review' };
    const el = document.getElementById(tabMap[view]);
    if (el) el.classList.add('active');
}

function showView(name) {
    ['hints', 'triggers', 'trigger-detail', 'stats', 'review'].forEach(v => {
        const el = document.getElementById('view-' + v);
        if (el) el.style.display = (v === name) ? '' : 'none';
    });
    setActiveTab(name);
}

function route() {
    const hash = location.hash.replace(/^#\/?/, '');
    if (hash !== 'review') {
        _clearReviewPoll();
    }

    if (!hash) {
        showView('hints');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ Hints';
        closeDrawer(false);
        renderHints(true);
        return;
    }

    const hintMatch = hash.match(/^hints\/(\d+)$/);
    if (hintMatch) {
        showView('hints');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ Hints';
        const id = parseInt(hintMatch[1], 10);
        if (!document.querySelector('.lrn-hint-card')) {
            renderHints(true).then(() => openHintDrawer(id, false));
        } else {
            openHintDrawer(id, false);
        }
        return;
    }

    const trigMatch = hash.match(/^triggers\/(\d+)$/);
    if (trigMatch) {
        showView('trigger-detail');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ Triggers ▸ Detail';
        renderTriggerDetail(parseInt(trigMatch[1], 10));
        return;
    }

    if (hash === 'triggers') {
        showView('triggers');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ Triggers';
        renderTriggers(true);
        return;
    }

    if (hash === 'stats') {
        showView('stats');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ Stats (last 30 days)';
        renderStats();
        return;
    }

    if (hash === 'review') {
        showView('review');
        document.getElementById('lrn-breadcrumb').textContent = 'Learning ▸ LLM Review';
        renderReview();
        return;
    }

    // Unknown hash → hints
    navigate('#');
}

window.addEventListener('hashchange', route);
document.addEventListener('DOMContentLoaded', () => {
    route();
    renderHealthBanner();
});

// ============================================================
// Health banner — surfaces silent learning-system degradation
// ============================================================

async function renderHealthBanner() {
    const el = document.getElementById('lrn-health-banner');
    if (!el) return;
    const MAP = {
        OK:       ['lrn-health-ok',       '✓ Learning system: OK'],
        DEGRADED: ['lrn-health-degraded', '⚠ Learning system: DEGRADED — a reliability check is failing; see server logs.'],
        FAILED:   ['lrn-health-failed',   '✕ Learning system: FAILED — learning is not functioning; see server logs.'],
        DISABLED: ['lrn-health-disabled', 'ℹ Learning system: disabled (OPTIMIZATION_ENABLED=false).'],
    };
    try {
        const data = await apiFetch('/health');
        const status = (data && data.status) || 'unknown';
        const [cls, label] = MAP[status]
            || ['lrn-health-degraded', `Learning system: ${status}`];
        el.innerHTML = `<div class="lrn-health-banner-inner ${cls}">${esc(label)}</div>`;
    } catch (e) {
        el.innerHTML = `<div class="lrn-health-banner-inner lrn-health-degraded">`
            + `Learning health unavailable: ${esc(e.message)}</div>`;
    }
}

// ============================================================
// Hints Browser
// ============================================================

async function renderHints(reset = false) {
    if (reset) { hintsOffset = 0; hintsTotal = 0; }

    const view = document.getElementById('view-hints');
    if (reset) {
        view.innerHTML = renderHintFilters() + '<div id="hints-list"></div><div id="hints-footer"></div>';
    }

    const list   = document.getElementById('hints-list');
    const footer = document.getElementById('hints-footer');

    const spinner = document.createElement('div');
    spinner.className = 'lrn-loading';
    spinner.textContent = 'Loading…';
    list.appendChild(spinner);

    const params = new URLSearchParams({ limit: HINTS_LIMIT, offset: hintsOffset });
    if (hintsFilters.status) params.set('status', hintsFilters.status);
    if (hintsFilters.scope)  params.set('scope',  hintsFilters.scope);
    if (hintsFilters.domain) params.set('domain', hintsFilters.domain);
    if (hintsFilters.search) params.set('search', hintsFilters.search);

    try {
        const data = await apiFetch('/hints?' + params);
        spinner.remove();
        hintsTotal  = data.total;
        hintsOffset += data.hints.length;

        if (reset && data.hints.length === 0) {
            list.innerHTML = '<div class="lrn-empty">No hints found. Try adjusting filters or add one with + Add feedback.</div>';
            footer.innerHTML = '';
            return;
        }

        data.hints.forEach(hint => {
            const card = document.createElement('div');
            card.className = 'lrn-hint-card';
            card.innerHTML = buildHintCard(hint);
            list.appendChild(card);
        });

        const showing = Math.min(hintsOffset, hintsTotal);
        const more = hintsOffset < hintsTotal;
        footer.innerHTML = `<div class="lrn-list-footer">Showing ${showing} of ${hintsTotal} hints${more ? ' &nbsp;·&nbsp; <button class="lrn-btn" onclick="loadMoreHints()">Load more</button>' : ''}</div>`;
    } catch (e) {
        spinner.remove();
        list.innerHTML += `<div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>`;
    }
}

function renderHintFilters() {
    const s = hintsFilters;
    return `
        <div class="lrn-filter-row">
            <select class="lrn-filter-select" id="filter-status" onchange="applyFilters()">
                <option value="">Status: All</option>
                <option value="flagged"${s.status==='flagged'?' selected':''}>Flagged</option>
                <option value="active"${s.status==='active'?' selected':''}>Active</option>
                <option value="auto_disabled"${s.status==='auto_disabled'?' selected':''}>Auto-disabled</option>
                <option value="llm_review_disabled"${s.status==='llm_review_disabled'?' selected':''}>LLM-disabled</option>
                <option value="retracted"${s.status==='retracted'?' selected':''}>Retracted</option>
            </select>
            <select class="lrn-filter-select" id="filter-scope" onchange="applyFilters()">
                <option value="">Scope: All</option>
                <option value="url"${s.scope==='url'?' selected':''}>URL</option>
                <option value="domain"${s.scope==='domain'?' selected':''}>Domain</option>
                <option value="global"${s.scope==='global'?' selected':''}>Global</option>
            </select>
            <input class="lrn-filter-input" id="filter-domain" type="text" placeholder="Domain filter…"
                   value="${esc(s.domain)}" oninput="debouncedApplyFilters()">
            <input class="lrn-filter-input" id="filter-search" type="text" placeholder="Search hint text…"
                   value="${esc(s.search)}" oninput="debouncedApplyFilters()">
        </div>
    `;
}

let _filterTimer = null;
function debouncedApplyFilters() {
    clearTimeout(_filterTimer);
    _filterTimer = setTimeout(applyFilters, 350);
}

function applyFilters() {
    hintsFilters = {
        status: document.getElementById('filter-status')?.value || '',
        scope:  document.getElementById('filter-scope')?.value  || '',
        domain: (document.getElementById('filter-domain')?.value  || '').trim(),
        search: (document.getElementById('filter-search')?.value  || '').trim(),
    };
    renderHints(true);
}

function loadMoreHints() { renderHints(false); }

function buildHintCard(hint) {
    const { label, cls } = hintStatusInfo(hint);
    const badge = `<span class="lrn-badge ${cls}">${esc(label)}</span>`;

    const metaParts = [hint.scope];
    if (hint.domain) metaParts.push(hint.domain);

    const flagNote = hint.conflict_flag_reason
        ? `<div class="lrn-hint-flag-reason">⚠ flagged ${reltime(hint.conflict_flagged_at)}: "${esc(truncate(hint.conflict_flag_reason, 100))}"</div>`
        : '';

    let actions = `<button class="lrn-btn" onclick="openHintDrawer(${hint.id});event.stopPropagation()">View detail</button>`;
    if (hint.is_active) {
        if (hint.conflict_flagged) {
            actions += ` <button class="lrn-btn lrn-btn-warn" onclick="quickUnflag(${hint.id});event.stopPropagation()">Unflag</button>`;
        }
        actions += ` <button class="lrn-btn lrn-btn-danger" onclick="quickRetract(${hint.id});event.stopPropagation()">Retract</button>`;
    } else {
        actions += ` <button class="lrn-btn lrn-btn-success" onclick="quickReactivate(${hint.id});event.stopPropagation()">Reactivate</button>`;
    }

    return `
        <div class="lrn-hint-card-top">
            ${badge}
            <span class="lrn-hint-text">${esc(truncate(hint.feedback_text, 160))}</span>
        </div>
        <div class="lrn-hint-meta">
            <span>${esc(metaParts.join(' · '))}</span>
            <span>applied ${hint.applied_count || 0}</span>
            <span>success ${hint.success_count || 0}</span>
            ${(hint.failure_count || 0) > 0 ? `<span>failure ${hint.failure_count}</span>` : ''}
            <span>created via ${esc(hint.created_via || '—')}</span>
            <span>last seen ${reltime(hint.last_seen)}</span>
        </div>
        ${flagNote}
        <div class="lrn-hint-actions">${actions}</div>
    `;
}

// ── Quick actions (from hint card, no drawer) ──

async function quickUnflag(id) {
    try {
        await apiFetch(`/hints/${id}/unflag`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        renderHints(true);
    } catch (e) { alert('Unflag failed: ' + e.message); }
}

async function quickRetract(id) {
    if (!confirm('Retract this hint? It will stop injecting into agent prompts.')) return;
    try {
        await apiFetch(`/hints/${id}/retract`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        renderHints(true);
    } catch (e) { alert('Retract failed: ' + e.message); }
}

async function quickReactivate(id) {
    try {
        await apiFetch(`/hints/${id}/reactivate`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        renderHints(true);
    } catch (e) { alert('Reactivate failed: ' + e.message); }
}

// ============================================================
// Hint Detail Drawer
// ============================================================

async function openHintDrawer(id, updateHash = true) {
    const overlay = document.getElementById('drawer-overlay');
    const body    = document.getElementById('drawer-body');
    const title   = document.getElementById('drawer-title');

    overlay.style.display = '';
    body.innerHTML = '<div class="lrn-loading">Loading…</div>';
    title.textContent = `Hint #${id}`;

    if (updateHash && location.hash !== `#/hints/${id}`) {
        history.pushState(null, '', `#/hints/${id}`);
    }

    try {
        const data     = await apiFetch(`/hints/${id}`);
        const hint     = data.hint;
        const timeline = data.timeline || [];

        const scopeParts = [hint.scope, hint.domain, hint.url].filter(Boolean);

        const timelineHtml = timeline.length === 0
            ? '<div class="lrn-empty" style="padding:0.75rem 0">No audit history yet</div>'
            : `<ul class="lrn-timeline">${timeline.map(buildTimelineItem).join('')}</ul>`;

        const editHtml = `
            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Edit</div>
                <div class="lrn-drawer-edit-row">
                    <div>
                        <label class="lrn-label" style="margin-bottom:2px">Scope</label>
                        <select class="lrn-select" id="edit-scope-${id}" onchange="toggleScopeFields(${id})">
                            <option value="url"${hint.scope==='url'?' selected':''}>url</option>
                            <option value="domain"${hint.scope==='domain'?' selected':''}>domain</option>
                            <option value="global"${hint.scope==='global'?' selected':''}>global</option>
                        </select>
                    </div>
                    <div>
                        <label class="lrn-label" style="margin-bottom:2px">Category</label>
                        <select class="lrn-select" id="edit-category-${id}">
                            ${['structural','B1','B2','C1','D1','uncategorized'].map(c =>
                                `<option value="${c}"${hint.category===c?' selected':''}>${c}</option>`).join('')}
                        </select>
                    </div>
                    <div style="align-self:flex-end">
                        <button class="lrn-btn" onclick="saveHintEdits(${id})">Save</button>
                    </div>
                </div>
                <div id="edit-scope-fields-${id}" style="margin-top:0.5rem">
                    ${buildScopeFields(id, hint.scope, hint.url, hint.domain)}
                </div>
            </div>
        `;

        let actionHtml = '<div class="lrn-hint-actions">';
        if (hint.is_active) {
            if (hint.conflict_flagged) {
                actionHtml += `<button class="lrn-btn lrn-btn-warn" onclick="drawerUnflag(${id})">Unflag</button>`;
            }
            actionHtml += `<button class="lrn-btn lrn-btn-danger" onclick="drawerRetract(${id})">Retract</button>`;
        } else {
            actionHtml += `<button class="lrn-btn lrn-btn-success" onclick="drawerReactivate(${id})">Reactivate</button>`;
        }
        actionHtml += '</div>';

        body.innerHTML = `
            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Status</div>
                ${badgeHtml(hint)}
                ${hint.conflict_flag_reason
                    ? `<div class="lrn-hint-flag-reason" style="margin-top:0.4rem">⚠ ${esc(hint.conflict_flag_reason)}</div>`
                    : ''}
            </div>

            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Hint text</div>
                <div class="lrn-drawer-hint-text">${esc(hint.feedback_text)}</div>
            </div>

            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Anchored to (similarity match)</div>
                <div class="lrn-drawer-hint-text">${esc(hint.anchor_query || '—')}</div>
            </div>

            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Metadata</div>
                <div class="lrn-kv-row"><span>Scope</span><span>${esc(scopeParts.join(' / '))}</span></div>
                <div class="lrn-kv-row"><span>Category</span><span>${esc(hint.category || '—')}</span></div>
                <div class="lrn-kv-row"><span>Failure category</span><span>${esc(hint.original_failure_category || '—')}</span></div>
                <div class="lrn-kv-row"><span>Created via</span><span>${esc(hint.created_via || '—')}</span></div>
                <div class="lrn-kv-row"><span>Created</span><span>${reltime(hint.created_at)}</span></div>
                <div class="lrn-kv-row"><span>Last seen</span><span>${reltime(hint.last_seen)}</span></div>
                <div class="lrn-kv-row"><span>Applied / Success / Fail</span><span>${hint.applied_count||0} / ${hint.success_count||0} / ${hint.failure_count||0}</span></div>
                <div class="lrn-kv-row"><span>Evidence count</span><span>${hint.evidence_count||0}</span></div>
            </div>

            ${editHtml}

            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Actions</div>
                ${actionHtml}
            </div>

            <div class="lrn-drawer-section">
                <div class="lrn-drawer-section-title">Audit timeline</div>
                ${timelineHtml}
            </div>
        `;
    } catch (e) {
        body.innerHTML = `<div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>`;
    }
}

function buildScopeFields(id, scope, url, domain) {
    if (scope === 'url') {
        return `<div class="lrn-scope-sub"><label>URL</label>
            <input type="text" id="edit-url-${id}" class="lrn-input" value="${esc(url||'')}" placeholder="https://…"></div>`;
    }
    if (scope === 'domain') {
        return `<div class="lrn-scope-sub"><label>Domain</label>
            <input type="text" id="edit-domain-${id}" class="lrn-input" value="${esc(domain||'')}" placeholder="example.com"></div>`;
    }
    return '';
}

function toggleScopeFields(id) {
    const scope  = document.getElementById(`edit-scope-${id}`)?.value;
    const urlVal = document.getElementById(`edit-url-${id}`)?.value    || '';
    const domVal = document.getElementById(`edit-domain-${id}`)?.value || '';
    const cont   = document.getElementById(`edit-scope-fields-${id}`);
    if (cont) cont.innerHTML = buildScopeFields(id, scope, urlVal, domVal);
}

async function saveHintEdits(id) {
    const scope    = document.getElementById(`edit-scope-${id}`)?.value;
    const category = document.getElementById(`edit-category-${id}`)?.value;
    const url      = document.getElementById(`edit-url-${id}`)?.value?.trim()    || undefined;
    const domain   = document.getElementById(`edit-domain-${id}`)?.value?.trim() || undefined;
    try {
        await apiFetch(`/hints/${id}`, {
            method: 'PATCH',
            body: JSON.stringify({ scope, category, url, domain, actor: getActor() }),
        });
        openHintDrawer(id, false);
    } catch (e) { alert('Save failed: ' + e.message); }
}

async function drawerUnflag(id) {
    try {
        await apiFetch(`/hints/${id}/unflag`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        openHintDrawer(id, false);
        renderHints(true);
    } catch (e) { alert('Unflag failed: ' + e.message); }
}

async function drawerRetract(id) {
    if (!confirm('Retract this hint?')) return;
    try {
        await apiFetch(`/hints/${id}/retract`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        openHintDrawer(id, false);
        renderHints(true);
    } catch (e) { alert('Retract failed: ' + e.message); }
}

async function drawerReactivate(id) {
    try {
        await apiFetch(`/hints/${id}/reactivate`, { method: 'POST', body: JSON.stringify({ actor: getActor() }) });
        openHintDrawer(id, false);
        renderHints(true);
    } catch (e) { alert('Reactivate failed: ' + e.message); }
}

function buildTimelineItem(item) {
    let body = '';
    if (item.source === 'hint_audit') {
        const labels = {
            create: 'created', unflag: 'unflagged', retract: 'retracted',
            reactivate: 'reactivated', change_scope: 'changed scope', change_category: 'changed category',
            patch: 'edited',
            llm_review_disable: 'LLM review: disabled',
            llm_review_reactivate: 'LLM review: reactivated',
            llm_review_unflag: 'LLM review: unflagged',
            llm_review_keep: 'LLM review: kept',
            llm_review_flagged: 'LLM review: flagged for human review',
            auto_disable: 'auto-disabled',
            trigger_1_flag: 'Trigger 1: flagged',
            trigger_2_flag: 'Trigger 2: flagged',
        };
        const verb  = labels[item.action] || item.action;
        const actor = item.actor || 'unknown';
        let detail  = '';
        if (item.before_value && item.after_value) {
            try {
                const bef = typeof item.before_value === 'string' ? JSON.parse(item.before_value) : item.before_value;
                const aft = typeof item.after_value  === 'string' ? JSON.parse(item.after_value)  : item.after_value;
                const bs  = Object.entries(bef).map(([k,v]) => `${k}=${v}`).join(', ');
                const as_ = Object.entries(aft).map(([k,v]) => `${k}=${v}`).join(', ');
                if (bs && as_) detail = ` (${bs} → ${as_})`;
            } catch (_) {}
        }
        body = `<span class="lrn-timeline-actor">${esc(actor)}</span> ${esc(verb)}${detail}`;
        if (item.reason) {
            body += `<div class="lrn-timeline-reason">${esc(truncate(item.reason, 120))}</div>`;
        }
    } else {
        // trigger_events
        const triggerVerb = item.action === 'flag_recommended_suppressed'
            ? 'recommended flag (suppressed by history guard)'
            : 'flagged this hint';
        body = `<span class="lrn-timeline-actor">${esc(item.trigger_type || 'trigger')}</span> ${triggerVerb}`;
        if (item.reason) {
            body += `<div class="lrn-timeline-reason">${esc(truncate(item.reason, 120))}</div>`;
        }
        if (item.workflow_id) {
            body += `<div class="lrn-timeline-reason" style="font-style:normal">workflow: <code style="font-size:0.75rem">${esc(item.workflow_id)}</code></div>`;
        }
    }
    return `
        <li class="lrn-timeline-item">
            <span class="lrn-timeline-when">${reltime(item.created_at)}</span>
            <div class="lrn-timeline-body">${body}</div>
        </li>
    `;
}

function closeDrawer(updateHash = true) {
    document.getElementById('drawer-overlay').style.display = 'none';
    if (updateHash && /^#\/?hints\//.test(location.hash)) {
        history.pushState(null, '', '#');
    }
}

function maybeCloseDrawer(e) {
    if (e.target === document.getElementById('drawer-overlay')) closeDrawer();
}

// ============================================================
// Add Feedback Modal
// ============================================================

function openAddModal() {
    const body   = document.getElementById('add-modal-body');
    const footer = document.getElementById('add-modal-footer');

    body.innerHTML = `
        <div class="lrn-form-group">
            <label class="lrn-label">Feedback text *</label>
            <textarea class="lrn-textarea" id="modal-text" maxlength="500"
                      oninput="updateCharCount()" placeholder="Describe the rule or correction…" rows="3"></textarea>
            <div class="lrn-char-count" id="modal-char-count">0 / 500</div>
        </div>

        <div class="lrn-form-group">
            <label class="lrn-label">Example user request this hint applies to *</label>
            <textarea class="lrn-textarea" id="modal-anchor" maxlength="500" rows="2"
                      placeholder="e.g. verify the product list loads after applying a filter"></textarea>
            <div class="lrn-hint-text-small">The similarity filter matches future test queries against this. Required, 3–500 characters.</div>
        </div>

        <div class="lrn-form-group">
            <label class="lrn-label">Scope *</label>
            <div class="lrn-radio-group">
                <label class="lrn-radio-label">
                    <input type="radio" name="modal-scope" value="url" onchange="onScopeChange()"> This URL only
                </label>
                <div id="modal-url-sub" class="lrn-scope-sub" style="display:none">
                    <label>URL</label>
                    <input type="text" id="modal-url" placeholder="https://…">
                </div>
                <label class="lrn-radio-label">
                    <input type="radio" name="modal-scope" value="domain" checked onchange="onScopeChange()"> This domain
                </label>
                <div id="modal-domain-sub" class="lrn-scope-sub">
                    <label>Domain</label>
                    <input type="text" id="modal-domain" placeholder="example.com">
                </div>
                <label class="lrn-radio-label">
                    <input type="radio" name="modal-scope" value="global" onchange="onScopeChange()"> Global (all sites)
                </label>
            </div>
        </div>

        <div class="lrn-form-group">
            <label class="lrn-label">Category</label>
            <select class="lrn-select" id="modal-category">
                <option value="structural">structural</option>
                <option value="B1">B1</option>
                <option value="B2">B2</option>
                <option value="C1">C1</option>
                <option value="D1">D1</option>
                <option value="uncategorized" selected>uncategorized</option>
            </select>
        </div>

        <div class="lrn-form-group">
            <label class="lrn-label">Original failure category</label>
            <select class="lrn-select" id="modal-ofc">
                <option value="">— none —</option>
                <option value="B1">B1</option>
                <option value="B2">B2</option>
                <option value="C1">C1</option>
                <option value="D1">D1</option>
            </select>
            <div class="lrn-hint-text-small">Leaving blank disables failure-based auto-disable for this hint</div>
        </div>

        <div class="lrn-form-group">
            <label class="lrn-checkbox-label">
                <input type="checkbox" id="modal-triage">
                Run automatic triage on this text (override category)
            </label>
        </div>
    `;

    footer.innerHTML = `
        <button class="btn btn-secondary" onclick="closeAddModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitAddFeedback()">Add feedback</button>
    `;

    document.getElementById('modal-overlay').style.display = '';
}

function updateCharCount() {
    const ta  = document.getElementById('modal-text');
    const ctr = document.getElementById('modal-char-count');
    if (!ta || !ctr) return;
    const n = ta.value.length;
    ctr.textContent = `${n} / 500`;
    ctr.className = 'lrn-char-count' + (n > 490 ? ' over' : '');
}

function onScopeChange() {
    const scope = document.querySelector('input[name="modal-scope"]:checked')?.value || 'domain';
    document.getElementById('modal-url-sub').style.display    = scope === 'url'    ? '' : 'none';
    document.getElementById('modal-domain-sub').style.display = scope === 'domain' ? '' : 'none';
}

async function submitAddFeedback() {
    const text   = (document.getElementById('modal-text')?.value     || '').trim();
    const anchor = (document.getElementById('modal-anchor')?.value   || '').trim();
    const scope  = document.querySelector('input[name="modal-scope"]:checked')?.value || 'domain';
    const url    = (document.getElementById('modal-url')?.value      || '').trim() || null;
    const domain = (document.getElementById('modal-domain')?.value   || '').trim() || null;
    const cat    = document.getElementById('modal-category')?.value  || null;
    const ofc    = document.getElementById('modal-ofc')?.value       || null;
    const triage = document.getElementById('modal-triage')?.checked  || false;

    if (!text)                            { alert('Feedback text is required'); return; }
    if (text.length > 500)                { alert('Feedback text must be ≤ 500 characters'); return; }
    if (anchor.length < 3)                { alert('Example user request is required (≥ 3 characters)'); return; }
    if (anchor.length > 500)              { alert('Example user request must be ≤ 500 characters'); return; }
    if (scope === 'url'    && !url)       { alert('URL is required for URL scope'); return; }
    if (scope === 'domain' && !domain)    { alert('Domain is required for domain scope'); return; }

    try {
        await apiFetch('/hints', {
            method: 'POST',
            body: JSON.stringify({
                feedback_text: text, anchor_query: anchor, scope, url, domain,
                category: cat,
                original_failure_category: ofc || null,
                run_triage: triage,
                actor: getActor(),
            }),
        });
        closeAddModal();
        renderHints(true);
    } catch (e) { alert('Failed to add feedback: ' + e.message); }
}

function closeAddModal() {
    document.getElementById('modal-overlay').style.display = 'none';
}

function maybeCloseModal(e) {
    if (e.target === document.getElementById('modal-overlay')) closeAddModal();
}

// ============================================================
// Triggers View
// ============================================================

async function renderTriggers(reset = false) {
    if (reset) { triggersOffset = 0; triggersTotal = 0; }

    const view = document.getElementById('view-triggers');
    if (reset) {
        view.innerHTML = `
            <div class="lrn-table-card">
                <div class="lrn-table-header">
                    <span class="lrn-table-header-title">Trigger Events</span>
                    <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
                        <select class="lrn-filter-select" id="filter-ttype" onchange="applyTriggerFilters()">
                            <option value="">All types</option>
                            <option value="trigger_1">Trigger 1 (fix-after-fail)</option>
                            <option value="trigger_2">Trigger 2 (user feedback)</option>
                        </select>
                        <select class="lrn-filter-select" id="filter-tsince" onchange="applyTriggerFilters()">
                            <option value="">All time</option>
                            <option value="30d" selected>Last 30 days</option>
                            <option value="7d">Last 7 days</option>
                            <option value="1d">Today</option>
                        </select>
                    </div>
                </div>
                <div style="overflow-x:auto">
                    <table class="lrn-table">
                        <thead>
                            <tr>
                                <th>When</th>
                                <th>Type</th>
                                <th>Workflow</th>
                                <th>Domain</th>
                                <th>Flagged hints</th>
                                <th>LLM status</th>
                                <th>Tokens (in→out)</th>
                                <th>Latency</th>
                            </tr>
                        </thead>
                        <tbody id="triggers-tbody">
                            <tr><td colspan="8" style="text-align:center;padding:2rem">Loading…</td></tr>
                        </tbody>
                    </table>
                </div>
                <div id="triggers-footer" class="lrn-list-footer" style="padding:0.75rem 1rem"></div>
            </div>
        `;
    }

    await loadTriggerRows();
}

async function applyTriggerFilters() {
    triggersOffset = 0; triggersTotal = 0;
    const tbody = document.getElementById('triggers-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem">Loading…</td></tr>';
    await loadTriggerRows();
}

async function loadTriggerRows() {
    const tbody  = document.getElementById('triggers-tbody');
    const footer = document.getElementById('triggers-footer');
    if (!tbody) return;

    const ttype  = document.getElementById('filter-ttype')?.value  || '';
    const tsince = document.getElementById('filter-tsince')?.value || '30d';

    const params = new URLSearchParams({ limit: TRIGGERS_LIMIT, offset: triggersOffset });
    if (ttype)  params.set('trigger_type', ttype);
    if (tsince) params.set('since', tsince);

    try {
        const data = await apiFetch('/triggers?' + params);
        triggersTotal  = data.total;
        triggersOffset += data.triggers.length;

        // First page clears placeholder; subsequent pages append
        if (triggersOffset === data.triggers.length) {
            tbody.innerHTML = '';
        }

        if (data.triggers.length === 0 && triggersOffset === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--text-secondary)">No trigger events found</td></tr>';
            if (footer) footer.textContent = '';
            return;
        }

        data.triggers.forEach(t => {
            const tr = document.createElement('tr');
            tr.onclick = () => navigate(`#/triggers/${t.id}`);

            const flaggedIds = safeParseJson(t.flagged_hint_ids, []);
            // schema v12: actually_flagged_hint_ids = enforcement. NULL on
            // legacy rows → fall back to flagged_hint_ids (old semantics).
            const actualIds = safeParseJson(
                t.actually_flagged_hint_ids != null
                    ? t.actually_flagged_hint_ids
                    : t.flagged_hint_ids,
                []
            );
            const flagCell = actualIds.length > 0
                ? `<span class="lrn-flagged-badge">[${actualIds.join(', ')}]</span>`
                : (flaggedIds.length > 0
                    ? `<span style="color:var(--text-secondary)" title="LLM recommended flagging these, but the strong-history guard protected them">[${flaggedIds.join(', ')}]<sup>*</sup></span>`
                    : '—');

            const inTok  = t.input_tokens  != null ? fmtNum(t.input_tokens)  : '—';
            const outTok = t.output_tokens != null ? fmtNum(t.output_tokens) : '—';
            const tokStr = (t.input_tokens != null || t.output_tokens != null) ? `${inTok}→${outTok}` : '—';
            const latStr = t.llm_latency_ms != null ? `${Number(t.llm_latency_ms).toFixed(0)}ms` : '—';

            tr.innerHTML = `
                <td>${reltime(t.created_at)}</td>
                <td><code style="font-size:0.78rem">${esc(t.trigger_type)}</code></td>
                <td><code style="font-size:0.75rem">${esc((t.workflow_id||'').slice(0,8))}…</code></td>
                <td>${esc(t.domain || '—')}</td>
                <td>${flagCell}</td>
                <td>${esc(t.status || '—')}</td>
                <td style="font-family:var(--font-mono);font-size:0.8rem">${tokStr}</td>
                <td style="font-family:var(--font-mono);font-size:0.8rem">${latStr}</td>
            `;
            tbody.appendChild(tr);
        });

        const showing  = Math.min(triggersOffset, triggersTotal);
        const canMore  = triggersOffset < triggersTotal;
        if (footer) {
            footer.innerHTML = `Showing ${showing} of ${triggersTotal} events${canMore ? ' &nbsp;·&nbsp; <button class="lrn-btn" onclick="loadMoreTriggers()">Load more</button>' : ''}`;
        }
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--error)">${esc(e.message)}</td></tr>`;
    }
}

function loadMoreTriggers() { loadTriggerRows(); }

function safeParseJson(s, fallback) {
    try { return JSON.parse(s || '[]'); } catch (_) { return fallback; }
}

// ============================================================
// Trigger Detail
// ============================================================

async function renderTriggerDetail(id) {
    const view = document.getElementById('view-trigger-detail');
    view.innerHTML = `<button class="lrn-back-link" onclick="navigate('#/triggers')">← Triggers</button>
                      <div class="lrn-loading">Loading…</div>`;

    try {
        const data      = await apiFetch(`/triggers/${id}`);
        const t         = data.trigger;
        const ex        = data.execution;
        const hintTexts = data.hint_texts || {};

        const flaggedIds = safeParseJson(t.flagged_hint_ids, []);
        const activeIds  = safeParseJson(t.active_hint_ids,  []);
        // schema v12: actually_flagged_hint_ids = enforcement (what was
        // committed to conflict_flagged=1). NULL on pre-v12 rows → fall
        // back so legacy data still renders.
        const actualIds = safeParseJson(
            t.actually_flagged_hint_ids != null
                ? t.actually_flagged_hint_ids
                : t.flagged_hint_ids,
            []
        );
        const recommendedSet = new Set(flaggedIds.map(String));
        const enforcedSet    = new Set(actualIds.map(String));

        const activeHintsHtml = activeIds.length > 0
            ? `<ul class="lrn-hint-list" style="list-style:none">${activeIds.map(hid => {
                const text = hintTexts[hid] ? ` — <span style="color:var(--text-secondary)">${esc(truncate(hintTexts[hid], 120))}</span>` : '';
                const k = String(hid);
                let badge = '';
                if (enforcedSet.has(k)) {
                    badge = '<span class="lrn-flagged-badge">FLAGGED</span> ';
                } else if (recommendedSet.has(k)) {
                    badge = '<span class="lrn-flagged-badge" style="background:var(--text-secondary);opacity:0.7" title="LLM recommended flagging — suppressed by strong-history guard">SUPPRESSED</span> ';
                }
                return `<li style="padding:0.35rem 0;border-bottom:1px solid var(--border-color);font-size:0.85rem">
                    ${badge}<strong>#${hid}</strong>${text}
                </li>`;
            }).join('')}</ul>`
            : '<div class="lrn-empty" style="padding:0.5rem 0">None recorded</div>';

        const codeSection = ex ? `
            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Code comparison</div>
                ${ex.user_query ? `<div style="font-size:0.82rem;color:var(--text-secondary);margin-bottom:0.75rem">Query: ${esc(ex.user_query)}</div>` : ''}
                <div class="lrn-code-pair">
                    <div>
                        <div class="lrn-label" style="margin-bottom:4px">Failed code (robot_code)</div>
                        <pre class="lrn-code-block">${esc(ex.robot_code || '—')}</pre>
                    </div>
                    <div>
                        <div class="lrn-label" style="margin-bottom:4px">Working code (working_code)</div>
                        <pre class="lrn-code-block">${esc(ex.working_code || '—')}</pre>
                    </div>
                </div>
            </div>
        ` : '';

        view.innerHTML = `
            <button class="lrn-back-link" onclick="navigate('#/triggers')">← Triggers</button>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Event #${t.id} — ${esc(t.trigger_type)}</div>
                <div class="lrn-kv-row"><span>When</span><span>${reltime(t.created_at)}</span></div>
                <div class="lrn-kv-row"><span>Workflow</span><span style="font-family:var(--font-mono);font-size:0.78rem">${esc(t.workflow_id || '—')}</span></div>
                <div class="lrn-kv-row"><span>Domain</span><span>${esc(t.domain || '—')}</span></div>
                <div class="lrn-kv-row"><span>LLM status</span><span>${esc(t.status || '—')}</span></div>
                <div class="lrn-kv-row"><span>Model</span><span>${esc(t.llm_model || '—')}</span></div>
                <div class="lrn-kv-row"><span>Tokens (in / out)</span><span style="font-family:var(--font-mono)">${fmtNum(t.input_tokens)} / ${fmtNum(t.output_tokens)}</span></div>
                <div class="lrn-kv-row"><span>Latency</span><span>${t.llm_latency_ms != null ? Number(t.llm_latency_ms).toFixed(0)+'ms' : '—'}</span></div>
            </div>

            ${t.reason ? `
                <div class="lrn-stats-section">
                    <div class="lrn-stats-section-title">LLM reason</div>
                    <div class="lrn-drawer-hint-text">${esc(t.reason)}</div>
                </div>` : ''}

            ${flaggedIds.length > 0 ? `
                <div class="lrn-stats-section">
                    <div class="lrn-stats-section-title">Flag outcome</div>
                    <div style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:0.5rem">
                        LLM judgment vs what the strong-history guard let through.
                    </div>
                    <div class="lrn-kv-row">
                        <span>LLM recommended</span>
                        <span style="font-family:var(--font-mono)">${flaggedIds.join(', ') || '—'}</span>
                    </div>
                    <div class="lrn-kv-row">
                        <span>Actually flagged</span>
                        <span style="font-family:var(--font-mono);color:${actualIds.length > 0 ? 'var(--error)' : 'var(--text-secondary)'}">${actualIds.length > 0 ? actualIds.join(', ') : 'none — all suppressed by strong-history guard'}</span>
                    </div>
                </div>` : ''}

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Hints considered (active at fire time)</div>
                ${activeHintsHtml}
            </div>

            ${codeSection}
        `;
    } catch (e) {
        view.innerHTML = `
            <button class="lrn-back-link" onclick="navigate('#/triggers')">← Triggers</button>
            <div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>
        `;
    }
}

// ============================================================
// Stats
// ============================================================

// Tooltip text locked in from plan spec
const TOOLTIPS = {
    active:           'Hints currently being injected into agent prompts. These are the rules actively shaping generated tests.',
    flagged:          'Hints temporarily suspended from injection by the LLM conflict detector. Data is preserved — unflag to restore. Review these on the Hints page.',
    auto_disabled:    'Hints the framework deactivated automatically because they consistently failed. Different from manually retracted.',
    retracted:        'Hints you deactivated manually. They no longer inject but remain in the database for audit.',
    admin_created:    'Hints you added directly via the Add Feedback form, not via running a workflow.',
    workflow_created: 'Hints created automatically when a user submitted feedback after a test run.',
    trigger1_fired:   'Number of times the framework auto-detected a fix-after-failure pattern (test failed → user fixed code → test passed). The strongest signal that a hint was misleading the generation.',
    trigger2_fired:   'Number of times the framework analyzed user feedback for conflicts with existing hints. Fires once per feedback submission with hints in scope.',
    pct_succeeded:    'Fraction of trigger calls where the LLM responded with valid JSON. Anything below 100% indicates infra issues (timeouts, network), not judgment issues.',
    flagged_on_pct:   'Of trigger calls that succeeded, the fraction that resulted in flagging ≥1 hint. Lower = more conservative LLM behavior.',
    engagement_rate:  "Fraction of flag events you've looked at (touched the flagged hint in some way after the flag fired). If this is low, the reversal rate beside it is unreliable — flags may be wrong but you haven't gotten to them.",
    reversal_rate:    "Within events you've reviewed, the fraction where you overrode the LLM via Unflag. Measures how often you disagree with the LLM's judgment. 0–10% trustworthy, 10–30% tune prompt, >30% investigate. Only meaningful when reviewed count is at least 10.",
    pending_review:   "Flagged hints you haven't acted on since they were flagged. Open the Hints page filtered by Flagged to clear these.",
    unflags:          'Total times Unflag was clicked on a flagged hint in the period. Includes implicit unflags from re-submitting identical feedback.',
    retracts:         'Total times a hint was retracted in the period. Retract = "kill it" rather than "restore it". Tracked separately because it doesn\'t measure LLM judgment quality.',
    input_tokens:     'Total LLM tokens consumed by Trigger 1 + Trigger 2 calls in the period.',
    output_tokens:    'Total LLM output tokens consumed by Trigger 1 + Trigger 2 calls in the period.',
    est_cost:         "Approximate dollar cost based on the active model's price table. Update the table in code when the model changes.",
    cat_a:        'Workflows where no hints were available — the organic baseline. Excludes R7 holdout runs.',
    cat_b:        'Workflows where learned hints were injected — the treatment group.',
    cat_c:        'R7 holdout: workflows where hints WERE available but deliberately suppressed (a ~5% sample). The unbiased control group.',
    biased_lift:  'Cat B pass rate minus Cat A. Biased — Cat B queries resemble past successes, so this flatters the system. Kept for continuity, not trusted.',
    honest_lift:  'Cat B pass rate minus Cat C — the trustworthy lift. Both groups had hints available, so selection bias is removed. Shows "insufficient data" until enough holdout (Cat C) samples accrue.',
};

function ii(key) { return infoIcon(TOOLTIPS[key] || ''); }

async function renderStats() {
    const view = document.getElementById('view-stats');
    view.innerHTML = '<div class="lrn-loading">Loading…</div>';

    try {
        const s          = await apiFetch('/stats');
        const inv        = s.hint_inventory    || {};
        const kpi        = s.llm_accuracy      || {};
        const man        = s.manual_actions    || {};
        const costData   = s.llm_cost          || {};
        const trigAct    = s.trigger_activity  || [];

        const t1 = trigAct.find(r => r.trigger_type === 'trigger_1') || {};
        const t2 = trigAct.find(r => r.trigger_type === 'trigger_2') || {};

        const {
            reviewed_events: reviewed = 0,
            reversed_events: reversed = 0,
            flagged_events:  flaggedEv = 0,
            pending_review:  pending   = 0,
            engagement_rate: engRate,
            reversal_rate:   revRate,
            threshold_applicable: threshOk = false,
        } = kpi;

        let revCls = '';
        if (threshOk && revRate != null) {
            if      (revRate < 0.10) revCls = 'rate-good';
            else if (revRate < 0.30) revCls = 'rate-warn';
            else                     revCls = 'rate-bad';
        }

        const revDisplay = revRate != null ? `${reversed} / ${reviewed} (${(revRate * 100).toFixed(1)}%)` : '—';
        const engDisplay = engRate != null ? `${reviewed} / ${flaggedEv} (${(engRate * 100).toFixed(1)}%)` : '—';
        const lowNNote   = (!threshOk && reviewed > 0)
            ? `<span class="lrn-kpi-note">&nbsp;(low sample — ${reviewed} reviewed; need 10 for threshold coloring)</span>`
            : '';

        const costRows    = costData.by_model || [];
        const totalCostUSD = costData.total_estimated_usd || 0;
        const totalInTok   = costRows.reduce((a, r) => a + (r.input_tokens  || 0), 0);
        const totalOutTok  = costRows.reduce((a, r) => a + (r.output_tokens || 0), 0);
        const rateInfo     = costRows[0]?.rate_info;
        const modelName    = costRows[0]?.llm_model || null;
        const rateNote     = (rateInfo && modelName)
            ? `<span class="lrn-kpi-note">(${esc(modelName)} @ $${rateInfo.input_cost_per_1m_tokens}/1M in, $${rateInfo.output_cost_per_1m_tokens}/1M out)</span>`
            : '';

        // Learning effectiveness — Cat A / B / C and the two lift figures.
        const eff  = (s.learning_effectiveness || {}).natural_comparison || {};
        const catA = eff.no_hints_available || {};
        const catB = eff.hints_injected     || {};
        const catC = eff.holdout_suppressed || {};

        view.innerHTML = `
            <div class="lrn-stats-legend">
                ℹ This page answers: "Is the framework's auto-suspension of bad hints trustworthy?"
                The headline is the reversal rate — how often you've overridden the LLM.
                Hover any <i class="lrn-info" data-tip="Info icon — hover to see metric explanation">i</i> icon for details.
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Hint inventory</div>
                <div class="lrn-stats-grid">
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Active ${ii('active')}</div>
                        <div class="lrn-stat-value">${inv.active ?? '—'}</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Flagged ${ii('flagged')}</div>
                        <div class="lrn-stat-value" style="color:var(--${(inv.flagged||0)>0?'error':'text-main'})">${inv.flagged ?? '—'}</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Auto-disabled ${ii('auto_disabled')}</div>
                        <div class="lrn-stat-value">${inv.auto_disabled ?? '—'}</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Retracted ${ii('retracted')}</div>
                        <div class="lrn-stat-value">${inv.retracted ?? '—'}</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Admin-created ${ii('admin_created')}</div>
                        <div class="lrn-stat-value">${inv.admin_created ?? '—'}</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Workflow-created ${ii('workflow_created')}</div>
                        <div class="lrn-stat-value">${inv.workflow_created ?? '—'}</div>
                    </div>
                </div>
                ${(inv.flagged||0) > 0 ? `<div style="margin-top:0.75rem;font-size:0.82rem;color:var(--warning)">
                    ${inv.flagged} hint${inv.flagged===1?'':'s'} need review —
                    <button class="lrn-back-link" style="display:inline;color:var(--warning);font-size:0.82rem"
                            onclick="applyFlaggedFilter()">Open Hints →</button>
                </div>` : ''}
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Learning effectiveness — is it helping?</div>
                <div class="lrn-stats-grid">
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Cat A — no hints ${ii('cat_a')}</div>
                        <div class="lrn-stat-value">${catA.total ?? 0}</div>
                        <div class="lrn-kpi-note">${fmtPctRate(catA.pass_rate)} pass rate</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Cat B — hints injected ${ii('cat_b')}</div>
                        <div class="lrn-stat-value">${catB.total ?? 0}</div>
                        <div class="lrn-kpi-note">${fmtPctRate(catB.pass_rate)} pass rate</div>
                    </div>
                    <div class="lrn-stat-item">
                        <div class="lrn-stat-label">Cat C — holdout ${ii('cat_c')}</div>
                        <div class="lrn-stat-value">${catC.total ?? 0}</div>
                        <div class="lrn-kpi-note">${fmtPctRate(catC.pass_rate)} pass rate</div>
                    </div>
                </div>
                <div class="lrn-kpi-block" style="margin-top:1rem">
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Biased lift (B − A) ${ii('biased_lift')}</span>
                        <span class="lrn-kpi-val">${fmtLift(eff.lift)}</span>
                        <span class="lrn-kpi-note">← Cat B resembles past successes — flattering, not trusted</span>
                    </div>
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Honest lift (B − C) ${ii('honest_lift')}</span>
                        <span class="lrn-kpi-val">${eff.honest_lift == null
                            ? '<span style="color:var(--text-muted);font-weight:400">insufficient data</span>'
                            : fmtLift(eff.honest_lift)}</span>
                        <span class="lrn-kpi-note">← unbiased control — the trustworthy number</span>
                    </div>
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Sufficient data (Cat A &amp; B)</span>
                        <span class="lrn-kpi-val">${eff.sufficient_data ? 'yes' : 'no'}</span>
                    </div>
                </div>
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Trigger activity (30 days)</div>
                <div class="lrn-stats-grid" style="row-gap:1.25rem">
                    <div class="lrn-stat-item" style="min-width:220px">
                        <div class="lrn-stat-label">Trigger 1 fired ${ii('trigger1_fired')}</div>
                        <div class="lrn-stat-value">${t1.fired ?? '—'}</div>
                        ${t1.fired ? `<div class="lrn-kpi-note">${pct(t1.succeeded,t1.fired)} succeeded ${ii('pct_succeeded')}&nbsp;·&nbsp; flagged on ${pct(t1.flagged_events,t1.succeeded)} of fires ${ii('flagged_on_pct')}</div>` : ''}
                    </div>
                    <div class="lrn-stat-item" style="min-width:220px">
                        <div class="lrn-stat-label">Trigger 2 fired ${ii('trigger2_fired')}</div>
                        <div class="lrn-stat-value">${t2.fired ?? '—'}</div>
                        ${t2.fired ? `<div class="lrn-kpi-note">${pct(t2.succeeded,t2.fired)} succeeded &nbsp;·&nbsp; flagged on ${pct(t2.flagged_events,t2.succeeded)} of fires</div>` : ''}
                    </div>
                </div>
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">LLM accuracy — the KPI (30 days)</div>
                <div class="lrn-kpi-block">
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Flag events</span>
                        <span class="lrn-kpi-val">${flaggedEv}</span>
                    </div>
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Engagement rate ${ii('engagement_rate')}</span>
                        <span class="lrn-kpi-val">${engDisplay}</span>
                        <span class="lrn-kpi-note">← of reviewed / flagged</span>
                    </div>
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Reversal rate ${ii('reversal_rate')}</span>
                        <span class="lrn-kpi-val ${revCls}">${revDisplay}</span>
                        <span class="lrn-kpi-note">← within reviewed${lowNNote}</span>
                    </div>
                    <div class="lrn-kpi-row">
                        <span class="lrn-kpi-label">Pending review ${ii('pending_review')}</span>
                        <span class="lrn-kpi-val">${pending}</span>
                        ${pending > 0 ? `<button class="lrn-back-link" style="display:inline;margin-left:0.5rem;font-size:0.8rem" onclick="applyFlaggedFilter()">Open Hints →</button>` : ''}
                    </div>
                </div>
                ${threshOk ? `
                    <div class="lrn-hint-text-small" style="margin-top:0.75rem">
                        Threshold coloring active (N=${reviewed} ≥ 10):
                        <span style="color:var(--success)">0–10% trustworthy</span> ·
                        <span style="color:var(--warning)">10–30% tune prompt</span> ·
                        <span style="color:var(--error)">&gt;30% investigate</span>
                    </div>` : ''}
                ${(!threshOk && flaggedEv === 0) ? `<div class="lrn-kpi-note" style="margin-top:0.5rem">No flag events yet — reversal rate will appear once triggers fire and flags are reviewed.</div>` : ''}
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">Manual actions (30 days)</div>
                <div class="lrn-kpi-row">
                    <span class="lrn-kpi-label">Unflagged manually ${ii('unflags')}</span>
                    <span class="lrn-kpi-val">${man.unflags_30d ?? '—'}</span>
                </div>
                <div class="lrn-kpi-row">
                    <span class="lrn-kpi-label">Retracted manually ${ii('retracts')}</span>
                    <span class="lrn-kpi-val">${man.retracts_30d ?? '—'}</span>
                </div>
            </div>

            <div class="lrn-stats-section">
                <div class="lrn-stats-section-title">LLM cost (30 days)</div>
                <div class="lrn-kpi-row">
                    <span class="lrn-kpi-label">Input tokens ${ii('input_tokens')}</span>
                    <span class="lrn-kpi-val" style="font-family:var(--font-mono)">${totalInTok > 0 ? (totalInTok/1000).toFixed(1)+'k' : '—'}</span>
                </div>
                <div class="lrn-kpi-row">
                    <span class="lrn-kpi-label">Output tokens ${ii('output_tokens')}</span>
                    <span class="lrn-kpi-val" style="font-family:var(--font-mono)">${totalOutTok > 0 ? (totalOutTok/1000).toFixed(1)+'k' : '—'}</span>
                </div>
                <div class="lrn-kpi-row">
                    <span class="lrn-kpi-label">Estimated cost ${ii('est_cost')}</span>
                    <span class="lrn-kpi-val" style="font-family:var(--font-mono)">~$${totalCostUSD.toFixed(4)}</span>
                    ${rateNote}
                </div>
            </div>
        `;
    } catch (e) {
        view.innerHTML = `<div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>`;
    }
}

function applyFlaggedFilter() {
    hintsFilters.status = 'flagged';
    navigate('#');
}

// ============================================================
// LLM Review
// ============================================================

let _reviewPollTimer = null;
let _reviewSelectedSessionId = null;

function _clearReviewPoll() {
    if (_reviewPollTimer) { clearInterval(_reviewPollTimer); _reviewPollTimer = null; }
}

function _statusBadge(status) {
    const map = {
        pending_llm:    ['badge-pending-llm',   'Analyzing…'],
        pending_review: ['badge-pending-review', 'Ready for review'],
        completed:      ['badge-completed',      'Completed'],
        failed:         ['badge-failed',         'Failed'],
    };
    const [cls, label] = map[status] || ['badge', status];
    return `<span class="badge ${cls}">${label}</span>`;
}

function _recBadge(rec) {
    const map = { keep: 'badge-keep', disable: 'badge-disable', reactivate: 'badge-reactivate', flag_review: 'badge-flag_review', unflag: 'badge-unflag' };
    return `<span class="badge ${map[rec] || 'badge'}">${rec}</span>`;
}

function _decisionBadge(dec) {
    if (!dec) return '—';
    return `<span class="badge ${dec === 'approved' ? 'badge-approved' : 'badge-rejected'}">${dec}</span>`;
}

async function renderReview() {
    _clearReviewPoll();
    const view = document.getElementById('view-review');
    view.innerHTML = '<div class="lrn-empty">Loading…</div>';
    try {
        const data = await apiFetch('/review-hints/sessions');
        _renderReviewSessions(data);
        if (data.is_any_running) {
            _reviewPollTimer = setInterval(async () => {
                const fresh = await apiFetch('/review-hints/sessions');
                _renderReviewSessions(fresh);
                if (!fresh.is_any_running) _clearReviewPoll();
            }, 8000);
        }
    } catch (e) {
        view.innerHTML = `<div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>`;
    }
}

function _renderReviewSessions(data) {
    const view = document.getElementById('view-review');
    const running = data.is_any_running;
    const sessions = data.sessions || [];

    const btnDisabled = running || sessions.some(s => s.status === 'pending_llm');
    const btnLabel = btnDisabled ? 'Review in progress…' : 'Run LLM Review';

    let sessionRows = sessions.map(s => `
        <tr onclick="_selectReviewSession(${s.id})" title="Click to view recommendations">
            <td>${s.id}</td>
            <td>${_statusBadge(s.status)}</td>
            <td>${s.hint_count ?? '—'}</td>
            <td>${s.llm_latency_ms != null ? (s.llm_latency_ms / 1000).toFixed(1) + 's' : '—'}</td>
            <td>${s.warning ? '<span style="color:#9a3412">⚠ Yes</span>' : '—'}</td>
            <td>${reltime(s.created_at)}</td>
            <td>${s.completed_at ? reltime(s.completed_at) : '—'}</td>
        </tr>
    `).join('');

    if (!sessionRows) sessionRows = '<tr><td colspan="7" style="text-align:center;padding:1.5rem;color:var(--text-secondary)">No reviews yet</td></tr>';

    view.innerHTML = `
        <div class="review-header">
            <button class="btn btn-primary" ${btnDisabled ? 'disabled' : ''} onclick="startReview()" style="padding:6px 16px;font-size:0.85rem">
                ${btnDisabled ? '<span class="spinner-sm"></span> ' : ''}${btnLabel}
            </button>
            <span style="font-size:0.8rem;color:var(--text-secondary)">Reviews are manually triggered. The LLM examines all active + recently-disabled hints and makes recommendations — nothing changes until you approve.</span>
        </div>
        <table class="review-sessions-table">
            <thead><tr>
                <th>ID</th><th>Status</th><th>Hints</th><th>LLM latency</th><th>Warning</th><th>Started</th><th>Completed</th>
            </tr></thead>
            <tbody>${sessionRows}</tbody>
        </table>
        <div id="review-panel"></div>
    `;

    if (_reviewSelectedSessionId) {
        _loadReviewPanel(_reviewSelectedSessionId);
    }
}

async function startReview() {
    try {
        const data = await apiFetch('/review-hints/start', { method: 'POST' });
        _reviewSelectedSessionId = data.session_id;
        await renderReview();
    } catch (e) {
        // The start failed — most often a 409 because another admin already
        // started a review since this page last refreshed. Re-render so the
        // button shows the true "Review in progress…" state instead of
        // staying clickable.
        alert('Failed to start review: ' + e.message);
        await renderReview();
    }
}

async function _selectReviewSession(sessionId) {
    _reviewSelectedSessionId = sessionId;
    await _loadReviewPanel(sessionId);
}

async function _loadReviewPanel(sessionId) {
    const panel = document.getElementById('review-panel');
    if (!panel) return;
    panel.innerHTML = '<div class="lrn-empty" style="margin-top:1rem">Loading recommendations…</div>';
    try {
        const data = await apiFetch(`/review-hints/sessions/${sessionId}`);
        _renderReviewPanel(data);
    } catch (e) {
        panel.innerHTML = `<div class="lrn-empty" style="color:var(--error)">${esc(e.message)}</div>`;
    }
}

function _chunkProgressHtml(pages, sessionStatus) {
    if (!pages || pages.length === 0) return '';
    const isRunning = sessionStatus === 'pending_llm';
    const firstPendingId = (pages.find(p => p.status === 'pending') || {}).id;

    const rows = pages.map(p => {
        const scopeLabel = p.scope_type === 'global' ? 'Global hints' : (p.scope_value || 'No domain');
        let icon, note;
        if (p.status === 'succeeded') {
            icon = '✅'; note = `${p.hint_count} hints reviewed`;
        } else if (p.status === 'failed') {
            icon = '❌'; note = p.error_message ? esc(p.error_message.slice(0, 80)) : 'Failed';
        } else if (isRunning && p.id === firstPendingId) {
            icon = '⟳'; note = 'In progress…';
        } else {
            icon = '⏳'; note = 'Queued';
        }
        return `<div style="padding:2px 0">${icon} <strong>${esc(scopeLabel)}</strong> — <span style="color:var(--text-secondary);font-size:0.85rem">${note}</span></div>`;
    }).join('');

    return `<div style="margin:0.5rem 0 0.75rem;padding:0.6rem 0.9rem;background:var(--bg-hover,#f3f4f6);border-radius:6px;font-size:0.88rem">${rows}</div>`;
}

function _renderReviewPanel(data) {
    const panel = document.getElementById('review-panel');
    if (!panel) return;
    const session = data.session;
    const recs = data.recommendations || [];
    const canDecide = session.status === 'pending_review';
    const hasApproved = recs.some(r => r.admin_decision === 'approved' && !r.applied);

    const chunkProgress = _chunkProgressHtml(data.pages || [], session.status);
    const warningHtml = session.warning
        ? `<div class="review-warning">⚠ ${esc(session.warning)}</div>`
        : '';

    const recRows = recs.map(r => {
        const applied = r.applied ? 1 : 0;
        const rate = r.applied_count > 0 ? Math.round((r.failure_count / r.applied_count) * 100) + '%' : 'n/a';
        const decBtns = canDecide && !applied ? `
            <div class="rec-decision-btns">
                <button class="btn btn-sm" onclick="_decideRec(${session.id}, ${r.id}, 'approved')" style="background:var(--success,#16a34a);color:#fff;padding:3px 10px;font-size:0.75rem">Approve</button>
                <button class="btn btn-sm" onclick="_decideRec(${session.id}, ${r.id}, 'rejected')" style="background:var(--bg-hover);padding:3px 10px;font-size:0.75rem">Reject</button>
            </div>
        ` : '';
        return `
            <tr id="rec-row-${r.id}">
                <td style="max-width:220px;word-break:break-word">${esc(truncate(r.feedback_text, 80))}</td>
                <td>${esc(r.scope)}${r.domain ? ' / ' + esc(r.domain) : ''}</td>
                <td style="white-space:nowrap">${r.applied_count} / ${r.success_count} / ${r.failure_count} (${rate})</td>
                <td>${r.exoneration_count}</td>
                <td>${_recBadge(r.recommendation)}</td>
                <td style="max-width:200px;word-break:break-word;font-size:0.78rem;color:var(--text-secondary)">${esc(r.reason)}</td>
                <td>${_decisionBadge(r.admin_decision)}${applied ? ' <span style="font-size:0.7rem;color:var(--text-secondary)">(applied)</span>' : ''}</td>
                <td>${decBtns}</td>
            </tr>
        `;
    }).join('');

    const applyBtn = canDecide && hasApproved
        ? `<button class="btn btn-primary" onclick="_applySession(${session.id})" style="padding:6px 16px;font-size:0.85rem">Apply Approved Changes</button>`
        : '';

    panel.innerHTML = `
        <div class="review-panel">
            <div class="review-panel-title">Session #${session.id} — ${_statusBadge(session.status)}</div>
            ${chunkProgress}
            ${warningHtml}
            ${recs.length === 0 ? '<div class="lrn-empty">No recommendations in this session.</div>' : `
            <div style="overflow-x:auto">
            <table class="rec-table">
                <thead><tr>
                    <th>Hint text</th><th>Scope</th><th>App/Suc/Fail</th><th>Exon.</th>
                    <th>Recommendation</th><th>Reason</th><th>Decision</th><th></th>
                </tr></thead>
                <tbody>${recRows}</tbody>
            </table>
            </div>
            <div style="margin-top:0.75rem">${applyBtn}</div>
            `}
        </div>
    `;
}

async function _decideRec(sessionId, recId, decision) {
    let notes = null;
    if (decision === 'approved') {
        notes = prompt('Optional notes for approval (leave blank to skip):') || null;
    }
    try {
        await apiFetch(`/review-hints/sessions/${sessionId}/recommendations/${recId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ admin_decision: decision, admin_notes: notes }),
        });
        await _loadReviewPanel(sessionId);
    } catch (e) {
        alert('Failed to save decision: ' + e.message);
    }
}

async function _applySession(sessionId) {
    const panel = document.getElementById('review-panel');
    const recs = panel ? panel.querySelectorAll('tbody tr') : [];
    const approved = [];
    recs.forEach(row => {
        const decCell = row.querySelector('td:nth-child(7)');
        if (decCell && decCell.textContent.includes('approved')) {
            const hint = row.querySelector('td:nth-child(1)');
            if (hint) approved.push(hint.textContent.trim().slice(0, 50));
        }
    });

    const confirmMsg = approved.length > 0
        ? `Apply ${approved.length} approved change(s)?\n\n${approved.map(h => '• ' + h).join('\n')}`
        : 'Apply approved changes?';

    if (!confirm(confirmMsg)) return;

    try {
        const data = await apiFetch(`/review-hints/sessions/${sessionId}/apply`, { method: 'POST' });
        alert(`Applied ${data.applied_count} change(s). Session is now completed.`);
        await renderReview();
    } catch (e) {
        alert('Failed to apply session: ' + e.message);
    }
}
