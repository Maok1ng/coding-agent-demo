"""一次用户任务的上下文；权限和资源由宿主管理，不接受模型自行构造。"""

from dataclasses import dataclass, field, replace
from pathlib import Path

from coding_agent.runtime.artifacts import ArtifactStore
from coding_agent.runtime.approval import validate_approval_mode
from coding_agent.runtime.edit_recovery import EditRecovery
from coding_agent.runtime.project_files import ProjectFiles
from coding_agent.runtime.turn import TurnContext
from coding_agent.tools.skills import SkillLoader


@dataclass
class RunContext:
    workspace: Path
    turn: TurnContext
    mode: str = "execute"
    role: str = "orchestrator"
    artifacts: ArtifactStore | None = None
    versions: dict[str, str] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    #宿主记录本轮真正应用的文件修改；候选、模型报告和拒绝操作不会加入。
    changes: dict[str, dict] = field(default_factory=dict)
    #当前真实用户回合的检查点记录器；子角色共享，只有宿主能创建和恢复检查点。
    checkpoints: object = None
    #同一 Turn 的角色共享编辑失败计数；不从会话历史恢复，也不因压缩而重置。
    edit_recovery: EditRecovery = field(default_factory=EditRecovery)
    processes: object = None
    skills: object = None
    #角色共享文件发现策略；每次查询刷新，不能把旧目录树当作当前文件状态。
    project_files: ProjectFiles | None = None
    #所有角色共用计数器，不能靠不断派发子角色绕过本轮调用上限。
    calls: list[int] = field(default_factory=lambda: [0])
    max_calls: int = 40
    #同一真实用户回合只召回一次；子角色共享有界背景，不复制主会话全部历史。
    memory_context: str = ""
    memory_home: Path | None = None
    #审批级别由宿主传入并与子角色共享；模型工具参数不能覆盖它。
    approval_mode: str = "auto"
    #距上次模型活动超过缓存 TTL 时为真：历史反正要全价重发，首轮可免费做机械收缩。
    cache_window: bool = False

    def __post_init__(self):
        self.workspace = self.workspace.resolve()
        if self.mode not in {"plan", "execute"}:
            raise ValueError("无效运行模式")
        validate_approval_mode(self.approval_mode)
        if self.artifacts is None:
            self.artifacts = ArtifactStore(self.workspace, self.turn.id)
        if self.skills is None:
            self.skills = SkillLoader(self.workspace / "skills")
        if self.project_files is None:
            self.project_files = ProjectFiles(self.workspace)

    def for_role(self, role: str):
        #replace 只换角色，工作区、证据、进程管理器和取消信号仍指向同一轮。
        return replace(self, role=role)

    def verification_records(self, start=0):
        """汇集宿主实际记录；后台任务补上启动时的代码版本，不相信角色摘要。"""
        evidence = self.evidence[start:]
        records = [dict(item) for item in evidence if "command" in item and "status" in item]
        starts = {item["task_id"]: item for item in evidence if item.get("kind") == "process_start"}
        for item in records:
            if item.get("id") in starts:
                item["source_versions"] = starts[item["id"]]["source_versions"]
                item["execution_order"] = starts[item["id"]].get("execution_order")
        recorded = {item.get("id") for item in records}
        if self.processes is not None:
            for item in self.processes.records:
                if item["id"] in starts and item["id"] not in recorded:
                    records.append({**item, "source_versions": starts[item["id"]]["source_versions"],
                                    "execution_order": starts[item["id"]].get("execution_order")})
        return records
