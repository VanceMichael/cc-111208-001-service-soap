# servicebridge

安装服务合作方回执的原文接收、业务接收与 XML 摘要工具。

## 本地运行

使用 Python 3.11 标准库，无 pip 依赖。所有来源与业务数据都是本地合成数据，不连接真实用户系统。

```sh
python -m scripts.materialize
python -m servicebridge inspect fixtures/receipt.xml
python -m compileall -q .
python -m unittest discover -s tests -v
python -m scripts.demo
```

## 命令

检查命令输出单份来源的结构摘要。`capture URL --method GET --state var` 仅接受 loopback HTTP，保存原始响应为 SHA256 文件，并在 SQLite captures 表登记；可用 `--header 'Name:Value'` 传头、`--body FILE` 传正文，不跟随重定向。检查器不执行多页、业务去重或正式接收。

`receive` 把一个查询窗口内的回执与照片接收为可重跑的业务记录（仅连接 loopback 授权本地接口）：

```sh
python -m servicebridge receive http://127.0.0.1:8765 --window WIN-2026-11-12 \
    --path /receipts --state var/receive
```

- 发送 SOAP 1.2 Query（`urn:service:receipt:1`），按 Batch 的 `next` 续页游标翻页，`next="done"` 结束；故障批、协议错误或传输失败不推进游标，退出码 3，可直接重跑。
- 同时支持 XML-only 与 `multipart/related`：按 `start` 定位根部（不依赖片段顺序），只以完整边界行分段，`xop:Include` 的 cid 先 URI 解码再匹配去尖括号的 Content-ID；重复 Content-ID、缺件、长度/sha256 不符、错误 SOAP 命名空间和 Fault 一律拒绝并留证。
- 照片按 sha256 内容寻址落盘（临时文件 fsync 后 rename），工单可见性、观察记录与游标在同一个 SQLite 事务提交；客服查询只返回照片齐全的工单，缺件工单保持不可见，补件后原地转完整，不重复生成工单。
- 同一工单同一修订但内容冲突时，冲突观察独立标记为 `conflict` 留证，当前记录不被覆盖；迟到的旧修订标记为 `late_revision`，不能让当前记录回退。
- 窗口结束后若仍有缺件工单，重跑会回到窗口起点重新拉取做幂等核对；已存观察按内容哈希去重。

```sh
python -m servicebridge export W1 --state var/receive          # 按工单导出当前记录（不可见则退出码 2）
python -m servicebridge failures --window WIN-2026-11-12 --state var/receive
```

`export` 输出当前可见工单的修订、照片（cid、长度、摘要、落盘路径）与原始响应引用；`failures` 输出窗口内缺件、校验失败、冲突、Fault 与恢复撤回的全部留证。

## 状态目录布局

```
var/receive/
├── receive.sqlite3     # 窗口/游标、批次、观察、工单、故障留证
├── raw/<sha256>        # 原始 HTTP 响应（含 Fault 原文），由 SQLite 引用
└── photos/aa/bb/<sha256>  # 内容寻址照片，分两级目录避免单目录膨胀
```

崩溃恢复不变量：照片文件不会先于 SQLite 记录可见，工单可见性不会先于游标推进；重启时核对可见工单引用的照片，文件缺失或摘要不符立即撤回可见性并登记 `recovery_hidden`。

## 可复现容器

```sh
docker build -t servicebridge:local .
docker run --rm --network none servicebridge:local python -m unittest discover -s tests -v
docker run --rm --network none servicebridge:local
```

镜像固定基础摘要。构建在正式运行前完成；运行和测试不安装包、不下载数据。演示命令实际走本地合成来源，展示“缺件不可见 → 补齐照片后可见”的完整业务闭环，通常不超过 5 秒。

## 协议与来源

- docs/source-contract.md 给出支持子集、字段及失败语义。
- docs/acceptance.md 给出业务验收边界。
- fixtures/source.json 是本地可重放 HTTP 路由（含分页演示路由 `/receive-demo`）；`python -m scripts.source_server --port 8765` 启动，`/__requests` 可检查实际请求。
- scripts/materialize.py 将版本化 base64 二进制夹具还原为原始字节，重复执行安全；二进制文件不是用户附件。
- journal.py 只记录原始响应，不代替 receive 流程自己的事务和恢复设计。
