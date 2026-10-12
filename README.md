# mtga-play

让 Codex 接管 MTGA 实时对局，依据玩家可见的场面变化及时判断，并快速、灵活、可靠地完成操作。

本仓库由用户提供的 v0.3.5 skill 建立。当前开发版为 **v0.4.3**：修复受支持 REPL 中缺少 `process` 导致协调器创建失败，保持 Windows 文件别名独占检查，并加入实际开始输入前可运行的无输入协调器预检。增量日志、卡牌规则、单步界面操作和耗时统计继续服务实时接管主线；合成预检和离线测试通过不代表已经取得真实对局基线或完成真实客户端验收。

## 目前可以做什么

| 能力 | 实现范围 |
|---|---|
| 持续观察 | 增量读取 Player.log，发布玩家可见状态，保留缺口和未知信息 |
| 跨局与中断 | room/GRE 明确比赛身份统一处理；同局冲突仍拒绝；异常保留最后成功投影和最小诊断 |
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

优先使用统一入口，将输出放到**源码之外的全新目录**。以下路径是本机示例；使用当前可用的 Python 和 Node 可执行文件，不要求安装新运行时：

```powershell
python -B -X utf8 tests/run_offline.py --output 'E:\MTGA\skill_evaluations\mtga-play\v0.4.3\本次评估\offline' --node 'C:\Program Files\nodejs\node.exe'
```

入口将子进程的 `MTGA_TEST_TMP` 指向本次输出目录下的 `test-tmp`，将 `MTGA_PYTHON` 设为启动入口的 Python。在正式测试前，使用实际 Python/Node 子进程验证该目录的创建、写入、追加、读取、重命名和删除。预检失败保留错误并停止，不修改权限、不静默换目录、不跳过失败用例。

输出含 `offline-report.json`、各条命令的原始 stdout/stderr、退出码、进程耗时和清理状态。报告写入被拒时，仍向控制台 stdout 输出包含原始阻塞及保存错误的 JSON。退出码 0 表示所选套件及清理通过，1 表示正式测试或清理未通过，2 表示预检/环境或报告保存阻塞；结合原始异常判断原因。原始用例数量和失败/跳过项见 stdout/stderr；入口同时向 stderr 回显正式套件的输出，成功 CI 也可核对用例数量和跳过项。不把进程退出码等同于已验证实战。`--suite python` 或 `--suite javascript` 可只运行对应套件；后者仍预检 Python，因为 bootstrap 测试会调用它。

单独运行测试时，所有文件 fixture 都遵守 `MTGA_TEST_TMP`；未设置时默认使用当前目录的 `.mtga-test-tmp/`。Python 与 JS 使用相同约定，已设置却不可用时直接失败。每个 fixture 仅删除自己创建的目录，异常初始化或会话关闭错误也会尝试清理；失败残留会明确报告，不删除历史记录。Windows 本机验收应使用上面的统一入口，避免把临时文件写回已安装的 skill。

Windows PowerShell 可用时，Python 测试会检查拖动轨迹的无输入模式；其它环境明确跳过这三项。它们使用既有的子进程级 `-ExecutionPolicy Bypass` 参数，不持久修改策略；工具或组织策略阻止时应报告实际阻塞。全部 UI 自动测试使用 mock 后端，**不发送真实鼠标或键盘输入**。

v0.4.0 的本机反馈中，两项 Python 测试在系统沙箱 Temp 写合成日志时被拒绝，另有 UI 测试留下 59 个临时文件。v0.4.1 修正后，本机离线 Python 57 项、JS 79 项及清理全部通过；唯一 Sparky 的 watcher 随后因接收座位冲突退出，对局未完成，异常也没有生成 summary。

v0.4.2 的回归使用合成跨局顺序，覆盖 GRE 先于 room、同座位与状态编号重用、同 key room 不丢基线、迟到旧局身份、无身份及固定座位冲突、异常前后最终报告和健康检查。反馈未包含触发异常的原始 GRE，因此具体消息类型和顺序仍未知；没有明确新比赛身份的真正 state/Req 冲突继续停止，不猜测换局。真实未结束对局的续接、新局流程及实际响应时间仍须本机验证，不能用常规 Windows CI 替代。

v0.4.3 的 `runReplPreflight({tempRoot})` 用真实文件系统、隔离合成投影和拒绝所有方法的后端，直接运行当前两个工厂的创建、独占、决策、取消、连续性拒绝、关闭及重新获取所有权，清理自身临时目录后返回证据。统一离线入口包含无 `process`、不可导入 `node:process` 的 ESM/bootstrap 回归和 Windows/POSIX 文件身份回归。实际调用方式及 `passed` 的有限含义见 [会话协调器](references/realtime-session.md)。

CI 将 Python、JavaScript 和 Windows 验证分开并行，各作业超时为 10 分钟。超时值是执行上限，不是已经取得的实际时长保证。

每次新版交付包含版本 ZIP、对应 PowerShell 安装命令和 Codex 测试指令。安装包保留逐文件校验清单；验收报告区分源码文件变化与测试新增产物，保存原始错误及清理结果，再依据实际证据安排下一版。

## 来源与证据范围

- 原始上传：`mtga-play.zip`，SHA-256 `1ca309a3e15ad6bcbc3c77ddec5035d776baed8a45b633aa2bebcee27d7fa899`。
- 原始 v0.3.5 以独立提交导入，后续改动可直接对照。
- `references/` 中既往比赛的数字属于原作者记录；对应完整原始对局没有包含在本仓库，不能据此声称已重新实测。
- 只使用合法可见信息；不提交原始私人日志、数据库或账户信息。
- 训练 JSONL 是经过筛选的候选示范；项目目前没有训练或更新模型权重。
