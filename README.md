<div align="center">
<img style="width:70%" src="https://count.getloli.com/@astrbot_plugin_splitter?name=astrbot_plugin_splitter&theme=booru-lewd&padding=5&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt=":name">

# 对话分段PRO


[![查看日志（v1.0.0）](https://img.shields.io/badge/查看日志-blue?style=for-the-badge)](#日志) 
[![未来计划](https://img.shields.io/badge/未来计划-purple?style=for-the-badge)](#未来计划)
[![来许愿！](https://img.shields.io/badge/来许愿！-ff69b4?style=for-the-badge)](#联系)

</div>

这里是一个简洁、无多余 Emoji 的 `README.md` 文档，涵盖了代码中的核心功能和配置项。

---

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

以下是插件支持的配置项，可在插件配置文件中调整：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `split_regex` | string | `[。？！?!\\n…]+` | 用于分段的正则表达式，匹配到此处时会进行拆分。 |
| `clean_regex` | string | `""` (空) | 用于清理内容的正则表达式，匹配到的内容会被替换为空。 |
| `enable_smart_split` | bool | `true` | 是否开启智能模式。开启后，不会在成对符号（引号、括号）内部进行拆分。 |
| `max_segments` | int | `7` | 最大分段数量。超过此数量后，剩余内容将合并发送。设为 0 则不限制。 |
| `delay_strategy` | string | `"log"` | 延迟策略。可选值：`log` (对数), `random` (随机), `fixed` (固定)。 |

### 延迟策略详情

根据 `delay_strategy` 的不同，以下参数生效：

*   **log (对数策略，默认)**: 根据字数计算延迟，字数越多延迟越久，但有上限。
    *   `log_base`: 基础延迟 (默认 0.5秒)
    *   `log_factor`: 系数 (默认 0.8)
    *   计算公式: `base + factor * log(length + 1)`
*   **random (随机策略)**:
    *   `random_min`: 最小秒数 (默认 1.0)
    *   `random_max`: 最大秒数 (默认 3.0)
*   **fixed (固定策略)**:
    *   `fixed_delay`: 固定延迟秒数 (默认 1.5)

## 注意事项

*   插件仅处理带有 LLM 回复标记的消息。
*   如果拆分后只有一段且无清理规则，插件将不做处理，直接发送原消息。
*   智能模式下，如果引号或括号未闭合（如只有左括号），可能会导致整段文本不拆分，这是为了保证语义完整性。
