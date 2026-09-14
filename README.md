# Mint Engine

通用 EVM mint 控制台：手选链、分析合约、Dry Run，到点后按延迟分片，签完即发。

没有统一的 `mint()`。链不会从地址猜。不绕过 allowlist，不伪造签名。当前适配 SeaDrop 与常见 Direct Mint。

## 文档

| 文档 | 内容 |
|---|---|
| [部署.md](./部署.md) | 环境、启动、公网部署、反向代理、安全 |
| [产品与配置方案.md](./产品与配置方案.md) | 页面配置、能力等级、抢跑时间线（产品设计） |
| [架构设计.md](./架构设计.md) | 引擎分层、Adapter、交易流水线（架构设计） |

设计稿里部分早期设想（例如从地址自动认链）已否决，**以本 README 和代码为准**。`quantum/` 是旧版 CLI 抢跑脚本，仅作对照，不是当前入口。

## 当前实现怎么跑

```text
填链 / 合约或 OpenSea 链接 / 数量 / 钱包
        ↓
检测环境（自有 RPC 测速；未填则公开节点。实时 Gas 走公开节点）
        ↓
分析合约（Proxy、ABI、协议、Sale、钱包状态）
        ↓
Dry Run（eth_call，不广播）
        ↓
启动：可选 T-60 PREPARE_LEAD（prepare_lead > sign_lead 时）→ T-sign_lead → T-5s 刷新档期 → T=0 前 re-probe 并重算分片 → 各钱包在分片节点上 estimateGas、签名并立刻广播；回执回来再发该节点下一钱包
        ↓
回执 / Token IDs；任一钱包回执、广播失败或 estimateGas 为 SoldOut 时，未发送的钱包全部 SKIP（页面「售罄后停止未广播钱包」）。已发出的仍等回执。SEND_FAILED 或 TIMEOUT 才加 gas 重试
```

- 链必须手选。贴 OpenSea 链接可自动回填链和真实合约，但手选链必须与 OpenSea 链一致。
- 填了「自有 RPC」：测速按延迟排序。**并发数 = 每个节点同时飞行的钱包数**（含等回执）。钱包按组切分，优先填延迟最低的节点。合约分析、Dry Run `eth_call`、gas 单价只打一次（主节点/公开节点）。每个钱包的余额、nonce、`estimateGas`、广播、回执都走该钱包的分片节点。某节点 429 则切到延迟下一名，最后才绕回更快的节点。未填自有 RPC 时回退到公开节点。`rpc.probe_on_start`（默认 true）：**开抢前再 probe 一次**并重算分片（预约等待期间节点延迟可能变化）。
- 分析合约通过且已填私钥即可启动。检测环境和 Dry Run 可选。启动名单是「有私钥的钱包」，不按分析页的 READY 过滤。
- `NOT_STARTED` 允许预约抢跑；`ENDED` / `SOLD_OUT` 拒绝发送。
- 启动需要私钥。只填地址（40 位十六进制）只能分析。
- 数量 `quantity` 写入**一笔** mint 的参数（例如 `mint(5)`），应付 `price × quantity`。每个钱包每轮只发这一笔，不是连发 5 笔。
- 启动前不查「这个地址已经 mint 过几枚」。已打满再点启动仍会广播，链上一般 `AlreadyMinted` revert，只该钱包失败，不停其他人。总量售罄（`SoldOut` / `MaxSupply*` 等 custom error）才会 SKIP 未发送的钱包；售罄判断解析 revert 载荷前 4 字节，避免误伤。`execution reverted` 会立刻失败，不再换遍所有 RPC 重试。
- 分析页钱包 ETH：`mint 应付 + gas_limit×maxFee`（与启动同套 Gas 配置；RPC 不可用时按配置兜底，约 1 gwei base + 额外 tip）。
- Direct Mint 多参数（如 `deadline`、多个 `address`）需在 `mint.extra_params` 填写；分析 gaps 会提示。
- OpenSea 多阶段 drop：**自动**跟当前阶段（跳过 `team` 轮）。未开始时按 **最早 upcoming** 预约；`next_stage` 仅在与该最早轮次一致时作确认，不会跳过更早的 GTD 等。进行中取 **开始最晚** 的一轮（PUBLIC 与 FCFS 重叠时整站走链上 **mintPublic**）→ 否则预售 **OpenSea mint API**（须 OpenSea 链接解析出 stages）。链上 ABI 失败时仍可走 OpenSea 预售路径启动。

## Gas 怎么算

控制台「Gas 与启动」三项会原样传给后端：`extra_priority_gwei`、`max_fee_multiplier`、`max_retries`。硬顶已从页面移除，后端 `hard_cap_gwei=0` 表示不限制。

