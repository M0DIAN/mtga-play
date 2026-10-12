# MTGA 界面操作

v0.4 实时路径优先使用 [会话协调器](realtime-session.md)，让单局对象管理决策版本、时限和帧绑定。本文件保留底层单步 API、校准与既往客户端证据。严格输入现已重算日志有效性；旧版只比较请求编号的说明和历史结果不代表当前完整门槛。

先读 computer-use 的指导文档，使用 `node_repl` 中的 `@oai/sky`。从实际返回的 app/window 对象选唯一 MTGA 窗口，不能硬编码窗口句柄。截图自动展示后直接检查；仅在本任务明确需要当前帧局部细看时，使用下述只读裁剪，仍保留全图。

## 可复用记录器

局势分析与效果确认优先结合 [实时日志状态](log-state.md)。日志不提供点击坐标，下面的输入后刷新与当前帧校准仍适用；已有日志证据时减少额外观察及重复看图解释。

在已通过 `sky.list_apps()` 等方法取得当前 `gameWindow` 后：

本机 node_repl 若对文件模块导入报 `realpath EPERM`，直接用终端工具运行 `python -B -X utf8 <skill>/scripts/repl_bootstrap.py`，读取输出，再将这段完整可信源码作为下一条 node_repl JS 代码运行。它定义 `globalThis.createArenaController`、`globalThis.createMatchSession`、`globalThis.cropCurrentFrame`、`globalThis.cropPNG` 和 `globalThis.runReplPreflight`，共享本次加载的状态契约，不发送 UI 输入。先按 [会话协调器](realtime-session.md) 调用 `runReplPreflight({tempRoot: '<已有可写评估目录的绝对路径>'})`，确认无输入合成检查与清理通过，再使用下例的 options 创建真实控制器；预检通过仍不表示真实基线已就绪。不要改试 data URL 模块导入、`eval` 或 `new Function`：本机 REPL 分别不支持 data URL 模块和字符串代码生成。生成器直接复用同一组 helper，避免另写不一致的记录器。

```js
globalThis.arenaModule = await import("file:///E:/MTGA/skills/mtga-play/scripts/arena_ui.mjs?v=0.4.5");
globalThis.arena = await arenaModule.createArenaController(sky, {
  window: gameWindow,
  activateBeforeCapture: true,
  sessionId: "本局实际 session_id",
  journalPath: "E:/MTGA/matches/本局目录/actions.jsonl",
  logStatePath: "E:/MTGA/matches/本局目录/log_tracker/state.json",
  requireExpectedRequest: true, // 局内缺少帧绑定请求时停止；导航需显式 stage: "navigation"。
  // 仅填入本次已验证的坐标映射；不可照抄历史偏移。
  calibration: {offsetX: 0, offsetY: 0, width: 1920, height: 1080}
});
await arena.observe();
```

`activateBeforeCapture:true` 在每次捕获前调用受支持的 `sky.activate_window({window})`，window 始终来自实际选择/捕获返回值。激活或捕获失败都会清除可用帧；日志中的恢复完成只表示 API 已返回，不证明窗口持续在前台，原生拖动仍独立检查真实前台。

停下来检查截图，并将相配的玩家视角请求保存为下文的 `locatingRequest`（定义见请求绑定段），再用另一调用执行一个输入：

```js
await arena.act(
  {kind: "click", x: 1777, y: 986, coordinateSpace: "screenshot"},
  {stage: "gameplay", expected_request: locatingRequest, turn: 6, phase: "precombat_main", chosen_action: "进入战斗", reason_summary: "已有合适攻击者。"}
);
```

坐标只是格式示例，实际必须来自刚检查的截图。`act` 消耗当前观察、发送一次输入并立即刷新；内部不猜测游戏效果。协调器还检查截图两侧的完整状态证据，只在返回候选仍有效且模型检查后才复用为下一决策。底层 `arena.act` 未传只读采样器的刷新不具备这项自动复用资格。

检查该帧后，在下一调用中将上一输入的效果确认与一个新输入一起记录：

