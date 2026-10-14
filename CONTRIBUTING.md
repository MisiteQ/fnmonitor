# 开发与同步规范

| 角色 | 仓库 | 职责 |
|---|---|---|
| 上游（原作） | [MisiteQ/fnmonitor](https://github.com/MisiteQ/fnmonitor) | 原作者 Misite齊。**唯一的发布方**：Releases、应用内在线更新、应用源 [MisiteQ/FnDepot](https://github.com/MisiteQ/FnDepot) 都由上游负责 |
| fork | [iqiuzixuan/fnmonitor](https://github.com/iqiuzixuan/fnmonitor) | 只用于开发：开发新功能和修复，通过 PR 合并到上游。**不发布版本，不提供应用源** |

既然只有上游发布，fork 里的代码和上游完全一致，不需要保留任何 fork 自己的仓库地址、发行方等身份信息。贡献者的署名见第 1 节。

---

## 1. 许可与署名

项目使用 MIT 许可。上游合并 fork 的代码时，只需要保留版权和许可声明：

- **`LICENSE`** 的版权行：
  ```
  Copyright (c) 2026 Misite齊
  Copyright (c) 2026 iqiuzixuan
  ```
- **README 作者栏**：`作者：Misite齊 · 贡献者：iqiuzixuan（新界面、虚拟机、安全加固、回归测试）`，新的贡献者依次追加
- **应用内「关于」页**（`ui-src/47-settings.js` 的 `renderAbout`）：链接全部指向上游，另起一行写 `界面与功能贡献：iqiuzixuan`。
- **通过 PR 合并**。这样 git 会保留提交作者。确实需要手动搬代码时，在提交信息末尾加上 `Co-authored-by: iqiuzixuan <iqiuzixuan@gmail.com>`。

## 2. 发布与版本号

- **版本号由上游决定**。PR 里不要修改 `manifest` 的 `version` / `changelog`、README 的版本徽章、「当前版本」和版本历史表，这些由作者发版时修改。建议的更新日志文案写在 PR 描述里，作者发版时贴进去即可。
- **fork 不推版本标签，不建 Release，不修改 `fnpack.json`。**
- 2.18.0 – 2.19.0 曾由 fork 单独发布过，上游不再使用这几个版本号（合并后的首个版本为 2.20.0 或更高）。

## 3. 日常开发与同步

### 3.1 远程仓库

```bash
git remote add upstream https://github.com/MisiteQ/fnmonitor
```

### 3.2 开发一个功能

```bash
git fetch upstream
git switch -c feat/<主题> upstream/main     # 一律从上游最新代码开分支
# 开发 → 在飞牛上测试（第 4 节）→ 提交
git push origin feat/<主题>
gh pr create --repo MisiteQ/fnmonitor --base main --head iqiuzixuan:feat/<主题>
```

- 一个 PR 只做一件事。
- PR 描述写清楚三点：改了什么、在什么机型上怎么验证的、建议的更新日志文案。
- 涉及写操作（启停、关机等）或改变产品定位的功能，先开 issue 和作者商量再写代码。

### 3.3 PR 合并后同步

```bash
git fetch upstream
git switch main && git merge --ff-only upstream/main && git push origin main
git branch -d feat/<主题>
```

### 3.4 上游直接修改了 `index.html`

如果作者没有通过 `ui-src/`，而是直接改了 `app/www/index.html`，CI 的前端检查会失败。需要把对应的改动迁移到 `ui-src/`，重新构建后提一个 PR。

## 4. 在飞牛上测试与自用安装

### 4.1 开发实例：改后端或界面时用

`server.py` 可以脱离应用中心单独运行，它会读取上一级目录里的 `manifest`。把 `app/` 和 `manifest` 按原来的目录结构复制到 NAS，换一个端口和数据目录启动，**已安装的正式版不受影响**：

```bash
# 本机：先构建前端，再同步到 NAS（主机已加入 OneSSH 时，用 OneSSH 的 file_transfer）
sh tools/build_ui.sh
rsync -a --delete app manifest <nas>:/tmp/fnm-dev/

# NAS：用 root 在前台运行（读取 docker.sock、smartctl、virsh 需要 root），Ctrl-C 停止
sudo python3 /tmp/fnm-dev/app/server.py --port 8778 --data-dir /tmp/fnm-dev/var
```

浏览器打开 `http://<NAS_IP>:8778`。

- 开发实例用单独的数据目录，历史数据为空。如果需要历史数据，可以把 `/var/apps/fnmonitor/var/` 复制一份过来。
- 不要在开发实例里点「在线更新」。测试结束后删除 `/tmp/fnm-dev`。

### 4.2 手动打包安装：改了 `cmd/*`、`wizard/`、`config/`、`manifest`，或者要长期自用时

1. 执行 `bash build.sh`（Windows 上用 `.\build.ps1`）。需要先装 [fnpack](https://developer.fnnas.com/docs/cli/fnpack/)。本机没有 fnpack 时，可以在 CI 里加一个只构建、不发布的任务，构建出的 fpk 作为 Actions artifact 下载。
2. 在飞牛「应用中心 → 手动安装」中选择 fpk 安装。
3. 测试升级时，先装当前的正式版，再手动安装测试包覆盖。

**测试包的版本号**：在本地分支上临时改成计划发布的下一个版本，例如当前正式版是 2.20.0，测试包就用 2.20.1。这个改动不要提交到 PR。

- 等正式发布该版本后，在应用中心手动安装官方 fpk，换回正式版。
- 应用内在线更新不会重装同版本，也不会降级。所以正式版和测试包版本号相同时，不会提示更新，只能手动安装。
- 飞牛手动安装能否覆盖同版本或更低的版本，需要在自己的 NAS 上实测一次，然后把结论补充到这里。

## 5. 提交前检查清单

```bash
python -m py_compile app/server.py tools/minify_ui.py
sh tools/build_ui.sh && git diff --exit-code app/www/index.html
python -m unittest discover -s tests -v
shellcheck -S error cmd/* build.sh tools/build_ui.sh
```

PR 额外检查：

- [ ] 分支是从 `upstream/main` 新建的
- [ ] 仓库地址都指向 `MisiteQ/fnmonitor`（`git grep -n github.com/` 检查一遍），fork 地址只出现在署名处
- [ ] 没有改 `fnpack.json`、`version`、`changelog` 和 README 里的版本号
- [ ] 改了接口返回结构时，已执行 `python tests/update_golden.py`，并检查过 `tests/golden/shapes.json` 的变化
- [ ] 在真实的 fnOS 上验证过（x86 / arm 至少一种），并在 PR 里写明

## 6. 代码约定

- **零第三方依赖**：后端只用 Python 标准库，兼容 Python 3.7 及以上（fnOS 自带 3.11）；前端用原生 JS，不引入框架或 CDN。
- **界面只改 `ui-src/`**：`app/www/index.html` 是构建产物。改完执行 `sh tools/build_ui.sh`，再把 `ui-src/` 和 `index.html` 一起提交。
- **生命周期脚本**（`cmd/*`、`build.sh`）必须通过 `shellcheck -S error`。
- **产品边界**：以只读监控为主。
  - 不对硬件做写操作，例如风扇调速（v2.17.1 已移除）。
  - 涉及硬件端口的读取（例如 EC 风扇）默认关闭，并标注为「实验性」。
  - 少数控制类操作（Docker 启停、虚拟机开关机）必须二次确认、可以撤销。
- **安全**：
  - 在线更新必须通过 SHA-256 校验，并拒绝降级。
  - 口令不明文存储，也不回传给前端。
  - 新增的接口要经过跨站请求防护和请求体大小限制。
- **给 AI 的提示**：让 AI 改代码时，把本节直接贴给它，重点强调「界面改 `ui-src/`，改完运行 `sh tools/build_ui.sh`」和「不要改版本号与 `fnpack.json`」。

## 7. 提交信息格式

格式为 `<gitmoji> <type>(<scope>): <中文简述>`。简述写用户能感知到的变化，例如：

```
🐛 fix(docker): 没有容器时不再误报 Docker 不可用
```

| gitmoji | type | 用途 |
|---|---|---|
| ✨ | feat | 新功能 |
| 🐛 | fix | 修复 |
| 🔒 | fix / feat | 安全相关 |
| ⚡ | perf | 性能 |
| ♻️ | refactor | 重构（行为不变） |
| ✅ | test | 测试 |
| 👷 | ci | CI 与工作流 |
| 📝 | docs | 文档 |
| 🔧 | chore | 配置与杂项 |

常用 scope：`ui`、`vm`、`docker`、`gpu`、`history`、`update`、`auth`、`lifecycle`。

提交作者必须设置为关联了 GitHub 账号的身份：

```bash
git config user.name  "<GitHub 用户名>"
git config user.email "<GitHub 账号已验证的邮箱>"
```

