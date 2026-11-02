# C10.2 隔离 Android 实际动作验收

- 模型仅为固定 MockTransport 策略；真实生产 build_application、工具执行器、来源/模式/批准/版本/媒体存储路径，真实模拟器 ADB 输入。没有真实模型效果结论。
- 主会话 446430e7：observe → click name → input → back → click note → input → back → click save。批准 7 次、dispatched 7 次，保存 1 次，截图 8 份。
- name=C102-88ba46d015；note=Note-8fbb9defd1。独立 `adb -P 5041 -s emulator-5556 shell run-as com.adam.practice cat files/record.json` 返回相同字段与 savedAt=1793642169020。
- 独立看图：final.png 为正式 mobile_action/action-7 来源的原媒体正文（媒体 2c4747d017db181e1ae0dc179a5ce9ad），竖屏 720×1280。两字段及 Saved JSON 可见，与文件读回完全一致。SHA256=77da7d380d097266a81730b18fa74a40b3a4824c75a1668fe2ad2b8e05d18035。
- 保存后使用最初旧 version 重放 save：ok=false，Stale mobile observation; observe again。没有第 8 次批准、没有第 8 次派发，随即结束。
- 断连会话 05c61ec9：fresh mobile_observe 后 emu kill；devices 确认空列表；使用 fresh version 请求 click save，结果 emulator disconnected or unauthorized，批准 0、派发 0。证据 disconnect.json、disconnect-events.json。
- 过程中初版脚本导入 browser 测试 helper 因缺 Playwright 在动作前退出；改为本脚本直接组装，不安装依赖。首次把封装媒体作为 PNG 查看失败，随后依据真实存储封装无损提取正文且 SHA256 与引用一致；无新增设备动作。
- 保持竖屏，关闭本次 emulator 与独立 adb；cleanup.json 记录无剩余 adb/emulator/qemu 进程及目标监听端口。AVD 保留。
- 原始证据：report.json、events.json、request-01 至 request-10.json、sessions/2026/11/03/446430e7.jsonl 及 sessions/_media/446430e7/。
- 未运行 pytest，未修改生产代码；swipe 由自动测试覆盖，本次实际动作仅 click/input/back。