```js
await arena.act(
  {kind: "click", x: 1500, y: 600, coordinateSpace: "screenshot"},
  {stage: "gameplay", expected_request: locatingRequest, turn: 6, phase: "declare_attackers", chosen_action: "选择旧构装体", reason_summary: "可攻击且无阻挡者。"},
  {decisionSeq: arena.lastDecisionSeq, generation: arena.generation,
   status: "confirmed", note: "已进入攻击者选择，旧构装体有可攻击标记。"}
);
```

第三个参数只确认上一输入，必须在看过返回帧后填写；新的输入仍是 `unverified`。`decisionSeq` 与 `generation` 必须对应正在检查的决策和帧，helper 拒绝过期或缺失引用。只有一条新输入和一次自动刷新，随后停下来检查。当前没有下一输入时，单独记录确认：

```js
await arena.confirmEffect("confirmed", "已进入选择攻击者，只有旧构装体可攻击。");
```

也可用 `failed` 或 `uncertain`。界面尚在动画时先 `await arena.observe()`；观察后再决定是否输入。外部输入或用户接管后调用 `arena.invalidate()` 并重新观察。模块不运行自动对局循环。

### 请求改变时停止过期输入

选择单个目标可能自动提交。第四局 seq92 的雕符目标已成功，seq96 重试前日志其实已进入攻击者选择；旧截图不能证明失败。准备重试或确认时，将**先前据以定位的请求**放入 decision 的 `expected_request: {match_key, type, msg_id}`，不要用刚变化的新请求给旧坐标背书。控制器在发送输入前再次读取 `logStatePath`；游戏、请求编号/类型、决定玩家、连续性或监听状态不符则写 `input_blocked`、清除坐标并停止，不发送输入也不额外截图。重新读简洁日志，判断原动作是否已完成，再按需要观察新控件。

v0.4 在此检查中重算 `ready_for_action`，包括心跳、日志追赶、连续性、当前受支持请求与己方决定权。可附 `game_state_id`、`source_generation` 进一步绑定状态与日志世代；会话协调器会提供这些字段。检查不证明画面已重绘或坐标属于正确物件；输入前后仍存在状态竞争，不确定时按画面回退流程处理。未配置日志的底层导航保持原行为。第五局v0.3.2实际客户端已验证的是请求61→64与351→357拦截；后者为主动探针，不能扩大为当前版本已经实测覆盖所有动画。

把请求快照保存在本次检查过的帧旁。读取新状态的函数只返回新状态，不暗中覆盖旧帧的请求；即使它显示另一个合法请求，也必须重新判断旧坐标是否可用。传入控制器的是此前定位时的请求，不是输入前才读到的请求：

```js
// inspectedState 必须是与刚检查帧相配的玩家视角状态。
const locatingRequest = Object.freeze({
  match_key: inspectedState.match_key,
  type: inspectedState.pending_request.type,
  msg_id: inspectedState.pending_request.msg_id
});
// 后续只读检查不得修改 locatingRequest；若新请求不同，先放弃旧坐标。
// 本次语义动作的 decision 中传 expected_request: locatingRequest。
```

第五局seq23在落地后的重排帧上点错为改装间，虽然调用者已给出新请求。它说明请求检查无法发现“合法请求+错误坐标”。手牌成员变动后，若返回帧还显示原牌位，先等相关日志变化再按需要观察一次，核对牌名和原始牌位；不要沿用动画前的相邻卡牌中心。

helper 在事件中记录单调时钟的 `timing`：决策的 `since_capture_ms`，执行的 `input_ms/capture_ms/action_cycle_ms`，额外观察的 `capture_ms`。这些只测量工具层和调用间隔，不证明游戏效果。导入后的脚本及其依赖会被 Node 缓存；更新 helper 后先关闭已有会话，再在下一局重置 REPL 并重新加载，避免只更换入口 URL 却仍调用旧依赖。不要在活动对局中重复 bootstrap。

观察到对局结束后，可用 `arena.note({type:"game_result", result:"win", self_life:8, opponent_life:-10})` 写入实际结果，再更新 session.json。这里的数值是格式示例，不能照抄。

## 坐标、遮挡与控件

