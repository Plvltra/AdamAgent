"""确定性图片字节样本；仅编码/解码元数据，不看图、不截图或请求模型。"""

import io


def image_bytes(image_format="PNG", *, size=(3, 2), orientation=None, animated=False):
    """用真实编码器生成小图；方向和动画分支只用于本地协议验收。"""
    from PIL import Image

    with io.BytesIO() as stream, Image.new("RGB", size, (20, 40, 80)) as image:
        options = {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options["exif"] = exif
        if animated:
            with Image.new("RGB", size, (80, 20, 40)) as second:
                image.save(
                    stream, format=image_format, save_all=True, append_images=[second], duration=100
                )
        else:
            image.save(stream, format=image_format, **options)
        return stream.getvalue()
