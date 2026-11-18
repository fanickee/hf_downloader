# Hugging Face 模型下载器

下载逻辑使用 Python 标准库，控制台进度使用 [Rich](https://rich.readthedocs.io/en/stable/progress.html)，使用 **uv 管理环境、依赖和锁文件**。支持 Python 3.11 及以上。

## 快速开始

在项目目录运行：

```powershell
uv sync --locked
uv run hf-download Qwen/Qwen2.5-0.5B-Instruct
```

默认下载到 `models/Qwen/Qwen2.5-0.5B-Instruct`。指定目录、线程数，并在完成后自动休眠：

```powershell
uv run hf-download Qwen/Qwen2.5-0.5B-Instruct -o "E:\models\Qwen2.5-0.5B" -w 8 --hibernate
```

只下载需要的格式，避免同时下载多个框架的权重：

```powershell
uv run hf-download Qwen/Qwen2.5-0.5B-Instruct --exclude "*.bin" "*.h5" "*.msgpack"
```

也可通过 `--include "*.safetensors" "*.json" "*.txt" "*.model"` 选择文件。匹配的是仓库内相对路径；筛选时请保留模型实际需要的配置和 tokenizer 文件。`--exclude` 优先于 `--include`。

## 断点续传与无人值守

- **单临时文件分区写入**：每个模型文件对应一个 `download.incomplete`，各线程独立打开文件、写入互不重叠的区间，不生成新的分块文件，也不需要最后合并。
- **可靠续传记录**：`.hf-download` 下的 SQLite 记录每个区间的已保存长度和前缀 SHA-256。数据先 `fsync`，再提交记录；重启时先重新读取并检查这些前缀，损坏区间会重新下载。临时文件的总长度不作为下载完成依据。
- **持续重试**：断网、连接超时、传输中断、HTTP 429 和服务端错误会持续重试，没有重试次数上限。默认从 2 秒开始指数退避，最高约 60 秒；服务端 `Retry-After` 可要求等待更久。
- **连接错误诊断**：请求失败时区分证书过期/验证失败、DNS 解析失败、连接超时、连接被拒绝和代理隧道错误，并继续重试；诊断信息不输出完整请求链接或认证信息。TLS 验证失败不会自动关闭证书校验。
- **系统证书校验**：通过 `truststore` 使用系统证书验证能力，Windows 使用原生 CryptoAPI，避免 Python 默认 OpenSSL 与 Windows 证书链验证差异造成部分镜像无法连接。始终验证证书和域名，适用于模型信息、文件下载及 CDN 重定向；无需额外参数。
- **刷新链接**：每次分块请求和失败重试都会重新请求固定版本的 `/resolve/` 地址，获取新的 CDN 地址；附加随机查询参数，避免命中过期重定向缓存。CDN 返回 401/403/404 时会重试刷新。
- **Rich 控制台进度**：显示百分比、下载字节、剩余字节、下载速度、预计剩余时间和文件完成数；百分比保留两位并向下取整，尚未下载完时不会四舍五入成 100%。恢复校验和完整校验有各自的进度行。网络下载 100% 后仍须等待完整校验及文件完成数。终端每秒刷新 4 次；重定向日志时每约 10 秒输出纯文本快照。`--quiet` 隐藏进度，保留结果和错误。
- **多线程及尾部分配**：所有文件共享一个下载线程池，默认最多 8 个网络下载线程。只剩少量待调度区间时，将未下载的尾部继续二分，每个新子区间至少约 1 MiB，交给空闲线程；正在写入的区间不会被同时交给另一线程，慢连接重试后其剩余部分可再拆分。
- **慢连接恢复**：使用 `read1()` 及时处理已到达数据，避免等满读缓冲。默认无数据等待上限为 30 秒；一个检测窗口（默认 30 秒）的平均速度低于 16 KiB/s 时，会保存进度并重新获取链接。可通过 `--stall-timeout`、`--slow-window`、`--min-speed` 调整；`--min-speed 0` 禁用低速判定。服务器完全限速时，重连不能保证提速。
- **内容校验**：LFS/Xet 文件使用 Hub 提供的 SHA-256，普通 Git 文件使用 Git blob SHA-1。校验完成后才发布最终文件。发现损坏会重新下载，连续三次校验失败则报错停止。
- **固定版本**：首次读取仓库元数据时记录 commit SHA，重启后继续该版本，避免模型更新期间混用不同版本的分块。需要获取分支最新版本时加 `--refresh`。
- **完成后睡眠或休眠**：使用 `--sleep` 睡眠或 `--hibernate` 休眠，两者不能同时使用。仅在所选文件全部校验通过后执行，失败或取消时不会执行。默认预留 60 秒倒计时，可以 Ctrl+C 取消。

仓库不存在、版本不存在、Hub 身份验证或授权失败会立即报错；磁盘写满或无法写入也会报错。这些情况需要修正配置，不能靠无限网络重试解决。

**Ctrl+C 退出**：第一次按下后会显示停止提示，取消重试并保留续传记录。如果网络请求或线程池卡住，最多等待 5 秒后强制退出；再次按 Ctrl+C 会立即强制退出。Windows 也支持 Ctrl+Break。退出时恢复防空闲睡眠状态，不执行自动休眠。正常重试或取消时会保存当前已写入进度；强制退出只信任最后一次成功提交的检查点，可能重下每个连接最近尚未记录的少量数据。下载过程中每新增约 4 MiB，或在收到数据时发现距上次记录已超过 2 秒，会保存检查点。修改代码后需要重新启动程序，已在运行的旧进程不会自动更新。

Windows 控制台的退出提示使用原生 Unicode 输出，兼容 GBK 和 UTF-8 代码页；重定向日志使用 UTF-8。此退出路径不依赖 Python 缓冲输出锁，也不会修改终端代码页。

如果服务器忽略 HTTP Range，程序会回退为该文件的完整下载；**这种服务器无法进行可靠的字节断点续传，断线后该文件须从头重下**。其他文件仍可并发下载。默认 Hugging Face 下载链路可使用 Range，第三方端点行为取决于服务端。

首次下载时，每个文件通常只需要约一份文件大小的空间，加少量 SQLite 记录；校验只完整读取临时文件一次，通过后直接改名，没有第二份合并副本。更新已有文件时，旧最终文件会保留到新文件校验通过，因此需额外空间。改变版本或分块大小产生的其他缓存不会自动删除。缓存不是 `huggingface_hub` 的缓存格式。

续传使用 `download.incomplete` 和 `ranges.sqlite3`，请保留 `.hf-download`，并保持输出目录和 `--chunk-size` 一致。旧版 `.part` 和 `assembled.tmp` 不再读取、迁移或自动删除；只有旧分块而没有新版续传记录的未完成文件会重新下载，已完成且内容校验通过的最终文件仍会跳过。

续传记录中的前缀哈希用于检测本地保存的数据是否变化，不代替 Hub 提供的最终文件哈希。SQLite 记录损坏或区间布局异常会报错停止；不要仅凭 `download.incomplete` 看起来大小正确，就手动改名使用。

## 身份验证、端点与代理

私有或受限模型使用有读取权限的 Token，且需要先在 Hugging Face 上获得该模型的访问授权。推荐用环境变量，避免 Token 出现在命令历史中：

```powershell
$env:HF_TOKEN = "hf_你的访问令牌"
uv run hf-download organization/private-model
```

程序不会把 Token 保存到下载清单，也不会把认证头转发给不同域名的 CDN。`HF_TOKEN` / `--token` 是本程序的认证来源，不自动读取 Hugging Face CLI 登录缓存。

自定义 Hub 兼容端点：

```powershell
$env:HF_ENDPOINT = "https://你的可信Hub兼容端点"
uv run hf-download organization/model
```

也可以使用 `--endpoint`。设置 Token 时它会发送给选定的端点，因此仅使用你信任的端点。端点必须支持模型信息接口的 `blobs=true` 参数和 `/resolve/` 下载。

HTTP 代理使用标准环境变量，例如：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
$env:HTTP_PROXY = "http://127.0.0.1:7890"
uv run hf-download organization/model
```

## 自动睡眠与休眠

**睡眠**使用 `--sleep`，默认倒计时 60 秒，可通过 `--sleep-delay` 调整。Windows 调用原生 `SetSuspendState(False, False, False)`，Linux 使用 `systemctl suspend`。Windows 不需要先启用休眠，但需要系统支持睡眠；保留系统唤醒事件设置。睡眠依赖内存供电，断电可能丢失未保存的工作。

```powershell
uv run hf-download leejet/MiniMax-H3-GGUF --include "qwen3vl_32b_minimax_h3-Q4_K_M.gguf" -o "E:\download\minimax-text-encoder" --endpoint https://hf-mirror.com -w 16 --sleep --sleep-delay 60
```

不下载文件，直接测试睡眠（**10 秒后真的睡眠，请先保存工作**；倒计时期间 Ctrl+C 可取消）：

```powershell
uv run python -c "import threading; from hf_downloader import sleep_system; sleep_system(threading.Event(), 10)"
```

`--sleep` 与 `--hibernate` 互斥，默认均不启用。原命令中的 `--hibernate` 换成 `--sleep` 即可；原有 `--hibernate-delay` 应换成 `--sleep-delay`。

Windows 使用 `shutdown.exe /h`，Linux 使用 `systemctl hibernate`，保存内存状态到磁盘后进入休眠。**默认不启用**；启用参数是 `--hibernate`，倒计时可通过 `--hibernate-delay 120` 调整。

Windows 需要系统已启用休眠。如果没有启用，可自行在管理员终端执行 `powercfg /hibernate on`。程序不会修改系统电源配置。下载运行期间会阻止 Windows 因空闲自动睡眠，退出后恢复原状态；这不覆盖手动睡眠、合盖或电量耗尽等系统行为。

Linux 需要已配置可用的睡眠/休眠和相应系统权限；程序不会自动提权。Windows 睡眠调用会临时启用当前进程已拥有的关机权限，返回后恢复，不修改系统权限策略。macOS 暂不支持这些自动电源选项。系统拒绝睡眠/休眠时会报告失败，已完成下载的文件会保留。

Windows 睡眠接口依据：[SetSuspendState](https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-setsuspendstate)。

## 常用参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `repo` | 必填 | 模型仓库 ID，支持 `组织/模型` 或无组织的仓库名 |
| `-o, --output` | `models/<repo>` | 下载输出目录 |
| `-w, --workers` | `8` | 网络下载线程数上限 |
| `--chunk-size` | `64` | 初始区间大小，单位 MiB；续传时保持一致 |
| `--revision` | `main` | 分支、标签或 commit SHA |
| `--refresh` | 关闭 | 重新获取元数据并更新固定版本 |
| `--include` / `--exclude` | 无 | 一个或多个带引号的通配符 |
| `--timeout` | `30` | 单次网络阻塞超时，单位秒 |
| `--stall-timeout` | `30` | 无数据等待上限，与 `--timeout` 取较小值，单位秒 |
| `--slow-window` | `30` | 每连接低速检测窗口，单位秒 |
| `--min-speed` | `16` | 每连接最低平均速度 KiB/s，`0` 禁用低速判定 |
| `--retry-base` / `--retry-max` | `2` / `60` | 重试退避间隔，单位秒 |
| `--hibernate` | 关闭 | 下载全部校验通过后休眠 |
| `--hibernate-delay` | `60` | 休眠前等待秒数 |
| `--sleep` | 关闭 | 下载全部校验通过后睡眠，与 `--hibernate` 互斥 |
| `--sleep-delay` | `60` | 睡眠前等待秒数 |
| `--quiet` | 关闭 | 隐藏动态进度 |

完整帮助：`uv run hf-download --help`。也支持 `uv run python hf_downloader.py ...`。

退出码：成功 `0`，失败 `1`，命令行参数错误 `2`，用户取消 `130`。

## 开发与测试

```powershell
uv sync --locked --dev
uv run pytest -q
```

测试通过本地 HTTP 服务模拟多线程 Range 下载、传输中断、停滞/持续低速、CDN 链接过期、限流、错误范围、损坏内容和不支持 Range 的服务端。同时检查未提交写入、前缀损坏、文件丢失、短写、尾部拆分、独立校验进度、文件锁和休眠条件。独立子进程测试会模拟 Ctrl+C 及强制退出后的续传，测试不会真的让电脑休眠。

接口依据：[Hugging Face Hub API](https://huggingface.co/docs/hub/api)、[文件下载文档](https://huggingface.co/docs/huggingface_hub/package_reference/file_download)。
