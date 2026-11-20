# 在实际视口中验收网页布局

单屏投影要求观众坐在同一位置就能看全本页。浏览器可以成功翻页，文档宽度也可以恰好等于视口，同时固定容器里的画布仍被裁掉。普通文章允许读者滚动，应按实际阅读路径验证底部内容能否读到。

## 给评审者的操作指引

1. 采用任务指定的视口尺寸，以 CSS 像素调用 `page.set_viewport_size`。没有指定尺寸时，说明实际验证过的尺寸和选择理由。打开交付的入口，等待目标内容、字体或图表就绪，从渲染结果识别标题、正文、图表标签和页脚。
2. 验收投影时，在正常翻页后保持观众所处的位置。对需要同时可见的区域运行 `expect(region).to_be_in_viewport(ratio=1)`。用 `region.bounding_box()` 与实际视口说明哪一侧超出边界。祖先的裁切、文字溢出和遮挡也会影响阅读，可检查图表内部标签、可读字号，并查看 `page.screenshot(full_page=False)` 保存的当前视口画面。
3. 验收可滚动页面时，通过滚轮、键盘或页面自带的导航进入后续区域，再检查文字能否阅读、控件能否操作。遇到失败时，记录视口尺寸、页面状态、具体缺失内容和实测边界。截图保存或查看受限时，报告可复核的测量和仍待确认的视觉问题。

工具说明与正式评审的执行指导都提供了这条路径。阈值取决于交付标准，部分露出的装饰或允许滚动的容器可使用相应标准。截图和几何测量各有用途，元素的视口交集比例无法判断文字是否被不透明浮层挡住。

## 上游与验证

Anthropic 的 webapp-testing 示例先在渲染完成的页面发现控件，再取得截图。Studio 沿用这一顺序，根据投影验收目的截取当前视口。示例采用独立脚本编写。[Anthropic 控件发现与截图示例](https://github.com/anthropics/skills/blob/e4bb5d960d87126ff3b3a8af1a104b7db67560ba/skills/webapp-testing/examples/element_discovery.py)

Microsoft Playwright 的 `viewportRatio` 读取 `IntersectionObserver` 返回的交集比例。上游测试覆盖滚动前后、部分可见的比例阈值、失败信息和被其他元素覆盖时的行为。Studio 直接使用 Playwright 已有断言。[视口比例实现](https://github.com/microsoft/playwright/blob/a018f24a4fc76e2120d3b35d2415f1e5bd4b073e/packages/injected/src/injectedScript.ts#L639-L650) · [比例、失败与遮挡测试](https://github.com/microsoft/playwright/blob/a018f24a4fc76e2120d3b35d2415f1e5bd4b073e/tests/page/expect-misc.spec.ts#L554-L611)

运行 `python -m unittest tests.test_browser_tool tests.test_execute_review_guidance`。真实 Chromium 用例检查了两种视口中的固定画布。标题可以翻页、文档宽度正常、页脚元素有布局，实际页脚却完全在视口外，工具正确返回断言失败。另一份长网页通过键盘滚动后，页脚能够完整阅读并通过核查。
