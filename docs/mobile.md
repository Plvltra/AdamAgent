# Android 模拟器观察

## C10.1 固定测试应用与观察

支持已准备的Android11/API30本地模拟器，固定测试包com.adam.practice。AndroidObserver必须显式配置adb路径、emulator序列号及隔离服务端口（本次5041/emulator-5556）；不枚举选取真实手机，不提供模型可调用的任意shell。动作与普通工具接入留C10.2，导航另用robotics包。

[测试应用源码](../tests/support/android_app/MainActivity.java)有Record name、Record note、Save record和结果文本。未申请网络/设备权限；记录写入应用私有files/record.json，可用受控adb run-as独立读回。调试包仅用于模拟器，debuggable是验收读回所需。窗口旋转由Android重建Activity并恢复有ID的输入，已保存结果从原文件读取。

[离线构建脚本](../scripts/build_android_practice.py)显式指定--sdk、--java（JDK bin）和--output，复用已有API30/build-tools30.0.2，经过aapt2、javac、D8、zipalign和apksigner，不修改SDK、不安装设备、不下载依赖。调试签名仅存在输出目录，固定android密码不是用户凭据。实际APK路径.cache/c101-app/adam-practice.apk，SHA256为BA44EC23F087B63475594FC6A27AC60D72F6F7A16D26488F31859B87E935935F，v1/v2/v3签名校验通过。参考[Android命令行构建](https://developer.android.com/build/building-cmdline)、[AAPT2](https://developer.android.com/tools/aapt2)。

AndroidObserver租约以模拟器serial命名，默认同用户临时目录，跨实例/进程竞争立即失败，异常退出释放。observe持租约，先取设备状态/UI树，截图后再次核对状态/树，一致才沿原SessionMedia发布引用。截图PNG限制16MiB、8192像素边长；层级2MiB/512节点；每个adb客户端15秒期限，取消仅杀本次客户端，不停止服务或其他设备。

设备版本含adb transport_id、boot_id、前台窗口和主屏旋转；完整观察版本还包含原XML和PNG字节。require_current只认当前宿主已保存观察，动作须在同租约下调用；比较设备版本与UI树，任何断连/重连身份变化、前后台或旋转/页面变化都会使旧观察失效。状态栏时钟像素不作为动作前页面对照，截图引用仍保留原字节。未观察到且恢复相同身份的瞬时断连不等于已检测，当前接口不宣称持续在线监控。

实机已发现API30的dumpsys window windows子命令不含mCurrentFocus，改用完整dumpsys window；input输出的首个SurfaceOrientation可能属于未激活设备，因此方向只读主屏displayId0的DisplayInfo。旋转过渡期缺失有效状态时拒绝，不自动重试；调用方须稳定后显式重新观察。

7项相关业务测试通过（实机查询修正后2.17秒），包依赖边界1项通过（此前合计8项2.24秒）；Ruff和定向Pyright通过。ADB是明确替身，实际SessionStore/SessionMedia发布、读回、租约竞争和失效策略执行；真实截图另由独立视觉验收。注释审查保留原来源/失败说明，新增模块说明与真实解析依据已核对。本小项不运行全量。

### C10.1 模拟器实机结果

独立视觉子代理完成测试APK安装和观察：正式API竖屏rotation0/720×1280、横屏rotation1/1280×720，旋转后旧版本返回device or page changed; observe again；重新观察成功。原SessionStore实际保存两条media_saved，引用与完整字节SHA见[正式API报告](evaluations/c101/api-report.json)，完整过程14.421秒。子代理分别看图确认标题、两输入字段、保存按钮、初始结果五组件均完整无遮挡；横屏字段和按钮扩展。主代理未查看图片。

首次正式API因windows子命令缺前台字段失败，已按原始证据修正。第二次横屏切换后报identity or rotation unavailable，失败瞬间未记录原始boot/window输出，根因未证实；稳定后同一正则/state成功。第三次验收显式等待旋转稳定5秒再重新观察成功，不在生产API内加入自动重试，也不将先前失败删除。

截图/层级和原始dumpsys证据保留.cache/c101-visual/api-portrait.png、api-landscape.png、对应XML及window*.txt。恢复竖屏后仅清理本次模拟器/ADB；[清理报告](evaluations/c101/cleanup.json)确认无自有进程或5041/5556/5557监听残留。保留测试AVD与APK。C10.1完成；填写保存、真实断连动作与普通工具批准留C10.2，前后台/取消综合对照留C10.4。
## C10.2 原工具授权动作

build_application显式注入mobile_observer和confirm_mobile才注册mobile_observe/mobile_action，主任务专用，子任务不继承。原执行器继续承担模式、规则、Hook、预算、取消和工具来源；观察为SESSION_STATE，动作是EXTERNAL_ACTION。没有逐次批准回调时拒绝动作，plan模式不派发。批准文本包含固定serial、一次性version、operation和实际输入参数，页面文字不能改变授权。

支持click（唯一content-desc且控件启用、属于测试包，点取bounds中心）、input（已有焦点的测试包EditText，追加1～200字符ASCII）、swipe（截图内两点，固定300毫秒）和back（固定KEYCODE_BACK）。input仅允许英文/数字/空格/下划线/点/连字符，空格转换成adb的%s；不支持中文或shell元字符，也不声称通用输入法能力。参数在批准前校验，不提供任意ADB/shell接口。

设备租约覆盖整个观察或批准/复检/派发/收尾，同实例嵌套观察可重入，其他实例/线程互斥。动作在批准前及批准后复核当前设备/页面，动作引用先消费；每次观察追加随机一次性标识，即使页面未变也拒绝旧版本重复动作。拒批、取消、设备变化或故障均丢弃旧引用。派发进入ADB后失败或动作后观察失败抛MobileActionUnknownError，原轮次停止，不自动重做；dispatched只表示派发并观察完成，业务成功仍须独立查询。

10项动作业务场景与7项原观察场景已验证；首次17项运行13通过/4失败（10.99秒），已修正拒批/plan预期及已知设备拒绝转ToolError，相关5项复验通过（4.69秒），不重复其他已通过项。全架构检查发现新依赖未登记：tool_calls→mobile和此前VoiceLoop→audio；同步允许这两条有明确职责的协调边后，相关3项通过（0.23秒），其余31项此前通过。audio/mobile仍只依赖自身与contracts。Ruff/定向Pyright通过；注释审查保留原来源/模式/中断说明，补充bootstrap移动端契约。未跑全量。
### C10.2 实机填写与独立查询

独立视觉子代理在原应用/工具管线运行固定MockTransport策略：观察→点名称→输入→返回收键盘→点备注→输入→返回→保存。7次逐次批准、7次dispatched，保存只有一次；随机name=C102-88ba46d015、note=Note-8fbb9defd1。独立adb run-as读取files/record.json，两字段完全一致，savedAt=1793642169020。[完整工具结果与读回](evaluations/c102/report.json)保留真实来源/版本；这证明固定脚本策略的实机动作，不是模型选工具能力验收。

重复原版本保存返回Stale mobile observation，批准数仍7，未重复保存。另建验收会话先取得fresh观察，再真实停止自己的模拟器；devices列表确认serial消失，用fresh版本点击保存返回emulator disconnected or unauthorized，0批准/0派发，见[断连证据](evaluations/c102/disconnect.json)。不是用已失效版本或仅停止ADB服务冒充断连。

独立看最终正式API截图确认两字段及Saved JSON一致，SHA256为77da7d380d097266a81730b18fa74a40b3a4824c75a1668fe2ad2b8e05d18035，证据.cache/c102-visual/final.png。初始辅助脚本导入浏览器测试helper因本地缺playwright而退出，未派发动作；改为直接组装原应用，未安装依赖或改生产逻辑。滑动自动边界已验，综合前后台/旋转/取消及模型策略对照留C10.4。
## C10.4 综合边界与策略对照

独立视觉验收通过原应用/工具管线完成：前台观察后Home进入后台，原动作拒绝且0批准/0派发，回到前台重新观察成功；旋转后旧版本拒绝且0批准/0派发，稳定横屏重新观察成功；批准回调取消共享令牌为1批准/0派发、抛RequestInterrupted，后续轮次用取消前引用仍被stale拒绝。各项原工具来源/日志在.cache/c104-android，见[完整结果](evaluations/c104/android-results.json)。

固定脚本复用C10.2的1/1成功；真实Qwen3-4B-Q4_K_M单次自选策略为0/1：先discover mobile并observe，第三次HTTP直接输出将继续的计划，没有mobile_action，0批准/0派发/0保存。独立文件查询返回不存在，独立最终看图为两字段空、No saved record。3请求，5728输入/242输出token，99.5秒；未人为追加“继续”或重跑到成功。模型使用原on_demand、act与批准接口，文本模型读取UI层级，截图为来源证据而非直接视觉输入。

两策略各一个同应用同任务样本，随机文本不同，只报告本次成功比例，不外推泛化。模型未进入动作阶段，不能声称已验证它的多步批准/保存能力；该能力的程序边界由确定性实机用例验证。详见[独立文字报告](evaluations/c104/android-report.md)，截图SHA与来源、HTTP正文、实际日志均保留缓存。恢复竖屏并清理自有模拟器/ADB/模型服务，[清理证据](evaluations/c104/android-cleanup.json)保留。

C10收尾相关63项通过（14.08秒），Ruff/定向Pyright通过，注释审查与架构核对完成。C10.4验收已完成，失败质量结论保留；不把模拟器或内存导航结果宣传为真实机器人安全。