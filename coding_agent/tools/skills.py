from pathlib import Path

import yaml
from coding_agent.runtime.workspace import WORKDIR

SKILLS_DIR = WORKDIR / "skills"

class SkillLoader:
    def __init__(self, skill_dir:Path):
        self.skills_dir = Path(skill_dir)
        self.skills: dict[str, dict[str, str]] = {}
        self.scan()

    @staticmethod #这个函数逻辑上属于这个类，但又不需要访问实例属性，也不需要访问类属性。
    #从一段文本开头解析 YAML frontmatter，并把“元数据”和“正文”分开返回。
    def parse_frontmatter(text: str) -> tuple[dict, str]: #输入字符串 返回元组 分别是字典和body
           lines = text.splitlines(keepends=True)#把文本按行拆开。 keepends=True 表示保留每一行末尾的换行符。
           if not lines or lines[0].rstrip("\r\n") != "---": #把第一行末尾的换行符去掉。
               return {}, text
   
           closing_index = next(
               (index for index, line in enumerate(lines[1:], start=1)
                if line.rstrip("\r\n") == "---"),#找第二个 --- 在第几行。
               None,
           )
           if closing_index is None:
               return {}, text #没有则说明没标准化
   
           frontmatter = "".join(lines[1:closing_index])#取两个 --- 中间的内容。
           body = "".join(lines[closing_index + 1:]).strip()
           try:
               metadata = yaml.safe_load(frontmatter) or {}
           except yaml.YAMLError:
               metadata = {}
           if not isinstance(metadata, dict):
               metadata = {}
           return metadata, body
    
    def scan(self):
            self.skills.clear() #清空之前扫描到的skill
            if not self.skills_dir.exists():#判断目录是否存在
                return
    
            skills_root = self.skills_dir.resolve()#获取绝对路径
            #整个 skills/ 被链接到工作区外时，也不能绕过文件工具的读取边界。
            if not skills_root.is_relative_to(self.skills_dir.parent.resolve()):
                return
            for manifest in sorted(self.skills_dir.glob("*/SKILL.md")): #扫描类似路径 skills/xxx/SKILL.md #sorted(...) 则是让文件按路径排序，保证每次扫描顺序稳定。
                if (not manifest.is_file() #检查xxx/SKILL.md是不是一个文件
                        or not manifest.resolve().is_relative_to(skills_root)):#安全检查 避免符号链接指向skill目录之外的东西
                    continue
                content = manifest.read_text(encoding="utf-8")
                metadata, body = self.parse_frontmatter(content)
                raw_name = metadata.get("name")
                name = raw_name.strip() if isinstance(raw_name, str) else ""
                name = name or manifest.parent.name #如果没有名字 就用目录名
                raw_description = metadata.get("description")
                description = (raw_description.strip()
                               if isinstance(raw_description, str) else "")
                description = description or body.split("\n", 1)[0] #没写描述就用正文的第一行
                description = " ".join(str(description).lstrip("# ").split())
                self.skills[name] = { #把这个skill放在self.skills
                    "name": name,
                    "description": description,
                    "content": content,
                }

    def catalog(self) -> str:#把当前已经扫描到的所有 skill，整理成一段简洁的文本目录。
                        if not self.skills:
                            return "(no skills found)"
                        return "\n".join(
                            f"- {skill['name']}: {skill['description']}"
                            for skill in self.skills.values()
                        )
                
    def load(self, name: str) -> str:#通过名字加载skill
                        skill = self.skills.get(name)
                        if skill:
                            return skill["content"]
                        available = ", ".join(self.skills) or "none"
                        return f"Error: Unknown skill '{name}'. Available: {available}"

SKILL_LOADER = SkillLoader(SKILLS_DIR)
