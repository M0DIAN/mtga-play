# 本机配置（2026-10-12 证据）

- MTGA 根目录：`E:/MTGA`。
- 持续维护源：`E:/MTGA/skills/mtga-play`。
- 安装目录：`C:/Users/Administrator/.codex/skills/mtga-play`（CODEX_HOME 若变化，以当前环境为准）。
- 后续对局建议目录：`E:/MTGA/matches`；首局旧目录为 `E:/MTGA/sparky_20261012_042617`。
- 本地规则解析器来源：`E:/MTGA/card_export/card_model.py`；本 skill 打包了只读所需部分，可独立运行。
- 中文 Mod 数据库：`E:/MTGA/BepInEx/extras/Raw_CardDatabase.mtga`，中文在 Localizations_jaJP，已从实际卡牌核对。
- 已用 Python：`C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。这是当前可用路径，若消失重新查运行时，不要求用户安装重复环境。
- 用户明确要求基于真实对局自主优化迭代该 skill。迭代可使用已完成记录和隔离测试；未来对局仍按当时请求的模式与局数进行。
- 实时日志路径：`C:/Users/Administrator/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log`。第二局接收座位为1，只是该局证据；后续由GRE接收座位核对。`track_state.py`默认每200ms轮询增量，日志能力与限制见[实时日志状态](log-state.md)。

首局 UI 环境：@oai/sky，1920×1080，窗口截图含约31像素标题栏；成功输入的本机映射为 x不变、y减31。缩放比例与截图 origin 不能仅凭此记录推定。窗口、DPI、尺寸或工具后端改变后，用低影响的可见导航/选择反馈重新校准。历史窗口 ID 已过期，不保留作选择依据。

第二局为1920×1080全屏、零偏移。用户在本局明确允许切换本地0.5秒连续拖动；SendInput 后端成功完成调度和占卜。许可范围属于会话上下文，未来调用仍核对当时的授权。`timed_drag.ps1` 自行验证路径、前台和客户区，默认只返回轨迹；原生 HWND 与 sky window.id 不相通。源码导入 EPERM 时使用 `repl_bootstrap.py` 输出完整 JS 到 REPL 直接运行，详见界面操作。

第五局 `E:/MTGA/matches/match_20261012_133743_30ef2c9a` 使用v0.3.2 bootstrap完成Sparky，T7胜13/0。seq15与100是实际请求guard拦截证据，后者为主动零输入探针。Scry319/gs141平原顶、幽湖底，T5抓平原；SelectNReq267/gs198七张查看信息和四张合法选择完整，验证此前unfiltered_cards修复。seq23手牌重排误施放、seq54悬浮图无效双击支持v0.3.3指引；发现本地施放选择仍需画面。19项核对与21条候选样本已保存，完整对比含整体速度变慢。新指引尚待后续实测。
