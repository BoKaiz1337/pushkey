# pushkey · 上游守护站

> 第十课作业。我卖的东西，我自己盯着 —— 推货、报价、心跳、**上游断了自己先停**。

线上面板：<https://pushkey-zbk.dev.oaiai.ai>

---

## 一、它在链条里的位置

```
   GOLEM 沙盘上游                 pushkey（本服务）              JANUS 中心站
   fake.oaiai.ai        ──→      st-zbk 里的这个 Pod      ──→    gate.oaiai.ai
   「进货口」                      「守护站」                     「卖货口」
   我开的 sk-golem-… 三把 key      盯 + 推 + 报 + 停              我推上去的 mgw_ 货源
```

中心站是**我的交易对手**，不是我的下游。它按 `mgw_` 凭证认我这个人，我按它的
`/openapi.json` 契约跟它打交道。契约有两份，都是公开的：`GET /openapi.json`。

| 站点 | 角色 | 地址 |
|---|---|---|
| GOLEM | 沙盘上游（我的货源） | `https://fake.oaiai.ai` |
| JANUS | 中心站（我的买家） | `https://gate.oaiai.ai` |

---

## 二、主考点：上游断了，**我先知道、我先动手**

课件口径很直白：

> 上游出 503 / 429 / 余额耗尽 → **立刻自己把 key 下架**。
> 等中心站探测到再帮你停，**算你违规**。

所以守护循环每一轮都走这套判定（[`app/guard.py`](app/guard.py)）：

```
① 推 key            POST /api/v1/keys
② 每 5 分钟心跳      POST /api/v1/keys/{id}/heartbeat   ← 带上游余额
③ 判定（两个独立信号，任一命中就动手）
     ├─ 权威信号：GOLEM 控制台上那条 key 的 status（故障开关直接写在它身上）
     ├─ 业务信号：真发一次最小请求，认响应码 402 / 429 / 5xx
     ├─ 正常 → 回 ②
     └─ 异常 ↓
④ 立刻下架          PATCH /api/v1/keys/{id} {"state":"paused","reason":…}
⑤ 充上钱 → 申请恢复  POST /api/v1/keys/{id}/resume（中心端会重新探测，不是我说了算）
```

### 两条硬规矩

**1. `reason` 只能用这四个词，不许自己造。** 手册枚举，多一个都算错：

| reason | 什么时候报 |
|---|---|
| `balance_exhausted` | 余额耗尽（402 / 上游明确说没钱） |
| `probe_failed` | 探活失败（429 / 500 / 503） |
| `heartbeat_timeout` | 心跳断了 |
| `manual` | 我自己要停 |

**2. 余额探不到就报 `null`，绝不瞎猜。**

沙盘压根没有余额查询接口（控制台和 openapi.json 里都没有）。所以心跳里的
`upstream_balance` 老老实实传 `null`。编一个数字上去，比不传更糟。

### 状态机是单向的

```
pending → probing → approved → available → imported
```

