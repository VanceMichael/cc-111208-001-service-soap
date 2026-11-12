# 安装商回执协议子集

仅访问授权本地 HTTP。向 /receipts 发送 SOAP 1.2 Query，业务命名空间为 urn:service:receipt:1，Query 使用 window 与 cursor 属性。Batch 的 id 和 next 是不透明字符串，next="done" 表示窗口结束；Receipt 的 id 是工单键，revision 为非负整数。Photo 包含 sha256 和 length 属性，并通过 xop:Include 的 href 关联照片。

Envelope/Body 的命名空间必须是 http://www.w3.org/2003/05/soap-envelope，XOP 命名空间是 http://www.w3.org/2004/08/xop/include。单次检查器仅支持 XML 回执概览；新增接收器还要处理 XML-only 和 multipart/related，按 start 找根部，不能依赖附件排列。MIME 只以完整边界行分段。CID 引用先 URI 解码，再和去除尖括号的 Content-ID 匹配；重复标识须拒绝，长度按原始二进制字节计算。

本地 /receipts 第一次返回缺少照片的 XML，第二次返回附件在前、根部在后的 multipart；/fault 返回 HTTP 200 的业务 Fault。缺件保持待补齐，不是未签收；Fault 不推进批次游标。完整工单可以先可见，但同修订内容冲突须独立保留，旧修订不得替换当前。接收状态必须关联窗口、游标、原文摘要及照片引用，恢复时不可读到不存在的照片。这里仅定义送装回执子集，不提供真实客户资料或通用 SOAP SDK。
