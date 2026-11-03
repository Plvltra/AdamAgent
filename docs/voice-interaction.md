# 语音交互

## C05.1 按键录音与可确认转写

工程入口已实现；真实麦克风成功采集仍待设备恢复，路线勾选保持未完成。`python -m scripts.voice_input` 显式指定 `--python`（独立ASR环境）、`--model`、`--records`，可指定 `--device` 与 `--language zh|en`。Enter开始、Enter结束，显示原始转写后可编辑确认或输入/cancel放弃。Ctrl+C释放本次设备/推理进程，确认前后本入口都不创建Conversation轮次；确认后的对话提交使用下述C05.2独立入口。

Microphone按设备原生采样率采集单声道16位PCM，避免硬设16kHz而被真实WASAPI拒绝。支持8～96kHz，默认30秒、配置上限60秒；时长按实际帧数换算整数毫秒。回调只复制有界字节，墙钟看守超时停止并关闭设备，不把截短录音当成功。少于100ms、RMS小于40、溢出或缺设备明确失败；RMS只是静音门槛，不是可靠VAD，也可能拒绝很轻的语音。

SpeechInput先保存带capture_id的原WAV和SHA来源快照，再调用一次ASR，成功得到pending_confirmation草稿。device和created_at记录采集设备说明与草稿创建时刻，created_at不冒充采集起点。用户修改只写confirmed_text，原transcript保持；重复确认同一文字幂等，换文字或从discarded确认拒绝。输入来源暂存在独立草稿目录，C05.2将其绑定原会话真实轮次，不伪造输入轮次以存放未确认文字。

WhisperASR运行固定本地worker，使用CPU/int8、单束和显式语言；无网络下载、云密钥或自动重试。取消/超时回收自己的子进程。C00预留音频毫秒、一次适配器尝试和输出上限，记录实际模型原生输出token（包含Whisper特殊/时间戳token，不当作聊天文本token）；音频输入不虚构文本输入token，单价缺失保持未知。已知结果后取消保留实际消费，响应丢失保留未知输出并停止额度准入。草稿记录失败不自动转写第二次；本阶段不自动恢复旧草稿。

