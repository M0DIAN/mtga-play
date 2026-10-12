# 实时日志状态

`scripts/track_state.py` 只读本地日志，不发送游戏动作、不读进程内存、不修改客户端设置。它持续读取新增字节，合并 GRE 完整快照和增量更新，输出过滤后的玩家视角。局内只读 `status` 或 `state.json` 的投影，不加载原始 Player.log 或内部回放 fixture。

## 开局前启动或续接，局内读取

先确认当前 `Player.log` 路径及当前牌组资料。详细日志在客户端账户选项中启用；本机第二局已经有 GRE 数据，不需为本次实现改设置或重启。

```powershell
python -B -X utf8 <skill>/scripts/track_state.py watch --log '<Player.log>' --start-at-end --output '<本局目录>/log_tracker' --stop-file '<本局目录>/stop_tracker' --cards '<本局目录>/card_records.jsonl' --cards '<本局目录>/related_cards.jsonl' --card-db '<当前官方Raw_CardDatabase_*.mtga>'
```

`--start-at-end` 只适合尚未触发的新局：首次 poll 跳过已有历史，随后接收新增消息；日志截断/替换时仍按有界回溯恢复。确认 watcher 已实际发布本次 run 的健康状态，才触发开始按钮，避免新局消息落在初始跳过范围。它不提供已有对局基线。续接用户授权的进行中对局时，去掉该参数，使用默认最多64MiB回溯重建；不能仅因截图已经在局内就跳过基线要求。

输出目录必须是新目录，每次运行独占，不覆盖旧回放。`watch` 持续运行至中断，优先通过终端工具的运行会话保持进程并保留 stdout/stderr 和退出码；若使用 Start-Process，记录 PID 及输出路径，必须核对实际产物和健康状态，不能把返回 PID 当作启动成功。`--duration-seconds N` 用于有限验证，默认0表示持续监听。不要因监听实现或离线验证自行开新对局。

结束后创建指定的 stop-file，并等待监听进程正常退出。正常停止及捕获到的异常均尝试写入最终 state 和 summary；失败状态为 `live=false`、`caught_up=false`、`status=error`，保留最后成功处理和投影的状态，不能用于继续输入。异常在首次发布前发生时，也会尝试留下报告。磁盘写入被拒或进程被外部强杀仍可能阻止保存；保留终端原始输出，不能补造文件。本机 Windows 终端 Ctrl-C 曾直接终止 Python 而未运行 finally，不能只凭最后一个心跳中的 `live=true` 判断进程仍在运行。stop-file 必须使用本局的新路径，避免旧文件让下一局监听立即退出。

开局前及等待新局基线期间，优先使用轻量检查，并核对 `run_id` 与本次 `run.json` 一致：

```powershell
python -B -X utf8 <skill>/scripts/track_state.py status --state '<本局目录>/log_tracker/state.json' --health-only
```

健康 `PASS` 返回0，缺文件、进程停止、心跳过期、尚未追平或报告错误时 `BLOCKED` 返回2。健康只表示监听可用；上一局已结束、当前还没有新局基线时，也可能健康但 `ready_for_action=false`。不要把健康检查当作游戏输入许可。发现监听错误后立即停止待执行输入，读 summary/error 和原始 stderr；不要再等待整段基线超时或加载全部历史投影寻找错误。

`error` 仅保存异常类型、阶段、失败事件及最近最多16条已处理事件的类型、座位、状态编号、哈希比赛身份和来源位置；原始异常文本保留在 stderr。`last_successful_state` 标识最后成功投影对应的比赛、座位、状态编号和来源，从未成功处理时为 null。最终文档的 `source.byte_offset` 是已读字节位置，不能解释为失败消息已经成功应用。

```powershell
python -B -X utf8 <skill>/scripts/track_state.py status --state '<本局目录>/log_tracker/state.json'
```

默认返回简洁的生命、回合、手牌、战场、堆叠、当前请求和可用动作；`--full` 才包括其它公开区域与近期历史。卡牌名称优先来自已准备资料，`--card-db` 以只读SQLite连接补充对手牌和衍生物名称。名称查询不替代完整规则查询。

`self_seat` 默认从会消费状态或请求的 GRE 单一接收座位推定，无法确定时不投影私人牌。无状态的连接、计时和 UI 通知不用于推定或改变座位。只有实际证据支持时才给 `--self-seat`；参数与真正状态/请求接收座位冲突会停止，不能把座位1当作所有对局的本方。

room 的 `matchId` 与 GRE `gameInfo.matchID` 使用同一种哈希身份。明确的新身份先清除旧局座位、对象、请求与基线，再应用新状态；同 key 的迟到 room 只更新房间状态，不重复清空基线。最近16个已离开的比赛身份不会被迟到消息重新打开；日志源重建会清空该记录。缺失身份的 room 不生成虚假 key，也不重开旧局，并标记连续性缺口。真正 state/Req 在同一身份、无新身份或显式固定座位下发生座位冲突时继续拒绝；不能仅凭 seat 变化、较小状态编号或旧局 Completed 推断新局。GRE 先到而尚无 Playing room 时保持等待，不能人为补写就绪状态。

## 决策与效果证据

