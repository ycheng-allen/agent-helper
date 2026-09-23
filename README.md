# Agent Helper

**中文** | [English](README.en.md)

> 你舍不得骑的自行车，别人站起来蹬，Agent Helper，帮你站起来蹬！

Agent Helper 是一个跑在本机的 **Codex / ZCode 双 Agent 任务监控与排程助手**。它盯着正在工作的任务，记住你想做的下一步，在额度恢复、前一项任务结束或指定时间到达时，替你把下一句 Prompt 送回正确的对话。总览页同时统计两个 Agent 的模型使用、token、错误与费用。

简单说：你负责提出宏伟目标，Codex 负责写代码，Agent Helper 负责在大家都快忘了这件事的时候把工作接起来。

## 功能总览

| 功能 | 一句话说明 |
|---|---|
| 📊 双 Agent 监控 | Codex 与 ZCode 的会话、轮次、token、耗时、错误，只读导入本机日志 |
| ⏱ 任务排程 | 定时 / 额度刷新后 / 关联任务完成后触发，支持新任务、发送下一步、续跑 |
| 🔁 额度恢复续跑 | 任务被额度打断后自动等额度窗口恢复，续上原对话 |
| 🚴 免费玩命蹬 | ZCode 闲时队列：时间窗口 + 最多 50 个任务 + 1–6 并发 |
| 📶 实时额度 | Codex 与 ZCode 双 5 小时 / 周额度窗口，30 秒采样，剩余已用同屏 |
| 💰 月度费用 | 按公开牌价折算本月花费（中美双市场价格），支持自定义汇率与单价 |
| 🕵️ 模型偷换探针 | 主动发一条小请求，核对请求模型与实际派出的模型是否一致 |
| 🌐 中英双语 | 一键切换，语言同时决定费用显示币种 |

## 界面布局：三个空间

| 空间 | 内容 |
|---|---|
| **总览** | 跨 Agent 统计：月度费用（合计 + 分 Agent）、模型成本表、每日 token / 费用趋势、实时额度 |
| **Codex** | Codex 专属的任务列表、排程、探针、额度与费用 |
| **ZCode** | ZCode 专属的任务列表、排程、玩命蹬队列、额度与费用 |

后台每 5 秒扫描一次本地任务状态；总览页每 30 秒刷新，点右上角刷新按钮可立即刷新。

## 任务排程

三种规则类型，Codex 与 ZCode 通用：

| 规则 | 触发方式 | 典型用途 |
|---|---|---|
| **启动新任务** | 指定时间 / 下一次额度刷新后 / 关联任务完成后 | 到点开工、错峰开工、流水线 |
| **发送下一步** | 当前轮结束后向同一对话追加 Prompt | 任务结束了，下一步不用靠记忆 |
| **续跑任务** | 等实时额度窗口恢复后继续原对话 | 被限流打断的工作自动接上 |

排程行为：

- 按 **项目 → 任务** 两级选择具体对话，任务选择器包含本机所有未归档任务（含较早结束的），排除已归档和子代理任务；
- 目标任务已结束时保存即执行；仍在运行则等当前轮完成后再发送；
- 排程队列分等待 / 执行 / 历史三档展示，等待中的规则可取消；
- ZCode 续跑按实时额度窗口判断恢复时机（每 30 秒采样；额度不可读时退回每 5 分钟重试，最多 8 次）；
- 应用退出或电脑睡眠时不补跑；重启后未完成的执行规则标记为「需检查」。

## 免费玩命蹬（ZCode 闲时队列）

> 白花花的 token 额度都散给了 sam altman，造孽啊！闲时免费额度不蹬白不蹬。

- 定义时间窗口：每日窗口（如 00:00–08:00，支持跨午夜）或一次性窗口；
- 排入最多 **50 个任务**，每个任务独立 Prompt，可单独编辑、拖拽排序；
- 项目可选现有目录或新建项目（首个任务启动时才创建目录）；
- 并发 1–6：1 即串行，N 为最多 N 个同时跑；
- 窗口结束或手动停止：在跑任务先 terminate、5 秒后强杀，剩余队列标记跳过；
- 应用重启后队列自动恢复，支持手动开始、中止全部、单任务删除。

## 实时额度

- **Codex**：账户接口实时额度，约 30 秒采样；存在等待额度的排程时缩短到约 10 秒；
- **ZCode**：账户用量接口的额度窗口（5 小时 + 周），剩余 / 已用百分比与重置时间同屏；
- 总览页与两个 Agent 空间都会显示各自的额度面板。

## 用量统计与月度费用

