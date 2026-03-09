# qzcli - 启智平台核心 CLI

本版本为精简版，核心命令如下：
- `login`
- `logout`
- `workspace-list`
- `workspace-usage`
- `project-list`
- `project-usage`
- `user-jobs`
- `project-user-usage`
- `train-suggest`

## 安装依赖

```bash
pip install -r requirements.txt
pip install -e .
```

若希望 `user-jobs -u zhangsan` 匹配中文用户名（如 `张三`），请确保安装了 `pypinyin`（已在 `requirements.txt` 中声明）。

## 自动登录（.env）

`qzcli login` 成功后会自动创建/覆盖 `.env` 并写入凭据。  
当 cookie 过期时，命令会自动尝试读取 `.env` 并刷新 cookie。  
支持以下字段（任一组即可）：

```bash
u=你的学工号
p=你的密码

# 或
QZCLI_USERNAME=你的学工号
QZCLI_PASSWORD=你的密码
```

`qzcli logout` 会删除 `.env` 并清理本地 cookie。

## 快速开始

```bash
# 1) 登录（自动获取并保存 cookie）
qzcli login

# 2) 查看工作空间占用率（不指定 -w 时默认统计全部 workspace）
qzcli workspace-usage -w ws-xxx
qzcli workspace-usage

# 可访问 workspace 列表
qzcli workspace-list

# 3) 查看项目利用率（不指定 -p 时默认显示全部项目）
qzcli project-usage -w ws-xxx -p project-xxx
qzcli project-usage -w ws-xxx

# 项目列表（默认全部 workspace）
qzcli project-list
qzcli project-list -w ws-xxx -n 20

# 用户任务（默认自己；建议显式传 -u）
qzcli user-jobs
qzcli user-jobs -u 张三 -w ws-xxx
qzcli user-jobs -u zhangsan -w ws-xxx

# 项目用户占用 + 该项目全部 job
qzcli project-user-usage -p project-xxx -w ws-xxx

# 4) 获取多节点训练建议（默认 8 节点）
qzcli train-suggest -n 8
```

## 优先级约定

- 低优先级：`1-4`
- 普通优先级：`5`
- 高优先级：`6-10`

## 命令说明

### 1) 登录

```bash
qzcli login
qzcli login -u 学工号 -p 密码 -w ws-xxx
```

### 2) 登出

```bash
qzcli logout
```

### 3) 工作空间占用率

输出核心指标：
- 运行任务数
- GPU 占用与利用率
- 节点占用率
- 高优任务 GPU 占用

```bash
qzcli workspace-usage -w ws-xxx
qzcli workspace-usage -w ws-xxx --min-priority 6
```

### 4) 项目利用率

输出核心指标：
- 项目任务数（按优先级过滤）
- 项目 GPU 占用
- 工作空间 GPU 占用
- 项目占比（项目 GPU / 工作空间 GPU）
- 项目任务明细
- 每个 job 的实时利用率：`GPU / CPU / MEM`

```bash
qzcli project-usage -w ws-xxx -p project-xxx
qzcli project-usage -w ws-xxx -p dis
qzcli project-usage -w ws-xxx -p project-xxx --min-priority 6 -n 20
```

### 5) 用户任务

输出：
- 指定用户（默认自己）的 job 列表
- 每个 job 的 `GPU / CPU / MEM` 利用率

```bash
qzcli user-jobs
qzcli user-jobs -u 张三
qzcli user-jobs -u zhangsan
qzcli user-jobs -u 张三 -w ws-xxx -n 20
```

### 6) 项目用户占用

输出：
- 项目内按用户的 GPU 占用汇总
- 该项目全部 job 列表
- 每个 job 的 `GPU / CPU / MEM` 利用率

```bash
qzcli project-user-usage -p project-xxx -w ws-xxx
qzcli project-user-usage -p dis --min-priority 1
```

### 7) 多节点训练建议

排序依据：
- 是否满足目标节点数
- 可用节点数（空闲节点 + 可回收低优节点）
- 高优排队压力

```bash
# 全工作空间推荐
qzcli train-suggest -n 8

# 指定工作空间
qzcli train-suggest -w ws-xxx -n 8

# 不计低优可回收
qzcli train-suggest -n 8 --no-low-priority
```

## 帮助

```bash
qzcli --help
qzcli workspace-usage --help
qzcli project-usage --help
qzcli user-jobs --help
qzcli project-user-usage --help
qzcli train-suggest --help
```
