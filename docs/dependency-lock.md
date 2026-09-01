# Python 依赖锁定

`requirements.txt` 是后端运行依赖的源声明；`requirements.lock` 是当前生产目标（Linux x86_64、glibc 2.39、Python 3.11）的完整传递依赖锁。`.github/requirements-ci.txt` 声明测试工具，`.github/requirements-ci.lock` 则在同一目标上锁定运行依赖与 CI 工具。`docker/requirements.lock` 单独锁定 Ubuntu 24.04 / glibc 2.39 / Python 3.12 容器图。

每日任务、backend gate、network smoke、发布前 gate 和 Docker 镜像均从 lock 安装，不再在 CI 中临时解析 `>=` 范围。消费 Python lock 的 Actions runner 固定为 `ubuntu-24.04`；Docker 也使用 Ubuntu 24.04，因为当前 Longbridge SDK wheel 要求 glibc 2.39。Git 依赖固定到 commit。

更新源依赖后，使用固定版本的 uv 重新解析：

```bash
python -m pip install uv==0.8.15
python -m uv pip compile requirements.txt --python-version 3.11 --python-platform x86_64-manylinux_2_39 --output-file requirements.lock --index-strategy first-index
python -m uv pip compile .github/requirements-ci.txt --python-version 3.11 --python-platform x86_64-manylinux_2_39 --output-file .github/requirements-ci.lock --index-strategy first-index
python -m uv pip compile requirements.txt --python-version 3.12 --python-platform x86_64-manylinux_2_39 --output-file docker/requirements.lock --index-strategy first-index
```

锁文件是目标平台特定产物。Windows 本地开发仍可从源声明安装；不得把 Linux lock 当作 Windows wheel 合约。升级关键 Provider、pandas、Markdown renderer、sanitizer 或交易日历后，还必须运行离线 contract tests。
