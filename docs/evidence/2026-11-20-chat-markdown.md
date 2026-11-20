# 群消息里的 Markdown 文件

聊天、工具预览和产物页共用 markdown-it 15.0.3。仓库随附官方 npm 包的浏览器构建，启动 Studio 时无需下载解析器。Vitaly Puzrin、Alex Kocharin 的 MIT 许可证及浏览器产物中五个依赖的署名见 `miniagent/web/vendor/markdown-it.LICENSE` 和 `markdown-it.NOTICES`。[上游渲染器](https://github.com/markdown-it/markdown-it/blob/a8c00d474ec9b304fad9b71c16d6fa8b4909279e/src/renderer.ts) · [固定依赖版本](https://github.com/markdown-it/markdown-it/blob/a8c00d474ec9b304fad9b71c16d6fa8b4909279e/package-lock.json)

Studio 保留上游的分词、嵌套列表、引用、表格、代码块和 HTML 转义规则。链接令牌按 Studio 的可见文件索引生成可点击附件。外部 HTTP(S) 来源可以手动在新标签打开，图片只显示替代文字，未经授权的本地链接保留标签文本。Markdown 正文使用现有 artifact API 的路径检查和发布摘要校验，最多预览 256 KiB，并提供原文下载。[上游异常链接用例](https://github.com/markdown-it/markdown-it/blob/a8c00d474ec9b304fad9b71c16d6fa8b4909279e/test/fixtures/markdown-it/xss.txt)

- 群成员在消息里给出明确链接、反引号内的路径或直接写相对路径时，点文件即可在原会话阅读。展开交付材料也可进入同一份文件。
- 历史消息按发送时可见的成员回执解析文件。弹层注明交接版本或诊断附件。已冻结的 Markdown 内部相对链接交给同一个回执的清单核实，保留当时未改动的依赖文件。读完可返回上一份或关闭回到原消息。
- 并行派工或不同角色的文件夹出现同名文件时，通用路径提示重名。明确的角色路径、具体的回执或 `dispatch:` 交付引用仍可打开。正文中的搜索网址不会被改写为本地文件。
- 用户在续聊时引用旧文件，依据本轮实际继承的 `base_run_id`。独立开始的新一轮没有这条关联。未发布的成员交接只使用该条消息明确附带的可读诊断附件。

浏览器回归 `node tests/web/markdown_reader.cjs` 通过真实本地服务打开一个两轮群聊，检查旧版、私稿、嵌套附录、外链、图片请求、表格、代码、键盘返回以及 375 像素宽度。提供 Python 项目环境和 Playwright 或 Chrome 时，可通过 `PYTHON`、`NODE_PATH`、`CHROMIUM_EXECUTABLE` 指向现有安装。[回归脚本](../../tests/web/markdown_reader.cjs)

另以真实浏览器延迟文件响应，覆盖弹层返回以及产物页连续选择 A、B、A 的顺序。回归会下载超过 256 KiB 的原始文件并校验末行，对超过 8 MiB 的文件展示本机查看说明。
