# M0-01 电脑维护：完整任务数据示例

> 契约：[核心数据契约 v1](m0-01-data-contracts.md)。日期：2026-11-15。
> **全部为虚构的设计数据，没有调用模型、执行工具或维修真实电脑。** usage、时间、回执和通过结果仅用于验证契约间的引用与算术。工件 hash/size 根据本文给出的 UTF-8 内容实际计算，但不代表内容结论已真实验证。
> 本文的 7 个 JSON 块现由 `tests/contracts_support.py` 直接读取，作为独立数据层回归 fixture；通过校验不代表真实执行流程已实现。

## 1. 场景与阅读方法

用户要求修复 A 项目启动，允许诊断和私有快照测试，不升级依赖，改目标配置前先批准。小维是 root owner；环境助手完成诊断和最终应用，小维准备补丁。所有任务使用内置稳定角色 ID，不依赖 M1 ProfileStore 已实现。

env-fixture-A 是模拟 VS Code 启动入口的受控环境。project.launch-config 是示例抽象资源；本文没有声称任何 VS Code 插件接受这些配置字段。根验收只覆盖 fixture，不证明真实宿主已修复。

各节 JSON 块键互不重复，按顶层键合并即可得到一个完整示例数据对象。核心记录为 10:04:00Z 的最终快照；Checkpoint 是过去时刻的不可变状态，不能拿最终 answered 请求假装等待时已经批准。catalog 是供引用的辅助目录/模拟外部回执，其内部格式不是新增的运行时契约；history_fixture 展示续接所需事实，不宣称是完整 provider wire history。

## 2. 时间线、身份与恢复

| UTC 时间 | 事件 | 契约效果 |
|---|---|---|
| 10:00:00 | T0 开始，RUN0a 运行 | root 预算 B0，attempt AT0 |
| 10:00:10 | 询问具体启动条目 | H-question pending，CP0q，RUN0a 结束挂起 |
| 10:00:30 | 用户回答 Run A | H-question answered；AT0 不变，新建 RUN0b |
| 10:00:35–40 | 登记 T1/T2/T3，等待协作 | 子任务 ID 不同；CP0children 保存待交付结果 |
| 10:00:40–10:01:00 | T1 诊断 | F1 + R1；T2 依赖满足 |
| 10:01:00–30 | T2 写补丁并在快照验证 | F2/F3 + R2；尚未修改目标配置 |
| 10:01:30–35 | T3 准备动作 X4 | H-approval pending，CP3approval；X4 仍 prepared，无工具 lease 占用 |
| 10:02:00 | 程序在等待期间退出 | 无活跃 run；不产生新 attempt，不重发预算 |
| 10:03:35 | 重启并收到批准 | H-approval answered + reply allow；RUN3b 续接 AT3，重核资源后才执行 X4 |
| 10:03:45 | T3 完成 | F4/F5 + R3；子结果按 delivery_key 单次交回 root |
| 10:04:00 | root 独立验收通过 | F6 + R0；T0 才 completed |

运行区间并集为 [0,10)、[30,95)、[215,240)，共 100 秒；两段人工等待分别 20 秒和 120 秒；端到端 240 秒。root 的亲自活跃时间是 35 秒，B0 的 active_used_ms 包含所有子任务的并集，是 100000。

## 3. 辅助目录与精确工件内容