- Codex：解析本机会话索引与 rollout 日志，统计模型、轮次、token、耗时、错误；
- ZCode：只读增量导入 `~/.zcode/cli/db/db.sqlite` 的用量数据库；
- 总览页：月度费用（合计 + Codex / ZCode 分列）、模型成本表、每日 token 与费用趋势（近 30 天）、缓存命中统计；
- 费用按公开 API 牌价折算，覆盖 OpenAI 与智谱中美双市场；汇率与单价可用 `~/.agent-helper/pricing.json` 覆盖。

## ZCode 无头执行（自动解锁）

ZCode 无头 CLI 缺省没有可用模型（模型选择由桌面 App 把守）。Helper 会：

1. 从本机 ZCode 凭证（`~/.zcode/v2/credentials.json`）解密 coding-plan API key；
2. 生成独立 personal provider 配置写入 `~/.agent-helper/zcode-provider-config.json`（0600 权限）；
3. 通过 `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` 环境变量注入 CLI——不改动 ZCode 自身任何文件；
4. API key 轮换导致执行失败时自动刷新配置并重试。

## 安装

### macOS Apple Silicon

下载 [最新 ARM64 DMG](https://github.com/ycheng-allen/agent-helper/releases/latest)，拖入「应用程序」后启动。当前构建未经 Apple Developer ID 签名，首次打开请在 Finder 中右键选择「打开」。

### Ubuntu / Linux x64

下载 [最新 AppImage 或 deb](https://github.com/ycheng-allen/agent-helper/releases/latest)：

```bash
# AppImage（需要 libfuse2：sudo apt install libfuse2）
chmod +x "Agent Helper-*.AppImage" && ./"Agent Helper-*.AppImage"

# 或 deb（Ubuntu 22.04+，托盘库依赖随包自动安装）
sudo dpkg -i agent-helper_*_amd64.deb
```

前置依赖：

- `python3`：Ubuntu 自带，后端直接可用；
- `nodejs`：仅 ZCode 排程/执行需要（凭证解密与无头 CLI 均经 Node 运行），`sudo apt install nodejs`，或用 `ZCODE_NODE_BIN` 指定路径；只监控 Codex 时不需要；
- [ZCode Linux 版](https://zcode.z.ai/cn/docs/install)：deb/rpm 安装到 `/opt/ZCode` 时自动识别，AppImage 安装请用 `ZCODE_BIN` 指向其内部的 `zcode.cjs`。

托盘在 Ubuntu 默认 GNOME（AppIndicator 扩展）下可用，点击托盘图标弹出菜单。

### 从源码构建

```bash
git clone https://github.com/ycheng-allen/agent-helper.git
cd agent-helper
npm install
npm run dist          # macOS：dist/Agent Helper-*.dmg
npm run dist:linux    # Linux：dist/*.AppImage 与 dist/*.deb
```

### 从源码直接运行

要求 Python 3.8+ 与 Node.js：

```bash
npm install
npm start
```

开发模式也可以直接运行本地服务：

```bash
python3 agent_helper.py --no-open
```

所有服务只监听 `127.0.0.1`。可用 `ZCODE_BIN` 指向自定义 ZCode CLI；`--agents codex,zcode` 与 `--zcode-home` 可显式指定监控范围。

## 数据与隐私

| 路径 | 内容 |
|---|---|
| `~/.agent-helper/state.db` | 排程规则、历史与项目目录 |
| `~/.agent-helper/pricing.json` | 可选的费用单价 / 汇率覆盖 |
| `~/.agent-helper/zcode-provider-config.json` | ZCode 无头执行用的 provider 配置（0600） |
| `~/.zcode/cli/db/db.sqlite` | ZCode 用量数据库（只读） |
| `~/.codex/sessions/` 等 | Codex 会话日志（只读） |

数据目录从旧项目名 `~/.codex-model-watch/` 自动迁移，无需手动处理。项目目录、Prompt 和任务标题不会上传到第三方；Helper 不提供云端队列，实际执行使用你本机已登录的 CLI。

## 已知边界

- 本机助手，不是云端任务队列：电脑关机、睡眠或应用退出时不触发规则；
- **ZCode 桌面端显示**：无头排程写入的轮次会持久化到 ZCode 数据库，但桌面端渲染已打开的会话用的是内存运行时——需要重新加载该会话（如重启 ZCode）后才会显示；执行与历史记录不受影响；
- ZCode 的偷换探针暂不支持（仅 Codex）；
- Codex rollout 和状态数据库属于本地内部格式，未来版本变化可能需要适配；
- 主动探针只在点击时发出请求；历史日志无法还原过去发生的模型切换。

## 致谢

总览、模型用量、额度窗口、容量错误和主动探针能力，继承并扩展自 [ysh1112/codex-model-watch](https://github.com/ysh1112/codex-model-watch)。感谢原作者 ysh1112 提供本地 Codex 会话日志解析、用量统计和模型探针的基础实现。

## License

MIT License，保留原始项目来源说明。详见 [LICENSE](LICENSE)。
