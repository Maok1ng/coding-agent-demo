#不切换或修改全局 Python；通过 make PYTHON=/path/to/agent/bin/python 选择环境。
PYTHON ?= python

.PHONY: help install-dev test lint check package-check

help:
	@echo "install-dev: 安装可编辑项目与开发依赖到所选 Python 环境"
	@echo "test:        无真实模型/凭据的 unittest 回归测试"
	@echo "lint:        仅检查已有 Ruff F 规则，不自动改写代码"
	@echo "check:       lint + test"
	@echo "package-check: 临时构建 wheel/sdist，检查包内容并验证安装后的 CLI"

install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

lint:
	$(PYTHON) -m ruff check coding_agent tests scripts main.py

check: lint test

package-check:
	$(PYTHON) scripts/check_package.py
