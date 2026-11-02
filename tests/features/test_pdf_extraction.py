"""真实 PDF 的页级文本、版本、无 OCR 提示与失败边界；不以文本测试声称看懂版面。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import get_configuration

from adam.contracts.errors import RequestInterrupted
from adam.documents.models import DocumentError
from adam.documents.pdf import PdfLimits, extract_pdf
from tests.features.support.pdf_data import make_pdf


def test_real_pdf_physical_pages_are_separate_and_version_covers_original_bytes():
    data = make_pdf([{"text": "Cover"}, {"text": "Printed page 1"}, {"text": "Printed page 2"}])
    first = extract_pdf(data, start_page=2, page_count=1)
    assert first.total_pages == 3 and first.next_page == 3
    assert first.pages[0].page == 2 and first.pages[0].text == "Printed page 1"
    assert first.version == hashlib.sha256(data).hexdigest()
    assert not first.layout_verified and not first.pages[0].truncated
    assert json.loads(json.dumps(first.to_dict()))["pages"][0]["page"] == 2
    last = extract_pdf(data, start_page=first.next_page)
    assert last.next_page is None and last.pages[0].text == "Printed page 2"
    assert last.version == first.version
    changed = make_pdf([{"text": "Changed cover"}, {"text": "Printed page 1"}])
    assert extract_pdf(changed, start_page=2).version != first.version


@pytest.mark.parametrize(
    "spec,status,images,ocr",
    [
        ({}, "no_extractable_text", False, False),
        ({"image": True}, "image_only", True, True),
        ({"form": True}, "image_only", True, True),
        ({"inline": True}, "image_only", True, True),
        ({"text": "Caption", "image": True}, "text", True, False),
    ],
    ids=["blank", "image", "nested-form", "inline", "mixed"],
)
def test_image_only_and_mixed_pages_report_capability_without_decoding_pixels(
    spec, status, images, ocr
):
    result = extract_pdf(make_pdf([spec]))
    page = result.pages[0]
    assert (page.status, page.has_images, page.needs_ocr) == (status, images, ocr)
    assert not result.layout_verified


def test_page_and_total_character_limits_are_visible_and_next_page_is_not_lost():
    data = make_pdf([{"text": "ABCDEFGHIJ"}, {"text": "KLMNOPQRST"}, {"text": "UVWXYZ"}])
    result = extract_pdf(data, limits=PdfLimits(max_page_chars=6, max_total_chars=9))
    assert [page.text for page in result.pages] == ["ABCDEF", "KLM"]
    assert [page.extracted_chars for page in result.pages] == [10, 10]
    assert all(page.truncated for page in result.pages) and result.next_page == 3
    assert extract_pdf(data, start_page=2).pages[0].text == "KLMNOPQRST"


@pytest.mark.parametrize("password", ["", "private-pass"], ids=["empty-password", "password"])
def test_encrypted_pdf_is_refused_even_if_library_can_open_without_password(password):
    with pytest.raises(DocumentError, match="加密"):
        extract_pdf(make_pdf([{"text": "PRIVATE_PDF_TEXT"}], password=password))


def test_bad_input_empty_document_invalid_ranges_and_byte_limit_fail_explicitly():
    for data in (b"not-pdf", b"%PDF-1.7\nPRIVATE_BAD_BODY", make_pdf([])):
        with pytest.raises(DocumentError) as failure:
            extract_pdf(data)
        assert "PRIVATE_BAD_BODY" not in str(failure.value)
    data = make_pdf([{"text": "body"}])
    for args in (
        {"start_page": True},
        {"start_page": 2},
        {"page_count": 0},
        {"page_count": 21},
        {"limits": PdfLimits(max_file_bytes=len(data) - 1)},
    ):
        with pytest.raises(DocumentError):
            extract_pdf(data, **args)
    with pytest.raises(DocumentError, match="页数|复杂度"):
        extract_pdf(make_pdf([{}, {}]), limits=PdfLimits(max_pages=1))


@pytest.mark.parametrize("compressed", [False, True], ids=["raw", "flate"])
def test_oversized_decoded_stream_fails_whole_document_even_after_good_page(compressed):
    data = make_pdf([{"text": "first good page"}, {"text": "A" * 2000}], compressed=compressed)
    with pytest.raises(DocumentError, match="超限"):
        extract_pdf(data, limits=PdfLimits(max_stream_bytes=512))


def test_cancellation_propagates_without_becoming_parse_error_or_partial_success():
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RequestInterrupted()

    with pytest.raises(RequestInterrupted):
        extract_pdf(make_pdf([{"text": "first"}, {"text": "second"}]), check_cancel=cancel)
    assert calls == 5


def test_configuration_is_local_and_parallel_readers_do_not_share_stream_budget():
    original = get_configuration()
    data = make_pdf([{"text": "A" * 2000}], compressed=True)

    def read(limit):
        try:
            return len(extract_pdf(data, limits=PdfLimits(max_stream_bytes=limit)).pages[0].text)
        except DocumentError:
            return "limited"
        finally:
            assert get_configuration() == original

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, [512, 8192])) == ["limited", 2000]
    assert get_configuration() == original


def test_javascript_and_embedded_attachments_are_not_opened_or_executed(monkeypatch):
    data = make_pdf([{"text": "only visible text"}], active_content=True)

    def forbidden(*args, **kwargs):
        pytest.fail("PDF extraction attempted an external side effect")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    result = extract_pdf(data)
    assert result.pages[0].text == "only visible text"
    assert "DO_NOT" not in json.dumps(result.to_dict())
