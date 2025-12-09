<div align="center">
<img style="width:70%" src="https://count.getloli.com/@astrbot_plugin_splitter?name=astrbot_plugin_splitter&theme=booru-lewd&padding=5&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt=":name">

# 对话分段PRO


[![查看日志（v1.1.1）](https://img.shields.io/badge/查看日志-blue?style=for-the-badge)](#日志) 
[![未来计划](https://img.shields.io/badge/未来计划-purple?style=for-the-badge)](#未来计划)
[![来许愿！](https://img.shields.io/badge/来许愿！-ff69b4?style=for-the-badge)](#联系)

</div>

懒得写md文件了凑合看吧（
---

## v1.1.1
这是一个用于 AstrBot 的简单的消息处理插件。它能将 LLM 生成的长文本回复自动拆分为多条短消息依次发送，模拟人类的说话节奏，避免长篇大论的文字墙。  
**特别的** ，解决了框架分段无法将@组件正常加入分段逻辑里的Bug

## 功能特性

*   **智能拆分**：根据标点符号（如句号、问号、感叹号、换行符）将长文本拆分为独立的消息气泡。
*   **上下文感知**：支持“智能模式”，自动识别成对的符号（如引号 `“”`、括号 `（）`、`[]` 等），避免在引用或完整句子中间强行断句。
*   **拟人延迟**：在发送下一条分段前自动计算等待时间，支持固定延迟、随机延迟或基于字数的对数延迟。
*   **防刷屏限制**：支持设置最大分段数（`max_segments`），超出限制的部分会自动合并到最后一条消息中。
*   **内容清理**：支持通过正则在发送前清理特定字符。

## 安装与使用

1.  将插件文件放入 AstrBot 的插件目录中。
2.  在控制台或配置文件中启用该插件。
3.  根据需求修改配置参数。

## 配置说明

具体还是去配置页面看吧，有什么不懂的提issues或者去群里问也可以

### 延迟策略详情

也是去配置页面看吧（）

## 注意事项

*   如果对正则不了解的话，建议不要修改，如果需要修改字符，可以用简单字符匹配
*   如果拆分后只有一段且无清理规则，插件将不做处理，直接发送原消息。
*   智能模式下，如果引号或括号未闭合（如只有左括号），可能会导致整段文本不拆分，这是为了保证语义完整性。

# 日志
## 2025.12.09
- 上架
- 添加了一点配置项目，如分段阈值和分段场景

# 未来计划
- [ ] 来提

# 联系
| 作者信息 | 交流/反馈 |
| :--- | :--- |
| **作者**: 糯米茨<br>**联系方式**: （许愿通道）<br>- [GitHub Issues](https://github.com/nuomicici/astrbot_plugin_Favour_Ultra/issues)<br>- [QQ](https://qm.qq.com/q/wMGXYfKKoS) | <img src="https://github.com/nuomicici/astrbot_plugin_Favour_Ultra/blob/main/QC.jpg?raw=true" width="240px"> |

## 求你们了
来~~鞭策~~支持一下叭！
