"""生成真实 PDF 字节供提取契约测试；仅合成字体/图像流，不调用外部程序。"""

from io import BytesIO

from pypdf import PdfWriter
from pypdf.actions import JavaScript
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)


def make_pdf(pages, *, password=None, compressed=False, active_content=False):
    """每项为 text/image/inline/form 字典；图像为单像素测试数据，不用于视觉效果验收。"""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for spec in pages:
        page = writer.add_blank_page(width=400, height=400)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = b""
        if "text" in spec:
            text = (
                spec["text"]
                .encode("ascii")
                .replace(b"\\", b"\\\\")
                .replace(b"(", b"\\(")
                .replace(b")", b"\\)")
            )
            content += b"BT /F1 12 Tf 20 300 Td (" + text + b") Tj ET\n"
        if spec.get("image") or spec.get("form"):
            image = DecodedStreamObject()
            image.set_data(b"\x00\x00\x00")
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im1"): writer._add_object(image)}
            )
            content += b"q 10 0 0 10 0 0 cm /Im1 Do Q\n"
            if spec.get("form"):
                form = DecodedStreamObject()
                form.set_data(content)
                form.update(
                    {
                        NameObject("/Type"): NameObject("/XObject"),
                        NameObject("/Subtype"): NameObject("/Form"),
                        NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 20, 20)]),
                        NameObject("/Resources"): resources,
                    }
                )
                resources = DictionaryObject(
                    {
                        NameObject("/XObject"): DictionaryObject(
                            {NameObject("/Fm1"): writer._add_object(form)}
                        )
                    }
                )
                content = b"q /Fm1 Do Q\n"
        if spec.get("inline"):
            content += b"q BI /W 1 /H 1 /CS /RGB /BPC 8 ID \x00\x00\x00 EI Q\n"
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(
            stream.flate_encode() if compressed else stream
        )
    if active_content:
        writer.add_open_action(
            JavaScript("app.launchURL('https://invalid.example/DO_NOT_FETCH', true);")
        )
        writer.add_attachment("payload.txt", b"DO_NOT_OPEN")
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
