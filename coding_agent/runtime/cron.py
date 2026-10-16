import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from coding_agent.runtime.workspace import WORKDIR

#Path.cwd() 取得启动程序时的工作目录；/ 是 Path 的路径拼接运算符。
#工作区现在统一由 workspace.py 选择，不再使用上面的启动目录作为默认值。
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


#一条定时任务的定义；pending_delivery 表示到期但还没交给模型。
#@dataclass 根据下面的字段自动生成 __init__ 等方法，asdict(job) 也要求 job 是数据类实例。
@dataclass
class CronJob:
    #冒号后的 str/bool 是类型标注；前五项没有默认值，创建 CronJob 时必须提供。
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    #等号右边是默认值；str | None 表示可以是字符串，也可以是 None。
    pending_delivery: bool = False
    last_fired: str | None = None


#任务表保存所有已注册任务，队列只保存到期且等待交付的任务。
#dict[str, CronJob] 和 list[CronJob] 只描述容器中元素的类型，不会自动校验数据。
scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
#RLock 允许同一个线程重复拿锁：例如 schedule_job 持锁时又调用持锁的 save_durable_jobs。
cron_lock = threading.RLock()


#判断单个 cron 字段是否匹配当前数值，支持通配符、步长、列表和范围。
#函数名开头的 _ 表示“内部使用”的约定；-> bool 是返回值的类型标注。
def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if "," in field:
        #先拆逗号再递归判断每一项；any(...) 只要遇到一个 True 就返回 True。
        #例如 "*/5,7" 在 value=7 时，前一项不匹配，后一项匹配。
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if field.startswith("*/"):
        #field[2:] 跳过开头两个字符 */；% 求余，余数为 0 表示整除。
        #例如 "*/5" 匹配 0、5、10……；这里直接按 value 求余。
        return value % int(field[2:]) == 0
    if "-" in field:
        #split("-", 1) 最多切一次；两个变量分别接收范围的起点和终点。
        start, end = field.split("-", 1)
        #链式比较等价于 int(start) <= value and value <= int(end)，包含两端。
        return int(start) <= value <= int(end)
    #没有特殊符号时按单个数字匹配，例如 "15" 只匹配 value=15。
    return value == int(field)


#五段式 cron 按本地时间匹配；星期日按 cron 习惯记为 0。
def cron_matches(cron_expr: str, moment: datetime) -> bool:
    #strip() 去掉两端空白，split() 按任意空白拆成五段。
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    minute, hour, day, month, weekday = fields
    #datetime.weekday() 是周一=0；加一再对 7 取余，转换成 cron 的周日=0。
    cron_weekday = (moment.weekday() + 1) % 7
    #and 会短路：分钟、小时或月份有一项不匹配，就不用继续检查日期。
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False

    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    #日期和星期都有限制时，两者满足一个即可；这沿用常见 cron 语义。
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    #日期和星期都指定时，只需其中一个匹配。
    return day_matches or weekday_matches


#先检查字段格式和数值范围，避免真正轮询时才发生解析错误。
def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if "," in field:
        #列表里的每一项都必须合法；递归让单值、范围和步长复用同一套校验。
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if field.startswith("*/"):
        step = field[2:]
        #isdigit() 先排除非数字，再检查步长不能为 0，避免匹配时除以 0。
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        #先检查范围顺序，再检查是否超出该字段允许的最小值和最大值。
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    #返回 None 表示校验通过；返回字符串表示具体错误原因。
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