- 检查 `ready_for_decision` / `fallback_reasons` 及 `ready_for_action` / `action_blockers`。默认要求监听心跳在5秒内、追到文件末尾、已有完整基线且增量连续、当前仍在对局。最近游戏事件一般也须在5秒内；若仍在等待同一受支持的当前己方请求，允许事件年龄更长。该例外不豁免心跳、缺口或请求有效性。`status` 和界面输入会分别在使用时重新计算，不信任文件中可能过期的就绪布尔值。
- 开始新局后先等待新的 `match_key` 和完整基线，再使用该局回合/生命；点击开始前的最后一份 state 可能仍是上一局结束。续接已授权的同一进行中对局时，用新 run 重建并核对当前比赛身份、完整连续基线、请求及画面，不要求为了得到不同 key 而重开。`ready_for_decision` 只表示观察状态可用；`ready_for_action` 额外要求己方决定权、受支持请求、相同状态编号与有效请求身份。两者仍不能证明界面已重绘或策略正确。
- 缺失 `prevGameStateId` 链接、中途从日志尾开始、乱序、未完成写入、未知请求或请求对应旧状态，都不能作为完整决策依据。已有对象继续输出供诊断，`coverage.continuous=false` 明示部分状态，直到新的完整快照才恢复。
- `last_received_event_time` 仅表示收到消息，不能使旧状态重新有效。`source_state_time` / `source_request_time` 来自已应用的状态与有效请求；必要状态无法解析会保留缺口。
- 区域列表是权威成员列表；对象按 instanceId 保存当前特征。变区可能产生新instanceId，不能把原实例当作同一区域的永久物。日志中列出的区域/对象是实体替换：省略成员表示空区，省略横置字段表示false。不能做普通字典浅合并而留下幽灵堆叠或横置状态。
- `GroupReq` 给出实际可看的占卜牌及分组边界；`GroupResp` 只表示客户端提交。`AnnotationType_Scry` 的 `topIds/bottomIds` 才是服务器确认，保留顺序并允许空列表。它不能验证点击完成之前的中间拖动位置，那个反馈仍看画面。
- `ZoneType_Stack` 给出当前对象，`AnnotationType_TargetSpec` 给出目标实例。入场触发和起动式异能可能在Pending区，再进入Stack；不要把“堆叠空”误解为“没有待处理触发”。必要时看 `--full` 的Pending区。
- `GameObjectType_Ability` 的 grpId 是异能编号，与卡牌编号可能重合；使用 `ability_id` 和 `source_card_id/source_name` 识别，不能把它查成同编号卡牌。灵气咒语的结附目标与其进场异能的放逐目标分别核对。
- `SelectNReq` 的实例候选与 `unfiltered_cards` 可包含当前合法查看的牌；身份仍要求本方 viewer 或公开可见，普通 Library 区域保持隐藏。选牌规则和触发排序都可能用同一种请求，按 context 区分，不能把所有 SelectN 都当作查看牌库。
- 战场按完整区域成员计数，以日志对象的当前power/toughness为主要观测，另用规则交叉检查。`self_artifact_count_observed` 在缺口后只是已还原数量；只有 `self_artifact_count_complete=true` 才可称完整计数。结界不是神器，生物神器只计一次。
- 可用动作保留实际实例、费用和异能ID；检索输出合法候选、牺牲输出费用选择，攻击输出合格攻击者。它们辅助判断，仍须核对时机、支付和规则，不是直接调用的控制API。

用 `arena.note` 或本局记录器记录证据，如 `method:"server_log"`、监听run路径、events.jsonl行seq、gameStateId、确认类型与一句实际变化。不得伪造这些引用。候选训练输入只取动作之前的状态；未来日志事件、胜负和抓牌放在结果元数据。

部分状态仍可提供独立效果证据：即使存在旧缺口，后来的服务器Scry注记可核对该次牌序，后来的对象更新可证明该对象当时的攻防。此类证据不能自动恢复全局连续性，也不能证明其它未知部分正确。

## 可见性与截图范围

投影不导出双方牌库顺序或对手隐藏手牌。私人牌只有当前玩家手牌及当前合法查看请求可见；公开牌回到隐藏区域后不会沿用旧身份。内部回放输入可能含本方牌库对象，不能直接用于决策或训练。不要将原始账户、凭证或对手隐藏数据送入模型。

日志不提供可靠的当前像素坐标、卡面遮挡、拖动中间反馈或动画完成状态。保持 computer-use 的输入和刷新要求；当前 `arena_ui` 每次GUI输入后仍刷新。减少的是额外 `observe()`、逐张截图识牌、重复数场面与为确认逻辑效果而追加的截图，不能借此盲点多步动作。缺口或过期时按具体未知字段补看，恢复后继续优先用日志。

## 本局离线验证

```powershell
python -B -X utf8 <skill>/scripts/track_state.py replay --events '<已冻结match_events.jsonl>' --output '<新的回放目录>' --self-seat 1 --cards '<本局card_records.jsonl>' --card-db '<官方数据库>'
```

普通日志离线回放用 `--log` 替换 `--events`。离线输出始终 `ready_for_decision=false`。默认启动扫描最多64MiB，随后仅读新增数据；截断、替换或已读尾部发生改变时重建状态，不能拼接新旧会话。

第二局 `match_20261012_104243_11e431b7` 的冻结输入与验证报告在 `E:/MTGA/skill_evaluations/mtga-play/v0.3.0`。服务器确认三次占卜，与截图复盘一致；谋杀目标及构装体8/8→7/7、14点伤害、最终17/-21均可核对。客户端把state155缩成摘要，Player.log与UTC日志都缺少该完整消息，因此本局不能宣称全程可由日志完整替代截图。第6回合对手2/2经官方数据库解析为Scathe Zombies，无死亡弃牌异能，见本局附加纠错。

第三局 `match_20261012_124245_fb857612` 在开局前监听，完整基线至结束 gs343 无增量缺口。占卜 gs112 确认禁锢留顶、战线置底，gs155 抓到禁锢；战线的星笼→融合仪→刹停冲势依次结算，头盔复制后为两只7/7及地图。此局构装体死亡来自 Unlikely Aid 强化后的战斗伤害，不能沿用上一局的 Murder 解释。冻结输入和回归检查保存在 v0.3.1 评估目录。