客户端**只能**把它置成 `paused`。想从 `paused` 回去，只能 `POST /keys/{id}/resume`
让中心站重新探测 —— 它不是我能自己掰回去的。所以代码里有个 `PAUSABLE` 白名单，
不在名单里的状态直接放弃，不去撞墙（[`guard.py:29`](app/guard.py#L29)）。

### 心跳断了也算事故

心跳间隔 5 分钟。中心站超过 10 分钟收不到心跳 → 标记 `stale` → **从池子里摘掉**。
摘掉就是「没管好」，跟被探测到故障是一个性质。

---

## 三、三类资源，格式是硬约束

推错格式直接 `400 format_mismatch`：

| `resource_type` | 允许的 `format` | 我在 GOLEM 的货源 |
|---|---|---|
| `gpt_pool` | `openai` | `gpt-pool` |
| `deepseek_relay` | `anthropic` | `deepseek-relay` |
| `ccmax_key` | `anthropic` | `ccmax` |

**类型和格式一律从 `/api/v1/resource-types` 读，代码里不写死。**
平台加一类资源，我这个服务零改动 —— 这是课件里点名要的。

---

## 四、报价

报价是**相对官方基准价的折扣**：

```json
{"price_ratio": 0.25, "model_overrides": {"deepseek-v4-pro": 0.3}}
```

`0.25` = 官方价的两五折。`model_overrides` 可以给单个模型单独定价。

任务单（`/api/v1/tasks`）带 `min_price_ratio` / `max_price_ratio` 区间，
**认领名额（`POST /tasks/{id}/claims`）之后的报价才受区间保护**。所以推货流程是
「先认领，再报价」。

本服务报的价在 [`app/config.py`](app/config.py) 的 `quote_ratio` 里，推之前会先
自己夹到区间内，不等平台回 400。

---

## 五、怎么跑

### 本地

```bash
cp .env.example .env      # 填四个值，绝不提交
docker build --platform linux/amd64 -t pushkey:local .
docker run --rm -p 8080:8080 --env-file .env -v "$PWD/data:/data" pushkey:local
```

> ⚠️ 本机是 aarch64，集群是 amd64。**构建必须带 `--platform linux/amd64`**，
> 否则推上去是 `exec format error`。CI 跑在 ubuntu-latest 上，天生 amd64。

### 集群

```bash
# 凭证进 Secret，不进任何文件
kubectl -n st-zbk create secret generic pushkey-secrets \
  --from-literal=MGW_KEY=... \
  --from-literal=GOLEM_EMAIL=... \
  --from-literal=GOLEM_PASSWORD=... \
  --from-literal=PANEL_PASSWORD=...

kubectl apply -f k8s.yaml
```

发版走 tag（第八课的契约，这里同样适用）：

```bash
git tag v1 && git push origin v1        # tag 才是发布按钮，推 main 不发版
kubectl -n st-zbk set image deployment/pushkey web=registry.dev.oaiai.ai/zbk/pushkey:v1
```

---

## 六、接口

| 方法 | 路径 | 干什么 |
|---|---|---|
| `GET` | `/` | 面板（Basic 认证） |
| `GET` | `/healthz` | 存活探针，不鉴权 |
| `GET` | `/api/state` | 我的货 + 审计日志 |
| `GET` | `/api/upstream` | 进货口视角：余额 / 账单 / 用量 |
| `GET` | `/api/earnings` | 卖货口视角：中心站的用量与结算单 |
| `POST` | `/api/sync` | 拉类型与任务 → 认领 → 推货 |
| `POST` | `/api/run/probe` | 立刻探活一轮（守护循环平时每 60s 自己跑） |
| `POST` | `/api/run/heartbeat` | 立刻心跳一轮（平时每 300s） |
| `POST` | `/api/keys/{rt}/pause` | 手动下架，`reason=manual` |
| `POST` | `/api/keys/{rt}/resume` | 申请恢复（中心站会重新探测） |
| `POST` | `/api/keys/{rt}/quote` | 改报价，越界会被平台回 `invalid_quote` |
| `DELETE` | `/api/keys/{rt}` | 从中心站撤回这条 key |

`{rt}` 是 `resource_type`（`gpt_pool` / `deepseek_relay` / `ccmax_key`）。

---

## 七、自测：主考点是**验过的**，不是嘴上说的

```bash
docker run --rm pushkey:local python selftest.py
```

它起一个桩中心站，只实现守护循环用到的端点，然后断言第 ④ 步真的发生了：

```
[上游 503]                  → PATCH state=paused, reason=probe_failed      ✓
[上游余额耗尽]               → PATCH state=paused, reason=balance_exhausted ✓
[上游 429 限流]             → PATCH state=paused, reason=probe_failed      ✓
[上游正常（对照）]           → 不发 PATCH                                    ✓
[中心站还在 pending]         → 不硬撞，如实说明「状态机不让」                  ✓
```

真上游那边也实测过（拨 GOLEM 的故障开关）：

| 货 | GOLEM 开关 | 真实响应码 | 判出的 reason |
|---|---|---|---|
| `gpt_pool` | `503` | 503 | `probe_failed` |
| `ccmax_key` | `balance_exhausted` | 402 | `balance_exhausted` |
| `deepseek_relay` | `normal` | 200 | — （对照组） |

> 线上那三条 key 目前停在 `probing`：**`probing → approved` 是管理员人工放行**，
> 供应商角色点不了。所以「真中心站上的第 ④ 步」要等放行之后才能演。

---

## 八、凭证怎么管的

这条是扣分重灾区，**上游 key 绝不能进日志**。本服务的做法：

- 所有凭证只从环境变量读（[`app/config.py`](app/config.py)），代码里没有任何明文。
- 面板和 API **只输出脱敏串**，唯一允许的形式是 `mask()`：
  `sk-golem…a1b2` 这种，前后各留几位。见 [`clients.py:36`](app/clients.py#L36)。
- 审计日志（`events` 表）只记**动作、资源类型、中心站 key id**，不记上游 key。
- 面板加了 Basic 认证 —— 公网裸奔等于把货源送人。

唯一必须留明文的地方是本地 SQLite：守护循环要靠这把 key 去探活，而 GOLEM
只在创建时回显一次。它在磁盘上，不在日志里，也不在任何响应体里。

---

## 九、踩过的坑

| 坑 | 现象 | 解 |
|---|---|---|
| GOLEM 没有改密接口 | 想统一口令，找不到任何路由，同邮箱重注册回 409 | 随机口令直接记进密码总表 |
| 沙盘没有余额接口 | `upstream_balance` 没处取 | 如实报 `null`，绝不编 |
| 控制台 401 | cookie jar 存了但没生效 | `MozillaCookieJar` 要显式 `.load()`；会话过期要用同锁续登一次 |
| 探测被思维链吃光 | 推理模型 300 token 以下全用来想，返回空 | `probe_max_tokens` 给到 300 |
| 域名准入 | 不是单段 `xxx.dev.oaiai.ai` 的 host 直接被拒 | 用 `pushkey-zbk.dev.oaiai.ai` |
| `--platform` | 本机构建推上去 `exec format error` | 构建一律 `--platform linux/amd64` |