| 字段 | 默认 | 0 的含义 |
|---|---|---|
| 额外 tip (gwei) | `1` | 不加额外 tip，只用链上 tip |
| maxFee 倍率 | `1` | 当作 `1.0`，即 `baseFee + tip` |
| 重试次数 | `3`（0–10） | 只发首次，失败不再加价重签 |

```text
priority = 链上 tip + 额外 tip
maxFee   = baseFee × 倍率 + priority
gasLimit = 该钱包自己的 estimateGas × 1.2（估失败则 280000）
```

失败且状态为 `SEND_FAILED` / `TIMEOUT` 时，同 nonce 重签：`priority × 1.5^attempt`，倍率再乘 `1.25^attempt`。

Robinhood（`chain_id=4663`）无公开 mempool，sequencer 先到先得：加 tip 插不了队。页面把 tip 和倍率锁成 0，走链上最低有效价；重试次数仍可改。快慢主要看 RPC 到 sequencer 的延迟和发出早晚，不看加价。

## 归集 NFT

mint 完成后，控制台「归集 NFT」把各源钱包里的 NFT 转到一个出售地址，再用该地址连 OpenSea 挂单。链 / RPC / 源钱包私钥 / Gas 沿用上方配置。

| 配置 | 说明 |
|---|---|
| NFT 合约 | 默认可用当前 Mint 合约 |
| 归集目标地址 | 确认归集时必填；预览不看这个地址 |
| 转出范围 | 默认链上扫描；也可本次 mint 结果 / 手动 token ID |
| 标准 | 自动 / ERC-721 / ERC-1155 |
| 归集并发 | 不同源钱包并行；同一钱包多枚串行 |

预览只问「这个源钱包在当前所选链上有没有该合约的 NFT」，不扫其他链。有则列出 token ID。扫描顺序：所选链 RPC 上的 `tokensOfOwner` / `walletOfOwner` → Enumerable → 该链浏览器持仓接口（Etherscan 带 `chainid` / Blockscout）→ 同链 Transfer 日志（按节点限制切块）。空钱包跳过。ERC-1155 只用本次结果或手动 ID。

## 本地启动

Python 3.11+。

```bash
cd /path/to/mint-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # 按需填 Key
python -m mint_engine
```

Windows CMD：

```bat
cd /d d:\project\mint-bot
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python -m mint_engine
```

或 `./scripts/start.sh`。默认 `http://0.0.0.0:8056/`，无登录。浏览器打开 **http://127.0.0.1:8056/**。改过代码或 `.env` 后必须重启进程。

## 环境变量

写在项目根目录 `.env`（已 gitignore）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8056` | 端口 |
| `OPENSEA_API_KEY` | 空 | 解析 OpenSea 链接必需 |
| `ETHERSCAN_API_KEY` | 空 | 拉已验证 ABI；没有则走 Sourcify / Blockscout |

改 `.env` 后必须重启进程（配置有缓存）。

## HTTP API

控制台按钮都打这些接口。`/api/run/start` 会阻塞到开售或回执结束。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 控制台 |
| GET | `/api/health` | 探活 |
| GET | `/api/chains` | 支持的链与公开 RPC |
| POST | `/api/opensea/resolve` | 链接 → 链 + 合约 |
| POST | `/api/rpc/probe` | 自有 RPC 测速（未填则公开）+ 公开节点 Gas |
| POST | `/api/gas/quote` | 实时 Gas |
| POST | `/api/inspect` | 分析合约 |
| POST | `/api/dry-run` | 模拟，不广播 |
| POST | `/api/run/start` | 启动 mint |
| POST | `/api/sweep/preview` | 归集预览：查各源钱包在该合约下的 token |
| POST | `/api/sweep/run` | 归集：把 NFT 转到目标地址 |

## 目录

```text
mint_engine/           后端引擎与 FastAPI
mint_engine/sweep/     NFT 归集（预览 + transfer）
web/console.html       控制台
tests/                 单元测试（售罄检测、Direct 参数、分片）
scripts/start.sh       启动脚本
quantum/               旧 CLI，不参与当前控制台
```

本地测试：`python -m unittest discover -s tests`

## 注意

- 公网默认无鉴权。`/api/run/start` 和 `/api/sweep/run` 会接收私钥，务必配合 [部署.md](./部署.md) 限制访问。
- 到点抢跑时 HTTP 请求会一直挂到开售，前面的反向代理要把空闲超时拉长。
- 不要把 `.env`、私钥、`results/*.json` 提交进仓库。
