"""B31单开关消融和只读报告；按计划配对，不把缺失或失败结果从分母移除。"""

import argparse
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from adam.config.settings import AppConfig
from scripts.task_eval_assets import CASES


def ablation_configs(base: AppConfig, mechanism: str) -> dict[str, AppConfig]:
    """生成只差一个enabled开关的两臂，保留其他有效配置。"""
    if mechanism not in {"memory", "planning", "reflection"}:
        raise ValueError("mechanism must be memory/planning/reflection")
    config = getattr(base, mechanism)
    return {
        name: replace(base, **{mechanism: replace(config, enabled=enabled)})
        for name, enabled in (("disabled", False), ("enabled", True))
    }


def compare(root: Path, baseline: str = "disabled") -> dict[str, Any]:
    """只读逐次产物并按case/iteration配对；缺失记missing，不依赖旧汇总成功数字。"""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    configs = manifest["configs"]
    if manifest.get("format") != "adam_task_evaluation_v1" or baseline not in configs:
        raise ValueError("invalid evaluation manifest or baseline")
    rows, identities = [], set()
    for planned in manifest["runs"]:
        identity = planned["run_id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity) or identity in identities:
            raise ValueError("invalid or repeated run identity")
        identities.add(identity)
        path = root / (identity + ".json")
        if path.exists():
            row = json.loads(path.read_text(encoding="utf-8"))
            if (
                any(row.get(k) != v for k, v in planned.items())
                or type(row.get("passed")) is not bool
            ):
                raise ValueError("run record does not match manifest")
            if row.get("status") not in {"passed", "failed", "interrupted", "not_run"}:
                raise ValueError("invalid run status")
            if row["passed"] != (row["status"] == "passed"):
                raise ValueError("contradictory run result")
        else:
            row = {
                **planned,
                "status": "missing",
                "passed": False,
                "known_tokens": 0,
                "unknown_usage": True,
            }
        rows.append(row)
    groups = {name: [r for r in rows if r["config"] == name] for name in configs}
    reference = {(r["case"], r["iteration"]): r for r in groups[baseline]}
    comparisons = []
    for name, selected in groups.items():
        if name == baseline:
            continue
        left, right = _flatten(configs[baseline]), _flatten(configs[name])
        changed = [
            key for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)
        ]
        paired = {
            "improved": [],
            "regressed": [],
            "both_passed": [],
            "both_failed": [],
            "unavailable": [],
        }
        treatment = {(r["case"], r["iteration"]): r for r in selected}
        for key in sorted(reference.keys() | treatment.keys()):
            before, after = reference.get(key), treatment.get(key)
            label = f"{key[0]}#{key[1]}"
            if (
                before is None
                or after is None
                or any(
                    r["status"] in {"missing", "not_run", "interrupted"} for r in (before, after)
                )
            ):
                paired["unavailable"].append(label)
            else:
                outcome = (before["passed"], after["passed"])
                name_for_pair = {
                    (False, True): "improved",
                    (True, False): "regressed",
                    (True, True): "both_passed",
                    (False, False): "both_failed",
                }[outcome]
                paired[name_for_pair].append(label)
        single_toggle = len(changed) == 1 and changed[0] in {
            "memory.enabled",
            "planning.enabled",
            "reflection.enabled",
        }
        comparisons.append(
            {
                "baseline": baseline,
                "config": name,
                "changed_fields": changed,
                "single_mechanism_toggle": single_toggle,
                "provider_identity_recorded": "provider_fingerprint" in configs[baseline]
                and "provider_fingerprint" in configs[name],
                "pairs": paired,
            }
        )
    return {
        "mode": manifest["mode"],
        "planned": len(rows),
        "quality": "真实固定任务结果；不自动推出因果或统计显著性"
        if manifest["mode"] == "live_model"
        else "离线工程证据，不能推断模型质量",
        "configs": {name: _stats(selected) for name, selected in groups.items()},
        "comparisons": comparisons,
    }


def markdown(report: dict[str, Any]) -> str:
    """生成能看到退步和缺失的简短报告；正常反例单独列出。"""
    lines = [
        "# 固定任务对照",
        "",
        report["quality"],
        "",
        "| 配置 | 通过/计划 | 未运行或缺失 | 已知token | 用量未知 | 正常反例通过/计划 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, stats in report["configs"].items():
        lines.append(
            f"| {name} | {stats['passed']}/{stats['planned']} | {stats['unavailable']} | {stats['known_tokens']} | {stats['unknown_usage']} | {stats['control_passed']}/{stats['control_planned']} |"
        )
    for comparison in report["comparisons"]:
        lines += [
            "",
            f"## {comparison['baseline']} → {comparison['config']}",
            "",
            "配置差异：" + ", ".join(comparison["changed_fields"]),
            f"单一机制开关：{comparison['single_mechanism_toggle']}；服务端身份留证：{comparison['provider_identity_recorded']}。",
        ]
        for kind, labels in comparison["pairs"].items():
            lines.append(f"- {kind}: {', '.join(labels) or '无'}")
    return "\n".join(lines) + "\n"


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    controls = [r for r in rows if r["case"] in {"counterexample", "no-tools"}]
    return {
        "planned": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "unavailable": sum(r["status"] in {"missing", "not_run", "interrupted"} for r in rows),
        "known_tokens": sum(r["known_tokens"] for r in rows),
        "unknown_usage": any(r["unknown_usage"] for r in rows),
        "control_passed": sum(r["passed"] for r in controls),
        "control_planned": len(controls),
        "cases": {
            case.case_id: {
                "planned": sum(r["case"] == case.case_id for r in rows),
                "passed": sum(r["case"] == case.case_id and r["passed"] for r in rows),
            }
            for case in CASES
        },
    }


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result = {}
    for key, item in value.items():
        name = prefix + key
        if isinstance(item, dict):
            result.update(_flatten(item, name + "."))
        else:
            result[name] = item
    return result


def main() -> None:
    """只读报告默认输出到stdout，不发请求、不修改原始试验。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--baseline", default="disabled")
    args = parser.parse_args()
    print(markdown(compare(args.root, args.baseline)))


if __name__ == "__main__":
    main()
