# 实时会话协调器（v0.4）

`scripts/match_session.mjs` 把单局的状态版本、等待、思考预算、帧绑定和当前行动步骤保存在一个 REPL 对象中。它复用只读监听与 `arena_ui.mjs`，没有后台模型调用或自动视觉定位。每次实际输入仍由当前受支持的电脑操作后端执行，并立即返回一张新观察供 Codex 检查。

## 前提与生命周期

先依照 [实时日志状态](log-state.md) 启动本局监听，确认本次 run 健康、当前 `match_key`、完整连续基线、当前套牌和窗口；再按 [界面操作](ui-play.md) 创建 `requireExpectedRequest: true` 的 `arena`。新开对局必须区别于此前旧局；续接用户授权的同一未结束对局时，创建新记录目录和协调器，重建并核对当前身份与画面，不重放旧 token/待执行动作，不要求另开一局以获取不同 key。只把该监听生成的玩家可见 `state.json` 交给协调器，不传原始 Player.log 或内部回放输入。

通过普通 ESM 导入 `createMatchSession`，或使用 `repl_bootstrap.py` 输出的同名全局函数。以下变量均取自当前已创建的会话，不照抄历史路径、卡牌实例或像素坐标：

```js
globalThis.matchSession = await createMatchSession(arena, {
  sessionId: arena.sessionId,
  logStatePath: arena.logStatePath,
  pollMs: 200,
  decisionTimeoutMs: 30000
});
```

同一进程只允许一个活跃会话拥有该 session ID 和状态文件。对象首次看到进行中的比赛后固定其 `match_key`；换局停止旧会话。明确结束结果会锁定 `finished`，不会因另一份 Playing 状态自动继续。重建对象时 token 使用新的会话实例 ID，不能复用上一个对象的异步结果。

这是 REPL 中持续保存的对象。意图、绑定、结果和取消事件写入现有动作日志；进程重启后不会自动恢复或重放待执行输入。不要让其它进程或旧闭包同时操作同一游戏。当前进程的独占检查不构成跨进程锁。

## 一个实际决策到单步输入

### 1. 等待并读取新的决定

```js
globalThis.decisionPacket = await matchSession.next({ timeoutMs: 8000 });
```

只有 `status === "needs_decision"` 才是新的模型决策包。包内 `state` 包含本方手牌、公开区域、动态异能、当前请求及可用选择，`evidence` 保留这次读取的事件序号。心跳、日志字节偏移和对象观察计数不改变 token；请求、当前场面、候选、动态异能或日志源世代变化会产生新 token。游戏状态 ID 的变化也按需重新判断，采用保守口径。

`next` 默认最多等待 8 秒，允许 0～15 秒；没有新决策时只读状态文件，不截图、不输入、不反复调用模型。它不会在工具返回后自行唤醒 Codex。要继续等待，需要当前工作流再次调用 `next`。

### 2. 观察与显式绑定

阅读决策包，比较必要候选后：

```js
globalThis.observation = await matchSession.observe(decisionPacket.token);
```

检查当前电脑工具展示的 `observation.frame` 截图，核对请求、牌名、原牌位置和布局稳定性。`observe` 只返回候选，不自动确认画面匹配。只有检查通过，才在后续调用中绑定：

```js
await matchSession.bind(decisionPacket.token, {
  generation: observation.generation,
  note: "当前画面显示这次施放选择，已确认原手牌位置，重排动画结束。",
  intent: {
    goal: "施放选定去除咒语",
    step: "select_card",
    expected_result: "进入该咒语的费用或目标选择提示",
    object_ids: [selectedCardInstanceId, selectedTargetInstanceId]
  }
});
```

`intent` 只接受 `id/goal/step/expected_result/object_ids`。保存当前步骤与结果，不接受后续坐标或点击列表。`bind` 的 `ready_for_input: true` 表示当前帧与意图已经明确绑定；它本身不输入。

若截图前后的语义状态发生变化，这张图不能绑定到新 token。先获取新决策，再观察一次，核对实际画面。收到新日志并不证明此前截图已经显示该状态。

### 3. 输入一次，随后核对效果

```js
globalThis.actionResult = await matchSession.act(decisionPacket.token, {
  kind: "click",
  x: inspectedX,
  y: inspectedY,
  coordinateSpace: "window"
});
```