- 截图点、桌面坐标、窗口坐标必须区分。输入映射要在当前窗口模式、尺寸与 DPI 下确认。分辨率、捕获区域或后端变化后，旧校准失效。helper 会拒绝尺寸不一致的校准。
- 首局1920×1080带标题栏截图中，成功点击使用 `input_y = screenshot_y - 31`。这是本机历史证据，不是 sky API 的普遍规则。
- 第二局全屏截图同为1920×1080，但实际使用零偏移。-31偏移点击编辑按钮选中了下方新手牌；零偏移选择指定牌组并打开编辑页成功。同样尺寸不足以证明校准可复用，先看有无标题栏及低影响操作的实际反馈。
- 卡牌悬浮放大会遮住其它目标；在不触发游戏动作的空白战场位置移开/点击后刷新，再定位目标。
- 施放手牌时定位底部原牌可见区域；悬浮的大卡图不一定可施放。第五局seq54双击悬浮战线未改变状态，seq57在底部原牌位置成功。不要把预览图边界当成原牌点击区域。
- 若截图或报错指向其他窗口，重新激活实际 MTGA 窗口并刷新。不得沿用旧坐标继续点。
- 手中施放在首局可用双击；牺牲/选目标先单击，核对橙色框及“提交 N 项”后再单独确认。异能也可能先显示动画，下一张截图才出现费用提示。
- 场上物件移走或新物件入场会重排布局。牺牲左侧构装体后剩余构装体移到中间，不能沿用右侧旧坐标。
- 选择攻击者与确认攻击是两步。检查攻击数量与召唤失调，不能将“全部攻击”第一次点击当作已完成战斗。
- 空格在首局未推进阶段。优先用当前截图中的“下一步/结束回合”按钮，并核对下一阶段文字。保留可用异能时客户端可能停留在伤害前优先权。

## 当前帧的只读裁剪（v0.4.5）

`scripts/frame_crop.mjs` 是当前截图的局部查看辅助，不识别卡牌或生成位置。模型先检查完整帧，再明确选择本帧 ROI；不得保存未来布局坐标。输入仅接受 `coordinateSpace: 'screenshot'` 的逻辑像素矩形；`controllerId/generation/screenshotId` 必须全部来自当前已返回帧。`Screenshot.url` 是实际图像 data URL，`width/height` 是可选逻辑尺寸，不能假定等于编码像素。缺少逻辑尺寸时拒绝；默认 PNG renderer 从字节头读取编码尺寸并返回比例、逻辑矩形及向外取整后的编码矩形。`originX/originY` 仅记录屏幕原点，不充当窗口输入校准。

普通 ESM 导入如下；bootstrap 已导出同名函数，不需要再导入。变量矩形必须取自当前全图中实际需要细看的区域：

```js
var { cropCurrentFrame, cropPNG } = await import('file:///E:/MTGA/skills/mtga-play/scripts/frame_crop.mjs?v=0.4.5');
var sourceGeneration = arena.generation;
var shot = arena.state.screenshots.find(s => s.id === inspectedScreenshotId);
var crop = await cropCurrentFrame(arena, {
  controllerId: arena.controllerId, generation: sourceGeneration, screenshotId: shot.id,
  coordinateSpace: 'screenshot',
  rect: { x: inspectedLeft, y: inspectedTop, width: inspectedWidth, height: inspectedHeight }
});
await nodeRepl.emitImage(crop.image.url);
// crop.full_frame 保留原图；crop.source / mapping / rect / pixelRect 保留完整来源与映射。
```

默认只支持有界（最多 16M 编码像素）、8 位、非交错 PNG，支持灰度、RGB、RGBA、灰度透明及 8 位调色板，并校验字节边界、CRC 与行过滤器。格式不支持时明确拒绝，不安装 OCR 或图像依赖。仅使用 Node 内置 `node:zlib`，不依赖 `Buffer` 全局或浏览器 canvas。

可选第三个参数是可信的只读 renderer；参数**只有** `{url, pixelRect}`，不得调用电脑后端或修改会话。传自定义 renderer 时还须明确给出由实际图像确定的 `encodedSize: {width, height}`；返回 `{url, width, height}` 必须是该编码矩形的真实裁剪。以下是可运行的 PNG callback 示例（尺寸读取自前一次默认 PNG 路径，仍在同一当前 generation 内）：

