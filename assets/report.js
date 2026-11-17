(function () {
  'use strict';
  const accepted = new Set(['pass', 'complete', 'expected-unsupported']);
  function abnormal(status) { return !accepted.has(status); }
  function matches(row, query, status, algorithm) {
    if (!(row.scene + ' ' + row.name).toLowerCase().includes(query.trim().toLowerCase())) return false;
    const values = algorithm === 'all' ? Object.values(row.statuses) : [row.statuses[algorithm]].filter(Boolean);
    if (!values.length) return false;
    return status === 'all' || values.some(value => status === 'abnormal' ? abnormal(value) : value === status);
  }
  function paginate(rows, query, status, algorithm, page, size = 10) {
    const filtered = rows.filter(row => matches(row, query, status, algorithm));
    const pages = Math.max(1, Math.ceil(filtered.length / size));
    page = Math.max(1, Math.min(page, pages));
    return {rows: filtered.slice((page - 1) * size, page * size), count: filtered.length, pages, page};
  }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {abnormal, matches, paginate}; return; }
  function overview() {
    const root = document.querySelector('[data-overview]');
    if (!root) return;
    const query = document.getElementById('scene-search');
    const status = document.getElementById('status-filter');
    const algorithm = document.getElementById('algorithm-filter');
    const previous = document.getElementById('page-prev');
    const next = document.getElementById('page-next');
    const rows = Array.from(root.querySelectorAll('tr[data-scene]')).map(element => ({
      element, scene: element.dataset.scene, name: element.dataset.name, statuses: JSON.parse(element.dataset.statuses)
    }));
    let page = 1;
    try {
      const saved = JSON.parse(sessionStorage.getItem(location.href.split('#')[0]) || 'null');
      if (saved && Array.from(status.options).some(option => option.value === saved.status)
          && Array.from(algorithm.options).some(option => option.value === saved.algorithm)) {
        query.value = saved.query || ''; status.value = saved.status; algorithm.value = saved.algorithm; page = saved.page || 1;
      }
    } catch (_) { /* file:// storage may be unavailable; controls still work. */ }
    function render() {
      const result = paginate(rows, query.value, status.value, algorithm.value, page);
      page = result.page;
      const visible = new Set(result.rows);
      rows.forEach(row => { row.element.hidden = !visible.has(row); });
      previous.disabled = page === 1; next.disabled = page === result.pages;
      document.getElementById('page-info').textContent = `第 ${page} / ${result.pages} 页 · 匹配 ${result.count} / ${rows.length} 个场景`;
      document.getElementById('empty-results').hidden = result.count !== 0;
      try { sessionStorage.setItem(location.href.split('#')[0], JSON.stringify({query: query.value, status: status.value, algorithm: algorithm.value, page})); } catch (_) {}
    }
    query.addEventListener('input', () => { page = 1; render(); });
    [status, algorithm].forEach(control => control.addEventListener('change', () => { page = 1; render(); }));
    previous.addEventListener('click', () => { page -= 1; render(); });
    next.addEventListener('click', () => { page += 1; render(); });
    root.querySelectorAll('[data-status-filter]').forEach(link => link.addEventListener('click', event => {
      event.preventDefault(); status.value = link.dataset.statusFilter; query.value = ''; algorithm.value = 'all'; page = 1; render();
    }));
    render();
  }
  function scene() {
    const tabs = Array.from(document.querySelectorAll('[data-tab]'));
    if (!tabs.length) return;
    const panels = Array.from(document.querySelectorAll('article[data-algorithm]'));
    function activate(id, focus = false) {
      if (!tabs.some(tab => tab.dataset.tab === id)) return;
      tabs.forEach(tab => { const selected = tab.dataset.tab === id; tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1; if (selected && focus) tab.focus(); });
      panels.forEach(panel => { panel.hidden = panel.dataset.algorithm !== id; });
    }
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => { activate(tab.dataset.tab); location.hash = tab.dataset.tab; });
      tab.addEventListener('keydown', event => {
        let index;
        if (event.key === 'ArrowRight') index = (i + 1) % tabs.length;
        if (event.key === 'ArrowLeft') index = (i + tabs.length - 1) % tabs.length;
        if (event.key === 'Home') index = 0;
        if (event.key === 'End') index = tabs.length - 1;
        if (index !== undefined) { event.preventDefault(); activate(tabs[index].dataset.tab, true); location.hash = tabs[index].dataset.tab; }
      });
    });
    const fromHash = () => activate(location.hash.slice(1));
    window.addEventListener('hashchange', fromHash); fromHash();
    panels.forEach(panel => {
      const difference = panel.querySelector('[data-diff-toggle]');
      if (difference) difference.addEventListener('click', () => {
        const shown = difference.getAttribute('aria-pressed') !== 'true';
        difference.setAttribute('aria-pressed', String(shown));
        difference.textContent = shown ? '隐藏差分 ×4' : '显示差分 ×4';
        panel.querySelectorAll('[data-difference]').forEach(view => { view.hidden = !shown; });
      });
      const roi = panel.querySelector('[data-roi-select]');
      if (roi) roi.addEventListener('change', () => {
        panel.querySelectorAll('[data-roi-panel]').forEach(view => { view.hidden = view.dataset.roiPanel !== roi.value; });
        panel.querySelectorAll('[data-metric-region]').forEach(row => { row.hidden = row.dataset.metricRegion !== roi.value; });
      });
    });
  }
  overview(); scene();
}());