#cron 必须正好有分钟、小时、日期、月份、星期五个字段。
def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"

    field_rules = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    ]
    #zip 把每段表达式和对应的(名称、最小值、最大值)配对；前面已保证正好五段。
    #for 里的 field, (name, minimum, maximum) 是嵌套解包。
    for field, (name, minimum, maximum) in zip(fields, field_rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


#只把 durable=True 的任务写入磁盘；临时文件加 os.replace 避免写出半份 JSON。
def save_durable_jobs():
    #with 在进入时加锁、离开时释放；异常发生也会释放锁。
    with cron_lock:
        #列表推导式：遍历所有任务，只保留 durable=True 的任务。
        #asdict 把数据类对象及其字段变成可序列化的字典。
        payload = [
            asdict(job)
            for job in scheduled_jobs.values()
            if job.durable
        ]
        #with_name 替换文件名但保留目录；进程 ID 和线程 ID 减少临时文件撞名。
        #临时文件与正式文件同目录，便于 os.replace 直接替换。
        temporary = DURABLE_PATH.with_name(
            f"{DURABLE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            #dumps 转成 JSON 字符串，indent=2 让文件缩进易读。
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            #写完后才替换正式文件；写入失败时不会留下半份正式 JSON。
            os.replace(temporary, DURABLE_PATH)
        finally:
            #finally 无论成功还是报错都会执行；替换成功后临时路径已不存在。
            temporary.unlink(missing_ok=True)


#启动时恢复持久化任务；上次已到期但未交付的任务重新放入队列。
def load_durable_jobs():
    if not DURABLE_PATH.exists():
        return
    try:
        #read_text 读出文本，loads 把 JSON 文本还原成 Python 对象。
        payload = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
        #只接受任务列表，避免文件内容是单个对象或其他类型。
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
    #一个 except 可以用元组列出多种异常；as error 取得实际异常对象。
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"  [cron] could not load {DURABLE_PATH.name}: {error}")
        return

    loaded = 0
    with cron_lock:
        for item in payload:
            try:
                #**item 把字典展开为关键字参数，如 {"id": "x"} 变成 id="x"。
                job = CronJob(**item)
                error = validate_cron(job.cron)
                if error:
                    #raise 主动报错，让这一条坏记录统一走下面的 except。
                    raise ValueError(error)
                if not job.id.startswith("cron_"):
                    raise ValueError("invalid job ID")
                if not job.prompt.strip():
                    raise ValueError("prompt cannot be empty")
                if not job.durable:
                    raise ValueError("saved job must be durable")
            except (TypeError, ValueError) as error:
                print(f"  [cron] skipped invalid saved job: {error}")
                #continue 仅跳过当前记录，接着处理 payload 中下一条。
                continue
            #可能重复调用加载函数；同一 ID 已在内存中时不再添加。
            if job.id in scheduled_jobs:
                continue
            scheduled_jobs[job.id] = job
            #已到期但未交付的任务同时恢复到待交付队列。
            if job.pending_delivery:
                cron_queue.append(job)
            #只有成功恢复且不是重复 ID 的记录才计数。
            loaded += 1
    if loaded:
        print(f"  [cron] loaded {loaded} durable job(s)")


#生成当前任务表中尚未使用的 cron_id。
def new_cron_id() -> str:
    #_ 表示循环次数要用到，但每次循环的序号本身不用。
    for _ in range(100):
        #token_hex(4) 产生 4 个随机字节，显示为 8 位十六进制字符。
        job_id = f"cron_{secrets.token_hex(4)}"
        if job_id not in scheduled_jobs:
            return job_id
    raise RuntimeError("Could not allocate a cron job ID")


#校验并登记新任务；若持久化失败，撤销刚才的内存登记。
def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    #返回类型 CronJob | str 表示成功时返回任务对象，校验失败时返回错误文本。
    error = validate_cron(cron)
    if error:
        return error
    #strip() 去掉两端空白；空字符串在 if 中视为 False，所以也会拒绝全空格内容。
    if not prompt.strip():
        return "Prompt cannot be empty"

    with cron_lock:
        #dataclass 自动生成的 __init__ 接收这些具名参数；其余字段使用默认值。
        job = CronJob(
            id=new_cron_id(),
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
        )
        scheduled_jobs[job.id] = job
        try:
            if durable:
                #这里再次进入 cron_lock，所以锁需要是可重入的 RLock。
                save_durable_jobs()
        except Exception:
            #文件保存失败时撤销刚加入的内存任务，并用 raise 原样抛出异常。
            scheduled_jobs.pop(job.id, None)
            raise
    #[:60] 只截取 prompt 的前 60 个字符用于日志，任务本身仍保存完整 prompt。
    print(f"  [cron] scheduled {job.id}: {cron} -> {prompt[:60]}")
    return job


#取消任务时同时从待交付队列移除；磁盘保存失败则回滚。
def cancel_job(job_id: str) -> str:
    with cron_lock:
        #dict.get 找不到键时返回 None；pop 则会从字典中删除该键。
        job = scheduled_jobs.get(job_id)
        if job is None:
            return f"Job {job_id} not found"

        #list(...) 复制当前队列，以便保存失败时恢复原来的顺序和内容。
        previous_queue = list(cron_queue)
        scheduled_jobs.pop(job_id)
        #cron_queue[:] 是整个列表切片：原地替换内容，保留同一个列表对象。
        #推导式只留下 ID 不等于被取消任务的队列元素。
        cron_queue[:] = [queued for queued in cron_queue if queued.id != job_id]
        try:
            if job.durable:
                save_durable_jobs()
        except Exception:
            #内存任务表和队列要一起回滚，保持与磁盘上的旧状态一致。
            scheduled_jobs[job_id] = job
            cron_queue[:] = previous_queue
            raise
    print(f"  [cron] cancelled {job_id}")
    return f"Cancelled {job_id}"


#先保存到期状态，再入内存队列，避免重启后丢掉已触发但未交付的任务。
def _enqueue_due_job(job: CronJob, minute_marker: str | None = None):
    #先记住修改前的值；持久化失败时要把同一个 job 对象恢复原状。
    old_pending = job.pending_delivery
    old_last_fired = job.last_fired
    job.pending_delivery = True
    if minute_marker is not None:
        job.last_fired = minute_marker
    try:
        if job.durable:
            #先保存 pending_delivery=True，再进入内存队列；重启后可以重新排队。
            save_durable_jobs()
    except Exception:
        job.pending_delivery = old_pending
        job.last_fired = old_last_fired
        raise
    #append 放入的是同一个 CronJob 对象，不是副本。
    cron_queue.append(job)


#每次检查当前分钟；last_fired 防止同一分钟重复触发。
def poll_due_jobs(moment: datetime):
    #strftime 把时间固定到“年-月-日 时:分”，同一分钟的多次轮询得到同一个标记。
    minute_marker = moment.strftime("%Y-%m-%d %H:%M")
    with cron_lock:
        #list(...) 先复制任务集合，避免遍历过程中集合改变影响本轮扫描。
        for job in list(scheduled_jobs.values()):
            try:
                #已在等待交付，或本分钟已经触发过的任务，不再重复入队。
                if job.pending_delivery or job.last_fired == minute_marker:
                    continue
                if cron_matches(job.cron, moment):
                    _enqueue_due_job(job, minute_marker)
                    print(f"  [cron] due {job.id}: {job.prompt[:60]}")
            except Exception as error:
                #某一条任务入队失败时只记录错误，其他任务继续检查。
                print(f"  [cron] could not enqueue {job.id}: {error}")


#把待交付任务一次性取出；真正交给模型后还需要 acknowledge。
def consume_cron_queue() -> list[CronJob]:
    with cron_lock:
        #复制任务列表后清空原队列；jobs 仍持有这些 CronJob 对象。
        jobs = list(cron_queue)
        cron_queue.clear()
    return jobs


#模型已接收 prompt 后确认交付：周期任务清除待交付标记，一次性任务删除。
def acknowledge_cron_jobs(jobs: list[CronJob]):
    #changed 的每项是 (任务对象, 修改前的 pending 值)，供持久化失败时回滚。
    changed: list[tuple[CronJob, bool]] = []
    #removed 记录本次删除的一次性任务，也用于失败回滚。
    removed: list[CronJob] = []
    with cron_lock:
        for delivered in jobs:
            #取消操作可能已删掉任务；找不到就跳过，不再改状态。
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue
            changed.append((current, current.pending_delivery))
            if current.recurring:
                #周期任务保留在任务表，等待下一次符合 cron 的时间。
                current.pending_delivery = False
            else:
                #一次性任务在首次成功交付后从任务表移除。
                removed.append(current)
                scheduled_jobs.pop(current.id)

        try:
            #生成器表达式逐项检查；(_, ...) 中的 _ 表示不使用元组第二项。
            #只要这批任务里有一个需要持久化，就保存一次最终任务表。
            if any(job.durable for job, _ in changed):
                save_durable_jobs()
        except Exception:
            #持久化失败时，先恢复被删的任务和原来的 pending 状态。
            for job in removed:
                scheduled_jobs[job.id] = job
            for job, pending in changed:
                job.pending_delivery = pending
            #集合推导式收集已在队列中的 ID，防止回滚时重复放入。
            queued_ids = {job.id for job in cron_queue}
            for job, _ in changed:
                if job.id not in queued_ids:
                    cron_queue.append(job)
            raise


#模型请求失败时把刚取出的任务放回队列，下次空闲时可以再试。
def restore_cron_jobs(jobs: list[CronJob]):
    with cron_lock:
        queued_ids = {job.id for job in cron_queue}
        for delivered in jobs:
            #任务可能在模型调用期间被取消；只恢复仍存在的任务。
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue
            current.pending_delivery = True
            #队列中的同一任务只保留一次；add 同步更新去重集合。
            if current.id not in queued_ids:
                cron_queue.append(current)
                queued_ids.add(current.id)


#队列处理线程用它判断是否有待运行的定时回合。
def has_cron_queue() -> bool:
    with cron_lock:
        #空列表转成 bool 是 False，非空列表转成 bool 是 True。
        return bool(cron_queue)


#创建任务的工具入口，把内部对象转换为可作为 tool_result 的文本。
def run_schedule_cron(cron: str, prompt: str, recurring: bool = True,
                      durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    #isinstance 区分错误字符串和成功返回的 CronJob 对象。
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: {cron} -> {prompt}"


#列出任务的工具入口，显示任务类型和是否持久化。
def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."

    lines = []
    for job in jobs:
        #条件表达式：条件成立取 if 前面的值，否则取 else 后面的值。
        frequency = "recurring" if job.recurring else "one-shot"
        storage = "durable" if job.durable else "session"
        lines.append(
            #相邻的两个 f 字符串会自动拼接成一个字符串。
            f"{job.id}: {job.cron} -> {job.prompt[:60]} "
            f"[{frequency}, {storage}]"
        )
    #join 用换行符连接每条任务描述，返回一个 tool_result 字符串。
    return "\n".join(lines)


#取消任务的工具入口。
def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


#供主 Agent 注册的工具定义；真正执行逻辑在 CRON_HANDLERS 中。
#input_schema 描述模型可传入的 JSON 参数；required 只列必填字段。
#recurring/durable 不在 required 中，未传时使用 run_schedule_cron 的默认值 True。
CRON_TOOLS = [
    {"name": "schedule_cron",
     "description": "Schedule a prompt with a 5-field cron expression.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string"},
                          "prompt": {"type": "string"},
                          "recurring": {"type": "boolean"},
                          "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List scheduled cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
]

#键是模型调用的工具名，值是对应的 Python 函数对象，这里没有立即调用函数。
CRON_HANDLERS = {
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
}