```js
var callbackCrop = await cropCurrentFrame(arena, {
  controllerId: crop.source.controllerId, generation: crop.source.generation,
  screenshotId: crop.source.screenshotId, coordinateSpace: 'screenshot', rect: crop.rect,
  encodedSize: { width: crop.mapping.encodedWidth, height: crop.mapping.encodedHeight }
}, async ({url, pixelRect}) => cropPNG({url, pixelRect}));
await nodeRepl.emitImage(callbackCrop.image.url);
```

调用前后都核对源帧对象、截图身份/字节/尺寸与 generation，异步 renderer 期间发生输入、失效或刷新即拒绝结果；有限值、正尺寸及逻辑/编码完整边界均验证。任意 callback 没有沙箱隔离，契约及尺寸检查不能证明一个不可信 callback 的内容正确。裁剪不刷新画面，不延长 token，不确认效果，不建立输入绑定；模型仍须检查完整上下文及最终返回图，后续输入继续使用已验证的窗口校准。离线像素回归和合成预检不代表真实 MTGA 截图格式已验收。

## 占卜与拖动

记录占卜牌、顶/底区域和当前分区数量。用新截图的起点终点，向 `sky.drag` 传当前 `screenshotId`，对两端使用同一坐标映射。移动后必须看见分区或数量变化再确认牌序。

首局拖动均未生效。早期调用缺少 screenshotId 且映射不统一，是待实测原因；本 helper 统一传递这些参数，但隔离测试只能证明参数正确，不能声称已经修复 MTGA 占卜拖动。

第二局调度置底中，零偏移且携带当前 screenshotId 的拖动仍未移动牌，点击预选后拖动也失败。放大卡面可能遮住原牌列，先恢复原始可见位置。用户明确授权改用本地连续拖动后，`mouse_event + SetCursorPos` 的约0.5秒路径仍失败；统一使用 `SendInput` 按下、32段绝对移动、松开后，调度两张及两次占卜各一张均成功。不能把结论简化成“只要慢拖就一定成功”。

若两次有依据的尝试仍无变化，不重复原操作。有新证据或用户给出具体技巧时继续检验不同假设；没有可用的合法替代方法时才请求必要协助，不另开一局。接受两顶等次选时记录实际分区，不能作为原置底计划的成功示范。

### 已授权连续拖动后端

本机 `scripts/timed_drag.ps1` 仅适用于已核对的 `E:\MTGA\MTGA.exe`、标题 MTGA、1920×1080 客户区与64位 PowerShell。它不激活窗口；焦点丢失即停止后续移动并释放左键。先通过正常截图确认前台、提示、目标和坐标，再记录一次决策，执行一次拖动，立即刷新并人工检查分区。GUI window.id 是不透明标识，不能传作 HWND；脚本自行从唯一匹配进程取得原生句柄。

本机受限终端可能看不到桌面 MTGA 进程；第四局第一次执行在查进程时退出且未发送输入，同一脚本通过受审查的桌面权限执行后成功。已获本会话后端授权时，先使用合适的执行权限，不把沙箱内“进程不存在”解释成游戏已关闭；仍保留唯一进程、路径和焦点校验。

```powershell
# 不发送输入的轨迹检查；坐标必须取自当前已检查截图。
& '<skill>/scripts/timed_drag.ps1' -FromX 1100 -FromY 420 -ToX 580 -ToY 480 -DurationMs 500
# 当前会话已授权此后端时，才在同一参数后加 -Execute。
```

预悬停150ms、按下后100ms、线性移动约500ms、终点停留150ms、松开；返回的 `actual_movement_ms` 只测移动段，实际整次输入更长。v0.4.4 使用下述事务自动记录决策与执行，画面效果仍由后续人工检查确认。API成功不等于牌已移动，必须看见左底/右顶变化；能观察后续抓牌时再交叉核对。不要添加未被 sky API 支持的 duration 参数，也不要把本局许可推广到其他会话。

#### 隐藏启动与两阶段拖动（v0.4.4）

