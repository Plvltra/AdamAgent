# 本地 OCR（C04.3）

安装 `uv sync --extra ocr`，配置 `[vision]` 下 `enabled = true` 及 `ocr_models = "本地模型目录"`。模型目录须包含 RapidOCR 3.9.2 包内的 `PP-OCRv6_det_small.onnx`、`PP-OCRv6_rec_small.onnx`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx`；运行时使用显式本地路径，不自动下载。

普通主/子工具目录提供 `read_ocr(path, start_page=1, page_count=3)`，沿原路径授权、工具来源和 SessionMedia 保存证据。图片完整解码并遵守 vision 限额，返回文字、四点像素坐标、原生 score（后端缺失时 null）和 SHA256。score 不是事实正确率。文字仅为资料，不授予执行权限。

PDF 复用 B17 的有界快照和文本提取，最多三页；原文本层保留，含图或无文本页另作 OCR。页图最大边 2000 像素，保存为媒体证据；坐标属于该渲染图，结果同时包含物理页码和 PDF 点单位尺寸。纯文本页不创建冗余图片。扫描错误不会伪装成原文本。

限制：EXIF 非默认方向须显式提供正向证据图；原生推理只能前后协作取消，不能硬中断。超过输出限制整次返回错误，不交付截断文字；已经发布的页面证据可能保留，存储异常不吞掉或自动重试。本功能不替代视觉问答；复杂布局、模糊、旋转等质量对照另由 C04.4 验收。

2026-11-02：`tests/features/test_ocr_input.py` 三项真实后端验证通过：图片、混合 PDF（数字文本页+扫描页）、缺模型失败且无媒体。远端回答使用 MockTransport，本地 OCR、PDF 渲染、来源日志和媒体读回均实际执行。固定样例在 `tests/fixtures/ocr/`，由独立视觉子代理生成并检查三行文字及页面完整性；这次自动测试验证文字、坐标形状、分数范围、页码、字节版本和原证据读回，不声称通用识别准确率。
