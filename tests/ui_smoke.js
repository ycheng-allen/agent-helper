const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(require('path').join(__dirname, '..', 'web', 'index.html'), 'utf8');
const markup = html.split('<script>')[0];
const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.strictEqual(ids.length, new Set(ids).size, 'HTML IDs must be unique');
for (const id of ['tab-stats', 'tab-overview', 'tab-schedule', 'tab-sprint', 'view-stats',
                  'view-overview', 'view-schedule', 'view-sprint', 'sList', 'spaceAll',
                  'agentCodex', 'agentZcode', 'quotaPanel', 'probePanel', 'zprobePanel',
                  'sAutoPanel', 'zSchedNote', 'optQuota']) {
  assert(ids.includes(id), `Missing ${id}`);
}

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const appScript = scripts[scripts.length - 1][1];
const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {
    value: '', hidden: false, innerHTML: '', textContent: '', dataset: {},
    children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(name, value) { this[name] = value; }, focus() {},
    appendChild(child) { this.children.push(child); },
    closest() { return null; },
    querySelector(sel) { return element(sel); },
  });
  return elements.get(key);
}
element('#sKind').value = 'new';
element('#sTrigger').value = 'at';
const context = {
  console, Date, Map, String, Number, JSON, Math, Promise, Intl,
  document: { querySelector: element, querySelectorAll: () => [],
              createElement: tag => element('<' + tag + '>'),
              getElementById: id => element('#' + id) },
  window: { addEventListener() {} },
  location: { hash: '' }, history: { replaceState() {} },
  fetch: async () => { throw Error('offline smoke test'); },
  setInterval() {},
};
vm.createContext(context);
vm.runInContext(appScript, context);
vm.runInContext(`renderQuota({live:{sampled_at:1790000000,primary:{usedPercent:54,resetsAt:1790076538},secondary:{usedPercent:59,resetsAt:1790496841}},latest:{primary_used:99,secondary_used:99}})`, context);
assert(element('#quota').innerHTML.includes('46%'));
assert(element('#quota').innerHTML.includes('41%'));
assert(element('#quota').innerHTML.includes('已用 54%'));
assert(element('#quota').innerHTML.includes('Codex 账户实时数据'));
// 总览空间：只显示统计标签页
vm.runInContext("switchAgent('all')", context);
assert.strictEqual(element('#tab-stats').hidden, false);
assert.strictEqual(element('#tab-overview').hidden, true);
assert.strictEqual(element('#tab-schedule').hidden, true);
assert.strictEqual(element('#view-stats').hidden, false);
assert.strictEqual(element('#spaceAll')['aria-selected'], 'true');
vm.runInContext("switchAgent('codex')", context);
assert.strictEqual(element('#tab-stats').hidden, true);
assert.strictEqual(element('#view-stats').hidden, true);
vm.runInContext("setTab('schedule')", context);
assert.strictEqual(element('#view-overview').hidden, true);
assert.strictEqual(element('#view-schedule').hidden, false);
assert.strictEqual(element('#tab-schedule')['aria-selected'], 'true');
// agent 切换：ZCode 空间隐藏 Codex 专属面板、显示玩命蹬
vm.runInContext("switchAgent('zcode')", context);
assert.strictEqual(element('#quotaPanel').hidden, false);  // ZCode 空间也显示实时额度
assert.strictEqual(element('#probePanel').hidden, true);
assert.strictEqual(element('#sAutoPanel').hidden, true);
assert.strictEqual(element('#zSchedNote').hidden, false);
assert.strictEqual(element('#tab-sprint').hidden, false);
assert.strictEqual(element('#optQuota').hidden, true);
assert.strictEqual(element('#agentZcode')['aria-selected'], 'true');
vm.runInContext("setTab('sprint')", context);
assert.strictEqual(element('#view-sprint').hidden, false);
vm.runInContext("switchAgent('codex')", context);
assert.strictEqual(element('#quotaPanel').hidden, false);
assert.strictEqual(element('#tab-sprint').hidden, true);
assert.strictEqual(element('#view-overview').hidden, false);  // codex 无 sprint，回落总览
// 排程数据按 agent 过滤
const filterResult = vm.runInContext(`scheduleData=filterScheduleData({rules:[{id:'a',agent:'codex'},{id:'b',agent:'zcode'}],
  tasks:[{id:'t1',agent:'zcode'},{id:'t2',agent:'codex'}],projects:[{id:'p'}]},'zcode'),
  scheduleData.rules.map(r=>r.id).join(',')+'|'+scheduleData.tasks.map(t=>t.id).join(',')`, context);
assert.strictEqual(filterResult, 'b|t1');
vm.runInContext(`scheduleData={tasks:[],rules:[
  {id:'a',kind:'new',trigger:'at',run_at:200,created_at:1,status:'waiting',prompt:'Second task'},
  {id:'b',kind:'next',trigger:'after',created_at:2,status:'running',prompt:'Running task'},
  {id:'c',kind:'new',trigger:'at',run_at:100,created_at:3,status:'waiting',prompt:'First task'},
  {id:'d',kind:'new',trigger:'at',run_at:50,created_at:4,status:'done',prompt:'Old task'}
]};renderScheduleQueue()`, context);
const queue = element('#sList').innerHTML;
assert(queue.indexOf('Running task') < queue.indexOf('First task'));
assert(queue.indexOf('First task') < queue.indexOf('Second task'));
assert(!queue.includes('Old task'));
assert(queue.includes('等待 #1'));
assert.strictEqual(element('#sTabCount').textContent, '3');
vm.runInContext("setRuleFilter('history')", context);
assert(element('#sList').innerHTML.includes('Old task'));
assert(!element('#sList').innerHTML.includes('First task'));
vm.runInContext(`scheduleData.tasks=[
  {id:'12345678-aaaa',title:'Fix login',project_id:'project-web',project_name:'Web app',turn:{status:'active'}},
  {id:'87654321-bbbb',title:'Update billing',project_id:'project-web',project_name:'Web app',turn:{status:'completed'}},
  {id:'33333333-cccc',title:'Review draft',project_id:'project-docs',project_name:'Documents',turn:{status:'completed'}}
];chooseKind('next')`, context);
assert(element('#sTaskProject').innerHTML.includes('Web app'));
assert(element('#sTaskProject').innerHTML.includes('Documents'));
assert(!element('#sTask').innerHTML.includes('Fix login'));
element('#sTaskProject').value = 'project-web';
vm.runInContext('updateTaskOptions()', context);
assert(element('#sTask').innerHTML.includes('Fix login'));
assert(element('#sTask').innerHTML.includes('Update billing'));
assert(!element('#sTask').innerHTML.includes('Review draft'));
assert(element('#sTask').innerHTML.includes('#12345678'));
element('#sTask').value = '87654321-bbbb';
vm.runInContext('updateTaskNote()', context);
assert(element('#sTaskNote').textContent.includes('立即执行'));
vm.runInContext("chooseKind('new');chooseProjectMode('create')", context);
assert.strictEqual(element('#sNewProjectBox').hidden, false);
assert.strictEqual(element('#sExistingProjectBox').hidden, true);
vm.runInContext(`scheduleData.projects=[{id:'project-1',name:'Web app',path:'/tmp/web-app'}];updateProjectOptions()`, context);
assert(element('#sProject').innerHTML.includes('Web app'));
assert(element('#sProject').innerHTML.includes('/tmp/web-app'));
console.log('UI tabs and schedule queue smoke test passed');
