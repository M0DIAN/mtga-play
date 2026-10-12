# 赛后数据与证据迭代

## 事件和样本

`actions.jsonl` 保留输入前意图、实际输入、刷新结果、后续观察、确认或纠错。记录器使用单调递增 `seq` 与当前 UTC 时间；`execution=sent_and_refreshed` 只描述工具层。`effect_verification` 才记录观察到的游戏层结果。

`decision_records.jsonl` 每个候选示范至少有：

```json
{
  "schema_version":1,"sample_id":"session_001","session_id":"实际session_id",
  "turn":6,"phase":"precombat_main",
  "observation":{"self_life":8,"opponent_life":20},
  "candidate_actions":["升级被封住的构装体","复制构装体"],
  "chosen_action":{"type":"activate_ability_and_search","source_card_id":94858,"search_card_id":87141},
  "reason_summary":"将无法战斗的资源变成去除，保留可进攻的生物。",
  "verified_result":"实际观察到圣物箱进入战场、唯一合法目标被放逐。",
  "evidence":{"action_event_sequences":[1,2,3],"effect_status":"confirmed"},
  "quality":{"label":"reviewed_self_play_candidate","expert_gold":false,"optimality_proven":false},
  "training_inclusion":true
}
```

上例是字段格式，不是可直接追加的真实样本。证据必须指向本局实际事件。状态可以用结构化语义或简洁文字；不能捏造完整手牌。区分未知、部分观察与确认完整的区域。

若标记 `self_battlefield.artifacts`，每项 quantity 计入总数，神器生物不再重复加一次。确实完整时设 `artifact_list_complete=true`。依数量动态计算的攻防使用明确公式：

```json
{"name":"Construct","quantity":1,"power":10,"toughness":10,"stats_basis":{"kind":"artifact_count","base_power":0,"base_toughness":0,"power_modifier":0,"toughness_modifier":0}}
```

计数器、灵气或其它修正要放入 modifier 或更合适的明确模型；不能对不同 Construct 衍生物仅凭名字套用此公式。

同一语义动作的多个点击合为一个示范，避免把导航、悬浮恢复和确认按钮当作战略样本。理由只保留简短可复核的依据，结果与策略置信度放在元数据。

`corrections.jsonl` 单独保留失败操作、误读、未验证的选择和错误理由。纠正后若动作确实完成且理由已复核，可保留那个独立动作，但不能把失败拖动升级为成功示范。决策输入中不含后续抓牌、对手未来动作、最终胜负等事后信息。

训练消息由 curate 生成；元数据逐行保留 sample_id、结果、引用和质量标签。训练/验证/测试按整个 session_id 分组，不能随机拆散同局步骤。

用 `scripts/match_timing.py` 生成耗时报告，方法与限制见 [对局提速](speed.md)。完整复盘和训练样本在赛后生成；局内保留足够的已观察状态与事件引用。日志中的耗时测量和效果确认不自动证明战略最优。

使用 [实时日志状态](log-state.md) 时，额外保存监听事件seq、gameStateId和服务器确认类型。按事件发生顺序重建动作前状态，不把赛后完整日志反向补进当时未知的手牌、牌序或选择。客户端提交、服务器结算和全局状态连续性分别标注；缺口后的独立效果证据不等于全局完整。旧样本发现新错误时新增附加纠错与版本化数据，保留原始事件及旧训练文件。

## 改进 skill

每局结束后针对有证据的问题做窄修改。保留：原始会话路径和事件编号、可观测失败、适用范围、改动位置、复验结果。评估产物放在本机 `E:/MTGA/skill_evaluations`，避免把完整日志和大量旧案例加载进 SKILL.md。

修改策略提示需有卡面规则、可复核战斗计算或更多对局支持。一次 Sparky 胜利不证明路线最优。UI 参数传递可用隔离测试验证；只有实际客户端操作成功才可提升为“客户端已验证”。

改动 helper 后运行对应测试，再运行 skill-creator 的 quick_validate.py。资料解析器用真实牌表核对 ID、数量、关联面和衍生物；数据生成器逐行解析、核对引用与排除结果。无需为文案措辞写镜像测试。

保存改动前的相关文件和评估摘要，再同步源目录与安装目录。若已存在不同的安装副本，先比较内容，不能覆盖用户改动。同步本 skill 自己的文件，不改动 MTGA 可执行文件、官方数据库或 Mod 配置。

## 首局证据入口

`E:/MTGA/sparky_20261012_042617`：胜利，第7个我方回合，8血；19条候选样本，5项纠错。

- seq 28–38、40、44：占卜计划未执行，实际两顶，黑源延后一回合。
- seq 54、56：把墓地区域误认为手牌，实际离场抓牌为海岛。
- seq 57、59、61：空格未推进，点击按钮后才改变阶段。
- seq 4、6：标题栏/客户区位置换算影响首页点击。
- seq 94、101：去除会同时改变构装体攻防，原理由过强。

这些证据支持当前的核验与重算规则。第二局 `E:/MTGA/matches/match_20261012_104243_11e431b7` 在用户授权的连续 SendInput 后端下完成两次置底占卜及一次两顶占卜，并在第9回合获胜。失败输入与误读独立保存；具体范围见 [界面操作](ui-play.md)。
