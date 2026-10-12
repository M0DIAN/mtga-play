# mtga-play

让 Codex 接管 MTGA 实时对局，依据玩家可见的场面变化及时判断，并快速、灵活、可靠地完成操作。

本仓库由用户提供的 v0.3.5 skill 建立。当前开发版为 **v0.4.0**：复用增量日志、卡牌规则和单步界面操作，增加一致的状态检查、持续会话协调和可信的耗时统计。真实客户端的快速完整接管是开发主线；离线测试通过不代表已经达到该目标。

## 目前可以做什么

| 能力 | 实现范围 |
|---|---|
| 持续观察 | 增量读取 Player.log，发布玩家可见状态，保留缺口和未知信息 |
| 状态有效性 | Python 观察与 JavaScript 输入使用同一套用例核对就绪条件；正常等待同一己方请求不会仅因事件年龄被判过期 |
| 会话协调 | 等待新的决策状态、管理决策 token 与时限、拒绝过期结果、显式绑定已检查帧、记录行动意图与步骤结果 |
| 界面操作 | 通过宿主 computer-use 后端执行一次输入并立即刷新；本机拖动后端保持原有窄范围 |
| 耗时证据 | 区分原始事件和实际截图，按控制器身份与代数去重，报告缺失指标和长尾 |

`match_session.mjs` 管理会话，**不会自动定位所有牌张、唤醒后台 Codex、调用模型 API 或自行启动对局**。需要模型判断和看图的步骤仍由当前 Codex 会话完成。模型决定“做什么”，代码负责状态、时限和已确定步骤的执行边界。

## 使用入口

- Codex skill：[`SKILL.md`](SKILL.md)。按当次指定套牌、模式和局数执行。
- 新会话协调器：[`references/realtime-session.md`](references/realtime-session.md)。在已初始化的 computer-use 环境中连接日志与单步控制器。
- 日志监听：[`references/log-state.md`](references/log-state.md)。可独立离线回放；监听本身不操作游戏。
- 开发主线、任务依赖与验收：[`docs/roadmap.md`](docs/roadmap.md)。

仓库代码与用户电脑上的已安装副本分开管理。首次接入时使用独立 checkout，核对已有安装差异后再同步 `SKILL.md`、`agents/`、`scripts/`、`references/` 和 `VERSION`；不要覆盖尚未核对的本机修改。历史 `E:/MTGA` 路径属于原开发机器，实际运行时必须确认当前路径和窗口。

## 离线验证

代码使用 Python 标准库和 Node 内置模块，无需安装运行时第三方依赖。CI 基线为 Python 3.11、Node 24，同时检查 Windows 兼容性。

```bash
python -B -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_arena_ui.mjs tests/test_state_contract.mjs tests/test_match_session.mjs
```

Windows PowerShell 可用时，Python 测试会检查拖动轨迹的无输入模式；其它环境明确跳过这三项。全部 UI 自动测试使用 mock 后端，**不发送真实鼠标或键盘输入**。默认临时目录为 `.mtga-test-tmp/`，可通过 `MTGA_TEST_TMP` 指定。

CI 将 Python、JavaScript 和 Windows 验证分开并行，各作业超时为 10 分钟。超时值是执行上限，不是已经取得的实际时长保证。

## 来源与证据范围

- 原始上传：`mtga-play.zip`，SHA-256 `1ca309a3e15ad6bcbc3c77ddec5035d776baed8a45b633aa2bebcee27d7fa899`。
- 原始 v0.3.5 以独立提交导入，后续改动可直接对照。
- `references/` 中既往比赛的数字属于原作者记录；对应完整原始对局没有包含在本仓库，不能据此声称已重新实测。
- 只使用合法可见信息；不提交原始私人日志、数据库或账户信息。
- 训练 JSONL 是经过筛选的候选示范；项目目前没有训练或更新模型权重。
