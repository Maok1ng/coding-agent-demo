"""命令行入口；先确定工作区，再加载依赖这些路径的业务模块。"""

from coding_agent.runtime.workspace import configure_cli


def main():
    configure_cli()
    #延迟导入，避免模块级 WORKDIR 在 --workspace 解析前就固定下来。
    from coding_agent.agents.loop import run_cli
    run_cli()
