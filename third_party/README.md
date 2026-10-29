# 第三方依赖

## mini-SWE-agent

- 上游：[SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)。
- 初始版本：`2.4.6`，提交 `04d809ceab9df28f9adaed044884180159172930`。
- 目录：`mini-swe-agent/`，由 Git submodule 管理。
- 许可证：MIT，保留第三方目录中的 `LICENSE.md`。
- 本地安装：`pyproject.toml` 的 uv source 以 editable 方式引用此目录。

本项目新增的工具、模型适配器、配置和文档放在外层项目。第三方核心确需修改时，先保留清晰的 diff；后续在我们维护的 fork 中提交，并更新外层 submodule 指针。

当前 submodule URL 指向官方上游。发布第三方核心修改前，应将 URL 切换到包含对应提交的团队 fork。

更新 submodule 版本时，同时核对依赖版本声明、`uv.lock` 和受影响的接口测试。