`inspectedX/Y` 必须来自刚检查的当前截图；坐标类型与校准遵循现有电脑后端。方法重读状态、检查 token 及预算，再由严格 `arena.act` 核对请求、游戏状态 ID、日志世代和输入就绪条件。一次绑定只发送一次输入，随即自动刷新；返回结果初始为未确认。

动作日志自动携带玩家投影中已知的 `own_turn` 作为我方回合编号，以及当前实际 `turn.phase`；未知值写为 null，不从全局回合编号猜测。这样耗时报告能汇总已观察到的我方回合。`match_start/game_start` 与 `game_result` 仍需依既有对局记录流程按真实开始、结束证据写入，协调器不会自动补造这些事件。

先检查 `actionResult.frame`，再记录这个具体步骤的实际效果：

```js
await matchSession.confirm({
  decisionSeq: actionResult.decisionSeq,
  generation: actionResult.generation,
  status: "confirmed",
  note: "返回画面已经进入选目标提示，目标物件仍在场。"
});
```

上述文字只是格式示例，应替换为实际观察。`confirmed` 确认当前步骤的预期结果，不等于咒语已结算。服务端结算证据仍需按日志参考记录；客户端提交或鼠标 API 返回不够。结果不清楚时用 `uncertain`，明确失败时用 `failed`，不自动重试。若需要补观察，可调用 `observe()`，随后用补观察返回的 generation 确认，不能沿用旧 generation。

## 继续意图与重新判断

完成当前步骤后，只有模型重新检查下一帧并显式 `bind`，才能再输入。若请求与状态没变，且 `candidate_token` 仍等于当前 token，可以直接检查并使用输入后返回的帧，省掉额外截图。

如果日志在输入过程中前进，返回帧仍可用于核对上一动作的效果，但不会自动绑定新请求。`next()` 获取新 token 后，需要 `observe(newToken)` 取得稳定候选。模型确认原目标仍适用时，可以在新意图描述中传入上一已确认的 `matchSession.intent.id`，更新 `step/expected_result/object_ids`；旧意图已取消或效果未知时不允许沿用该 ID。

每份决策默认有 30 秒预算，可配置 1～120 秒。预算是从决策包生成到输入检查的总时间，包含思考与观察。过期结果无法输入；再次 `next` 会为当前状态发新 token。它不测量或中止后台模型推理，也不保证端到端延迟达到该预算。状态读取完成到实际输入之间仍有竞争窗口，当前实现不宣称完全消除。

## 返回状态与停止

| status | 含义及下一步 |
| --- | --- |
| `needs_decision` | 有新的可处理请求；读取包、决策、观察并显式绑定。 |
| `needs_observation` | 需要检查返回帧、核对上一效果，或处理请求语义盲区；看具体 reason。 |
| `waiting` | 暂无新决策，或已经绑定等待调用方输入，或结果刚记录；看 reason 与 ready_for_input。 |
| `blocked` | 旧 token、过期思考、监听失效、状态缺口、换局或已关闭；停止旧操作并处理原因。 |
| `finished` | 这一局已有明确结束结果；保存复盘并结束本局监听。 |

`needs_observation` 不会放宽输入检查。未支持请求和日志缺口的人工画面回退遵循现有 `ui-play.md` 流程，不能改写就绪标记或伪装为导航。进入外部回退或用户接管前先取消意图，恢复严格流程时重新获取状态并观察。

```js
await matchSession.cancel("用户接管，取消当前意图");
await matchSession.close("本局结束");
```

`cancel` 使旧 token 和意图失效，可打断正在等待的 `next`；显式再调用 `next` 表示继续处理当前局面。显式取消或关闭也会取消上一步已确认但尚可续行的意图，之后不能沿用其 ID；已确认步骤的真实 `last_result` 保留。普通请求或场面变化仍允许模型在重新检查后续行已确认意图。`next({signal})` 也支持 `AbortSignal` 取消本次等待。`close` 永久停止这个对象并释放当前进程的所有权。已经交给操作后端的输入无法撤回；此时结果标为未知，不自动重放。关闭协调器不会终止监听进程，应另外按日志参考的 stop-file 流程结束监听。

## 当前验证范围

`node --test tests/test_match_session.mjs` 使用临时玩家投影文件、真实 `arena_ui` 控制器与模拟电脑后端，验证等待、状态变化、预算、绑定、自动提交、效果确认及取消。测试不启动 MTGA、不调用模型 API、不发送真实电脑输入。自动定位、整局策略和真实客户端响应时间仍需要后续接入验证。
