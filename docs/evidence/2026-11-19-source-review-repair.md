# 来源评审退修时，作者如何复核争议

## 触发这次修改的交付

群聊运行 `studio-20261119-205023-b10b1e528d34` 的半导体演示第 13 页原本正确陈述了微软对 OpenAI 投资的损益方向。公开文件的 SHA-256 以 `27289de3` 开头。在公开群记录第 16 条分工中，独立评审要求作者把本期和上年同期的方向分别反转。作者照改，下一份公开文件 `ae8faceb…` 的两个符号均与公司披露相反。

微软的报告显示，FY27Q1 本期 GAAP 净利为 366.29 亿美元，剔除 OpenAI 投资收益 2.07 亿美元后得到 364.22 亿美元。投资本身增加净利、增加每股收益 0.03 美元。上年同期出现投资损失，减少净利 30.86 亿美元、减少每股收益 0.41 美元。原评审引用的是从 GAAP 到调整后净利所做的加减。公司的指标定义和管理层讨论均直接说明了投资本身的方向。[微软披露的调节表和定义](https://www.microsoft.com/en-us/Investor/earnings/FY-2027-Q1/press-release-webcast) · [微软向 SEC 提交的 10-Q](https://www.sec.gov/Archives/edgar/data/789019/000119312526436133/msft-20260930.htm)

## 返工与签署

当正式签署的产品退修包含必需的 `source` 项时，Studio 在原作者实际收到的分工中明确：打开原始发布者的具体材料，逐项对照争议断言，数字须对齐对象、期间、单位、分母与符号。原文支持退修时更正。原文支持原稿时可以保留正确事实，在新版补上原始链接、可定位的引文或计算依据，并在交接中解释分歧。证据不足时标记待核实。原评审看到新版后，须按原标准独立核对引文与原文并签署。

这一改动借鉴了 obra/superpowers 的 `receiving-code-review`。其反馈处理流程要求先按现有事实核对意见，发现错误时给出技术理由。Studio 把核验对象从代码行为换成发布者原文，并沿用宿主的作者、评审角色和候选版本绑定。数字复算参考 Anthropic 的 `ib-check-deck`：脚本保留页码和数字上下文，指南要求从原始端点复算、统一单位和期间，核对文字与数字的关系。这里采用的是工作方法，判断仍由拿到原始材料的角色完成。[Superpowers 评审反馈处理](https://github.com/obra/superpowers/blob/22f0b463e7a5e579904dd6211a520321035526f7/skills/receiving-code-review/SKILL.md) · [Anthropic 的数字核查说明](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/SKILL.md) · [提取数字及其上下文的实现](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/scripts/extract_numbers.py#L100-L219)

## 平台回归

`tests/test_studio_required_source_review.py` 的 `test_source_repair_author_can_substantiate_original_fact_for_independent_resignature` 通过正常 Studio 群聊入口发布一张派送单。评审已打开签发方文件，但提出了错误地址。作者在返工分工中收到查证要求，亲自打开签发方文件，在新版保留正确地址并补上原文位置。原评审再次打开原件、读取新稿，签署新版。用例检查两个角色实际收到的上下文、作者没有签署工具、两次签署版本不同，以及最终签署所绑定的作者工件哈希。此回归使用脚本提供方和模拟原始网页，验证提示能到达具体分工与签署边界；真实模型能否纠正争议仍须以新运行的实际交付核对。
