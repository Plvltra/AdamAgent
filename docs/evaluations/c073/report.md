# C07.3 真实模型原工具图片验收

- 日期：2026-11-03（Asia/Shanghai）；仅单次业务轮次，无重开、无挑选结果、无MockTransport、无人工替代工具调用。
- 文本模型：本地Qwen3-4B-Q4_K_M，llama-server b12032，CPU/8线程，16384上下文，temperature=0，seed=42，reasoning=off。
- 原应用full工具发现，最多4模型请求/4工具调用/1000输出token；实际3请求、2工具调用：generate_media → generation_result → final。
- 生成后端：真实SD-Turbo离线CUDA，512×512，1步，seed42。提示原样采用指定红色陶瓷茶壶提示。生成一次，查询一次，均经过原工具执行器。
- 作业teapot73成功；真实SessionMedia来源绑定session/request/tool_call。生成预算实际输入22 token、图片1张、调用1次；预留全部归零；未配置价格，最终明确费用unknown。
- 最终模型原样交付media_id=a2ca25d9594ec682fad93d2afae79ae1，version=c0958036f49221b48cf04cde6822edfaefc6bd7b1003b2c94370c666ba3098f6。
- 从真实SessionMedia按原media_id/version读回366633字节PNG；SHA-256与version一致。
- 业务轮次116.329秒，总运行130.837秒。

## 独立视觉发现

子代理直接用view_image查看SessionMedia读回的readback.png。画面主体为红色、有釉面高光的陶瓷茶壶，置于浅白色带纹理的台面，灰褐色背景，光照柔和且具摄影风格，没有可见文字。茶壶左侧为壶嘴、上方壶盖、右侧为把手；壶身基本可辨识。

提示符合度为部分符合，不能称完全通过：前景额外生成红色茶杯与杯碟，超出单一茶壶主体的严格构图预期；右侧把手出现类似双环/多余连接的形体歧义；台面有纹理，不是完全纯白无纹理。红色陶瓷茶壶、浅色桌面、柔光摄影、无文字等核心要素成立。本轮没有重新生成改善质量。

## 证据

- report.json：原最终回复、耗时、原MediaRef读回、SHA与进程回收结果。
- requests.json / responses.json：全部真实HTTP请求及回复，不含认证头。
- events.json、sessions/：真实SessionStore事件与原持久化记录。
- generation-snapshot.json：生成成功快照、真实来源、消费、固定种子。
- budget.json：消费及归零的预留。
- readback.png：依据原MediaRef从SessionMedia读回的图片。
- server-process.json、server.stdout.log、server.stderr.log：独立服务参数与运行日志。
- run_real.py：本次单轮验收脚本。

## 清理及范围

自建llama-server PID36932已终止并wait回收，复查Get-Process无该PID。返回码1来自主动terminate清理；业务已正常完成。未操作其他服务，未修改生产代码、测试或文档，未提交，未运行pytest。脚本docstring按已读Python注释规范核对；仅新增本缓存脚本，无删除既有说明。

## 文本模型真实token用量

3次请求的输入/输出token依次为5383/84、5877/30、6315/101；累计输入17575、输出215、合计17790，缓存token均为0。这是文本模型用量，SD提示编码的22 token另计，不能混同。
