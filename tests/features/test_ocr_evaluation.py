"""评分保留漏检、多检和空格错误，定位成功不能代替转写或问题正确性。"""

from scripts.ocr_visual_evaluation import score_ocr


def test_localization_matches_geometry_instead_of_recognized_text():
    expected = [{"text": "A B", "box": [0, 0, 10, 10]}, {"text": "C", "box": [20, 0, 30, 10]}]
    actual = [
        {"text": "wrong", "polygon": [[20, 0], [30, 0], [30, 10], [20, 10]]},
        {"text": "AB", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
    ]
    result = score_ocr(expected, actual)
    assert result["located_lines"] == 2
    assert result["matches"][0]["edit_distance"] == 1
    assert result["matches"][1]["actual"] == "wrong"
    assert result["character_errors"] == 6 and result["cer"] == 1.5


def test_missing_and_extra_lines_are_not_silently_dropped():
    result = score_ocr(
        [{"text": "ABC", "box": [0, 0, 10, 10]}],
        [{"text": "XY", "polygon": [[20, 0], [30, 0], [30, 10], [20, 10]]}],
    )
    assert result["character_errors"] == 5
    assert result["located_lines"] == 0
    assert result["matches"][0]["actual"] == ""
    assert len(result["extra_lines"]) == 1
