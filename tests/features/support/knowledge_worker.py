"""B12.3 独立进程：真实知识文件与锁，HTTP 仅由 MockTransport 替代。"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

from adam.config.settings import DocumentConfig, PermissionConfig
from adam.contracts.errors import AgentError
from adam.documents.index import DocumentIndex
from adam.documents.knowledge_access import KnowledgeAccess
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.knowledge_review import KnowledgeModelReviewer
from adam.documents.snapshots import load_snapshot
from adam.llm.client import LLMClient
from adam.tool_calls.policy import PathPolicy
from tests.features.test_context_prefix_evaluation import model_config, response_for
from tests.features.test_knowledge_review import opinion_for


def lifecycle_at(root):
    """重新构建服务而不重建测试来源，让父子进程面对相同持久范围。"""
    index = DocumentIndex(
        root / "cache",
        root,
        DocumentConfig(roots=(Path("docs"),)),
        PathPolicy(root, PermissionConfig()).resolve,
    )
    return KnowledgeLifecycle(KnowledgeProposals(root / "knowledge", index))


def pause(job):
    """通知父进程已到真实边界；竞态显式释放，强杀场景不会运行异常清理。"""
    Path(job["marker"]).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not Path(job["release"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test parent did not release or terminate worker")
        time.sleep(0.01)


def install_boundary(job, lifecycle):
    """仅暂停实际快照的原子替换；不伪造进行中、响应或发布记录。"""
    phase = job.get("phase", "")
    if phase in {"", "confirm", "transport"}:
        return
    original = os.replace

    def replace_at_boundary(src, dst):
        path = Path(dst)
        payload = load_snapshot(Path(src)) if path.suffix == ".json" else None
        matches = False
        if path == lifecycle.path and payload:
            status = payload["events"][-1]["status"]
            matches = phase in {status + "_before", status + "_after"}
        elif path.parent.name == "reviews" and payload:
            matches = phase == payload["state"] + "_after"
        if matches and phase.endswith("_before"):
            pause(job)
        original(src, dst)
        if matches and phase.endswith("_after"):
            pause(job)

    os.replace = replace_at_boundary


def run(job):
    """只在显式 review 分支发送模拟 HTTP；重启查询不能自动续发或采用遗留意见。"""
    root = Path(job["root"])
    lifecycle = lifecycle_at(root)
    install_boundary(job, lifecycle)
    identity = job["candidate_id"]

    def confirm(view):
        Path(job["shown"]).write_text(json.dumps(view), encoding="utf-8")
        if job.get("phase") == "confirm":
            pause(job)
        return True

    def handle(request):
        # 记录真正进入传输适配器的请求；重启不允许新增这一行。
        with (root / "http-calls.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"model": json.loads(request.content)["model"]}) + "\n")
            output.flush()
            os.fsync(output.fileno())
        if job.get("phase") == "transport":
            pause(job)
        return response_for(request, content=json.dumps(opinion_for(request)))

    action = job["action"]
    if action == "review":
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            reviewer = KnowledgeModelReviewer(
                LLMClient(model_config(), http), "review", lifecycle.proposals.root / "reviews"
            )
            result = lifecycle.review(identity, reviewer)
    elif action == "publish":
        result = lifecycle.publish(identity, confirm)
    elif action == "reject":
        result = lifecycle.reject(identity, "独立进程拒绝")
    elif action == "withdraw":
        result = lifecycle.withdraw(identity, "独立进程撤回")
    elif action == "current":
        result = KnowledgeAccess(lifecycle).query()
    elif action == "inspect":
        result = {
            "view": lifecycle.inspect(identity),
            "history": lifecycle.history(identity),
            "current": KnowledgeAccess(lifecycle).query(),
            "requests": [
                load_snapshot(path)
                for path in sorted((lifecycle.proposals.root / "reviews").glob("*.json"))
            ],
        }
    else:
        raise AssertionError("unknown test action")
    return result


def main():
    """业务错误通过 JSON 返回，进程故障由非零退出码明确报告。"""
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        result = {"ok": True, "result": run(job)}
    except AgentError as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