下面的字符串按 JSON 解码后直接以 UTF-8 编码，结尾换行参与 hash/size。receipt 和 provider_usage 都明确是模拟证据。
```json
{
  "catalog": {
    "user-1": {
      "kind": "user",
      "name": "示例用户"
    },
    "profile-wei": {
      "kind": "builtin_profile",
      "name": "小维"
    },
    "profile-env": {
      "kind": "builtin_profile",
      "name": "环境助手"
    },
    "project-A": {
      "kind": "project",
      "environment_id": "env-fixture-A"
    },
    "env-staging-T2": {
      "kind": "private_staging_fixture",
      "version": "staging-1",
      "derived_from": "env-fixture-A",
      "owner_task_id": "T2"
    },
    "RESOURCE-staging-config": {
      "kind": "resource",
      "project_id": "project-A",
      "environment_id": "env-staging-T2",
      "logical_name": "private.launch-config",
      "initial_version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f",
      "final_version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
    },
    "RESOURCE-staging-lock": {
      "kind": "resource",
      "project_id": "project-A",
      "environment_id": "env-staging-T2",
      "logical_name": "private.dependencies.lock",
      "initial_version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf",
      "final_version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
    },
    "env-fixture-A": {
      "kind": "controlled_fixture",
      "version": "fixture-1",
      "description": "模拟 VS Code 启动入口；不是用户宿主机"
    },
    "CON-1": {
      "kind": "constraints",
      "version": 1,
      "rules": [
        "不升级依赖",
        "不修改项目外配置",
        "应用目标配置前取得用户批准"
      ],
      "source_id": "MSG0"
    },
    "MSG0": {
      "kind": "user_message",
      "actor": {
        "kind": "user",
        "id": "user-1"
      },
      "at": "2026-11-15T10:00:00Z",
      "content": "A 项目在 VS Code 启动失败。可检查和测试，不升级依赖；修改项目配置前让我确认。"
    },
    "POL-1": {
      "kind": "policy",
      "version": 1,
      "project_id": "project-A",
      "description": "获准诊断、私有副本测试、固定工件发布；目标配置变更需匹配的 allow 回复；不得访问其他项目"
    },
    "AUTH-spec": {
      "kind": "authority",
      "source_id": "MSG0",
      "policy_id": "POL-1",
      "description": "根目标由用户确定；子任务检查不得削弱根目标或约束"
    },
    "RESOURCE-config": {
      "kind": "resource",
      "project_id": "project-A",
      "logical_name": "project.launch-config",
      "format": "示例中的抽象配置，不是 VS Code 插件配置 API",
      "initial_version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f",
      "final_version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
    },
    "RESOURCE-lock": {
      "kind": "resource",
      "project_id": "project-A",
      "logical_name": "dependencies.lock",
      "initial_version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf",
      "final_version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
    },
    "E-S0": {
      "kind": "evaluator",
      "version": 1,
      "evidence_kind": "deterministic",
      "description": "示例中约定的可信检查器；尚未实现"
    },
    "E-S1": {
      "kind": "evaluator",
      "version": 1,
      "evidence_kind": "deterministic",
      "description": "示例中约定的可信检查器；尚未实现"
    },
    "E-S2": {
      "kind": "evaluator",
      "version": 1,
      "evidence_kind": "deterministic",
      "description": "示例中约定的可信检查器；尚未实现"
    },
    "E-S3": {
      "kind": "evaluator",
      "version": 1,
      "evidence_kind": "deterministic",
      "description": "示例中约定的可信检查器；尚未实现"
    },
    "RC-X1": {
      "kind": "tool_receipt",
      "action_id": "X1",
      "at": "2026-11-15T10:00:55Z",
      "ok": true,
      "output_artifact_ids": [
        "F1"
      ],
      "environment_id": "env-fixture-A"
    },
    "AUTH-X1": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "task_id": "T1",
      "attempt_id": "AT1",
      "action_id": "X1",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:3590107a06073c3f15a2725a7bf03395b5534815f7858e9e5b5afd9b2485fb01",
      "decision": "allow",
      "reply_id": null,
      "checked_at": "2026-11-15T10:00:42Z"
    },
    "RC-X2": {
      "kind": "tool_receipt",
      "action_id": "X2",
      "at": "2026-11-15T10:01:10Z",
      "ok": true,
      "output_artifact_ids": [
        "F2"
      ],
      "environment_id": "env-fixture-A"
    },
    "AUTH-X2": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "task_id": "T2",
      "attempt_id": "AT2",
      "action_id": "X2",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:b1bc50c92626823ff621eb8f7f909a7017566504682e8373ea126f7384d73fbc",
      "decision": "allow",
      "reply_id": null,
      "checked_at": "2026-11-15T10:01:04Z"
    },
    "RC-X3": {
      "kind": "tool_receipt",
      "action_id": "X3",
      "at": "2026-11-15T10:01:25Z",
      "ok": true,
      "output_artifact_ids": [
        "FS",
        "F3"
      ],
      "environment_id": "env-staging-T2"
    },
    "AUTH-X3": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "task_id": "T2",
      "attempt_id": "AT2",
      "action_id": "X3",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:c36af7f3a5d8afa12a6163d3f58d96690a283ea8d05199e9fe20a365e501c165",
      "decision": "allow",
      "reply_id": null,
      "checked_at": "2026-11-15T10:01:10Z"
    },
    "RC-X4": {
      "kind": "tool_receipt",
      "action_id": "X4",
      "at": "2026-11-15T10:03:38Z",
      "ok": true,
      "output_artifact_ids": [
        "F4"
      ],
      "environment_id": "env-fixture-A"
    },
    "AUTH-X4": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "task_id": "T3",
      "attempt_id": "AT3",
      "action_id": "X4",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:747a092651bab889c8201bef807c9eb59fd0b9ba26a88d8e3bfdcf24b8fe1471",
      "decision": "allow",
      "reply_id": "RP-approval",
      "checked_at": "2026-11-15T10:03:35Z"
    },
    "RC-X5": {
      "kind": "tool_receipt",
      "action_id": "X5",
      "at": "2026-11-15T10:03:43Z",
      "ok": true,
      "output_artifact_ids": [
        "F5"
      ],
      "environment_id": "env-fixture-A"
    },
    "AUTH-X5": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "task_id": "T3",
      "attempt_id": "AT3",
      "action_id": "X5",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:786f6b14a8472bfcdc4b66ddc50be6877f19835b66de0e6bfc590ab76bd4a029",
      "decision": "allow",
      "reply_id": null,
      "checked_at": "2026-11-15T10:03:38Z"
    },
    "RC-X6": {
      "kind": "tool_receipt",
      "action_id": "X6",
      "at": "2026-11-15T10:03:58Z",
      "ok": true,
      "output_artifact_ids": [
        "F6"
      ],
      "environment_id": "env-fixture-A"
    },
    "AUTH-X6": {
      "kind": "authorization_decision",
      "policy_ref": {
        "id": "POL-1",
        "version": 1
      },
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "task_id": "T0",
      "attempt_id": "AT0",
      "action_id": "X6",
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "intent_digest": "sha256:c38482a145abc37377da61656333d574f1c5893dfa1b27af6945305a1fb81c7f",
      "decision": "allow",
      "reply_id": null,
      "checked_at": "2026-11-15T10:03:50Z"
    },
    "MSG-reply-question": {
      "kind": "user_message",
      "actor": {
        "kind": "user",
        "id": "user-1"
      },
      "reply_id": "RP-question",
      "at": "2026-11-15T10:00:30Z",
      "content": "Run A"
    },
    "CTX-T0": {
      "kind": "run_context",
      "task_id": "T0",
      "project_id": "project-A",
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "selected_launch_profile": "Run A",
      "selection_source": {
        "kind": "reply",
        "id": "RP-question"
      }
    },
    "CTX-T1": {
      "kind": "run_context",
      "task_id": "T1",
      "project_id": "project-A",
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "selected_launch_profile": "Run A",
      "selection_source": {
        "kind": "reply",
        "id": "RP-question"
      }
    },
    "CTX-T2": {
      "kind": "run_context",
      "task_id": "T2",
      "project_id": "project-A",
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "selected_launch_profile": "Run A",
      "selection_source": {
        "kind": "reply",
        "id": "RP-question"
      }
    },
    "CTX-T3": {
      "kind": "run_context",
      "task_id": "T3",
      "project_id": "project-A",
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "selected_launch_profile": "Run A",
      "selection_source": {
        "kind": "reply",
        "id": "RP-question"
      }
    },
    "CTX-T0-initial": {
      "kind": "run_context",
      "task_id": "T0",
      "project_id": "project-A",
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "selected_launch_profile": null
    },
    "HIST0q": {
      "kind": "history_fixture",
      "format": "provider-neutral-documentation",
      "events": [
        {
          "kind": "user_message",
          "ref": "MSG0"
        },
        {
          "kind": "pending_question",
          "request_id": "H-question",
          "tool_call_id": "TC-question"
        }
      ]
    },
    "HIST0children": {
      "kind": "history_fixture",
      "format": "provider-neutral-documentation",
      "events": [
        {
          "kind": "human_reply",
          "reply_id": "RP-question"
        },
        {
          "kind": "plan_registered",
          "task_ids": [
            "T1",
            "T2",
            "T3"
          ]
        }
      ]
    },
    "HIST3approval": {
      "kind": "history_fixture",
      "format": "provider-neutral-documentation",
      "events": [
        {
          "kind": "inputs_selected",
          "artifact_ids": [
            "F0",
            "FL",
            "F2",
            "F3"
          ]
        },
        {
          "kind": "pending_tool",
          "action_id": "X4",
          "tool_call_id": "TC-X4"
        }
      ]
    },
    "USAGE-LM0a": {
      "kind": "provider_usage",
      "call_id": "CALL-LM0a",
      "at": "2026-11-15T10:00:08Z",
      "tokens": 1000,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM0b": {
      "kind": "provider_usage",
      "call_id": "CALL-LM0b",
      "at": "2026-11-15T10:00:35Z",
      "tokens": 500,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM1": {
      "kind": "provider_usage",
      "call_id": "CALL-LM1",
      "at": "2026-11-15T10:00:42Z",
      "tokens": 1800,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM2": {
      "kind": "provider_usage",
      "call_id": "CALL-LM2",
      "at": "2026-11-15T10:01:04Z",
      "tokens": 2400,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM3a": {
      "kind": "provider_usage",
      "call_id": "CALL-LM3a",
      "at": "2026-11-15T10:01:34Z",
      "tokens": 800,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM3b": {
      "kind": "provider_usage",
      "call_id": "CALL-LM3b",
      "at": "2026-11-15T10:03:44Z",
      "tokens": 600,
      "source": "synthetic fixture; no API called"
    },
    "USAGE-LM0c": {
      "kind": "provider_usage",
      "call_id": "CALL-LM0c",
      "at": "2026-11-15T10:03:50Z",
      "tokens": 500,
      "source": "synthetic fixture; no API called"
    }
  },
  "artifact_contents_utf8": {
    "F0": "{\"entry\":\"app.py\",\"interpreter\":\"system-python\"}\n",
    "FL": "example-lib==1.0.0\n",
    "F1": "# 诊断\n启动条目：Run A\n入口：app.py\n实际解释器：system-python\n项目解释器：.venv/bin/python\n原入口退出码：1\n错误：ModuleNotFoundError: example_lib\n项目解释器能够导入 example_lib。\n",
    "F2": "{\"resource\":\"RESOURCE-config\",\"replace\":{\"interpreter\":\".venv/bin/python\"},\"preserve\":[\"entry\",\"dependencies.lock\"]}\n",
    "F3": "# 私有快照检查\n应用 F2 后从 Run A 启动，exit_code=0，health=ready。\n依赖锁文件未改变，目标配置尚未写入。\n",
    "FS": "{\"entry\":\"app.py\",\"interpreter\":\".venv/bin/python\"}\n",
    "F4": "{\"entry\":\"app.py\",\"interpreter\":\".venv/bin/python\"}\n",
    "F5": "# 目标环境复验\nenvironment=env-fixture-A\nlaunch_profile=Run A\nexit_code=0\nhealth=ready\n依赖锁文件未改变，仅 RESOURCE-config 有预期变更。\n",
    "F6": "# 根目标独立检查\n重新从 Run A 启动：exit_code=0，health=ready。\nRESOURCE-config 为已批准补丁结果；RESOURCE-lock 与原版本一致。\n变更日志无项目外写入。\n"
  }
}
```

## 4. 最终 Task、Attempt 和 Run

