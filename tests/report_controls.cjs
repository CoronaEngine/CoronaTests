// Run with Node's built-in assertion library; no browser, server or packages.
const assert = require('node:assert/strict');
const {abnormal, matches} = require('../scripts/report_assets/report.js');
const rows = Array.from({length: 25}, (_, i) => ({scene: `room-${i}`, name: i === 0 ? 'Room <北>' : `Room ${i}`,
  statuses: {gi: i === 0 ? 'failed' : i === 1 ? 'image-failed' : 'pass', 'pt2048': i === 2 ? 'expected-unsupported' : 'pass'}}));
assert.equal(abnormal('expected-unsupported'), false);
assert.equal(abnormal('support-changed'), true);
assert.equal(abnormal('future-new-failure'), true);
assert.equal(matches(rows[0], '北', 'failed', 'gi'), true);
assert.equal(matches(rows[0], ' ROOM ', 'abnormal', 'pt2048'), false);
assert.equal(matches(rows[0], '', 'all', 'pt-reblur'), false);
assert.equal(matches(rows[2], '', 'abnormal', 'all'), false);
assert.equal(rows.filter(row => matches(row, '', 'abnormal', 'all')).length, 2);
assert.equal(rows.filter(row => matches(row, '', 'all', 'all')).length, 25);
assert.equal(rows.filter(row => matches(row, 'not-found', 'all', 'all')).length, 0);
assert.equal(rows.filter(row => matches(row, '', 'expected-unsupported', 'pt2048')).length, 1);
console.log('Report filter assertions passed');

// Exercise the production event handlers without a browser or network.
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(dataset = {}) { this.dataset = dataset; this.events = {}; this.attributes = {}; this.hidden = false; }
  addEventListener(name, listener) { (this.events[name] ||= []).push(listener); }
  emit(name, event = {}) { (this.events[name] || []).forEach(listener => listener({preventDefault() {}, ...event})); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  focus() { this.focused = true; }
}
const controls = Object.fromEntries(['scene-search', 'status-filter', 'algorithm-filter', 'match-info', 'empty-results'].map(id => [id, new Element()]));
controls['scene-search'].value = '';
controls['status-filter'].value = 'abnormal';
controls['status-filter'].options = ['all', 'abnormal', 'failed', 'pass'].map(value => ({value}));
controls['algorithm-filter'].value = 'all';
controls['algorithm-filter'].options = ['all', 'gi', 'pt2048'].map(value => ({value}));
const tableRows = rows.map(row => new Element({scene: row.scene, name: row.name, statuses: JSON.stringify(row.statuses)}));
const summaryLink = new Element({statusFilter: 'failed'});
const overviewRoot = {querySelectorAll(selector) { return selector === 'tr[data-scene]' ? tableRows : [summaryLink]; }};
const tabs = ['gi', 'pt2048'].map(name => new Element({tab: name}));
const panels = tabs.map(tab => {
  const panel = new Element({algorithm: tab.dataset.tab});
  panel.diff = new Element(); panel.diff.setAttribute('aria-pressed', 'false');
  panel.roi = new Element(); panel.roi.value = 'full';
  panel.images = [new Element(), new Element()]; panel.images.forEach(image => { image.hidden = true; });
  panel.crops = ['center', 'corner'].map(name => new Element({roiPanel: name}));
  panel.metrics = ['full', 'center', 'corner'].map(name => new Element({metricRegion: name}));
  panel.querySelector = selector => selector === '[data-diff-toggle]' ? panel.diff : panel.roi;
  panel.querySelectorAll = selector => selector === '[data-difference]' ? panel.images : selector === '[data-roi-panel]' ? panel.crops : panel.metrics;
  return panel;
});
const location = {href: 'file:///report/index.html', hash: '#pt2048'};
const context = {document: {
  querySelector: () => overviewRoot,
  getElementById: id => controls[id],
  querySelectorAll: selector => selector === '[data-tab]' ? tabs : panels
}, location, sessionStorage: {getItem() { throw Error('file storage unavailable'); }, setItem() { throw Error('file storage unavailable'); }},
window: new Element()};
vm.runInNewContext(fs.readFileSync(require.resolve('../scripts/report_assets/report.js'), 'utf8'), context);
assert.equal(tableRows.filter(row => !row.hidden).length, 2);
assert.equal(panels[1].hidden, false); assert.equal(panels[0].hidden, true);
controls['status-filter'].value = 'all'; controls['status-filter'].emit('change');
assert.equal(tableRows.filter(row => !row.hidden).length, 25);
assert.equal(tableRows[24].hidden, false); assert.equal(tableRows[0].hidden, false);
assert.equal(controls['match-info'].textContent, '匹配 25 / 25 个场景');
controls['scene-search'].value = '北'; controls['scene-search'].emit('input');
assert.equal(tableRows.filter(row => !row.hidden).length, 1);
controls['scene-search'].value = 'missing'; controls['scene-search'].emit('input'); assert.equal(controls['empty-results'].hidden, false);
summaryLink.emit('click'); assert.equal(controls['scene-search'].value, ''); assert.equal(tableRows.filter(row => !row.hidden).length, 1);
tabs[1].emit('keydown', {key: 'ArrowLeft'}); assert.equal(panels[0].hidden, false); assert.equal(tabs[0].focused, true);
panels[0].diff.emit('click'); assert.equal(panels[0].images[0].hidden, false);
panels[0].diff.emit('click'); assert.equal(panels[0].images[0].hidden, true);
panels[0].roi.value = 'corner'; panels[0].roi.emit('change');
assert.equal(panels[0].crops[0].hidden, true); assert.equal(panels[0].crops[1].hidden, false);
assert.equal(panels[0].metrics[0].hidden, true); assert.equal(panels[0].metrics[2].hidden, false);
console.log('Offline UI event assertions passed');
