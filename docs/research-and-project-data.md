# 资料检索与项目数据交接

## 研究员如何把证据交给核查员

研究员在修改回合或团队任务中使用 `search_sources`。参数 `query` 接受 1–300 个字符，`collection` 可选 `news` 或 `papers`，`limit` 为 1–10，默认 5。新闻通过 GDELT DOC 查询，限流或响应异常时转用 Google News RSS，记录此次切换及原因。RSS 结果会注明聚合链接及来源站点。论文通过 Crossref 检索 DOI 元数据。已有资料的私聊只读问答可以从项目读取之前保存的文件。[GDELT 查询接口](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) · [Crossref 查询接口](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)

检索结果自动保存为 `sources/<内容哈希>.json`。研究员依据候选条目选取原始来源，再用 `http_get(url)` 获取可读取的公开网页或文本接口。工具会保存请求网址、最终网址、UTC 获取时间、截断标记、所读原始字节的 SHA-256 和提取出的文本。网页和文本接口最多读取 200,000 字节。带文本的 PDF 可读取最多 8 MiB 原始文件并提取最多 200,000 字节正文。可用 `pdf_page` 读取特定页，页码从 1 开始。来源记录标明页码、提取器、截断及原文件摘要。原始 PDF 仅在临时文件内解析，工作区只保存可交接的文本记录。解析超时、扫描版无正文、文件超限或本机缺少 Poppler 时，工具报告原因，可继续查同一出版方的 HTML 正文。PDF 解析使用已安装的 Poppler 命令，项目只负责限时、有界读取和来源归档。[Poppler 对输入、页码和失败的处理](https://skia.googlesource.com/third_party/poppler/+/7eb91c754431a87c35fc461e31cbd5bafb2f0ddc/utils/pdftotext.cc#259)

**网页正文与自动提取的旁链。** `http_get` 先检查请求地址，再用原有项目发布规则筛选候选链接的标题和目标地址。可能含服务凭据的旁链整条省略，观察到的正文、主 URL 和原始响应字节摘要保留。来源归档的 `links_withheld` 表示按发布规则省略的去重旁链数，`links_truncated` 标记正常旁链因容量限制未能全部收录。只有保留下来的链接进入文件和工具答复。主资料本身未通过校验时，工具直接返回原因。整个归档以过滤后的字节重新计算路径；历史归档原样保存。

11 月 20 日基金组实测中，原研究目录有 38 份文件，一份网页归档自动采集的系列导航触发了发布拦截。用该次已保存的原始提取结果离线回放，102 条旁链保留 101 条，页面正文、主 URL 和响应摘要一致，其余 37 份文件逐字节相同。新归档能从 `read_source` 阅读，整个目录能通过正常的成员快照转交。Scrapy 在解析后逐条判定可提取的链接，并单独跳过无效地址；Studio 在同一位置使用项目自己的发布校验器，保留省略数量。针对性回归还覆盖正常链接和敏感主资料。[Scrapy 固定版本的链接筛选](https://github.com/scrapy/scrapy/blob/155a504f24b9dd180b8786a4fa709f87cfe8fb66/scrapy/linkextractors/lxmlhtml.py#L217-L245) · [Scrapy 包含和拒绝规则测试](https://github.com/scrapy/scrapy/blob/155a504f24b9dd180b8786a4fa709f87cfe8fb66/tests/test_linkextractors.py#L103-L120) · [Studio 的资料工具回归](../tests/test_research.py)

成员可以用 `read_source(path, query, offset, limit)` 阅读已保存的资料。只需提供来源路径，其余参数可省略。比如在已抓取的协议文档中查找 `task`，提供 `query="task"` 即可从首个匹配处阅读。随后省略搜索词，用回执的 `next_offset` 继续翻页。起点和长度按正文字符计算，默认每次 4,000 字符，单次最多 12,000 字符。`eof` 表示已读到存档末尾，原抓取自身的截断会另行标出。工具从磁盘核对快照摘要，同伴可以在交接后阅读相同的版本。分页与结束提示参考 MCP Fetch 的字符游标实现，Studio 将其应用于本地归档。[MCP Fetch 分页](https://github.com/modelcontextprotocol/servers/blob/c799c8552b0f27b7a33a7b9233773da144dddcf5/src/fetch/src/mcp_server_fetch/server.py#L1093-L1105) · [完整翻页与越界测试](https://github.com/modelcontextprotocol/servers/blob/c799c8552b0f27b7a33a7b9233773da144dddcf5/src/fetch/tests/test_protocol.py#L820-L895) · [Studio 来源读取回归](../tests/test_research.py)

交接消息应指明「要核查的论断、来源文件、支持或反驳该论断的具体段落」。接手人可以打开资料文件，与当时的草稿逐项比对。搜索命中、聚合链接和 GDELT 收录日期均只能用于发现线索。发表日期和关键结论应回到原始来源核实。

搜索服务使用固定 HTTPS 接口。来源抓取会逐次校验跳转，连接到已验证的公网 IP，并以原始域名校验 TLS。当本机 HTTP 代理为域名返回 `198.18.0.0/15` 合成地址时，工具会用 Google Public DNS 查询公开 A 记录，通过系统代理连接到经校验的数字地址。普通公网 DNS 无需这一查询。[Google Public DNS 查询协议](https://developers.google.com/speed/public-dns/docs/doh/json)

## 预约数据怎样交给下一轮

在角色工作目录中，UTF-8 CSV、JSON 可与代码一起进入下一版。SQLite 使用 `.db`、`.sqlite` 或 `.sqlite3`，保留原始字节；发布前应提交写入并关闭连接。若 WAL 仍在使用，请先妥善结束写入并完成 checkpoint。仍有 `-wal`、`-shm`、`-journal` 附属文件时系统会阻止不完整交付。

下载运行的产物 ZIP 后，CSV 可直接查看，数据库可以用 SQLite 打开。控制目录、隐藏文件、链接、Python 字节码缓存不进入项目交接。每个项目快照的单文件上限为 8 MiB，总量为 20 MiB。工作室核对 SQLite 文件头并检查整个数据库字节，遇到当前服务凭据、可识别的常见 API 密钥或工作室控制库结构时拒绝交接。数据库会保留原始字节，可能包含业务数据和已删除记录；交付团队应仅将可分享的数据写入项目库。

用于软件场景验收时，建议第一轮建立一笔已取消和两笔有效预订，第二轮读取同一个数据库核对统计。例如两笔有效预订分别为 45 和 60 分钟，取消的预订为 30 分钟；若约定统计有效预订时长，仪表盘应显示 105 分钟，并将取消单独计数。

### HTML 来源的后续核验

HTML 提取同时返回可见正文和可追踪链接。工具会在来源记录的 `links` 数组保留锚文本及完整 HTTP(S) 地址，使用最终页面 URL 和有效的 HTML `base` 解析相对路径。主内容区的链接优先。每页最多保留 160 个链接并限制编码大小，工具答复提供部分完整链接，其余可从来源记录检索。打开后续链接仍经过同一公网校验。实现借鉴 python-markdownify 的 `convert_a`，其将锚文本与 href 作为一对保留；本项目用结构化 JSON 交接并限制工具回复长度。[上游固定源码](https://github.com/matthewwithanm/python-markdownify/blob/1971972753bf258272e605c653cfe67bff90e8a6/markdownify/__init__.py)

群负责人在首次选人时能看到执行成员实际注册的工具，包括来源检索和网页读取。研究员负责检查具体来源的可访问性，负责人可根据回执调整分工。新闻组线上评测遇到过主编根据自身工具栏推断团队无法联网的情况。这里借鉴 A2A 由 AgentCard 公布可委派能力的机制，成员工具清单直接来自本地注册表。[A2A AgentCard 和 AgentSkill](https://a2a-protocol.org/latest/specification/#44-agent-discovery-objects)

成员上下文也标明委派要求来自群负责人。内部指定的测试方法如与验证要求冲突，可附证据回到负责人，由团队重新分工。用户对业务范围或外部权限的决定单独记录。

独立事实核查可以在委派的逐条标准中指定 `method: source`。核查员先读候选稿，独立打开可支持或反驳具体主张的原始来源，然后引用两条回执，说明适用人群、时间、数据或不确定性。提交时宿主核对稿件版本、访问来源的角色与尝试、最终网址、实收内容的 SHA-256。仅有搜索结果或作者写下的链接时，核查员可说明证据不足。私有候选的新增检索文件按工具实际生成的内容摘要单独识别，候选原始文件仍按原摘要核对。

这个判断借鉴 FEVER 区分支持、反驳与证据不足的标注方法。新闻组曾从一份公告没有列出课程细节，推断学校从未公布细节。遇到此类主张，核查员应比对有发布职责的目录或政策页，主张的范围应限定在实际查阅的资料内。真实回执能证明打开过来源，来源是否支持稿件中的结论由核查员判断。[FEVER 原论文](https://aclanthology.org/N18-1074/)

公开文本允许引用完整的 Studio 任务标识。Markdown 本页章节链接中，如果可见英文标题的单词与链接片段一一对应，`task` 是完整单词且后面至少还有两个词，凭据扫描也能识别其词尾。扫描从该词之后继续，URL、相邻标记、配置值和同一归档的其余正文仍照常检查。当前服务密钥始终对原始字节做完整匹配。对 A2A 协议的回放用官方原文及旧抓取元数据还原了内容摘要以 `75de7328` 开头的归档，实际快照交接保留全部 163523 字节。区分标记边界的思路参考 Gitleaks，Studio 在 Markdown 内另核对可见文字与本页片段。[Gitleaks 标记边界](https://github.com/gitleaks/gitleaks/blob/v8.24.2/cmd/generate/config/utils/generate.go#L21-L28) · [A2A 协议原文](https://github.com/a2aproject/A2A/blob/main/docs/specification.md) · [Studio 归档交接与凭据回归](../tests/test_project_data.py)

### 研究资料的临时文件和交接

研究员可直接用 `http_get` 查阅公开 PDF，`pdf_page` 可定位到具体页面。工具用系统临时文件解析原件，并将出处、采集时间、观察到的字节摘要和正文写入项目中的 `sources/*.json`。手动处理二进制来源时，可在单次 Python 调用中用 `tempfile.TemporaryDirectory()` 保存自行下载的临时副本，再把需要交接的摘录与来源记录留在项目里。

若普通成员提交时，项目内仍有不支持的格式，宿主会在当前草稿中提示可安全显示的相对路径。成员可以移出自行下载的临时副本并再次交接。用户要求交付原件时，应保留原文件并报告格式限制。包含凭据或控制字符的路径会隐藏。正式发布仍会在子进程收尾后校验完整快照。

Pi 的输出文件实现将工作目录内的工件与工具的系统临时文件分开，临时文件独占创建且权限为 0600，其回归覆盖完成与取消两种状态。Studio 沿用现有的 Python 临时文件和来源记录机制。[Pi 临时文件实现](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/src/utils/output-files.ts#L1-L34) · [Pi 临时工件回归](https://github.com/earendil-works/pi/blob/f6d0c2f9b6ffbcb00c5da52e7e88570539c78bde/packages/coding-agent/test/bash-executor-truncation.test.ts#L6-L45)

### 企业新闻引用的凭据误报

SK hynix 官方英文新闻中心会把品牌名写在文章路径开头。Studio 在精确的 HTTPS 来源、正文里的 URL 边界和由短词组成的新闻标题同时成立时，识别开头的品牌标记。判断可以同时处理产品新闻和季度财务公告。识别后，扫描器从标记内部之后继续检查，路径其余位置、查询、片段、邻接文本和以完整原始字节匹配的实际配置密钥都保留检查。拒绝时反馈物理行号，诊断里不回显命中的值。[制造商产品新闻](https://news.skhynix.com/en/sk-hynix-ships-samples-of-12-layer-next-gen-hbm4e-2/) · [制造商财务公告](https://news.skhynix.com/en/sk-hynix-announces-3q25-financial-results/)

搜索结果和部分公开 JSON 接口同时提供可读标题与文章链接。对于 Studio 自身格式的 `sources/<sha256>.json`，校验器核对相对路径、完整内容摘要、JSON 编码及来源记录种类。文章的 HTTPS 链接必须出现在相应 URL 字段内，最后一级路径中，从歧义单词开始的各个短词须按顺序出现在可见标题中，品牌的两个词还须相邻。这能识别品牌写法和 Krasnoyarsk 等地名的词尾。重复值缺少对应标题、附带凭据的参数、字符转义导致无法定位以及其他不能确认的候选，都会保留拒绝。内容摘要只证明字节一致；成员也能生成相同格式的归档，因此仍要逐个检查标记与原始字节。二进制数据库与 PNG 继续扫描完整字节。[联邦公报标题与永久链接](https://www.federalregister.gov/documents/2026/09/17/2026-19021/azur-air-office-29-vzletnaya-st-57-krasnoyarsk-russia-660020-order-renewing-temporary-denial-of)

这种做法采用 Gitleaks 按单个检测值并结合多个条件判断误报的思路。Studio 为带标题的出版物链接另外核对文内位置和词序。2026 年 11 月 20 日第六题首次研究成员的原始目录经 `copy_project` 完整回放，52 个文件合计 1,091,513 字节，其中研究底稿 61,306 字节。独立复验逐文件比较了原件、输出和清单摘要。此回放验证的是成员交接。[Gitleaks 条件和检测值作用域](https://github.com/gitleaks/gitleaks/blob/v8.24.2/config/allowlist.go#L26-L52) · [Gitleaks 正反例](https://github.com/gitleaks/gitleaks/blob/v8.24.2/detect/detect_test.go#L883-L925) · [Studio 交接回归](../tests/test_project_data.py)