完成后的 Task 不再 wait；最近检查点仍保留历史。AT0 对应三个 run，AT3 对应两个 run，重启没有新增 attempt。
```json
{
  "tasks": [
    {
      "schema_version": 1,
      "record_type": "TaskRecord",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 11,
      "updated_at": "2026-11-15T10:04:00Z",
      "task_id": "T0",
      "root_id": "T0",
      "parent_id": null,
      "creation_key": "MSG0:root",
      "objective": "恢复 A 项目中用户选择的启动入口",
      "project_id": "project-A",
      "owner_profile_id": "profile-wei",
      "assignee_profile_id": "profile-wei",
      "deps": [],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "acceptance_ref": {
        "id": "S0",
        "version": 1
      },
      "budget_scope_id": "B0",
      "input_artifact_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F5",
          "content_hash": "sha256:cf8b5559811951186df489fbafc7e559aa3c2496dca5ec4f85da8c8ed89c153c"
        }
      ],
      "output_artifact_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F6",
          "content_hash": "sha256:282fbdb2b37582209298dc46046a4abf2339c6d40d3b39edd84293cbb8820d76"
        }
      ],
      "status": "completed",
      "reason": null,
      "current_attempt_id": "AT0",
      "latest_report_id": "R0",
      "wait_ref": null
    },
    {
      "schema_version": 1,
      "record_type": "TaskRecord",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 5,
      "updated_at": "2026-11-15T10:01:00Z",
      "task_id": "T1",
      "root_id": "T0",
      "parent_id": "T0",
      "creation_key": "T0:plan-v1:T1",
      "objective": "收集启动入口、解释器与错误证据",
      "project_id": "project-A",
      "owner_profile_id": "profile-wei",
      "assignee_profile_id": "profile-env",
      "deps": [],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "acceptance_ref": {
        "id": "S1",
        "version": 1
      },
      "budget_scope_id": "B1",
      "input_artifact_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "output_artifact_refs": [
        {
          "artifact_id": "F1",
          "content_hash": "sha256:5a7abc14821a60a4eeb8973e1a3610db7d51542b917731534460af25a78c48ea"
        }
      ],
      "status": "completed",
      "reason": null,
      "current_attempt_id": "AT1",
      "latest_report_id": "R1",
      "wait_ref": null
    },
    {
      "schema_version": 1,
      "record_type": "TaskRecord",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 6,
      "updated_at": "2026-11-15T10:01:30Z",
      "task_id": "T2",
      "root_id": "T0",
      "parent_id": "T0",
      "creation_key": "T0:plan-v1:T2",
      "objective": "准备最小修复并在私有快照验证",
      "project_id": "project-A",
      "owner_profile_id": "profile-wei",
      "assignee_profile_id": "profile-wei",
      "deps": [
        "T1"
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "acceptance_ref": {
        "id": "S2",
        "version": 1
      },
      "budget_scope_id": "B2",
      "input_artifact_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F1",
          "content_hash": "sha256:5a7abc14821a60a4eeb8973e1a3610db7d51542b917731534460af25a78c48ea"
        }
      ],
      "output_artifact_refs": [
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        },
        {
          "artifact_id": "FS",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F3",
          "content_hash": "sha256:9b13bdb7cc491b481e11844e411b8d2d0884896f6b27d44670756aca06f5fa87"
        }
      ],
      "status": "completed",
      "reason": null,
      "current_attempt_id": "AT2",
      "latest_report_id": "R2",
      "wait_ref": null
    },
    {
      "schema_version": 1,
      "record_type": "TaskRecord",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 9,
      "updated_at": "2026-11-15T10:03:45Z",
      "task_id": "T3",
      "root_id": "T0",
      "parent_id": "T0",
      "creation_key": "T0:plan-v1:T3",
      "objective": "应用获批修改并复验",
      "project_id": "project-A",
      "owner_profile_id": "profile-wei",
      "assignee_profile_id": "profile-env",
      "deps": [
        "T2"
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "acceptance_ref": {
        "id": "S3",
        "version": 1
      },
      "budget_scope_id": "B3",
      "input_artifact_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        },
        {
          "artifact_id": "F3",
          "content_hash": "sha256:9b13bdb7cc491b481e11844e411b8d2d0884896f6b27d44670756aca06f5fa87"
        }
      ],
      "output_artifact_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F5",
          "content_hash": "sha256:cf8b5559811951186df489fbafc7e559aa3c2496dca5ec4f85da8c8ed89c153c"
        }
      ],
      "status": "completed",
      "reason": null,
      "current_attempt_id": "AT3",
      "latest_report_id": "R3",
      "wait_ref": null
    }
  ],
  "attempts": [
    {
      "schema_version": 1,
      "record_type": "AttemptRecord",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 7,
      "updated_at": "2026-11-15T10:04:00Z",
      "attempt_id": "AT0",
      "task_id": "T0",
      "number": 1,
      "state": "succeeded",
      "context_ref": "CTX-T0",
      "checkpoint_id": "CP0children",
      "report_ids": [
        "R0"
      ],
      "started_at": "2026-11-15T10:00:00Z",
      "ended_at": "2026-11-15T10:04:00Z",
      "end_reason": "acceptance_passed"
    },
    {
      "schema_version": 1,
      "record_type": "AttemptRecord",
      "created_at": "2026-11-15T10:00:40Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:00Z",
      "attempt_id": "AT1",
      "task_id": "T1",
      "number": 1,
      "state": "succeeded",
      "context_ref": "CTX-T1",
      "checkpoint_id": null,
      "report_ids": [
        "R1"
      ],
      "started_at": "2026-11-15T10:00:40Z",
      "ended_at": "2026-11-15T10:01:00Z",
      "end_reason": "acceptance_passed"
    },
    {
      "schema_version": 1,
      "record_type": "AttemptRecord",
      "created_at": "2026-11-15T10:01:00Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:30Z",
      "attempt_id": "AT2",
      "task_id": "T2",
      "number": 1,
      "state": "succeeded",
      "context_ref": "CTX-T2",
      "checkpoint_id": null,
      "report_ids": [
        "R2"
      ],
      "started_at": "2026-11-15T10:01:00Z",
      "ended_at": "2026-11-15T10:01:30Z",
      "end_reason": "acceptance_passed"
    },
    {
      "schema_version": 1,
      "record_type": "AttemptRecord",
      "created_at": "2026-11-15T10:01:30Z",
      "revision": 5,
      "updated_at": "2026-11-15T10:03:45Z",
      "attempt_id": "AT3",
      "task_id": "T3",
      "number": 1,
      "state": "succeeded",
      "context_ref": "CTX-T3",
      "checkpoint_id": "CP3approval",
      "report_ids": [
        "R3"
      ],
      "started_at": "2026-11-15T10:01:30Z",
      "ended_at": "2026-11-15T10:03:45Z",
      "end_reason": "acceptance_passed"
    }
  ],
  "runs": [
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:10Z",
      "run_id": "RUN0a",
      "task_id": "T0",
      "attempt_id": "AT0",
      "state": "ended",
      "started_at": "2026-11-15T10:00:00Z",
      "ended_at": "2026-11-15T10:00:10Z",
      "end_reason": "suspended",
      "resumed_from": null
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:00:30Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:40Z",
      "run_id": "RUN0b",
      "task_id": "T0",
      "attempt_id": "AT0",
      "state": "ended",
      "started_at": "2026-11-15T10:00:30Z",
      "ended_at": "2026-11-15T10:00:40Z",
      "end_reason": "suspended",
      "resumed_from": "CP0q"
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:03:45Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:04:00Z",
      "run_id": "RUN0c",
      "task_id": "T0",
      "attempt_id": "AT0",
      "state": "ended",
      "started_at": "2026-11-15T10:03:45Z",
      "ended_at": "2026-11-15T10:04:00Z",
      "end_reason": "completed",
      "resumed_from": "CP0children"
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:00:40Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:00Z",
      "run_id": "RUN1",
      "task_id": "T1",
      "attempt_id": "AT1",
      "state": "ended",
      "started_at": "2026-11-15T10:00:40Z",
      "ended_at": "2026-11-15T10:01:00Z",
      "end_reason": "completed",
      "resumed_from": null
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:01:00Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:30Z",
      "run_id": "RUN2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "state": "ended",
      "started_at": "2026-11-15T10:01:00Z",
      "ended_at": "2026-11-15T10:01:30Z",
      "end_reason": "completed",
      "resumed_from": null
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:01:30Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:35Z",
      "run_id": "RUN3a",
      "task_id": "T3",
      "attempt_id": "AT3",
      "state": "ended",
      "started_at": "2026-11-15T10:01:30Z",
      "ended_at": "2026-11-15T10:01:35Z",
      "end_reason": "suspended",
      "resumed_from": null
    },
    {
      "schema_version": 1,
      "record_type": "RunRecord",
      "created_at": "2026-11-15T10:03:35Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:45Z",
      "run_id": "RUN3b",
      "task_id": "T3",
      "attempt_id": "AT3",
      "state": "ended",
      "started_at": "2026-11-15T10:03:35Z",
      "ended_at": "2026-11-15T10:03:45Z",
      "end_reason": "completed",
      "resumed_from": "CP3approval"
    }
  ]
}
```

## 5. 验收要求与结果

