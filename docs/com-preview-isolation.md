---
feature_ids:
  - preview-com-isolation-v1.0.15
topics:
  - powerpoint
  - com
  - preview
  - isolation
  - performance
doc_kind: architecture-decision
created: 2026-07-16
---

# PowerPoint COM 预览隔离决策

## 结论

PowerPoint 是 Multiuse（单实例）COM 服务。`DispatchEx` 可以创建新的 COM 代理，
但不能保证创建第二个 `POWERPNT.EXE`。在同一 Windows 用户会话里，没有受支持的
PowerPoint `/x` 独立实例开关。因此产品不把“多个 COM 代理”冒充“多个渲染引擎”。

v1.0.15 固定采用单 COM 通道：

1. 没有 PowerPoint 时，创建一个可证明 PID 所有权的隐藏实例；只打开本工具的只读快照。
2. 用户 PowerPoint 已打开时，前台预览可借用其 Application，但只打开
   `ReadOnly=1, WithWindow=0` 的快照；不改变 `Visible`、活动文稿或全局设置。
3. 后台预热不得借用用户会话；相邻页预取只能复用前台预览已经验证的快照。
4. 清理只关闭本工具持有的 Presentation。借用模式永不调用 `Application.Quit`，
   永不终止用户 PowerPoint PID。
5. 空闲后释放 PowerPoint/COM，但保留阻塞在本地 socket 上的渲染服务进程；它不轮询，
   实测空闲 CPU 为 0%，约占 29 MB，下一次请求免去子进程重启与握手。

## 为什么不做多 COM 并发

本机 Office `16.0.20131.20126`，固定使用同一个 8 MB / 40 页文稿、每组导出 12 页：

| COM 客户端 | PowerPoint 进程 | 导出墙钟时间（复测） | PowerPoint CPU | 峰值内存 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1 | 628 ms | 531 ms | 197 MB |
| 2 | 1 | 721 ms | 656 ms | 200 MB |
| 3 | 1 | 668 ms | 688 ms | 204 MB |

首次测试中三客户端为 866 ms。两轮都没有吞吐收益；多客户端只是在同一 Office
STA 服务上增加争用、CPU 与内存，因此并发数硬限制为 1。

表中的 628 ms 是“一次连续导出 12 页”的墙钟时间；下文 IPC 的 628 ms 是另一组
“启动源代码渲染子进程并导出首张单页”的端到端结果，数值碰巧相同，不能互相换算。

## 借用用户会话的安全门

快照打开后必须同时满足：

- `Presentation.ReadOnly != 0`；
- `Presentation.Windows.Count == 0`；
- `Presentation.FullName` 等于预期快照路径；
- `Application.Windows.Count` 与打开前一致；
- 打开前存在活动文稿时，打开后活动文稿路径不变。

任一条件无法证明或不满足，立即关闭该快照并返回渲染失败。关闭暂时被 COM 拒绝时，
保留精确对象引用供下一轮重试，不遗忘、不扩大清理范围。

## 真机共存结果

可见用户文稿保持打开时，产品完整渲染路径首张 1600px 图片约 251–315 ms，
复用同一快照导出相邻页约 49–116 ms。渲染前、中、后用户可见窗口始终只有原文稿，
活动文稿和 Saved 状态不变，快照窗口数始终为 0，收尾后 PowerPoint 文稿数恢复原值。

IPC 模式首张约 628 ms；释放 COM 后第二次约 366 ms，复用同一渲染子进程，节省约
262 ms。子进程空闲 2.1 秒期间 CPU 采样为 0%，RSS 29 MB；应用关闭后子进程退出。

## 外部依据

- Microsoft：PowerPoint 属于 Multiuse（Single Instance）COM 服务：
  https://learn.microsoft.com/en-us/previous-versions/office/troubleshoot/office-developer/use-visual-c-automate-run-program-instance
- Microsoft：`Presentations.Open` 的 `WithWindow=msoFalse` 可隐藏演示文稿窗口：
  https://learn.microsoft.com/en-us/office/vba/api/powerpoint.presentations.open
- Microsoft：PowerPoint 支持 `/EMBEDDING` 隐藏启动，但不提供 `/x` 独立实例开关：
  https://support.microsoft.com/en-US/Office/lifecycle/command-line-switches-for-microsoft-office-products