当前会话明确授权此后端后，先 `await matchSession.cancel("进入限定拖动回退")`；再 `await arena.observe()`，检查这张新帧并冻结匹配请求。此窄事务仅处理正常日志仍完整、连续、当前受支持己方请求内的视觉控件盲区，使用与普通 gameplay 相同的请求/就绪检查，不能用于绕过缺口或不受支持请求。

```js
globalThis.dragPrepared = await arena.prepareTimedDrag(
  {kind:"drag", from_x:inspectedFromX, from_y:inspectedFromY,
   to_x:inspectedToX, to_y:inspectedToY, coordinateSpace:"screenshot"},
  {stage:"gameplay", request_binding_mode:"visual_fallback", expected_request:locatingRequest,
   chosen_action:"把已核对的占卜牌移到牌库底"}
);
```

准备只验证并消耗当前帧，返回一次性 `ticket` 与已映射的 `mapped_input`；尚未发送输入。准备后暂停其它 arena 操作。在外部终端工具执行同一打包脚本，参数复制此 ticket 与映射后的数字；**不得在 node_repl 内启动 PowerShell**，不得将 GUI window.id 当 HWND：

```powershell
& '<skill>/scripts/timed_drag.ps1' -Hidden -Execute -ResultDirectory '<本次已有绝对目录>' `
  -TransactionId '<dragPrepared.ticket>' -FromX <mapped.from_x> -FromY <mapped.from_y> `
  -ToX <mapped.to_x> -ToY <mapped.to_y> -DurationMs 500
```

外层使用64位 PowerShell，隐藏子进程执行同脚本的原有 guards，不改变 ExecutionPolicy，不主动恢复焦点。它保存唯一 stdout/stderr/result 文件、子退出码、PID、时间戳、ticket 与坐标，父退出码保留子失败。终端工具长时间运行时保留并轮询同一工具会话，不强杀持有鼠标的 helper、不再启动第二份。启动错误也保留原始错误；若工具没有返回结果，结束事务时传 `null`，记录未知而非补造成功。

将本次结构结果读为 `helperOutcome` 后，在 REPL 立即完成一次刷新：

```js
globalThis.dragResult = await arena.finishTimedDrag(dragPrepared.ticket, helperOutcome);
```

等待这次工具返回，实际检查展示的 `dragResult.frame`；确认执行及刷新均成功且画面显示预期变化后，下一次独立调用才记录真实效果：

```js
await arena.confirmEffect("confirmed", "新帧显示目标牌已进入底部分区。");
```

`finishTimedDrag` 只接受当前 ticket 一次，核对结果中的 ticket、坐标、exit 0 与解析后 `executed:true`；无论 helper 成败均只尝试一次捕获，并只写一条执行终态。仅执行及刷新成功返回 `sent_and_refreshed/unverified`。其余为 `uncertain`，原始结果和错误保留，部分输入是否发生为未知，后续禁止 `confirmed`；只能根据实际观察记录 `failed/uncertain`，不重试旧坐标。截图或终态写日志失败也清除可用帧。准备到完成的跨工具间隔单列 `external_dispatch_wait_ms`；`input_ms` 仅在 helper 起止时间可用时记录，包含启动、编译、guards 与输入，缺失为 null。

第二局 `match_20261012_104243_11e431b7` 的 seq98–103、149–155、256–263 是成功证据；调度两张、占卜海岛和沼泽的实际移动分别约516.60、508.93、502.30、513.89ms。第4回合保留的硬光禁锢在第5回合正常抽到。第三次占卜保留两顶，直接点完成合法。实现参照 [MOUSEINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput) 与 [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput) 文档；该客户端证据不保证所有版本、DPI和窗口模式都可复用。

检索界面单选可能自动提交，联合战线选择第二张后也可能立即进入光环附着。每次刷新后读当前提示，不机械追加“完成”；光环附着仍可能要求选一个物件后另点“提交1项”。

第五局雕符离场的发现3显示本地“免费施放”选择，但过滤日志仍是通用ActionsAvailableReq。该画面需截图回退核对；supported=true只表示已解析该请求结构，不证明所有本地控件已还原。

