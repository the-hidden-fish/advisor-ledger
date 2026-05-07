# Advisor Ledger(学术黑榜镜像)

**新：可快速手动添加、只能添加不能删除的、AI自动总结的红黑榜: https://append.page/p/advisors**

**新：openreview版本黑榜：https://openadvisor.pages.dev/**

只增不减地镜像社区维护的"学术黑榜 / Advisor Red Flags Notes" Google Doc,记录每一次编辑,保留每一次删除。

**渲染后的实时视图**:https://the-hidden-fish.github.io/advisor-ledger/

## 做什么 / 为什么

原文档是匿名可编辑的,也就是说实质性的观察可能被悄悄删掉。本仓库每隔几分钟抓一次原文档并把结果提交到 git,这样编辑历史——包括被撤回或被覆盖的内容——都保留下来。

`main` 分支上的每一个 commit 对应原文档的一次真实变更。

## 目录结构

| 路径 | 用途 |
|---|---|
| `snapshots/YYYY/MM/DD/<source>/*.json` | 每次抓取的完整 `documents.get` JSON |
| `snapshots/.../*.txt` | 纯文本导出 |
| `snapshots/.../*.meta.json` | Drive 元信息 + 抓取内容的 SHA-256 |
| `deltas/.../*.delta.json` | 相对上一次快照的结构化差异(按段落的 insert / delete / replace) |
| `reviews/.../*.review.json` | 每次 diff 的本地 LLM 审查结果,标注可能的人肉信息、纯人身攻击、可疑删除。**只做提示,不会阻塞 commit** |
| `docs/index.html` | 渲染视图:当前文本,被删段落原地保留(删除线 + 删除时间戳),新增段落高亮。由 GitHub Pages 提供 |
| `scripts/` | 流水线:fetch → normalize → diff → review → render → commit → push |

## 流水线

由 systemd timer 每 2 分钟触发:

1. 查询 Drive 的 `modifiedTime`,如果自上次快照以来没变化,直接短路退出。
2. 抓取结构化 JSON 和纯文本导出。
3. 把段落规范化成确定性、便于 diff 的形式(NFC Unicode、按行 rstrip、每段生成内容哈希)。
4. 对比新旧规范化快照,按段落内容哈希生成操作,让真正没变的段落不算 churn。
5. 对本次 delta 跑一次本地 LLM 审查,标三类问题:对私人的身份信息(PII)、纯人身攻击(不是对具体行为的批评)、看起来像压制性删除的改动。审查结果以 JSON 写在 delta 旁边。
6. 重新渲染 `docs/index.html`——当前文本加上按最后已知位置锚定的 ghost 段落。
7. `git add` 新快照、delta、review、渲染产物;commit;push。

流水线用 `flock` 保护,防止手动触发和 timer 触发撞车。

## 关于原文档

本仓库是**观察性镜像**。不代表原文档中被点名的任何一方,也不由其制作、背书或审核。`snapshots/` 和 `docs/` 里的内容归原匿名贡献者所有。要补充、更正或撤回,请直接编辑原 Google Doc——本仓库只观察。

## 许可证

流水线代码(`scripts/`)以公有领域(CC0)发布。`snapshots/`、`deltas/`、`docs/` 中被镜像的内容保留原作者权利。