四个任务各有 Spec/Report。S0 的 launch_profile_slot 由真实来源标识的 RP-question 填入上下文；要求本身没有被执行者降低。S2/R2 绑定独立 env-staging-T2 及其修复后的 FS 配置版本；F0/FL 仍是基线输入。R2 只证明私有快照候选，R0 对 env-fixture-A 目标单独检查。
```json
{
  "acceptance_specs": [
    {
      "schema_version": 1,
      "record_type": "AcceptanceSpec",
      "created_at": "2026-11-15T10:00:00Z",
      "spec_id": "S0",
      "version": 1,
      "task_id": "T0",
      "objective": "恢复用户选择的启动入口",
      "target": {
        "project_id": "project-A",
        "environment_id": "env-fixture-A",
        "resource_ids": [
          "RESOURCE-config",
          "RESOURCE-lock"
        ]
      },
      "criteria": [
        {
          "id": "C01",
          "description": "同一启动入口正常启动并 health=ready",
          "required": true
        },
        {
          "id": "C02",
          "description": "依赖锁文件不变",
          "required": true
        },
        {
          "id": "C03",
          "description": "只有已批准的项目配置变更",
          "required": true
        }
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S0",
        "version": 1,
        "kind": "deterministic"
      },
      "params": {
        "launch_profile_slot": "selected_launch_profile"
      },
      "required_output_roles": [],
      "authority_ref": "AUTH-spec"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceSpec",
      "created_at": "2026-11-15T10:00:35Z",
      "spec_id": "S1",
      "version": 1,
      "task_id": "T1",
      "objective": "收集可核对的启动诊断证据",
      "target": {
        "project_id": "project-A",
        "environment_id": "env-fixture-A",
        "resource_ids": [
          "RESOURCE-config",
          "RESOURCE-lock"
        ]
      },
      "criteria": [
        {
          "id": "C11",
          "description": "报告中的入口、解释器和错误与诊断回执相符",
          "required": true
        }
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S1",
        "version": 1,
        "kind": "deterministic"
      },
      "params": {},
      "required_output_roles": [
        "diagnosis"
      ],
      "authority_ref": "AUTH-spec"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceSpec",
      "created_at": "2026-11-15T10:00:35Z",
      "spec_id": "S2",
      "version": 1,
      "task_id": "T2",
      "objective": "准备并验证最小配置修复",
      "target": {
        "project_id": "project-A",
        "environment_id": "env-staging-T2",
        "resource_ids": [
          "RESOURCE-staging-config",
          "RESOURCE-staging-lock"
        ]
      },
      "criteria": [
        {
          "id": "C21",
          "description": "补丁只变更解释器选择",
          "required": true
        },
        {
          "id": "C22",
          "description": "私有快照运行通过且依赖不变",
          "required": true
        }
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S2",
        "version": 1,
        "kind": "deterministic"
      },
      "params": {},
      "required_output_roles": [
        "patch",
        "staging_config",
        "staging_probe"
      ],
      "authority_ref": "AUTH-spec"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceSpec",
      "created_at": "2026-11-15T10:00:35Z",
      "spec_id": "S3",
      "version": 1,
      "task_id": "T3",
      "objective": "应用获批补丁并检查目标",
      "target": {
        "project_id": "project-A",
        "environment_id": "env-fixture-A",
        "resource_ids": [
          "RESOURCE-config",
          "RESOURCE-lock"
        ]
      },
      "criteria": [
        {
          "id": "C31",
          "description": "实际配置与批准的补丁结果一致",
          "required": true
        },
        {
          "id": "C32",
          "description": "目标入口检查通过且约束满足",
          "required": true
        }
      ],
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S3",
        "version": 1,
        "kind": "deterministic"
      },
      "params": {},
      "required_output_roles": [
        "target_config",
        "target_probe"
      ],
      "authority_ref": "AUTH-spec"
    }
  ],
  "acceptance_reports": [
    {
      "schema_version": 1,
      "record_type": "AcceptanceReport",
      "created_at": "2026-11-15T10:01:00Z",
      "report_id": "R1",
      "task_id": "T1",
      "attempt_id": "AT1",
      "spec_ref": {
        "id": "S1",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S1",
        "version": 1,
        "kind": "deterministic"
      },
      "verdict": "passed",
      "checks": [
        {
          "criterion_id": "C11",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F1"
            },
            {
              "kind": "receipt",
              "id": "RC-X1"
            }
          ]
        }
      ],
      "evaluated_artifact_refs": [
        {
          "artifact_id": "F1",
          "content_hash": "sha256:5a7abc14821a60a4eeb8973e1a3610db7d51542b917731534460af25a78c48ea"
        }
      ],
      "evaluated_resource_versions": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "evidence_refs": [
        {
          "kind": "artifact",
          "id": "F1"
        },
        {
          "kind": "receipt",
          "id": "RC-X1"
        }
      ],
      "reason": null,
      "evaluated_at": "2026-11-15T10:01:00Z"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceReport",
      "created_at": "2026-11-15T10:01:30Z",
      "report_id": "R2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "spec_ref": {
        "id": "S2",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S2",
        "version": 1,
        "kind": "deterministic"
      },
      "verdict": "passed",
      "checks": [
        {
          "criterion_id": "C21",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F2"
            },
            {
              "kind": "artifact",
              "id": "FS"
            },
            {
              "kind": "artifact",
              "id": "F3"
            },
            {
              "kind": "receipt",
              "id": "RC-X3"
            }
          ]
        },
        {
          "criterion_id": "C22",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F2"
            },
            {
              "kind": "artifact",
              "id": "FS"
            },
            {
              "kind": "artifact",
              "id": "F3"
            },
            {
              "kind": "receipt",
              "id": "RC-X3"
            }
          ]
        }
      ],
      "evaluated_artifact_refs": [
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        },
        {
          "artifact_id": "FS",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F3",
          "content_hash": "sha256:9b13bdb7cc491b481e11844e411b8d2d0884896f6b27d44670756aca06f5fa87"
        }
      ],
      "evaluated_resource_versions": [
        {
          "resource_id": "RESOURCE-staging-config",
          "state": "present",
          "version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "resource_id": "RESOURCE-staging-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "evidence_refs": [
        {
          "kind": "artifact",
          "id": "F2"
        },
        {
          "kind": "artifact",
          "id": "FS"
        },
        {
          "kind": "artifact",
          "id": "F3"
        },
        {
          "kind": "receipt",
          "id": "RC-X3"
        }
      ],
      "reason": null,
      "evaluated_at": "2026-11-15T10:01:30Z"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceReport",
      "created_at": "2026-11-15T10:03:45Z",
      "report_id": "R3",
      "task_id": "T3",
      "attempt_id": "AT3",
      "spec_ref": {
        "id": "S3",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S3",
        "version": 1,
        "kind": "deterministic"
      },
      "verdict": "passed",
      "checks": [
        {
          "criterion_id": "C31",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F4"
            },
            {
              "kind": "artifact",
              "id": "F5"
            },
            {
              "kind": "receipt",
              "id": "RC-X5"
            }
          ]
        },
        {
          "criterion_id": "C32",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F4"
            },
            {
              "kind": "artifact",
              "id": "F5"
            },
            {
              "kind": "receipt",
              "id": "RC-X5"
            }
          ]
        }
      ],
      "evaluated_artifact_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "F5",
          "content_hash": "sha256:cf8b5559811951186df489fbafc7e559aa3c2496dca5ec4f85da8c8ed89c153c"
        }
      ],
      "evaluated_resource_versions": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "evidence_refs": [
        {
          "kind": "artifact",
          "id": "F4"
        },
        {
          "kind": "artifact",
          "id": "F5"
        },
        {
          "kind": "receipt",
          "id": "RC-X5"
        }
      ],
      "reason": null,
      "evaluated_at": "2026-11-15T10:03:45Z"
    },
    {
      "schema_version": 1,
      "record_type": "AcceptanceReport",
      "created_at": "2026-11-15T10:04:00Z",
      "report_id": "R0",
      "task_id": "T0",
      "attempt_id": "AT0",
      "spec_ref": {
        "id": "S0",
        "version": 1
      },
      "evaluator_ref": {
        "id": "E-S0",
        "version": 1,
        "kind": "deterministic"
      },
      "verdict": "passed",
      "checks": [
        {
          "criterion_id": "C01",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F4"
            },
            {
              "kind": "artifact",
              "id": "FL"
            },
            {
              "kind": "artifact",
              "id": "F6"
            },
            {
              "kind": "receipt",
              "id": "RC-X6"
            }
          ]
        },
        {
          "criterion_id": "C02",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F4"
            },
            {
              "kind": "artifact",
              "id": "FL"
            },
            {
              "kind": "artifact",
              "id": "F6"
            },
            {
              "kind": "receipt",
              "id": "RC-X6"
            }
          ]
        },
        {
          "criterion_id": "C03",
          "verdict": "passed",
          "reason": "该示例的受控检查满足本条件",
          "evidence_refs": [
            {
              "kind": "artifact",
              "id": "F4"
            },
            {
              "kind": "artifact",
              "id": "FL"
            },
            {
              "kind": "artifact",
              "id": "F6"
            },
            {
              "kind": "receipt",
              "id": "RC-X6"
            }
          ]
        }
      ],
      "evaluated_artifact_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F6",
          "content_hash": "sha256:282fbdb2b37582209298dc46046a4abf2339c6d40d3b39edd84293cbb8820d76"
        }
      ],
      "evaluated_resource_versions": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "evidence_refs": [
        {
          "kind": "artifact",
          "id": "F4"
        },
        {
          "kind": "artifact",
          "id": "FL"
        },
        {
          "kind": "artifact",
          "id": "F6"
        },
        {
          "kind": "receipt",
          "id": "RC-X6"
        }
      ],
      "reason": null,
      "evaluated_at": "2026-11-15T10:04:00Z"
    }
  ]
}
```

