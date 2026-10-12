# 本地资料与工具

脚本仅处理本地文件，不操作客户端，也不启动对局。使用可用的 Python 3；Windows 建议加 `-X utf8`，避免终端中文乱码。下文的 `<skill>` 指本 skill 的绝对路径。

本文件的 `mtga_data.py` 用于牌表及赛后数据；局内持续状态读取用 [实时日志状态](log-state.md) 的 `track_state.py`，不要每次操作重新扫描日志尾部或加载完整牌库对象。

## 找到当前套牌

```powershell
python -X utf8 <skill>/scripts/mtga_data.py list-decks --player-log <Player.log>
```

默认日志为 `%USERPROFILE%/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log`。只读取默认 8 MiB 的尾部，只解析已验证的 `DeckUpsertDeckV3` 结构，不打印原始请求。必要时明确增加 `--tail-mib`（最多 64）；找不到就从客户端导出牌表，不扫描数 GiB 旧日志或把最后出现的牌组当作当前套牌。

已验证字段为外层 JSON 的 `request`（可能是 JSON 字符串），其内 `Summary.Name`、`Summary.DeckId`、`Summary.Attributes` 与 `Deck.MainDeck/Sideboard/CommandZone/Companions`。每张牌有 `cardId`、`quantity`。按 DeckId 保留最近记录；同名不同 ID 必须消歧。

## 创建对局资料目录

```powershell
python -X utf8 <skill>/scripts/mtga_data.py prepare --game-root E:/MTGA --deck-name '神器！神器！' --output-root E:/MTGA/matches --translation-db E:/MTGA/BepInEx/extras/Raw_CardDatabase.mtga
```

也可用 `--deck-id` 精确指定。此命令只准备资料，`status=preparing`，并不代表已经完成对局。生成随机后缀目录以避免覆盖既有对局。

- `session.json`：牌组、模式标签、来源、实际数据库版本与回合编号定义。
- `deck.txt`：英文主牌/备牌导出；指挥官区与伙伴另存于元数据，不能无声丢弃。
- `card_records.jsonl`：牌组中各 ID 的本地完整规则与数量。
- `related_cards.jsonl`：关联面与明确链接的衍生物。遇到未支持的关联编码或版式，查看原始字段，不猜测。
- `actions.jsonl`：初始为空，供局内记录事件。

数据库通过 SQLite `mode=ro` 打开。官方英文库从 `MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga` 中选择最近版本。显式提供的中文库使用本地 Mod 已验证的 `Localizations_jaJP` 映射；其他安装若非此映射，省略参数并用英文。不要默认将日文表视为中文。

打包的 `card_model.py` 来自本工作区已有的无损规则解析器；`export_cards.py` 仅保留只读文本解析所需函数，无图像依赖。客户端更新后若 schema 不匹配，应报告字段缺失并回退到可见卡面/用户牌表，不能无声使用过时的固定哈希路径。

解析器来源与适配记录在 [catalog-provenance.json](catalog-provenance.json)。

## 核对与生成数据

```powershell
python -X utf8 <skill>/scripts/mtga_data.py validate --session E:/MTGA/matches/<session>
python -X utf8 <skill>/scripts/mtga_data.py curate --session <session> --output-root <new-directory>
```

`validate` 只读校验 session、JSONL、事件引用、训练消息一致性及声明为完整的神器计数。以字段明确标注的动态攻防公式作校验，不会对所有名为 Construct 的衍生物套用同一规则。需要保存校验结果时加 `--report <path>`。

`curate` 从已复核的 `decision_records.jsonl` 生成 `training.jsonl` 与逐行对应的 `training_metadata.jsonl`，默认不覆盖。它不会自行判断战略正确性。新样本必须有 `training_inclusion=true`、`evidence.effect_status=confirmed`、`quality.label=reviewed_self_play_candidate`、`expert_gold=false` 及事件引用。复验旧版首局记录时，可显式加 `--accept-reviewed-legacy`；它仅接受已有相同复核标签且有确认结果的旧格式。
