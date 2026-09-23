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

// I18N 表完整性：每个 key 必须同时有 ch 与 en；en 不得含中文
const i18n = vm.runInContext('I18N', context);
const cjk = /[\u4e00-\u9fff]/;
for (const [key, entry] of Object.entries(i18n)) {
  assert(entry && typeof entry.ch === 'string' && entry.ch.trim(), `I18N.${key} missing ch`);
  assert(entry && typeof entry.en === 'string' && entry.en.trim(), `I18N.${key} missing en`);
  assert(!cjk.test(entry.en), `I18N.${key}.en contains Chinese: ${entry.en}`);
}
// 所有 data-i18n* 引用的 key 必须存在（曾出现 sPromptPh 引用缺失 key，占位符显示成键名）
for (const m of html.matchAll(/data-i18n(?:-ph|-aria|-title)?="([^"]+)"/g)) {
  assert(i18n[m[1]], `data-i18n references missing I18N key: ${m[1]}`);
}
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
assert(queue.includes('等待中 #1'));
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

// ---- EN 模式：动态渲染面不得出现中文（账号名/Prompt 等用户数据除外，本测试数据全英文）----
vm.runInContext("setLang('en')", context);
vm.runInContext(`renderQuota({live:{sampled_at:1790000000,primary:{usedPercent:54,resetsAt:1790076538},
  secondary:{usedPercent:59,resetsAt:1790496841}},latest:{primary_used:99,secondary_used:99}})`, context);
assert(!cjk.test(element('#quota').innerHTML), 'EN quota shows Chinese: ' + element('#quota').innerHTML);
vm.runInContext(`scheduleData={tasks:[],rules:[
  {id:'a',kind:'new',trigger:'at',run_at:200,created_at:1,status:'waiting',prompt:'First task'},
  {id:'b',kind:'resume',trigger:'quota',created_at:2,status:'running',prompt:'Retry task',agent:'zcode'},
  {id:'c',kind:'new',trigger:'at',run_at:100,created_at:3,status:'failed',prompt:'Broken task',error:'boom'}
]};renderScheduleQueue()`, context);
assert(!cjk.test(element('#sList').innerHTML), 'EN schedule queue shows Chinese: ' + element('#sList').innerHTML);
assert(!cjk.test(element('#sSummary').innerHTML), 'EN schedule summary shows Chinese');
vm.runInContext(`sprintData={sprints:[{id:'s1',name:'night-run',status:'running',kind:'daily',
  start_hm:'00:00',end_hm:'08:00',window:[Date.now()/1000-3600,Date.now()/1000+3600],
  concurrency:1,cwd:'/tmp/night',manual:false}],
  tasks:{s1:[{id:'t1',status:'done',prompt:'fix bug'},{id:'t2',status:'running',prompt:'write tests'},
             {id:'t3',status:'pending',prompt:'ship it'}]}};
renderSprints()`, context);
assert(!cjk.test(element('#spList').innerHTML), 'EN sprint list shows Chinese: ' + element('#spList').innerHTML);
assert(!cjk.test(element('#spSummary').innerHTML), 'EN sprint summary shows Chinese');
element('#sTask').value = '';
vm.runInContext("chooseKind('next');updateTaskOptions()", context);
assert(!cjk.test(element('#sTaskNote').textContent), 'EN task note shows Chinese: ' + element('#sTaskNote').textContent);
assert(!cjk.test(element('#sTask').innerHTML), 'EN task options show Chinese: ' + element('#sTask').innerHTML);
// EN 下切换回中文仍可用
vm.runInContext("setLang('ch')", context);
assert.strictEqual(vm.runInContext('LANG', context), 'ch');
console.log('UI tabs and schedule queue smoke test passed');