## 6. 全部预算记录

七个模型请求和六个工具请求各有唯一 lease。程序内部状态更新、发问和结果投递不计为外部 tool_call；若实现把它们也计入动作上限，应另设明确计数维度。四份验收报告使用已登记的工具证据；X5 和 X6 是程序触发的目标复验与根目标检查，tool_call_id=null，分别计入 LT5/LT6；不产生隐藏模型请求。
```json
{
  "budget_scopes": [
    {
      "schema_version": 1,
      "record_type": "BudgetScope",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:04:00Z",
      "scope_id": "B0",
      "task_id": "T0",
      "root_scope_id": "B0",
      "parent_scope_id": null,
      "limits": {
        "tokens": 12000,
        "llm_calls": 12,
        "tool_calls": 20
      },
      "active_limit_ms": 180000,
      "active_used_ms": 100000,
      "expires_at": "2026-11-15T10:10:00Z"
    },
    {
      "schema_version": 1,
      "record_type": "BudgetScope",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:01:00Z",
      "scope_id": "B1",
      "task_id": "T1",
      "root_scope_id": "B0",
      "parent_scope_id": "B0",
      "limits": {
        "tokens": 3000,
        "llm_calls": 3,
        "tool_calls": 3
      },
      "active_limit_ms": 45000,
      "active_used_ms": 20000,
      "expires_at": "2026-11-15T10:10:00Z"
    },
    {
      "schema_version": 1,
      "record_type": "BudgetScope",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:01:30Z",
      "scope_id": "B2",
      "task_id": "T2",
      "root_scope_id": "B0",
      "parent_scope_id": "B0",
      "limits": {
        "tokens": 4000,
        "llm_calls": 3,
        "tool_calls": 5
      },
      "active_limit_ms": 60000,
      "active_used_ms": 30000,
      "expires_at": "2026-11-15T10:10:00Z"
    },
    {
      "schema_version": 1,
      "record_type": "BudgetScope",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:03:45Z",
      "scope_id": "B3",
      "task_id": "T3",
      "root_scope_id": "B0",
      "parent_scope_id": "B0",
      "limits": {
        "tokens": 3000,
        "llm_calls": 3,
        "tool_calls": 5
      },
      "active_limit_ms": 45000,
      "active_used_ms": 15000,
      "expires_at": "2026-11-15T10:10:00Z"
    }
  ],
  "budget_leases": [
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:08Z",
      "lease_id": "LM0a",
      "scope_id": "B0",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0a",
      "call_id": "CALL-LM0a",
      "kind": "llm",
      "purpose": "clarification",
      "reserved": {
        "tokens": 1200,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 1000,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:00:00Z",
      "settled_at": "2026-11-15T10:00:08Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM0a"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:00:30Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:35Z",
      "lease_id": "LM0b",
      "scope_id": "B0",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0b",
      "call_id": "CALL-LM0b",
      "kind": "llm",
      "purpose": "planning",
      "reserved": {
        "tokens": 800,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 500,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:00:30Z",
      "settled_at": "2026-11-15T10:00:35Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM0b"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:00:40Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:42Z",
      "lease_id": "LM1",
      "scope_id": "B1",
      "task_id": "T1",
      "attempt_id": "AT1",
      "run_id": "RUN1",
      "call_id": "CALL-LM1",
      "kind": "llm",
      "purpose": "diagnosis",
      "reserved": {
        "tokens": 2200,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 1800,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:00:40Z",
      "settled_at": "2026-11-15T10:00:42Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM1"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:01:00Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:04Z",
      "lease_id": "LM2",
      "scope_id": "B2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "run_id": "RUN2",
      "call_id": "CALL-LM2",
      "kind": "llm",
      "purpose": "repair_plan",
      "reserved": {
        "tokens": 3000,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 2400,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:01:00Z",
      "settled_at": "2026-11-15T10:01:04Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM2"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:01:30Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:34Z",
      "lease_id": "LM3a",
      "scope_id": "B3",
      "task_id": "T3",
      "attempt_id": "AT3",
      "run_id": "RUN3a",
      "call_id": "CALL-LM3a",
      "kind": "llm",
      "purpose": "prepare_action",
      "reserved": {
        "tokens": 1000,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 800,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:01:30Z",
      "settled_at": "2026-11-15T10:01:34Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM3a"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:03:43Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:44Z",
      "lease_id": "LM3b",
      "scope_id": "B3",
      "task_id": "T3",
      "attempt_id": "AT3",
      "run_id": "RUN3b",
      "call_id": "CALL-LM3b",
      "kind": "llm",
      "purpose": "result_summary",
      "reserved": {
        "tokens": 1000,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 600,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:03:43Z",
      "settled_at": "2026-11-15T10:03:44Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM3b"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:03:45Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:50Z",
      "lease_id": "LM0c",
      "scope_id": "B0",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0c",
      "call_id": "CALL-LM0c",
      "kind": "llm",
      "purpose": "root_verification_request",
      "reserved": {
        "tokens": 800,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "actual": {
        "tokens": 500,
        "llm_calls": 1,
        "tool_calls": 0
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:03:45Z",
      "settled_at": "2026-11-15T10:03:50Z",
      "evidence_ref": {
        "kind": "catalog",
        "id": "USAGE-LM0c"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:00:42Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:55Z",
      "lease_id": "LT1",
      "scope_id": "B1",
      "task_id": "T1",
      "attempt_id": "AT1",
      "run_id": "RUN1",
      "call_id": "CALL-LT1",
      "kind": "tool",
      "purpose": "probe_launch",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:00:42Z",
      "settled_at": "2026-11-15T10:00:55Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X1"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:01:04Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:10Z",
      "lease_id": "LT2",
      "scope_id": "B2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "run_id": "RUN2",
      "call_id": "CALL-LT2",
      "kind": "tool",
      "purpose": "write_patch",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:01:04Z",
      "settled_at": "2026-11-15T10:01:10Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X2"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:01:10Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:25Z",
      "lease_id": "LT3",
      "scope_id": "B2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "run_id": "RUN2",
      "call_id": "CALL-LT3",
      "kind": "tool",
      "purpose": "test_snapshot",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:01:10Z",
      "settled_at": "2026-11-15T10:01:25Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X3"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:03:35Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:38Z",
      "lease_id": "LT4",
      "scope_id": "B3",
      "task_id": "T3",
      "attempt_id": "AT3",
      "run_id": "RUN3b",
      "call_id": "CALL-LT4",
      "kind": "tool",
      "purpose": "apply_patch",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:03:35Z",
      "settled_at": "2026-11-15T10:03:38Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X4"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:03:38Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:43Z",
      "lease_id": "LT5",
      "scope_id": "B3",
      "task_id": "T3",
      "attempt_id": "AT3",
      "run_id": "RUN3b",
      "call_id": "CALL-LT5",
      "kind": "tool",
      "purpose": "verify_target",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:03:38Z",
      "settled_at": "2026-11-15T10:03:43Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X5"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "BudgetLease",
      "created_at": "2026-11-15T10:03:50Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:58Z",
      "lease_id": "LT6",
      "scope_id": "B0",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0c",
      "call_id": "CALL-LT6",
      "kind": "tool",
      "purpose": "evaluate_goal",
      "reserved": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "actual": {
        "tokens": 0,
        "llm_calls": 0,
        "tool_calls": 1
      },
      "status": "settled",
      "dispatched_at": "2026-11-15T10:03:50Z",
      "settled_at": "2026-11-15T10:03:58Z",
      "evidence_ref": {
        "kind": "receipt",
        "id": "RC-X6"
      },
      "reason": null
    }
  ]
}
```

## 7. 固定工件与精确授权

