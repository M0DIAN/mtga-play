# 实时日志状态

`scripts/track_state.py` 只读本地日志，不发送游戏动作、不读进程内存、不修改客户端设置。它持续读取新增字节，合并 GRE 完整快照和增量更新，输出过滤后的玩家视角。局内只读 `status` 或 `state.json` 的投影，不加载原始 Player.log 或内部回放 fixture。

## 开局前启动，局内读取

先确认当前 `Player.log` 路径及当前牌组资料。详细日志在客户端账户选项中启用；本机第二局已经有 GRE 数据，不需为本次实现改设置或重启。

```powershell
python -B -X utf8 <skill>/scripts/track_state.py watch --log '<Player.log>' --output '<本局目录>/log_tracker' --stop-file '<本局目录>/stop_tracker' --cards '<本局目录>/card_records.jsonl' --cards '<本局目录>/related_cards.jsonl' --card-db '<当前官方Raw_CardDatabase_*.mtga>'
```

输出目录必须是新目录，每次运行独占，不覆盖旧回放。`watch` 持续运行至中断，可通过终端工具的运行会话保持进程；若使用 Start-Process，保持窗口隐藏并记录 PID 以便本局结束后停止。`--duration-seconds N` 用于有限验证，默认0表示持续监听。不要因监听实现或离线验证自行开新对局。

结束后创建指定的 stop-file，并等待监听进程正常退出；它会写入 `live=false` 的最终状态及 summary。本机 Windows 终端 Ctrl-C 曾直接终止 Python 而未运行 finally，不能只凭最后一个心跳中的 `live=true` 判断进程仍在运行。stop-file 必须使用本局的新路径，避免旧文件让下一局监听立即退出。

```powershell
python -B -X utf8 <skill>/scripts/track_state.py status --state '<本局目录>/log_tracker/state.json'
```

默认返回简洁的生命、回合、手牌、战场、堆叠、当前请求和可用动作；`--full` 才包括其它公开区域与近期历史。卡牌名称优先来自已准备资料，`--card-db` 以只读SQLite连接补充对手牌和衍生物名称。名称查询不替代完整规则查询。

`self_seat` 默认从 GRE 单一接收座位推定，无法确定时不投影私人牌。只有实际证据支持时才给 `--self-seat`；参数与消息接收座位冲突会停止，不能把座位1当作所有对局的本方。

## 决策与效果证据

- 检查 `ready_for_decision` 和 `fallback_reasons`。默认要求监听心跳与最近游戏事件都在5秒内、追到文件末尾、已有完整基线且增量连续、当前仍在对局。心跳新鲜不能使旧对局状态变新；`status` 每次重新计算新鲜度。遇到长时间等待合法输入，可以看当前截图确认提示，不把旧状态擅自标为新鲜。
- 开始新局后先等待新的 `match_key` 和完整基线，再使用该局回合/生命；点击开始前的最后一份 state 可能仍是上一局结束。`ready_for_decision` 只表示还原与新鲜度通过，不代表本方有优先权；另核对 `decisionPlayer`、当前请求和时机。
- 缺失 `prevGameStateId` 链接、中途从日志尾开始、乱序、未完成写入、未知请求或请求对应旧状态，都不能作为完整决策依据。已有对象继续输出供诊断，`coverage.continuous=false` 明示部分状态，直到新的完整快照才恢复。
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