主环境仅需audio可选依赖；独立ASR环境使用[scripts/support/audio-requirements.txt](../scripts/support/audio-requirements.txt)。实际预检发现faster-whisper 1.2.1调用的metadata_errors参数已不被PyAV19接受，因此固定av16.0.1。运行[准备脚本](../scripts/setup_speech_model.py)下载固定revision公开模型，`--destination`必须显式指定；推理只使用本地文件。参考[官方模型](https://huggingface.co/Systran/faster-whisper-small)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[PortAudio流接口](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)。

## 已有证据与待验收

- 相关11项通过（采样率修改后0.69秒）；架构边界1项通过（0.04秒），Ruff、定向Pyright通过。成功路径保存真实WAV/JSON、编辑确认、原消费；失败覆盖空转写、放弃、静音、设备缺失、超长、取消、保存失败，以及真实等待子进程的取消/超时回收。设备和转写fixture边界均已标明。
- 正式SpeechInput→WhisperASR→用户确认路径处理明确的Windows Huihui合成样本，5.847秒音频原句逐字匹配，26输出token、1次尝试、费用未知。[原草稿与报告](evaluations/c051/asr-report.json)保留来源哈希。验收辅助脚本在确认成功后调用了不存在的quota.state，报告由原已保存草稿恢复，没有再跑一次ASR；该次准确延迟未保留，记未知。
- Realtek真实设备打开失败：MME原始16kHz及原生44.1kHz均报主机错误，WASAPI原生48kHz报无效设备。[设备报告](evaluations/c051/device-report.json)与前两次失败均保留，没有保存环境声音，未宣称麦克风已成功采集或释放后重开成功。已向用户确认麦克风连接/Windows录音状态。
- 5轮真实语音对话、TTS播放、打断和延迟仍待C05.3～4，合成样本不替代麦克风实机验收。

新增函数契约、失败处理和SDK回调说明已审查；没有删除旧关键说明。架构已同步。本小项只跑相关验证，不重跑正在主工作树进行的三大项批次全量。

## C05.2 确认文字接入原对话

`python -m scripts.voice_chat --config <配置> --drafts <草稿目录> --capture-id <已确认ID>` 将确认文字交给原 AgentApplication.send / Conversation；恢复原会话须加 `--session <会话ID>`。入口沿用原配置、默认模式与命令/编辑批准，不因文字声称“语音命令”而授予权限。原WAV仅保存为输入来源，不发给聊天模型。

load_confirmed核对确认状态、WAV格式、大小和SHA；VoiceConversation先在受保护的_media/voice_submissions排他占用capture_id，再沿原on_turn_started回调绑定真实轮次并发布SessionMedia。记录失败阻止模型请求；原回调在finally恢复。相同ID再次提交只查询原状态；会话不符或文字/录音变化拒绝。失败、取消或状态未知不自动补发，即使尚未产生模型请求也保持占用。成功历史可以修复最后一次回执保存失败，无法证实成功时保持unknown。再次运行脚本须显式恢复所属会话。

6项业务测试通过（4.71秒）：编辑文字进入原请求、一次轮次/媒体读回、重启去重、未确认与录音变更拒绝、模型失败不重发、收尾写失败恢复、原命令批准拒绝及跨会话隔离。仅模拟远端模型HTTP，本地应用、媒体和会话存储实际执行；这不代替C05.4真实麦克风五轮验收。Ruff与定向Pyright通过；新增契约和原有说明已审查，未删除旧关键解释。按小项局部验证提交，未新增全量运行。
## C05.3 合成与播放

`python -m scripts.voice_chat ... --speak` 在原成功回答保存后朗读；Ctrl+C停止本次合成/播放。WindowsSpeech使用已安装的Microsoft Huihui Desktop，System.Speech输出16kHz单声道16位WAV，独立SoundPlayer进程播放。文字经UTF-8文件传给固定PowerShell脚本，不插入命令；取消和超时终止并等待自己的工作进程。默认不朗读，不下载模型或调用云服务。实现依据[System.Speech文档](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer)；正常退出Dispose，强制取消由进程退出释放设备。

SpeechOutput先排他占用session/turn，再合成、保存WAV及SHA、播放。独立记录位于受保护的_media/speech_output，记录playback_id、model_state和朗读state；重复同轮次只返回原记录，不重播或重新请求模型。旧进程留下synthesizing/playing只说明最后已知阶段，不证明仍在运行，也不自动恢复播报。朗读失败保持原成功模型历史。音频不伪造为用户input或工具MediaRef；这是宿主朗读附件，实际成功轮次由调用宿主绑定。

文字上限1500字，音频上限60秒/16MiB，工作进程默认超时90秒。显式注入原UnitBudget预留60000音频毫秒和1次合成，按实际PCM帧数结算；失败无法取得完整输出时音频用量未知，费用无单价保持未知。playback_started_seconds是开始调用播放后端前的耗时，不冒充扬声器首音延迟。暂不分句流式播放；超长回答明确朗读失败，原文字回答不受影响。

5项相关业务测试通过（2.51秒）：WAV及重启去重、合成/设备失败不重发原模型、取消释放和初始保存失败阻止合成；模型HTTP和设备边界显式模拟，本地原应用/成功历史/回执实际执行。音频包依赖边界1项通过（0.05秒），Ruff与定向Pyright通过。注释审查保留旧契约，新增失败/取消/进程说明已核对。

[实机报告](evaluations/c053/report.json)：明确测试短句“这是语音回复测试。”，实际合成2580毫秒、调用1次，正常播放API完成；定时取消返回RequestInterrupted，触发至返回约19毫秒，再次播放API成功。报告中的model_state由测试宿主提供，不冒充真实LLM响应；原模型成功与播放失败隔离由上述业务测试验证。无人耳听感/扬声器首音验收，不把进程返回等同于听感。真实麦克风五轮和安全点打断仍留C05.4。
## C05.4 打断控制器（工程进度，未完成实机验收）

SpeechPlayback.begin绑定session/turn和随机playback_id，先取消并等待旧线程；start只允许当前身份一次，身份传入SpeechOutput原回执。工作线程仅执行合成/播放，不访问模型或会话JSONL；失败保存在error和原朗读状态。旧轮次迟到回复、重复启动、旧身份打断均拒绝。

interrupt先使身份失效，再取消并等待后端释放设备，最后调用宿主提供的投递回调。原活轮次可传app.submit_input，仍由B27请求/完整工具批次安全点消费；停止超时不调用回调，队列拒绝不自动改走app.send或重播。空闲轮次须由宿主显式选择下一轮入口。begin与interrupt串行，宿主必须在原轮次开始回调更换身份；控制器本身不猜测当前模型轮次或读取草稿。

新增4项测试，与C05.3共9项通过（2.82秒），Ruff/定向Pyright通过。覆盖停止后原B27接受并消费输入、迟到回复和跨身份拒绝、重复启动、设备停止超时不投递、队列拒绝不重播；实际应用/输入日志执行，模型与播放边界显式模拟。保存记录中的playback_id与控制器身份一致。注释审查保留先占用、失败隔离和不补播约束，speak说明更新可选身份。

此提交只有可注入宿主控制器及安全点验证，尚未接入完整按键录音宿主；C05.4复选框保持未完成。真实麦克风5轮（含一次打断）、转写准确度、扬声器首音延迟与取消耗时仍需实机证据。C05.3测试短句及模拟输入不能代替这项验收。
### C05.4 连续按键宿主

已新增 `python -m scripts.voice_loop --config <配置> --python <ASR环境python> --model <离线Whisper目录> --records <草稿目录> [--device <编号>] [--session <ID>]`。Enter开始/结束录音，显示转写后Enter确认或编辑，/cancel放弃，/quit退出。默认plan模式，不把语音确认当作工具批准；需要交互批准的操作不由这个输入线程自动批准。

VoiceLoop由主线程拥有原应用、SessionStore、模型请求和输入媒体发布；唯一输入线程只负责录音/ASR/草稿确认，以及调用原线程安全app.submit_input。开始录音前持锁标记录音中并关闭旧播报；模型在此期间返回时不启动声音。录音确认时仍有活轮次则提交B27；轮次已结束则进入容量1的下一轮队列。队列满、正在封口的B27拒绝或保存失败都保留草稿并明确失败，不自动改路重发。原on_turn_started回调先建立B27队列，再发布固定活轮次身份。

补充语音在受保护的_media/voice_supplements中排他记录capture_id、session/turn、确认正文、原录音SHA和接受回执；完整WAV仍在原草稿目录。该记录不是SessionMedia输入引用，也不把accepted当成已消费；实际消费看原_inputs事件in_context。主线程的首段语音仍沿VoiceConversation保存真实输入媒体。退出取消自己的ASR/模型令牌并关闭录音/播报；阻塞stdin线程是唯一daemon输入泵，退出后不再投递。

2项新增宿主测试通过（2.96秒）：实际原应用与持久化中确认模型运行时补充、停止旧播报后新轮次，断言模型只在主线程执行、B27实际in_context、补充录音仍可回查、正确1或2轮成功历史。仅替换模型/录音/ASR/播放边界，未宣称真实麦克风通过；Ruff/定向Pyright及命令--help通过。新增说明和原回调契约已审查，架构已同步，未跑全量。

完整按键录音宿主已接入；C05.4剩余真实麦克风五轮（含一次打断）、转写准确度、首音与取消耗时。此前“尚未接入宿主”为上一提交的阶段记录，当前以本节为准；复选框仍保持未完成。

2026-11-03用户确认：当前没有可用麦克风，先完成其他部分。C05.1真实录音成功路径、C05.4真实五轮及首音/打断验收继续待办；不重复探测设备，也不将已有合成样本或模拟业务测试标为实机通过。