每个 grant 在对应工件存在后签发，限定到具体执行角色和任务。输入读取通过 grant；工具/验收控制层只在其授权职责内读取证据。授予小维某任务访问，不自动授予其所有其他任务访问。
```json
{
  "artifacts": [
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:00:00Z",
      "artifact_id": "F0",
      "logical_name": "initial-config.txt",
      "role": "initial_config",
      "media_type": "text/plain",
      "size_bytes": 49,
      "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f",
      "storage_key": "fixture-objects/F0",
      "creator_ref": {
        "kind": "import",
        "actor": {
          "kind": "user",
          "id": "user-1"
        },
        "source_id": "MSG0"
      },
      "input_refs": [],
      "source_refs": [
        {
          "kind": "catalog",
          "id": "MSG0"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:00:00Z",
      "artifact_id": "FL",
      "logical_name": "dependencies.lock.txt",
      "role": "dependency_baseline",
      "media_type": "text/plain",
      "size_bytes": 19,
      "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf",
      "storage_key": "fixture-objects/FL",
      "creator_ref": {
        "kind": "import",
        "actor": {
          "kind": "user",
          "id": "user-1"
        },
        "source_id": "MSG0"
      },
      "input_refs": [],
      "source_refs": [
        {
          "kind": "catalog",
          "id": "MSG0"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:00:55Z",
      "artifact_id": "F1",
      "logical_name": "diagnosis.md",
      "role": "diagnosis",
      "media_type": "text/markdown",
      "size_bytes": 221,
      "content_hash": "sha256:5a7abc14821a60a4eeb8973e1a3610db7d51542b917731534460af25a78c48ea",
      "storage_key": "fixture-objects/F1",
      "creator_ref": {
        "kind": "task",
        "task_id": "T1",
        "attempt_id": "AT1",
        "action_id": "X1"
      },
      "input_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X1"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:01:10Z",
      "artifact_id": "F2",
      "logical_name": "patch.txt",
      "role": "patch",
      "media_type": "text/plain",
      "size_bytes": 117,
      "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42",
      "storage_key": "fixture-objects/F2",
      "creator_ref": {
        "kind": "task",
        "task_id": "T2",
        "attempt_id": "AT2",
        "action_id": "X2"
      },
      "input_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "F1",
          "content_hash": "sha256:5a7abc14821a60a4eeb8973e1a3610db7d51542b917731534460af25a78c48ea"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X2"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:01:25Z",
      "artifact_id": "FS",
      "logical_name": "staging-config.txt",
      "role": "staging_config",
      "media_type": "text/plain",
      "size_bytes": 52,
      "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701",
      "storage_key": "fixture-objects/FS",
      "creator_ref": {
        "kind": "task",
        "task_id": "T2",
        "attempt_id": "AT2",
        "action_id": "X3"
      },
      "input_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X3"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:01:25Z",
      "artifact_id": "F3",
      "logical_name": "staging-probe.md",
      "role": "staging_probe",
      "media_type": "text/markdown",
      "size_bytes": 138,
      "content_hash": "sha256:9b13bdb7cc491b481e11844e411b8d2d0884896f6b27d44670756aca06f5fa87",
      "storage_key": "fixture-objects/F3",
      "creator_ref": {
        "kind": "task",
        "task_id": "T2",
        "attempt_id": "AT2",
        "action_id": "X3"
      },
      "input_refs": [
        {
          "artifact_id": "FS",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X3"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:03:38Z",
      "artifact_id": "F4",
      "logical_name": "target-config.txt",
      "role": "target_config",
      "media_type": "text/plain",
      "size_bytes": 52,
      "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701",
      "storage_key": "fixture-objects/F4",
      "creator_ref": {
        "kind": "task",
        "task_id": "T3",
        "attempt_id": "AT3",
        "action_id": "X4"
      },
      "input_refs": [
        {
          "artifact_id": "F0",
          "content_hash": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "artifact_id": "F2",
          "content_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X4"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:03:43Z",
      "artifact_id": "F5",
      "logical_name": "target-probe.md",
      "role": "target_probe",
      "media_type": "text/markdown",
      "size_bytes": 159,
      "content_hash": "sha256:cf8b5559811951186df489fbafc7e559aa3c2496dca5ec4f85da8c8ed89c153c",
      "storage_key": "fixture-objects/F5",
      "creator_ref": {
        "kind": "task",
        "task_id": "T3",
        "attempt_id": "AT3",
        "action_id": "X5"
      },
      "input_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X5"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactRecord",
      "created_at": "2026-11-15T10:03:58Z",
      "artifact_id": "F6",
      "logical_name": "root-probe.md",
      "role": "root_probe",
      "media_type": "text/markdown",
      "size_bytes": 192,
      "content_hash": "sha256:282fbdb2b37582209298dc46046a4abf2339c6d40d3b39edd84293cbb8820d76",
      "storage_key": "fixture-objects/F6",
      "creator_ref": {
        "kind": "task",
        "task_id": "T0",
        "attempt_id": "AT0",
        "action_id": "X6"
      },
      "input_refs": [
        {
          "artifact_id": "F4",
          "content_hash": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "artifact_id": "FL",
          "content_hash": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        },
        {
          "artifact_id": "F5",
          "content_hash": "sha256:cf8b5559811951186df489fbafc7e559aa3c2496dca5ec4f85da8c8ed89c153c"
        }
      ],
      "source_refs": [
        {
          "kind": "receipt",
          "id": "RC-X6"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      }
    }
  ],
  "artifact_grants": [
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:00Z",
      "grant_id": "G-T0-F0",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F0"
      ],
      "task_ids": [
        "T0"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:00Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:00Z",
      "grant_id": "G-T0-FL",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "FL"
      ],
      "task_ids": [
        "T0"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:03:38Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:03:38Z",
      "grant_id": "G-T0-F4",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F4"
      ],
      "task_ids": [
        "T0"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:03:43Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:03:43Z",
      "grant_id": "G-T0-F5",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F5"
      ],
      "task_ids": [
        "T0"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:03:58Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:03:58Z",
      "grant_id": "G-T0-F6",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F6"
      ],
      "task_ids": [
        "T0"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T1-F0",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F0"
      ],
      "task_ids": [
        "T1"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T1-FL",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "FL"
      ],
      "task_ids": [
        "T1"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:55Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:55Z",
      "grant_id": "G-T1-F1",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F1"
      ],
      "task_ids": [
        "T1"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T2-F0",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F0"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T2-FL",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "FL"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:55Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:55Z",
      "grant_id": "G-T2-F1",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F1"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:01:10Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:01:10Z",
      "grant_id": "G-T2-F2",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F2"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:01:25Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:01:25Z",
      "grant_id": "G-T2-FS",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "FS"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:01:25Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:01:25Z",
      "grant_id": "G-T2-F3",
      "subject": {
        "kind": "profile",
        "id": "profile-wei"
      },
      "artifact_ids": [
        "F3"
      ],
      "task_ids": [
        "T2"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T3-F0",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F0"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:00:35Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:00:35Z",
      "grant_id": "G-T3-FL",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "FL"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:01:10Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:01:10Z",
      "grant_id": "G-T3-F2",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F2"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:01:25Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:01:25Z",
      "grant_id": "G-T3-F3",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F3"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:03:38Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:03:38Z",
      "grant_id": "G-T3-F4",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F4"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    },
    {
      "schema_version": 1,
      "record_type": "ArtifactGrant",
      "created_at": "2026-11-15T10:03:43Z",
      "revision": 1,
      "updated_at": "2026-11-15T10:03:43Z",
      "grant_id": "G-T3-F5",
      "subject": {
        "kind": "profile",
        "id": "profile-env"
      },
      "artifact_ids": [
        "F5"
      ],
      "task_ids": [
        "T3"
      ],
      "project_id": "project-A",
      "operations": [
        "read",
        "materialize"
      ],
      "authority_ref": "POL-1",
      "expires_at": "2026-11-15T10:10:00Z",
      "status": "active"
    }
  ]
}
```

## 8. 人工请求、回复与动作

