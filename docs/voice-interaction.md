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