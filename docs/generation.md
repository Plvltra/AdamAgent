# 媒体生成作业

## C07.1 作业契约与单次派发

GenerationSpec固定单个image或audio产物的提示、标量参数和可选32位种子。参数键唯一、数量与文本有界，适配器负责具体参数、凭据和种子支持校验。GenerationBackend.prepare只能本地检查并返回原ConsumptionPlan，不得调用生成；generate只尝试一次，返回完整字节、MIME、实际Quantities和实际种子回执。

GenerationJobs是显式宿主API，不是已注册的聊天工具。宿主注入后端、原SessionMedia.publish、共享UnitBudget以及原子替换作业快照的save回调；调用前由宿主提供已授权的真实MediaSource。生成包只依赖自身和contracts，不直接导入SessionStore、应用服务或供应商SDK。

submit同步执行，另一线程可通过get查看快照、cancel请求停止；默认不创建后台队列，最多保留64个身份。每个身份在预留额度且保存开始快照后才派发；同ID无论成功、失败、取消或未知都拒绝再提交。get只读本地快照，不偷偷查询供应商或重新生成。产物默认上限32MiB，引用必须与原来源、实际字节摘要、大小及MIME一致；此层检查不代替后端解码与质量验收。

| 事实 | 状态与消费 |
| --- | --- |
| 开始快照已保存，仍在执行 | running；实际用量未确认，在途量由原UnitBudget预留 |
| 有效产物发布且最终快照保存 | succeeded；保存原媒体引用与实际单位 |
| 派发前保存失败 | failed、零消费，不调用后端 |
| 已知产物在发布前被取消 | cancelled、不交付；已发生用量照常结算 |
| 派发后超时、中断或无可靠用量 | unknown；已知尝试一次，其余用量未知，停止后续额度准入 |
| 发布或最终状态保存失败 | unknown、不重做；保留已知用量及已拿到的引用 |
| 宿主重建后恢复遗留running | unknown；连是否真正派发也未知，不补一次或零次调用 |

每次终态以同一成员键刷新C00累计快照，不重复相加；预留在退出时释放，未知消费标记仍保留。单价由prepare冻结，调用方可用原quote计算估价；缺少价格保持未知。测试中的USD 0.12是明确的fixture费率计算，不是供应商真实账单。额度限制准入，不能保证供应商不会超量计费。

cancel只设置本作业使用的取消令牌并保存请求标记，不能证明远端已经停止、产物被撤销或费用退回。回调必须遵循取消；不承诺硬中断。已经成功交付的终态不因后来cancel而改变。异常继续抛出，调用方可get原ID了解事实，不能为重试而自动换ID重新派发。

save由宿主实现原子替换；错误保留内存快照和原异常说明。restore只接受宿主已经验证的GenerationJob快照，恢复时重建其额度，不派发生成。目前没有文件解码器、跨宿主任务锁、供应商异步轮询或自动恢复调度；生产持久化与原工具调用在C07.3接入。

9项相关场景与1项生成包架构边界通过（2.13秒）：真实临时媒体/JSON存储、固定种子、单次结算和重复拒绝、预算/不支持种子、响应丢失与恢复、运行中取消、已知消费后取消，以及开始记录/媒体发布后/最终记录三类保存故障。后端只返回明确的可解码WAV fixture，不证明真实图片或音频生成能力。Ruff与定向Pyright通过；新增契约、权限职责、未知消费及失败处理说明已核对，没有删除原关键注释。真实双模态后端留C07.2，普通模型工具闭环留C07.3；本项未运行全量测试。

## C07.2 本地图片与音乐后端

SDTurboBackend与MusicGenBackend是两个独立适配器，分别使用公开的SD-Turbo与MusicGen-small。LocalRuntime只运行固定adam.generation.worker，不把提示拼入shell；提示与有限参数通过临时JSON传递。每次生成独占一个隐藏工作进程，退出后回收GPU进程资源。取消或超时杀死本次worker并等待退出，不自动重启；C07.1仍保留未知事实，不把停止当作远端退款。

生成阶段显式启用Hub/Transformers离线模式，from_pretrained仅使用本地文件、safetensors和已安装库，不加载远端自定义代码。模型准备单独通过 [下载脚本](../scripts/setup_generation_models.py) 固定官方仓库revision完成；模型与Python缺失在prepare明确报错，没有云API凭据依赖，也不自动回退到付费服务。本实现需要CUDA，不在失败后隐式改用CPU。

图片只开放width/height（64～512、8的倍数）及steps（1～4），固定CFG=0；完整PNG解码与尺寸一致后才交付。音乐只开放duration_ms（1000～5000、20ms的倍数）；真实MusicGen波形裁掉多余尾部码步，不填充缺失音频，最终检查32kHz、单声道、16位PCM及准确帧数。零信号、非有限采样或不足长度会失败；这些检查不证明音乐符合提示。

两个适配器都支持固定种子，跨设备逐字节一致不作保证。提示超过各自文本编码器限制会报错，不静默截断。C00预留图片77个/音频256个提示token，结算实际编码器输入长度；output_tokens为0，因为这里没有生成文本，音频码不混作文本token。图片数/真实PCM毫秒和一次物理尝试单独计量，未配置价格时费用未知。

运行时与主验证环境分开：主环境安装generation可选依赖，仅用于PNG解码；独立Python环境按官方cu126索引安装torch==2.8.0，再安装 [固定推理依赖](../scripts/support/generation-requirements.txt)。执行setup_generation_models.py时显式指定--destination，得到sd-turbo、musicgen-small目录及manifest.json；将该Python与相应模型目录注入LocalRuntime。实际验收环境使用.cache/generation-venv与.cache/local-generation。

参考：[SD-Turbo官方模型与用法](https://huggingface.co/stabilityai/sd-turbo)、[MusicGen官方模型](https://huggingface.co/facebook/musicgen-small)、[Transformers MusicGen接口](https://huggingface.co/docs/transformers/model_doc/musicgen)、[官方PyTorch历史版本安装](https://pytorch.org/get-started/previous-versions/)。模型许可随官方卡片保存；MusicGen权重标为CC-BY-NC-4.0，本批为本地学习验收。

真实验收（2026-11-02）：两个正式后端各生成一次，无重试。512×512图片耗时16.93秒，原PNG独立核图为红色陶瓷茶壶、浅白桌面、柔光且无文字，但额外出现杯子与杯托，提示仅部分符合。3秒音乐耗时18.75秒，实际解码96000帧、32kHz单声道16位PCM，峰值5431、RMS 884.43；尚未人工听感验收，不据波形推断钢琴、铃声或无人声。

两项均经过真实SessionStore来源、SessionMedia发布及原MediaRef读回，原子保存作业快照；实际输入token分别22/19，1张图片/3000ms音频，各1调用，费用未知。预留已释放、宿主退出0且无残留worker。完整参数、固定模型版本、哈希、原引用与质量限制见 [真实报告](evaluations/c072/report.json)，推理环境见 [依赖快照](evaluations/c072/requirements-freeze.txt)。原PNG/WAV保留在.cache/c072-generation，未将二进制加入仓库。

相关14项测试通过（2.49秒），覆盖原作业故障与本地缺配置/参数边界、真实子进程超时和取消回收；Ruff、定向Pyright通过。新增函数契约、离线边界及失败处理注释已核对，无原关键说明删除。架构已同步，本小项不重复全量；C07.3仍待接入普通工具闭环。