H-approval 在 10:03:35 收到 allow；该时刻 X4 才经过重新授权检查、申请 LT4 并执行。answered、allow、succeeded、passed 分别由四种不同记录表达。若用户答 deny，H-approval 也会 answered，但 X4 必须取消或保持未执行，不能产生成功回执。
```json
{
  "human_requests": [
    {
      "schema_version": 1,
      "record_type": "HumanRequest",
      "created_at": "2026-11-15T10:00:10Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:00:30Z",
      "request_id": "H-question",
      "task_id": "T0",
      "attempt_id": "AT0",
      "kind": "question",
      "prompt": "VS Code 中具体是哪一个启动条目失败？",
      "choices": [
        "Run A",
        "其他条目"
      ],
      "requested_from": {
        "kind": "user",
        "id": "user-1"
      },
      "action_id": null,
      "action_digest": null,
      "status": "answered",
      "expires_at": "2026-11-15T10:10:00Z",
      "reply_id": "RP-question",
      "continuation": {
        "kind": "answer_question",
        "tool_call_id": "TC-question"
      }
    },
    {
      "schema_version": 1,
      "record_type": "HumanRequest",
      "created_at": "2026-11-15T10:01:35Z",
      "revision": 2,
      "updated_at": "2026-11-15T10:03:35Z",
      "request_id": "H-approval",
      "task_id": "T3",
      "attempt_id": "AT3",
      "kind": "approval",
      "prompt": "是否将 F2 应用于 A 的启动配置，只把解释器从 system-python 改为 .venv/bin/python？不修改依赖和项目外配置。",
      "choices": [
        "批准",
        "拒绝"
      ],
      "requested_from": {
        "kind": "user",
        "id": "user-1"
      },
      "action_id": "X4",
      "action_digest": "sha256:747a092651bab889c8201bef807c9eb59fd0b9ba26a88d8e3bfdcf24b8fe1471",
      "status": "answered",
      "expires_at": "2026-11-15T10:10:00Z",
      "reply_id": "RP-approval",
      "continuation": {
        "kind": "authorize_action",
        "tool_call_id": "TC-X4"
      }
    }
  ],
  "human_replies": [
    {
      "schema_version": 1,
      "record_type": "HumanReply",
      "created_at": "2026-11-15T10:00:30Z",
      "reply_id": "RP-question",
      "request_id": "H-question",
      "actor": {
        "kind": "user",
        "id": "user-1"
      },
      "kind": "clarification",
      "answer": "Run A",
      "decision": null,
      "replied_at": "2026-11-15T10:00:30Z"
    },
    {
      "schema_version": 1,
      "record_type": "HumanReply",
      "created_at": "2026-11-15T10:03:35Z",
      "reply_id": "RP-approval",
      "request_id": "H-approval",
      "actor": {
        "kind": "user",
        "id": "user-1"
      },
      "kind": "approval",
      "answer": "仅批准展示的这份修改。",
      "decision": "allow",
      "replied_at": "2026-11-15T10:03:35Z"
    }
  ],
  "actions": [
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:00:42Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:00:55Z",
      "action_id": "X1",
      "task_id": "T1",
      "attempt_id": "AT1",
      "tool_call_id": "TC-X1",
      "operation": "probe_launch",
      "effect": "read",
      "args": {
        "launch_profile": "Run A"
      },
      "args_hash": "sha256:7dc863c5d76cfb7c0ae673830c2367c19730d407ae41f997f3e7717eaf205d00",
      "expected_resources": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:3590107a06073c3f15a2725a7bf03395b5534815f7858e9e5b5afd9b2485fb01",
      "authorization_ref": "AUTH-X1",
      "approval_request_id": null,
      "status": "succeeded",
      "execution_run_id": "RUN1",
      "lease_id": "LT1",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X1"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:01:04Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:10Z",
      "action_id": "X2",
      "task_id": "T2",
      "attempt_id": "AT2",
      "tool_call_id": "TC-X2",
      "operation": "write_patch",
      "effect": "private_write",
      "args": {
        "input_artifact_ids": [
          "F0",
          "F1"
        ],
        "output_artifact_id": "F2"
      },
      "args_hash": "sha256:742110244aa04b73f464c27beb6d9270af4228827e540d6e10b5c462c99bb453",
      "expected_resources": [],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:b1bc50c92626823ff621eb8f7f909a7017566504682e8373ea126f7384d73fbc",
      "authorization_ref": "AUTH-X2",
      "approval_request_id": null,
      "status": "succeeded",
      "execution_run_id": "RUN2",
      "lease_id": "LT2",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X2"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:01:10Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:01:25Z",
      "action_id": "X3",
      "task_id": "T2",
      "attempt_id": "AT2",
      "tool_call_id": "TC-X3",
      "operation": "test_snapshot",
      "effect": "private_write",
      "args": {
        "baseline_artifact_id": "F0",
        "patch_artifact_id": "F2",
        "launch_profile": "Run A"
      },
      "args_hash": "sha256:19ab4f8cd538a55e08a657f10a089fd75789293a4592bd9268142f3105e88f13",
      "expected_resources": [
        {
          "resource_id": "RESOURCE-staging-config",
          "state": "present",
          "version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "resource_id": "RESOURCE-staging-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:c36af7f3a5d8afa12a6163d3f58d96690a283ea8d05199e9fe20a365e501c165",
      "authorization_ref": "AUTH-X3",
      "approval_request_id": null,
      "status": "succeeded",
      "execution_run_id": "RUN2",
      "lease_id": "LT3",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X3"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:01:34Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:38Z",
      "action_id": "X4",
      "task_id": "T3",
      "attempt_id": "AT3",
      "tool_call_id": "TC-X4",
      "operation": "apply_patch",
      "effect": "external_write",
      "args": {
        "resource_id": "RESOURCE-config",
        "patch_artifact_id": "F2",
        "patch_hash": "sha256:d205ddd99a8c24d9ec6bcb44016eeacfc28d45447f253f589a08e94a4b738e42"
      },
      "args_hash": "sha256:1704e8c0ecd7501e8d5fa4020e3c804cf45fbffe2d3d9cddacb710a4be16d0cc",
      "expected_resources": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:7bf7bab7275e66ee39c27b667a572f739bc4882ac5dba609c721c862beade34f"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:747a092651bab889c8201bef807c9eb59fd0b9ba26a88d8e3bfdcf24b8fe1471",
      "authorization_ref": "AUTH-X4",
      "approval_request_id": "H-approval",
      "status": "succeeded",
      "execution_run_id": "RUN3b",
      "lease_id": "LT4",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X4"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:03:38Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:43Z",
      "action_id": "X5",
      "task_id": "T3",
      "attempt_id": "AT3",
      "tool_call_id": null,
      "operation": "verify_target",
      "effect": "read",
      "args": {
        "launch_profile": "Run A"
      },
      "args_hash": "sha256:7dc863c5d76cfb7c0ae673830c2367c19730d407ae41f997f3e7717eaf205d00",
      "expected_resources": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:786f6b14a8472bfcdc4b66ddc50be6877f19835b66de0e6bfc590ab76bd4a029",
      "authorization_ref": "AUTH-X5",
      "approval_request_id": null,
      "status": "succeeded",
      "execution_run_id": "RUN3b",
      "lease_id": "LT5",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X5"
      },
      "reason": null
    },
    {
      "schema_version": 1,
      "record_type": "ActionRecord",
      "created_at": "2026-11-15T10:03:50Z",
      "revision": 3,
      "updated_at": "2026-11-15T10:03:58Z",
      "action_id": "X6",
      "task_id": "T0",
      "attempt_id": "AT0",
      "tool_call_id": null,
      "operation": "evaluate_goal",
      "effect": "read",
      "args": {
        "launch_profile": "Run A",
        "spec_id": "S0",
        "spec_version": 1
      },
      "args_hash": "sha256:5855c726129f23c5e5fb19bff6561260fdeca45c3f22da6dcff0303318c0a382",
      "expected_resources": [
        {
          "resource_id": "RESOURCE-config",
          "state": "present",
          "version": "sha256:3f9191f8590651bf2a8507fb665b22111d46a406c102f0ea5b55c5662a44e701"
        },
        {
          "resource_id": "RESOURCE-lock",
          "state": "present",
          "version": "sha256:8f64cce20f967b96250e31ca3494eae4c38727b8b39919d778a99b8135371caf"
        }
      ],
      "scope": {
        "project_id": "project-A",
        "root_task_id": "T0"
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "intent_digest": "sha256:c38482a145abc37377da61656333d574f1c5893dfa1b27af6945305a1fb81c7f",
      "authorization_ref": "AUTH-X6",
      "approval_request_id": null,
      "status": "succeeded",
      "execution_run_id": "RUN0c",
      "lease_id": "LT6",
      "receipt_ref": {
        "kind": "receipt",
        "id": "RC-X6"
      },
      "reason": null
    }
  ]
}
```

## 9. 等待检查点与结果投递

