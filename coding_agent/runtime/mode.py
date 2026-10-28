"""Plan 的授权边界：只处理真正的用户输入，不扫描聊天记录中的 user 消息。"""

from dataclasses import asdict, dataclass
import re
import uuid


@dataclass(frozen=True)
class HumanInput:
    text: str
    #每次终端输入都有新的事件 ID；历史消息、工具结果不能伪造一次批准。
    event_id: str

    @classmethod
    def received(cls, text: str):
        return cls(text, uuid.uuid4().hex)


@dataclass
class PlanState:
    mode: str = "execute"
    plan_id: str | None = None
    revision: int = 0
    request: str = ""
    summary: str = ""
    awaiting_confirmation: bool = False
    approved_event: str | None = None

    def enter(self, request=""):
        self.mode = "plan"
        self.plan_id = uuid.uuid4().hex
        self.revision += 1
        self.request = request
        self.summary = ""
        self.awaiting_confirmation = False
        self.approved_event = None

    def invalidate(self):
        self.approved_event = None
        self.awaiting_confirmation = False

    def publish(self, summary):
        if self.plan_id is None:
            self.plan_id = uuid.uuid4().hex
        self.revision += 1
        self.summary = summary
        self.awaiting_confirmation = bool(summary.strip())
        self.approved_event = None

    def authorize(self, event: HumanInput):
        self.mode = "execute"
        self.approved_event = f"{self.plan_id}:{self.revision}:{event.event_id}"
        self.awaiting_confirmation = False

    def snapshot(self):
        #批准只属于当前进程当前输入，不写入可恢复快照。
        value = asdict(self)
        value["approved_event"] = None
        value["awaiting_confirmation"] = False
        return value


def user_intent(event: HumanInput, state: PlanState) -> str:
    """保守识别常见明确表达；不确定就保持只读，不让分类模型决定权限。"""
    if not isinstance(event, HumanInput):
        raise TypeError("只有真实 HumanInput 可以参与授权")
    text = event.text.strip().lower()
    #引文、代码块及长段指令不视为简单确认，防止把用户引用的话误当同意。
    quoted = any(mark in text for mark in ('"', "'", "“", "”", "「", "」", "`", "\n"))
    if re.search(r"先.{0,8}(方案|计划|规划)|只读|只.{0,5}(分析|规划|计划)|(?:不要|别|不准|禁止).{0,5}(修改|执行|改|写)|(?:修改|调整|更新|重写|重新|更改|修订).{0,5}(?:方案|计划)|(?:方案|计划).{0,5}(?:改为|调整为|换成)|(?:update|revise|change|rework) (?:the |this )?(?:plan|proposal)|plan only|read.only|do not (edit|implement|write)", text):
        return "plan"
    confirmations = (
        r"(?:请)?(?:就)?按(?:照)?(?:这[个份]?|上面[的]?|刚才[的]?|之前[的]?)?(?:方案|计划)(?:开始)?(?:实施|执行|修改|实现)(?:吧)?[。！!]?",
        r"(?:请)?(?:开始|继续)(?:实施|实现|执行)(?:这个|上述|该)?(?:方案|计划)?(?:吧)?[。！!]?",
        r"(?:同意|确认)(?:这个|该)?(?:方案|计划)?[，, ]*(?:开始)?(?:实施|执行|修改)[。！!]?",
        r"(?:please )?(?:implement|execute|build) (?:this|the|that) (?:plan|proposal)[.!]?",
        r"(?:go ahead|proceed)(?: with (?:the )?(?:plan|implementation))?[.!]?",
    )
    if not quoted and len(text) < 160 and any(re.fullmatch(p, text) for p in confirmations):
        return "confirm" if state.plan_id and state.summary.strip() else "clarify"
    #“好的/可以”也可能仅表示听懂解释；没有明确的实施动词，一律询问而不提权。
    if text in {"可以", "好的", "好", "ok", "yes", "同意"}:
        return "clarify"
    return "ordinary"
