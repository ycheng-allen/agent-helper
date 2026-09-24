# ZCode 额度 `percentage` 字段语义核查结论（Phase 0 实证）

- 日期：2026-09-24
- 核查对象：《ZCode 额度水位不准：修复 Implementation 计划》（`zcode-quota-waterline-fix-plan.md`）
- 结论一句话：**计划文档的核心前提是错的——上游 `percentage` 是"已用百分比"，不是"剩余百分比"；当前代码的映射方向本来就是正确的，不存在"水位反转"。按计划 Phase 0 的兜底分支执行：保留原方向，消除歧义、补校验与回归测试。**

---

## 1. 三重独立证据链

### 证据 A：真实接口数值关系（只读调用，2026-09-24 实测）

`GET https://open.bigmodel.cn/api/monitor/usage/quota/limit`（lite 档账号）返回：

| 窗口 | usage | currentValue | remaining | percentage | nextResetTime |
|---|---:|---:|---:|---:|---|
| 5h（unit=3, number=5） | 2000 | 0 | 2000 | 0 | None |
| 周（unit=6, number=1） | 2000 | 2008 | 0 | **100** | 1790758175944 |

关键推理：周窗口 `usage/currentValue ≈ 99.6%`（几乎耗尽）、`remaining = 0`，此时 `percentage = 100`。
若 `percentage` 是"剩余百分比"，此处应为 0 而非 100。
**数值关系唯一自洽的读法：`percentage ≈ usage/currentValue`（已用百分比，四舍五入到整数）。**

（5h 窗口在 lite 档未生效：`currentValue=0`、无重置时间，`percentage=0`，此时 `remaining` 字段值不可信。）

### 证据 B：官方客户端主进程归一化公式（app.asar /out/host/index.js）

ZCode 桌面端把 MCP quota 聚合成同款 `{usage, currentValue, remaining, percentage, nextResetTime}` 形状时：

```js
// buildMcpQuotaAggregateLimit（/out/host/index.js）
let n = Math.min(Math.max(0, e.totalUsage.remaining), t),   // n = remaining
    r = Math.max(0, e.totalUsage.used),                     // r = used
    o = Math.max(0, Math.min(100, 100 - n / t * 100));      // percentage = 100 − 剩余占比
return { ..., usage: r, remaining: n, percentage: o, ... }
```

`percentage = 100 − remaining/total×100`，即**已用百分比**。

### 证据 C：官方桌面 UI 渲染公式（app.asar renderer bundle）

```js
// lSn —— 桌面端进度条显示的"剩余%"
function lSn(e){
  let t = uSn(e?.percentage);
  if (t !== null) return Math.max(0, Math.min(100, 100 - t));   // 剩余% = 100 − percentage
  let n = e?.remaining, r = e?.number;                          // 兜底：remaining/number
  return ... Math.max(0, Math.min(100, n / r * 100)) : null
}
```

官方界面显示的剩余% 同样是 `100 − percentage`。计划文档所引用的"官方界面显示剩余百分比"为真，
但那是 **UI 层做了一次取反后的结果**，不是上游字段本身的语义。

---

## 2. 对当前实现的逐点判定

| 位置 | 现状 | 判定 |
|---|---|---|
| `watch_scheduler.py` `zcode_usage_raw()`：`"usedPercent": float(pct)` | `percentage` → `usedPercent` | ✅ 方向正确 |
| `web/index.html` `renderQuota()`：`remaining = 100 - usedPercent` | 前端取反 | ✅ 正确（Codex/ZCode 均适用） |
| `watch_scheduler.py` `quota_ready()`：任一窗口 `usedPercent >= 100` → 不派发 | 门控 | ✅ 正确且偏保守（percentage 为整数舍入，实际 ≈99.5% 即提前拦截） |
| `tests/test_e2e_isolated.py` `FakeQuotaServer`：`percentage=100` 定义为耗尽 | 测试语义 | ✅ 正确，计划要求"改为 percentage=0"反而是错的 |
| `tests/test_zcode_schedule.py` 恢复门控用例（`usedPercent=100` 等待、`=10` 派发） | 测试语义 | ✅ 正确 |

**计划描述的"水位反转"不存在**。若按计划 Phase 1–4 把 `percentage` 翻转为 `remainingPercent`
（后端存 remaining、前端改用 `window.remainingPercent`），才会真正制造出计划要修的那个 bug。

计划的 Phase 0 自带兜底条款：*"如果真实接口响应证明 `percentage` 是已用百分比，则保留原方向，
但必须通过字段命名消除歧义。"* —— 本次核查即触发该分支。

---

## 3. 仍然成立的改进点（与方向无关，建议执行）

1. **`zcode_usage_raw()` 缺输入校验**：`float(pct)` 遇到非数值字符串会抛 `ValueError`
   并令整次取数失败，而不是跳过坏窗口；越界值（`101`、`-1`）未拒收。应：
   仅接受可转 float 且落在 `0..100` 的 `percentage`，否则跳过该窗口；
   跳过全部窗口时维持现有 "没有可用窗口" 报错路径。
2. **字段语义留痕**：在 docstring/注释中固化本核查结论（`percentage` = 已用百分比，
   依据 = 实测数值关系 + 官方客户端 `100 − percentage` 公式），防止后续会话再次"修复"成反转。
3. **窗口身份识别**：当前按 `windowDurationMins` 排序取 `primary/secondary`；
   实测映射为 `unit=3&number=5 → 5h`、`unit=6&number=1 → 周`，可作为注释固化。
4. **回归测试锁语义**：补齐 `percentage ∈ {0, 10, 100, None, 101, -1, "abc"}` 与
   未知 `unit`、缺失 `nextResetTime` 的单测用例，把 0/10/100 三个边界固化进测试。
5. **前端**：`renderQuota()` 逻辑无需改方向；可加一行注释说明 `usedPercent` 对两个
   agent 均为"已用百分比"、`remaining = 100 − usedPercent` 是唯一取反点（单一出处）。
6. 观测性：live quota 输出可附 `source: "bigmodel_usage_api"`、`sourceField: "percentage"`，
   便于日后口径审计（P2，可选）。

计划中其余条目（数据兼容迁移、stale 标注、日志脱敏）多数已在现网实现
（历史 quota 表保留 raw、UI 已区分 live/日志回退来源并显示采样时间、全程不打印 key），无需按计划重做。

---

## 4. 证据复现方式

```bash
# 证据 A：只读调用真实接口（复用仓库内解密逻辑，勿打印 key）
cd /Users/allencheng/Documents/Codex/2026-09-21/zai/codex-model-watch
python3 - <<'PY'
import json, urllib.request
from watch_scheduler import zcode_api_key, ZCODE_QUOTA_URL
req = urllib.request.Request(ZCODE_QUOTA_URL, headers={"Authorization": zcode_api_key()})
with urllib.request.urlopen(req, timeout=10) as r:
    p = json.loads(r.read().decode())
for lim in (p.get("data") or {}).get("limits") or []:
    print({k: lim.get(k) for k in ("unit","number","usage","currentValue","remaining","percentage")})
PY

# 证据 B/C：解包桌面客户端后 grep（一次性，/tmp 下）
npx asar extract /Applications/ZCode.app/Contents/Resources/app.asar /tmp/zcode-asar-full
# B: /out/host/index.js 里搜 "100-" 与 buildMcpQuotaAggregateLimit
# C: /out/renderer/assets/styles-*.js 里搜函数 lSn
```
