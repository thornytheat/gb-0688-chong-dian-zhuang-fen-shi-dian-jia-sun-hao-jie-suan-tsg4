const feedback = document.querySelector('#settle-feedback');
const settleBtn = document.querySelector('#settle-btn');
const recordsBox = document.querySelector('#daily-records');
const countBox = document.querySelector('#daily-count');

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmt(v) { return Number(v).toFixed(3); }

async function refreshRecords() {
  if (!recordsBox) return;
  const res = await fetch('/api/m8/daily');
  if (!res.ok) { recordsBox.textContent = '日结载入失败'; return; }
  const rows = await res.json();
  countBox.textContent = `${rows.length} 份已入库`;
  if (!rows.length) { recordsBox.innerHTML = '<p class="empty-hint">尚无入库日结。</p>'; return; }
  recordsBox.innerHTML = rows.map(r => `
    <article class="daily-card ${r.review !== '待复核' ? 'locked' : ''}">
      <header>
        <div><b>${esc(r.station)}</b><code>${esc(r.day)}</code></header>
        <span class="tag ${r.state !== '通过' ? 'tag-warn' : ''}">${esc(r.state)}</span>
      </header>
      <div class="daily-grid">
        <span>总表净量 <b>${fmt(r.net_kwh)}</b></span>
        <span>充电 <b>${fmt(r.charge_kwh)}</b></span>
        <span>放电 <b>${fmt(r.discharge_kwh)}</b></span>
        <span>损耗 <b>${fmt(r.loss_kwh)}</b></span>
        <span>充侧损耗 <b>${fmt(r.charge_loss_kwh)}</b></span>
        <span>放侧损耗 <b>${fmt(r.discharge_loss_kwh)}</b></span>
      </div>
      <footer>
        <code>${esc(r.result_id)}</code>
        <span class="review-state">${esc(r.review)} · V${r.version}${r.review !== '待复核' ? '（锁定，重算不覆盖）' : ''}</span>
        <button class="btn tiny" data-detail="${esc(r.station)}|${esc(r.day)}">版本/事件</button>
        ${r.review === '待复核' ? `<button class="btn tiny solid" data-review="${esc(r.station)}|${esc(r.day)}" data-version="${r.version}">复核通过</button>` : ''}
      </footer>
      <div class="detail-box" hidden></div>
    </article>`).join('');
}

recordsBox?.addEventListener('click', async ev => {
  const detail = ev.target.dataset.detail;
  const reviewKey = ev.target.dataset.review;
  if (detail) {
    const [station, day] = detail.split('|');
    const card = ev.target.closest('.daily-card');
    const box = card.querySelector('.detail-box');
    if (!box.hidden) { box.hidden = true; return; }
    const [dRes, eRes] = await Promise.all([
      fetch(`/api/m8/daily/${encodeURIComponent(station)}/${encodeURIComponent(day)}`),
      fetch(`/api/m8/events?station=${encodeURIComponent(station)}&day=${encodeURIComponent(day)}`),
    ]);
    const detailData = await dRes.json();
    const events = await eRes.json();
    const flows = (detailData.flows || []).map(f =>
      `<tr><td><code>${esc(f.order)}</code></td><td>${esc(f.direction_label)}</td><td>${fmt(f.energy_kwh)}</td><td>${fmt(f.loss_kwh)}</td><td>${fmt(f.net_kwh)}</td></tr>`).join('');
    box.innerHTML = `
      <table class="ledger"><thead><tr><th>订单</th><th>方向</th><th>电量</th><th>损耗</th><th>净量去向</th></tr></thead><tbody>${flows}</tbody></table>
      <h4>历史版本</h4>
      <ul class="version-list">${(detailData.versions || []).map(v => `<li>V${v.version} <code>${esc(v.result_id)}</code> <small>${esc(v.archived_at)}</small></li>`).join('') || '<li>无归档版本</li>'}</ul>
      <h4>事件</h4>
      <ul class="event-list">${events.map(e => `<li><b>${esc(e.event)}</b> <small>${esc(e.created_at)}</small><p>${esc(e.detail)}</p></li>`).join('')}</ul>`;
    box.hidden = false;
  }
  if (reviewKey) {
    const [station, day] = reviewKey.split('|');
    const res = await fetch(`/api/m8/review/${encodeURIComponent(station)}/${encodeURIComponent(day)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: Number(ev.target.dataset.version), decision: '通过' }),
    });
    const data = await res.json();
    if (!res.ok) { feedback.textContent = data.message || '复核失败'; return; }
    refreshRecords();
  }
});

settleBtn?.addEventListener('click', async () => {
  settleBtn.disabled = true;
  feedback.textContent = '正在入库日结…';
  try {
    const form = document.querySelector('#meter-form');
    const text = form ? form.elements.text.value : undefined;
    const res = await fetch('/api/m8/settle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) {
      feedback.className = 'warn';
      feedback.textContent = `${data.message}：${(data.failures || []).map(f => `${f.station} ${f.day}`).join('，')}；旧版日结已保留`;
    } else {
      feedback.className = 'ok';
      const summary = data.actions.map(a => `${a.station} ${a.day}→${a.action}`).join('；');
      feedback.textContent = `入库完成：${summary}`;
    }
    refreshRecords();
  } catch (err) {
    feedback.className = 'warn';
    feedback.textContent = err.message;
  } finally {
    settleBtn.disabled = false;
  }
});

refreshRecords();
