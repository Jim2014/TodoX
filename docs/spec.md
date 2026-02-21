# AI 驱动的项目开发与迭代系统 (TodoX) 需求与架构文档

## 1. 系统定义与目标

TodoX 是一个以 AI 为核心主导、人类管理员（Admin）辅助决策的自动化项目管理与全流程开发系统。
系统名称 TodoX 寓意“赋予任务 (Todo) 无限的执行可能 (X)”。它不仅是一个任务追踪工具，更是一个利用 LLM 作为逻辑引擎、通过文件系统进行状态持久化的“AI 任务操作系统”，覆盖从项目规划、设计、实现、运行到反馈的全生命周期。

## 2. 核心设计哲学

- **叙事性日志 (Narrative Logic)**：这是系统的灵魂。所有的产出物索引、AI 思考过程、错误日志和用户指令均沉淀在 `comments` 数组中。项目不仅是代码的堆砌，更是一段可追溯的叙事史诗。
- **无状态执行 (Stateless Execution)**：AI Worker 每次运行均为独立原子操作，通过读取本地 JSON 状态文件实现逻辑上的“热启动”，保证了系统的轻量化与容错性。
- **权力移交协议 (Hand-off Protocol)**：通过 `assignee` 字段显式转交任务控制权，实现 Admin 与 AI 之间的异步任务接力。
- **扁平化物理存储 (Flat Physical Storage)**：物理路径不随逻辑递归加深（所有任务文件夹平铺在项目 `tasks` 目录下），规避操作系统路径长度限制并简化检索。

## 3. 系统组件与职责分工

### 3.1 主控调度程序 (Orchestrator - Python)

- **状态监听**：持续扫描全局 `projects.json` 及各项目目录。
- **任务触发**：检测到 `assignee == "AI"` 且 `status == "in_progress"` 时，拉起执行单元。
- **提示词管理**：维护种子提示词模板，并在创建项目时初始化项目专属的行为内核。
- **指令中枢**：解析来自 Telegram 的无状态 C&C 指令，原子化更新 JSON 状态。

### 3.2 智能体执行单元 (AI Worker - Gemini CLI)

- **上下文感知**：启动时进入项目目录，读取 `system_prompt.md` 和对应的 `task.json`。
- **自主执行**：编写代码、运行脚本、分析数据，利用 bash 或 code_execution 权限完成既定目标。
- **状态反馈**：退出前更新 JSON 状态，并在 `comments` 中追加操作摘要。

### 3.3 数据存储层 (JSON State Machine)

作为 Orchestrator 和 AI Worker 之间唯一的“共享内存”，通过标准 JSON 维护任务树与生命周期。

## 4. 目录拓扑与命名规范

### 4.1 命名约定 (ID Convention)

- **PID (Project ID)**：项目代号，全局唯一字母代码（如 `SILVER`）。
- **TID (Task ID)**：`PID-N` 格式。项目根节点固定为 `PID-0`。
- **状态文件**：项目根状态文件固定命名为 `project.json`；子任务状态文件固定为 `task.json`。

### 4.2 目录拓扑结构

```text
/TodoX/                        # 主程序代码库 (Git 管理)
├── .env                       # 环境配置 (DATA_DIR, TG_TOKEN, API_KEY)
├── docs/                      # 平台级文档 (architecture.md)
├── orchestrator/              # 调度服务代码
│   └── prompts/               # 种子提示词模板 (default_agent.md)
├── web/                       # Web 可视化面板
└── requirements.txt

/path/to/data/                 # 数据工作区 (独立存储 & Git 备份)
├── projects.json              # 全局项目清单 (PID 映射)
└── projects/                  # 项目存储根目录
    ├── PID/                   # 例如 SILVER/ (项目目录)
    │   ├── project.json       # 项目根状态 (包含 PID-0 信息)
    │   ├── system_prompt.md   # AI 行为内核 (由主程序自动创建)
    │   ├── files/             # 项目级公共产出物
    │   └── tasks/             # 扁平化任务池 (不进行物理嵌套)
    │       └── TID/           # 例如 SILVER-1/
    │           ├── task.json  # 任务描述与状态
    │           └── files/     # 任务级产物 (代码、日志等)
```

## 5. 核心数据协议 (The 9-Field Schema)

每一个 `project.json` 和 `task.json` 必须包含：

- **id**: `PID-N` 唯一标识。
- **status**: `not_started`, `in_progress`, `blocked`, `need_confirm`, `complete`, `failed`。
- **title**: 任务一句话摘要。
- **descriptions**: 详细目标与边界约束。
- **creator**: 发起人 (Admin/AI)。
- **assignee**: 当前负责人 (Admin/AI)。
- **parent_id**: 父节点 TID。
- **child_ids**: 子节点 TID 列表。
- **comments**: 叙事日志数组（含 `author`, `time`, `content`）。

## 6. Telegram 无状态指令集 (C&C Protocol)

主程序通过解析指令原子化更新 JSON，确保在手机端也能快速操控：

| 指令 | 描述 | 逻辑行为 |
| :--- | :--- | :--- |
| `/projects` | 查看大盘 | 展示所有项目的 PID、状态与负责人。 |
| `/p [PID]` | 项目详情 | 查看 PID-0 的描述及核心任务树状态。 |
| `/t [TID]` | 任务详情 | 展示指定任务的叙事历史 (Comments) 与产物索引。 |
| `/new [PID] "[Desc]"` | 新建项目 | 创建目录结构，注入种子提示词，注册项目。 |
| `/comment [TID] "[Msg]"` | 注入指令 | 向任务评论区添加消息，作为 AI 下一次运行的上下文。 |
| `/assign [TID] [Role]` | 权限切换 | 修改 assignee；若转给 AI，自动将状态设为 in_progress。 |

## 7. 进化机制

- **提示词迭代**：Admin 对项目 `system_prompt.md` 的修改会立即改变 AI 行为。
- **基因回馈**：成熟的提示词技巧应手动同步回 `orchestrator/prompts/`，使系统整体“基因”进化。

## 8. 未来演进

- **自动复盘**：项目完成后 AI 根据评论流自动生成开发总结（史记）。
- **可视化时光机**：Web UI 通过 `parent_id` 渲染动态任务树，并支持按时间线回溯。
- **环境隔离**：引入 Docker 环境执行 AI 生成的脚本，确保系统安全性。