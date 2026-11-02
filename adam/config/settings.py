"""经过校验的配置值；不读取文件、不创建客户端或执行工具。"""

import math
from dataclasses import dataclass, field
from pathlib import Path

from adam.config.hooks import HooksConfig
from adam.config.mcp import MCPConfig
from adam.contracts.errors import ConfigError
from adam.contracts.models import valid_session_id


@dataclass(frozen=True)
class LLMConfig:
    """模型连接配置；密钥不出现在对象的调试表示中。"""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120
    stream: bool = True
    protocol: str = "chat_completions"
    semantic_idle_seconds: float = 120
    stream_total_seconds: float = 600

    def __post_init__(self) -> None:
        """协议在启动时确定；Messages首版要求显式关闭流式。"""
        if self.protocol not in ("chat_completions", "messages"):
            raise ConfigError("llm.protocol 必须是 chat_completions 或 messages。")
        if self.protocol == "messages" and self.stream:
            raise ConfigError("Messages首版需要 llm.stream=false。")
        for name in ("semantic_idle_seconds", "stream_total_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigError(f"llm.{name} 必须为有限正秒数。")


@dataclass(frozen=True)
class AgentConfig:
    """每轮请求、工具次数与会话累计消费限制。"""

    max_model_requests: int = 8
    max_tool_calls: int = 16
    max_session_tokens: int = 50000
    max_output_tokens: int = 4096
    max_edit_attempts: int = 3


@dataclass(frozen=True)
class NoProgressConfig:
    """连续相同工具失败的提醒与停止阈值；独立于请求和工具总次数预算。"""

    enabled: bool = True
    warn_after: int = 2
    stop_after: int = 3

    def __post_init__(self) -> None:
        """提醒后至少留一次实际尝试；不允许布尔值、无界计数或先停止后提醒。"""
        if type(self.enabled) is not bool:
            raise ConfigError("no_progress.enabled 必须是布尔值。")
        if (
            type(self.warn_after) is not int
            or type(self.stop_after) is not int
            or not 2 <= self.warn_after < self.stop_after <= 64
        ):
            raise ConfigError(
                "no_progress 阈值必须是整数且满足 2 <= warn_after < stop_after <= 64。"
            )


@dataclass(frozen=True)
class SubagentConfig:
    """单个主会话的子任务上限；子 Agent 不允许再委派，消费沿用 agent 总额度。"""

    max_concurrent: int = 2
    max_created_per_turn: int = 4

    def __post_init__(self) -> None:
        """直接构造与 TOML 使用相同校验，防止无界线程或无限派发。"""
        for name, value in (
            ("max_concurrent", self.max_concurrent),
            ("max_created_per_turn", self.max_created_per_turn),
        ):
            if type(value) is not int or not 1 <= value <= 32:
                raise ConfigError(f"subagents.{name} 必须是 1 到 32 的整数。")


@dataclass(frozen=True)
class PermissionConfig:
    """工作目录之外的读取与写入授权分别配置，互不隐含授权。"""

    additional_read_dirs: tuple[Path, ...] = ()
    additional_write_dirs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ToolLimits:
    """单次读取/结果限制；扫描和 PDF 为协作超时，Git 和通用命令为子进程超时。"""

    max_read_lines: int = 200
    max_result_chars: int = 8000
    max_scan_entries: int = 5000
    max_text_file_bytes: int = 1048576
    max_scan_bytes: int = 33554432
    max_regex_file_bytes: int = 262144
    regex_timeout_seconds: int = 1
    max_pdf_file_bytes: int = 8388608
    max_pdf_read_pages: int = 20
    max_pdf_stream_bytes: int = 2097152
    max_git_path_list_bytes: int = 1048576
    scan_timeout_seconds: int = 5
    git_timeout_seconds: int = 5
    command_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        """PDF上限与解析器一致，查询字节保持有界；直接构造和TOML均不能关闭边界。"""
        for name, ceiling in (
            ("max_scan_bytes", 100000000),
            ("max_regex_file_bytes", 262144),
            ("regex_timeout_seconds", 30),
            ("max_pdf_file_bytes", 33554432),
            ("max_pdf_read_pages", 100),
            ("max_pdf_stream_bytes", 8388608),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ConfigError(f"tool_limits.{name} 必须为1～{ceiling}的整数。")


@dataclass(frozen=True)
class ArtifactConfig:
    """长工具结果的文本保存与读取配额；关闭时保留原有截断行为。"""

    enabled: bool = True
    max_bytes: int = 1048576
    max_session_bytes: int = 16777216
    max_items: int = 128
    max_read_chars: int = 4000

    def __post_init__(self) -> None:
        """直接构造和 TOML 共用校验，单位与 ArtifactStore 接口保持一致。"""
        if type(self.enabled) is not bool:
            raise ConfigError("artifacts.enabled 必须是布尔值。")
        for name, maximum in (
            ("max_bytes", 100000000),
            ("max_session_bytes", 1073741824),
            ("max_items", 4096),
            ("max_read_chars", 1000000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ConfigError(f"artifacts.{name} 必须是 1～{maximum} 的整数。")


@dataclass(frozen=True)
class ContextConfig:
    """单次输入的应用预算；不是模型窗口容量，输出额度由 AgentConfig 单独预留。"""

    max_input_tokens: int = 32768
    safety_margin_tokens: int = 2048
    retain_recent_turns: int = 2
    summary_max_output_tokens: int = 2048
    prune_tool_results: bool = True
    compression_trigger_ratio: float = 0.8
    auto_compact: bool = True
    compression_target_ratio: float = 0.6
    tool_prune_threshold_chars: int = 4000
    tool_prune_head_chars: int = 1500
    tool_prune_tail_chars: int = 500

    def __post_init__(self) -> None:
        """拒绝非法计数和耗尽输入预算的余量；直接构造与 TOML 加载规则一致。"""
        if type(self.max_input_tokens) is not int or not 0 < self.max_input_tokens <= 100000000:
            raise ConfigError("context.max_input_tokens 必须是合理范围内的正整数。")
        if (
            type(self.safety_margin_tokens) is not int
            or not 0 <= self.safety_margin_tokens < self.max_input_tokens
        ):
            raise ConfigError("context.safety_margin_tokens 必须是小于输入上限的非负整数。")
        if type(self.retain_recent_turns) is not int or not 0 <= self.retain_recent_turns <= 1000:
            raise ConfigError("context.retain_recent_turns 必须是 0 到 1000 的整数。")
        if (
            type(self.summary_max_output_tokens) is not int
            or not 0 < self.summary_max_output_tokens <= 100000
        ):
            raise ConfigError("context.summary_max_output_tokens 必须是合理范围内的正整数。")
        if type(self.auto_compact) is not bool:
            raise ConfigError("context.auto_compact 必须是布尔值。")
        if (
            type(self.compression_trigger_ratio) not in {int, float}
            or not 0 < self.compression_trigger_ratio < 1
        ):
            raise ConfigError("context.compression_trigger_ratio 必须在 0 和 1 之间。")
        if (
            type(self.compression_target_ratio) not in {int, float}
            or not 0 < self.compression_target_ratio < self.compression_trigger_ratio
        ):
            raise ConfigError("context.compression_target_ratio 必须大于 0 且低于触发比例。")
        if type(self.prune_tool_results) is not bool:
            raise ConfigError("context.prune_tool_results 必须是布尔值。")
        if (
            any(
                type(value) is not int or value < 0
                for value in (
                    self.tool_prune_threshold_chars,
                    self.tool_prune_head_chars,
                    self.tool_prune_tail_chars,
                )
            )
            or self.tool_prune_head_chars + self.tool_prune_tail_chars + 256
            >= self.tool_prune_threshold_chars
        ):
            raise ConfigError("context 工具缩减长度必须是非负整数，头尾及标记要小于阈值。")


@dataclass(frozen=True)
class MemoryConfig:
    """记忆选择的独立输入上限；仍须服从本轮总输入预算。"""

    enabled: bool = True
    max_entries: int = 8
    max_input_tokens: int = 1024
    method: str = "keyword"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    semantic_min_score: float = 0.5
    max_semantic_records: int = 256

    def __post_init__(self) -> None:
        """直接构造和 TOML 配置使用同一校验，不接受布尔值充当计数。"""
        if type(self.enabled) is not bool:
            raise ConfigError("memory.enabled 必须是布尔值。")
        if type(self.max_entries) is not int or not 1 <= self.max_entries <= 128:
            raise ConfigError("memory.max_entries 必须在 1～128 之间。")
        if type(self.max_input_tokens) is not int or not 0 <= self.max_input_tokens <= 100000:
            raise ConfigError("memory.max_input_tokens 必须在 0～100000 之间。")
        if not isinstance(self.method, str) or self.method not in {"keyword", "semantic"}:
            raise ConfigError("memory.method 必须是 keyword 或 semantic。")
        if (
            not isinstance(self.embedding_model, str)
            or not self.embedding_model.strip()
            or len(self.embedding_model) > 200
        ):
            raise ConfigError("memory.embedding_model 必须是有效模型名称。")
        if (
            type(self.semantic_min_score) not in {int, float}
            or not math.isfinite(self.semantic_min_score)
            or not 0 <= self.semantic_min_score <= 1
        ):
            raise ConfigError("memory.semantic_min_score 必须是 0～1 的有限数。")
        if type(self.max_semantic_records) is not int or not 1 <= self.max_semantic_records <= 1024:
            raise ConfigError("memory.max_semantic_records 必须在 1～1024 之间。")


@dataclass(frozen=True)
class PlanningConfig:
    """主 Agent 计划限制；单次状态更新仍占现有工具与模型请求额度。"""

    enabled: bool = True
    max_steps: int = 8
    max_replans: int = 3

    def __post_init__(self) -> None:
        """直接构造和 TOML 一致校验，拒绝布尔值冒充计数。"""
        if type(self.enabled) is not bool:
            raise ConfigError("planning.enabled 必须是布尔值。")
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 64:
            raise ConfigError("planning.max_steps 必须在 1～64 之间。")
        if type(self.max_replans) is not int or not 0 <= self.max_replans <= 100:
            raise ConfigError("planning.max_replans 必须在 0～100 之间。")


@dataclass(frozen=True)
class ReflectionConfig:
    """反思是有界附加请求，仍计入原轮次与共享额度。"""

    enabled: bool = True
    max_per_turn: int = 1
    max_input_tokens: int = 4096
    max_output_tokens: int = 1024

    def __post_init__(self) -> None:
        """直接构造与 TOML 共用约束，不接受布尔值作数量。"""
        if type(self.enabled) is not bool:
            raise ConfigError("reflection.enabled 必须是布尔值。")
        for name, value, maximum in (
            ("max_per_turn", self.max_per_turn, 4),
            ("max_input_tokens", self.max_input_tokens, 32768),
            ("max_output_tokens", self.max_output_tokens, 4096),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ConfigError(f"reflection.{name} 必须在 1～{maximum} 之间。")


@dataclass(frozen=True)
class DocumentConfig:
    """指定文档范围和检索资源上限；空 roots 表示不启用文档工具。"""

    roots: tuple[Path, ...] = ()
    chunk_chars: int = 400
    max_files: int = 200
    max_file_bytes: int = 1048576
    max_total_bytes: int = 10485760
    max_pdf_pages: int = 512
    max_pdf_text_chars: int = 1000000
    max_pdf_stream_bytes: int = 2097152
    max_scan_entries: int = 5000
    max_chunks: int = 10000
    scan_timeout_seconds: int = 10
    top_k: int = 5
    method: str = "keyword"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    semantic_min_score: float = 0.5
    hybrid_candidates: int = 20
    rrf_constant: int = 60
    rerank: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3-int8"
    rerank_candidates: int = 20
    rerank_query_chars: int = 256
    rerank_passage_chars: int = 512
    contextualize: bool = False
    context_prefix_chars: int = 300
    context_input_chars: int = 1200

    def __post_init__(self) -> None:
        """限制整个语料的资源消耗；目录配置本身不授予额外读取权限。"""
        if (
            not isinstance(self.roots, tuple)
            or len(self.roots) > 32
            or any(not isinstance(p, Path) or "\0" in str(p) for p in self.roots)
        ):
            raise ConfigError("documents.roots 必须是不超过 32 项、不含空字符的路径元组。")
        if not isinstance(self.method, str) or self.method not in {"keyword", "semantic", "hybrid"}:
            raise ConfigError("documents.method 必须是 keyword、semantic 或 hybrid。")
        if type(self.rerank) is not bool or (self.rerank and self.method != "hybrid"):
            raise ConfigError("documents.rerank 必须是布尔值，且只能在 hybrid 方法启用。")
        if type(self.contextualize) is not bool:
            raise ConfigError("documents.contextualize 必须是布尔值。")
        if (
            not isinstance(self.reranker_model, str)
            or not self.reranker_model.strip()
            or len(self.reranker_model) > 200
        ):
            raise ConfigError("documents.reranker_model 必须是有效模型名称。")
        if (
            not isinstance(self.embedding_model, str)
            or not self.embedding_model.strip()
            or len(self.embedding_model) > 200
        ):
            raise ConfigError("documents.embedding_model 必须是有效模型名称。")
        if (
            type(self.semantic_min_score) not in {int, float}
            or not math.isfinite(self.semantic_min_score)
            or not 0 <= self.semantic_min_score <= 1
        ):
            raise ConfigError("documents.semantic_min_score 必须是 0～1 的有限数。")
        ranges = {
            "chunk_chars": (32, 4000),
            "max_files": (1, 10000),
            "max_file_bytes": (1, 33554432),
            "max_total_bytes": (1, 104857600),
            "max_pdf_pages": (1, 2048),
            "max_pdf_text_chars": (1, 1000000),
            "max_pdf_stream_bytes": (1, 8388608),
            "max_scan_entries": (1, 100000),
            "max_chunks": (1, 50000),
            "scan_timeout_seconds": (1, 300),
            "top_k": (1, 20),
            "hybrid_candidates": (1, 20),
            "rrf_constant": (1, 1000),
            "rerank_candidates": (1, 20),
            "rerank_query_chars": (1, 1000),
            "rerank_passage_chars": (1, 4000),
            "context_prefix_chars": (1, 1000),
            "context_input_chars": (32, 8000),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"documents.{name} 必须是 {low}～{high} 的整数。")


@dataclass(frozen=True)
class ReviewConfig:
    """独立审查的轮次上限；仍共用 V6 的并发、子创建数与 token 限制。"""

    enabled: bool = True
    max_rounds: int = 2

    def __post_init__(self) -> None:
        """只允许有限次审查，避免执行者与审查者无限往返。"""
        if type(self.enabled) is not bool:
            raise ConfigError("review.enabled 必须是布尔值。")
        if type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 4:
            raise ConfigError("review.max_rounds 必须在 1～4 之间。")


@dataclass(frozen=True)
class SkillsConfig:
    """任务指导开关与用户目录；None 供程序化组装仅使用项目技能，CLI 加载时补默认路径。"""

    enabled: bool = True
    user_dir: Path | None = None

    def __post_init__(self) -> None:
        """已解析目录必须为绝对路径；授权仅限 Skill 材料，不能扩张普通工具。"""
        if type(self.enabled) is not bool:
            raise ConfigError("skills.enabled 必须是布尔值。")
        if self.user_dir is not None and (
            not isinstance(self.user_dir, Path)
            or "\0" in str(self.user_dir)
            or not self.user_dir.is_absolute()
        ):
            raise ConfigError("skills.user_dir 必须是已解析的绝对路径。")


@dataclass(frozen=True)
class RulesConfig:
    """项目规则开关；范围和读取上限由规则模块统一管理。"""

    enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("rules.enabled 必须是布尔值。")


@dataclass(frozen=True)
class ToolDiscoveryConfig:
    """说明加载与目录排序分开选择；full 对照和语义候选均不改变执行权限。"""

    mode: str = "on_demand"
    ranking: str = "keyword"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    max_index_tools: int = 256
    semantic_min_score: float = 0.5

    def __post_init__(self) -> None:
        if self.mode not in ("on_demand", "full"):
            raise ConfigError("tool_discovery.mode 必须为 on_demand 或 full。")
        if self.ranking not in ("keyword", "semantic"):
            raise ConfigError("tool_discovery.ranking 必须为 keyword 或 semantic。")
        if (
            not isinstance(self.embedding_model, str)
            or not 1 <= len(self.embedding_model) <= 200
            or self.embedding_model != self.embedding_model.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in self.embedding_model)
        ):
            raise ConfigError("tool_discovery.embedding_model 必须是1～200字符的本地模型名称。")
        if type(self.max_index_tools) is not int or not 1 <= self.max_index_tools <= 1024:
            raise ConfigError("tool_discovery.max_index_tools 必须为1～1024的整数。")
        if (
            type(self.semantic_min_score) not in {int, float}
            or not 0 <= self.semantic_min_score <= 1
        ):
            raise ConfigError("tool_discovery.semantic_min_score 必须介于0与1之间。")


@dataclass(frozen=True)
class SessionSearchConfig:
    """主 Agent 旧对话检索；默认仅当前会话，额外 ID 是用户的显式读取授权。"""

    enabled: bool = True
    allowed_session_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """最多额外授权 31 个会话，为当前会话保留一个名额；不接受路径或重复 ID。"""
        if type(self.enabled) is not bool:
            raise ConfigError("session_search.enabled 必须是布尔值。")
        values = self.allowed_session_ids
        if (
            not isinstance(values, tuple)
            or len(values) > 31
            or any(not isinstance(item, str) or not valid_session_id(item) for item in values)
            or len(set(values)) != len(values)
        ):
            raise ConfigError(
                "session_search.allowed_session_ids 必须是不超过 31 项的不同短会话 ID。"
            )


@dataclass(frozen=True)
class OrchestrationConfig:
    """显式启用固定 WSL 隔离编排；不提供宿主回退或自行放宽挂载与内核策略。"""

    enabled: bool = False
    distribution: str = "Ubuntu"

    def __post_init__(self) -> None:
        """只接收发行版名称，不接收可执行文件、启动命令或环境变量。"""
        _validate_wsl_selection(self.enabled, self.distribution, "orchestration")


@dataclass(frozen=True)
class CommandIsolationConfig:
    """独立启用只读原生命令工具；不改变宿主命令，也不接收任意挂载和启动程序。"""

    enabled: bool = False
    distribution: str = "Ubuntu"

    def __post_init__(self) -> None:
        _validate_wsl_selection(self.enabled, self.distribution, "command_isolation")


def _validate_wsl_selection(enabled: bool, distribution: str, section: str) -> None:
    """两种隔离策略只共用发行版名称校验，不共享是否启用或执行权限。"""
    if type(enabled) is not bool:
        raise ConfigError(f"{section}.enabled 必须是布尔值。")
    if (
        not isinstance(distribution, str)
        or not 1 <= len(distribution) <= 64
        or not distribution[0].isascii()
        or not distribution[0].isalnum()
        or any(not c.isascii() or not (c.isalnum() or c in "_.-") for c in distribution)
    ):
        raise ConfigError(f"{section}.distribution 必须是有效的 WSL 发行版名称。")


@dataclass(frozen=True)
class AppConfig:
    """组装应用所需的完整配置；组装时只向各组件传递其需要的部分。"""

    llm: LLMConfig
    sessions_dir: Path
    system_prompt: str
    agent: AgentConfig = field(default_factory=AgentConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    tool_limits: ToolLimits = field(default_factory=ToolLimits)
    context: ContextConfig = field(default_factory=ContextConfig)
    subagents: SubagentConfig = field(default_factory=SubagentConfig)
    memories_dir: Path | None = None
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    documents: DocumentConfig = field(default_factory=DocumentConfig)
    documents_dir: Path | None = None
    hooks: HooksConfig = field(default_factory=HooksConfig)
    artifacts: ArtifactConfig = field(default_factory=ArtifactConfig)
    tool_discovery: ToolDiscoveryConfig = field(default_factory=ToolDiscoveryConfig)
    no_progress: NoProgressConfig = field(default_factory=NoProgressConfig)
    session_search: SessionSearchConfig = field(default_factory=SessionSearchConfig)
    knowledge_dir: Path | None = None
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    command_isolation: CommandIsolationConfig = field(default_factory=CommandIsolationConfig)