CP0children 的 delivered=false 是 10:00:40 的历史快照；delivery_receipts 是 10:03:45 的投递证据。恢复读取 checkpoint 后还要合并其后已提交事件，不能仅重放旧快照。下列 history/context 条目是示例辅助目录格式；生产恢复必须保存完整消息协议状态，不能只保存这些摘要。
```json
{
  "checkpoints": [
    {
      "schema_version": 1,
      "record_type": "Checkpoint",
      "created_at": "2026-11-15T10:00:10Z",
      "checkpoint_id": "CP0q",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0a",
      "context_ref": "CTX-T0-initial",
      "history_ref": "HIST0q",
      "resume_cursor": {
        "step": 0,
        "phase": "before_action",
        "pending_action_ids": [],
        "pending_tool_call_ids": [
          "TC-question"
        ],
        "observed_tool_call_ids": []
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "budget_scope_id": "B0",
      "acceptance_failures": 0,
      "loop_state": {
        "repetitions": []
      },
      "applied_message_ids": [
        "MSG0"
      ],
      "wait_ref": {
        "kind": "human",
        "request_id": "H-question"
      },
      "pending_delegations": [],
      "completed_action_ids": []
    },
    {
      "schema_version": 1,
      "record_type": "Checkpoint",
      "created_at": "2026-11-15T10:00:40Z",
      "checkpoint_id": "CP0children",
      "task_id": "T0",
      "attempt_id": "AT0",
      "run_id": "RUN0b",
      "context_ref": "CTX-T0",
      "history_ref": "HIST0children",
      "resume_cursor": {
        "step": 2,
        "phase": "before_model",
        "pending_action_ids": [],
        "pending_tool_call_ids": [],
        "observed_tool_call_ids": [
          "TC-question"
        ]
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "budget_scope_id": "B0",
      "acceptance_failures": 0,
      "loop_state": {
        "repetitions": []
      },
      "applied_message_ids": [
        "MSG0",
        "MSG-reply-question"
      ],
      "wait_ref": {
        "kind": "children",
        "child_task_ids": [
          "T1",
          "T2",
          "T3"
        ]
      },
      "pending_delegations": [
        {
          "child_task_id": "T1",
          "tool_call_id": null,
          "delivery_key": "DEL-T1",
          "delivered": false
        },
        {
          "child_task_id": "T2",
          "tool_call_id": null,
          "delivery_key": "DEL-T2",
          "delivered": false
        },
        {
          "child_task_id": "T3",
          "tool_call_id": null,
          "delivery_key": "DEL-T3",
          "delivered": false
        }
      ],
      "completed_action_ids": []
    },
    {
      "schema_version": 1,
      "record_type": "Checkpoint",
      "created_at": "2026-11-15T10:01:35Z",
      "checkpoint_id": "CP3approval",
      "task_id": "T3",
      "attempt_id": "AT3",
      "run_id": "RUN3a",
      "context_ref": "CTX-T3",
      "history_ref": "HIST3approval",
      "resume_cursor": {
        "step": 0,
        "phase": "before_action",
        "pending_action_ids": [
          "X4"
        ],
        "pending_tool_call_ids": [
          "TC-X4"
        ],
        "observed_tool_call_ids": []
      },
      "constraints_ref": {
        "id": "CON-1",
        "version": 1
      },
      "budget_scope_id": "B3",
      "acceptance_failures": 0,
      "loop_state": {
        "repetitions": []
      },
      "applied_message_ids": [],
      "wait_ref": {
        "kind": "human",
        "request_id": "H-approval"
      },
      "pending_delegations": [],
      "completed_action_ids": []
    }
  ],
  "delivery_receipts": [
    {
      "delivery_key": "DEL-T1",
      "child_task_id": "T1",
      "parent_task_id": "T0",
      "attempt_id": "AT0",
      "delivered_at": "2026-11-15T10:03:45Z",
      "report_id": "R1"
    },
    {
      "delivery_key": "DEL-T2",
      "child_task_id": "T2",
      "parent_task_id": "T0",
      "attempt_id": "AT0",
      "delivered_at": "2026-11-15T10:03:45Z",
      "report_id": "R2"
    },
    {
      "delivery_key": "DEL-T3",
      "child_task_id": "T3",
      "parent_task_id": "T0",
      "attempt_id": "AT0",
      "delivered_at": "2026-11-15T10:03:45Z",
      "report_id": "R3"
    }
  ]
}
```

## 10. Task 状态轨迹

每行只展示 Task 的状态事务；对象内容补全与该次状态更新一起提交。revision 从 created 的 1 递增。未具备的未来输入在创建时为空/仅登记逻辑需求，生产后才添加真实 ArtifactRef，本文最终快照不表示一开始已拥有这些工件。

| Task | 时间 | revision | 状态 |
|---|---|---|---|
| T0 | 2026-11-15T10:00:00Z | 1 | created |
| T0 | 2026-11-15T10:00:00Z | 2 | ready |
| T0 | 2026-11-15T10:00:00Z | 3 | running |
| T0 | 2026-11-15T10:00:10Z | 4 | waiting_user |
| T0 | 2026-11-15T10:00:30Z | 5 | ready |
| T0 | 2026-11-15T10:00:30Z | 6 | running |
| T0 | 2026-11-15T10:00:40Z | 7 | waiting_agent |
| T0 | 2026-11-15T10:03:45Z | 8 | ready |
| T0 | 2026-11-15T10:03:45Z | 9 | running |
| T0 | 2026-11-15T10:03:58Z | 10 | verifying |
| T0 | 2026-11-15T10:04:00Z | 11 | completed |
| T1 | 2026-11-15T10:00:35Z | 1 | created |
| T1 | 2026-11-15T10:00:35Z | 2 | ready |
| T1 | 2026-11-15T10:00:40Z | 3 | running |
| T1 | 2026-11-15T10:00:55Z | 4 | verifying |
| T1 | 2026-11-15T10:01:00Z | 5 | completed |
| T2 | 2026-11-15T10:00:35Z | 1 | created |
| T2 | 2026-11-15T10:00:35Z | 2 | blocked |
| T2 | 2026-11-15T10:01:00Z | 3 | ready |
| T2 | 2026-11-15T10:01:00Z | 4 | running |
| T2 | 2026-11-15T10:01:25Z | 5 | verifying |
| T2 | 2026-11-15T10:01:30Z | 6 | completed |
| T3 | 2026-11-15T10:00:35Z | 1 | created |
| T3 | 2026-11-15T10:00:35Z | 2 | blocked |
| T3 | 2026-11-15T10:01:30Z | 3 | ready |
| T3 | 2026-11-15T10:01:30Z | 4 | running |
| T3 | 2026-11-15T10:01:35Z | 5 | waiting_user |
| T3 | 2026-11-15T10:03:35Z | 6 | ready |
| T3 | 2026-11-15T10:03:35Z | 7 | running |
| T3 | 2026-11-15T10:03:44Z | 8 | verifying |
| T3 | 2026-11-15T10:03:45Z | 9 | completed |

## 11. 可手算核对的结果与反例

| 项目 | 示例值 |
|---|---|
| root 题目数 / 通过数 | 1 / 1（仅示例数据，不是性能结论） |
| 逻辑子任务数 / 通过数 | 3 / 3；run/attempt 不重复算任务 |
| 实际 token | T0=2000，T1=1800，T2=2400，T3=1400；root 子树合计 7600 |
| 根 token 剩余 | 12000−7600=4400；所有 lease 已结算，无未释放预留 |
| 模型 / 工具请求 | 7 / 6；root 仍受 12 / 20 的总上限约束 |
| 根活跃时间 / 端到端时间 | 100 秒 / 240 秒 |
| 人工等待 | 20+120=140 秒；绝对 expires_at=10:10:00Z 未过期 |
| 重启后的身份 | T0/AT0、T3/AT3 不变；新建 RUN3b、RUN0c |

替代路径必须遵守主契约：

- 拒绝：回复 decision=deny，请求仍 answered；原动作不执行。若无法完成目标，明确结案，不能保留本例的成功回执/报告。
- 文件版本变化：原 H-approval 不能授权不同动作意图；核对变更后新建 Action/Request 或使用已覆盖的新授权。
- 目标探针失败：R3/R0 不能 passed；预算允许可在同 attempt 修正，否则 failed/unverified 分别记录。
- 10:10:00Z 后才回答：请求过期，不恢复执行；不能以新 run 重置期限。
- 修改成功但回执丢失：X4=unknown，LT4 保留预留，相关任务阻塞；先核实目标配置，再结算和续接，不直接重复修改。

本示例校验的是文档字段、引用、hash、状态和账目的一致性。真实协议兼容、执行隔离、工具行为、持久化恢复和验收正确性仍需后续实现并测试。
