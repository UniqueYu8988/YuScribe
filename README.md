# YuScribe · 原稿

**把影音，变成原稿。**

把明确选定的一 P B站视频交付为可追溯的 UTF-8 Markdown。字幕优先；显式允许时获取音轨，在独立 Python 进程中用本地 Qwen3-ASR 转写。没有桌面窗口、HTTP 服务、托盘、自动摘要或侧边栏控制。

本仓库只包含 YuScribe 独立工具，不包含阅读应用界面或接入适配代码。真实验收与限制见 [STATUS.md](STATUS.md)，调用及职责见 [CONTRACT.md](CONTRACT.md)。

## 使用

在本目录运行，Python 3.12；字幕路径只用标准库，不需安装依赖。每个命令返回一行 JSON。`inspect` 查询平台元数据和字幕目录，不启动模型；`status` 仅查本地状态。

```powershell
python -m yuscribe inspect https://www.bilibili.com/video/BV15NJi65E2S --page 1 --allow-proxy-fake-ip
python -m yuscribe submit https://www.bilibili.com/video/BV15NJi65E2S --page 1 --language ai-zh --allow-proxy-fake-ip
python -m yuscribe status <task_id>
python -m yuscribe cancel <task_id>
python -m yuscribe verify <artifact_id>
```

必须显式选择 `--page`；链接带 p 参数时两者必须一致。`--language` 使用 inspect 实际返回的语言代码，例如 `zh-CN`、`ai-zh`，不会猜语言或静默取首条字幕。只接受标准完整链接或 BV 号，不接受短链接、任意网址。

如果本地代理的 DNS 使用 198.18.0.0/15 合成地址，可按需启用对应选项。`--allow-proxy-fake-ip` 是**本地调用方**的网络选项：只额外允许这一范围，仍限固定 API、固定字幕主机及平台返回的受限音轨 CDN，保持主机名 TLS 校验、不跟随跳转、不使用环境代理或 Cookie。默认关闭；RFC1918、回环、链路本地等地址仍拒绝。未来网页不能自行传递网络策略。

## 本地转写

不自动安装、联网下载或更新旧环境。复制 `local.example.json` 为不进版本库的 `local.json`，指定可信本地 Python、qwen-asr 包目录与**已下载完整**的原版 Qwen3-ASR 模型目录。此桥接使用 `qwen-asr` 包后端；新的 `-hf` 权重格式不能直接混用。

转写需自行准备兼容的 Python、PyTorch CUDA、Transformers 与 qwen-asr 环境，以及本地模型权重。已验收组合为 Python 3.12、torch 2.8.0+cu128、transformers 4.57.6、qwen-asr 0.0.6 与原版 Qwen3-ASR-0.6B；不代表其他组合均已验证。建议使用独立环境；新缓存位于 data/cache，工具不会自动安装或升级依赖。

```powershell
# 仅无字幕或明确需要登录时兜底；网络错误、归属错误、语言不匹配不自动转写
python -m yuscribe submit https://www.bilibili.com/video/BV15NJi65E2S --page 1 --language ai-zh --allow-proxy-fake-ip --fallback-asr
# 本地样本只接受用户明确指定文件；此入口不能直接向网页开放
python -m yuscribe local 'C:\path\to\confirmed-sample.wav'
```

串行执行；相同运行请求返回原任务，相同已校验结果可复用。`--reprocess` 创建新任务和新原稿版本，旧稿保留。取消返回 `cancel_requested` 后要再查询到 `cancelled` 才表示实际退出。关闭发起命令或网页不影响已启动的监督进程；该进程只活到本任务结束。

## 保存与边界

- 正式交付：`data/artifacts/<artifact_id>/manifest.json` 明确列出 `transcript.md`、`segments.json`、`source.json`、`raw.json` 和 SHA-256。
- 任务：`data/tasks/<task_id>/`；缓存和未完成切片：`data/cache/<task_id>/`。数据根可通过首个参数 `--data-dir` 更改，只能由可信本地后端配置。
- 原稿是正式材料。首版不做自动清理；仅缓存可在确认无任务运行后手动清理。`artifacts` 和个人笔记不属于缓存。
- 总执行上限四小时，媒体上限四小时/4GiB，本次音轨下载上限256MiB，单份JSON响应16MiB。超限明确失败。
- 30秒固定切片仅提供区间定位；不承诺词级时间、语义分段、识别准确率或无接缝。段落排版保留所有文字，按停顿与标点保守合并。
- 新任务可能重新下载音轨；尚未实现断点续传、失败切片续跑或自动重试。部分输出明确 `complete:false`，不导入为完整原稿。
- 标准输出一行、子进程输出丢弃，不保存凭据或临时签名下载地址。日志不会连续刷字幕。每任务空间有上限，但历史任务/原稿总量不自动设定删除上限。

版本库只包含源代码、合成测试、配置示例与独立工具说明。媒体、模型、任务、原稿、配置与私人材料被忽略。运行合成测试：`python -m unittest discover -s tests -v`。
