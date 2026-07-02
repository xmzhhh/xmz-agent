# Git 与 GitHub 学习路线

## 基本约定

- `main` 始终保持可运行。
- 一个 Issue 对应一个清晰任务，一个分支解决一类问题。
- 分支格式：`feat/xxx`、`fix/xxx`、`docs/xxx`、`refactor/xxx`。
- 提交格式使用简化 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。
- 不提交 `.env`、API Key、真实持仓、数据库、模型权重、缓存和大体积原始数据。

## 分阶段掌握

### 第 0～1 阶段：本地 Git

理解四个位置：工作区、暂存区、本地仓库、远程仓库。熟悉：

```powershell
git status
git diff
git add <file>
git commit -m "docs: add project roadmap"
git log --oneline --graph
```

不要机械背命令。每次操作前先预测文件会从哪个位置移动到哪个位置，再用 `git status` 验证。

### 第 2～3 阶段：分支与 GitHub

学习创建分支、推送、Issue、Pull Request、代码评审和合并。即使是个人项目，也在 PR 中写清：

- 为什么改；
- 改了什么；
- 如何测试；
- 截图或演示；
- 风险与后续工作。

### 第 4～6 阶段：冲突、回退与发布

在练习分支中主动制造一次小冲突并解决；掌握安全的 `revert`；使用语义化版本标签，如 `v0.4.0`。

### 第 7～9 阶段：协作与自动化

加入 Issue/PR 模板、保护 `main`、GitHub Actions、自动测试、Release Notes 和依赖更新。

## 每阶段标准流程

1. 建立里程碑和 Issue。
2. 从最新 `main` 创建功能分支。
3. 小步实现和测试，保持原子提交。
4. 自己审查 `git diff`。
5. 推送并建立 PR。
6. CI 通过后合并。
7. 更新学习日记；阶段完成时打标签。

## 常见危险操作

初学阶段不要在没理解影响时使用 `git reset --hard`、强制推送或大范围删除。遇到误操作先停下，保存 `git status` 和 `git log --oneline --all --graph` 的结果，再分析恢复方案。

