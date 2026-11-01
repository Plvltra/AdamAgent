"""先完整解码再发布原字节；会话授权与来源落盘通过注入的媒体端口完成。"""

from collections.abc import Callable

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource
from adam.vision.models import ImageDecoder, LoadedImage


class ImageLoader:
    """只消费图片解码器与发布端口；不认识文件路径、SessionStore 或模型配置。"""

    def __init__(
        self,
        decoder: ImageDecoder,
        publish: Callable[[MediaSource, bytes, str], MediaRef],
    ) -> None:
        self._decoder, self._publish = decoder, publish

    def load(self, source: MediaSource, data: bytes, *, cancel: CancellationToken) -> LoadedImage:
        """解码失败或发布前取消不保存媒体；发布失败向上传播，不重试或返回伪引用。

        保留原字节和 EXIF，不旋转、不重编码。发布端口须先保存正文再提交来源；
        发布期间取消可能已留下证据，调用方不能据此自动重复加载。
        """
        cancel.check()
        image = self._decoder.decode(data, cancel=cancel)
        cancel.check()
        return LoadedImage(self._publish(source, data, image.mime_type), image)
