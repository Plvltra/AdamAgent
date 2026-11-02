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