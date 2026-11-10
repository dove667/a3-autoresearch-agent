# 源码快照与作业文件传递

Agent 用下文的 `workspace_files` 提交研究脚本。Harness 版本由维护者选择和部署。历史试运行的 `version_id` 仍可用于追溯完整源码，文件传递回执用于核对实际作业输入。

普通 `POST /sessions` 不对 Agent 开放。Host 内部启动可复用版本装配能力；该内部路径不构成研究 Agent 的新会话入口。旧 worker 准备与分发流程已移除。快照与回执记录实际字节和来源，研究完成须由真实执行结果判断。

## Send selected workspace files to a remote job

`POST /jobs` also accepts `workspace_files`, without a version or extra local session:

```json
{
  "command": ["python", "runner.py"],
  "gpu": false,
  "workspace_files": [
    {
      "path": "research/runner.py",
      "sha256": "<64 lowercase hex characters>",
      "destination": "runner.py"
    }
  ],
  "input_exports": ["<already authorized export ID>"]
}
```

For jobs, sources must be under the authenticated caller's `research/`. A reference cannot read another identity's workspace. `destination` is an
optional canonical relative job path, defaulting to the source path. Jobs use the same
bounded, no-follow, hash-checked reads as session attachments. Ordinary `files` may supply
additional content strings, but duplicate destinations, file/directory conflicts and
combined inputs exceeding 128 files or 20 MiB are rejected before remote submission.
The 16-reference, 1 MiB-per-file and 8 MiB-total reference limits still apply.

The Host sends materialized strings using the unchanged controller protocol. The response's
`source_files` and Host `job-inputs.jsonl` associate each path, destination, actual digest
and byte count with the returned job ID. Remote status and artifacts confirm execution;
the receipt alone confirms materialization. If submission has an uncertain outcome,
reconcile recorded requests and jobs before retrying.

The current research Agent submits under its own identity and may reference read-only exports already authorized to research identities in the same run; the Host establishes the required job-mount grant. It need not create a child with asset access merely to read and serialize its
source. Cross-run exports still require explicit authorization. This does not enable arbitrary Host paths or add
`version_id` to `/jobs`. Ordinary `/sessions` creation is denied to Agent tokens. Formal trials require an already fixed version and do not accept `workspace_files`. Deploy the updated Host before using this form;
publishing source changes does not alter a running Host process.