### 第六局验证后的绑定边界

v0.3.3 第六局 `match_20261012_144617_fc039b92` 中，落地后重新核对牌位，第二融合仪在悬浮圣物箱旁的可见原牌区域施放（seq54）；悬浮完全遮挡时先点空白战场，再点恢复的原牌（seq92、94）。两种方法成功，未出现上局错牌与预览双击无效；这只覆盖本局实际布局。

日志读取与绑定分开：`readState()` 仅返回新日志。`act()` 或 `observe()` 返回后也不要自动调用 `bind()`；先人工检查那张返回帧是否已显示相应提示/牌位，再显式冻结相配请求。输入后返回帧可能仍是旧画面，而日志已经前进，自动 `Read(); Bind()` 会再次把新请求配给旧坐标。本局调用器仍有这个隐患，靠后续检查和必要观察避免了误点；不能声称控制器已验证帧与日志同步。

实时对局控制器设置 `requireExpectedRequest: true`。v0.3.4 将缺失绑定记录为 `input_blocked: missing_expected_request`，清除旧帧，不发送输入、不自动截图；纯导航显式使用 `stage: "navigation"`。第七局seq3–5用省略绑定的零输入探针验证：请求与截图代数不变、旧帧清除、无输入及自动截图；这是主动探针，不是自然误点。已有绑定改变仍由原 guard 拦截，不能拿新请求自动替换旧绑定来绕过。停止监听后不再用局内绑定操作客户端。

### 只读进度等待（v0.3.5）

不要在局内临时拼装同时读取、截图和绑定的循环。`waitForProgress` 只读过滤投影，不输入、不截图、不换绑；传入本次输入**之前**的状态快照。它检测gameStateId或请求类型/编号变化，默认等新的当前己方请求，也能在服务器结果出现时返回。

```js
// act前保存，不自动刷新 locatingRequest。
const beforeInput = structuredClone(inspectedState);
// ...一次 act，立即返回截图，先人工检查...
// 仅当画面仍在动画且确需新坐标时：
const progress = await arena.waitForProgress(beforeInput, {
  timeoutMs: 8000, pollMs: 200, settleMs: 400
});
// progress.document是缓冲前的日志快照，不保证返回时仍新鲜。
// changed只是观察时机依据；按需要另一次observe，检查帧，重新读日志再显式绑定。
```

超时不代表输入失败；先读日志检查自动提交和局势，再按画面诊断，不能重发旧坐标。`match_changed`或`watcher_stopped`停止局内输入。`requireSelfDecision:false`仅用于读取提交/结算进展，不授权下一输入。缓冲限0–1000ms，整个轮询限15秒，缓冲最多再加1秒；调用时可以不缓冲。缺口后的返回仍是部分投影，不使严格act重新可用。该helper经过隔离及本局日志离线复验，尚未下一局验证提速。

每局重建一个会话对象管理state、locatingRequest、arena和目录，或先重置REPL。不要让新函数通过旧闭包引用上一局Read/Bind/目录；读函数只读，等待函数不顺便截图。第七局旧闭包诊断和多余observe增加了操作间隔。

### 日志盲区的显式画面回退

缺口后不能伪造continuous=true、取消requireExpectedRequest，或把gameplay改标navigation。严格act仍要求完整绑定。若已有用户授权完成本局，当前画面可明确核对合法输入时，可走computer-use的单步API回退，并记录`request_binding_mode:"visual_fallback"`、具体盲区、定位代数、当时match_key和请求类型/编号（缺失显式null）。这不算严格绑定成功。

输入前再次只读比较上述定位快照：换局、请求类型/编号变化、帧失效则停止并重新观察；不能读取一个新请求直接替换旧快照。若连续日志和可用请求已恢复，则重新观察后回到严格act。每步仍只有一次输入，紧接一次截图，不把单目标自动提交再点成旧确认。

回退也应记录输入、截图、cycle和上一截图返回至决策的单调时间；截图若通过observe记录，execution附`capture_already_counted:true`，按generation只算一次。没有记录的指标保持unknown，不在赛后估成API时长。原生拖动保留独立授权范围。
