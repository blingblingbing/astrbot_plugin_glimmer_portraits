# 浮光群像（Glimmer Portraits）

面向 AstrBot 群聊的轻量用户画像插件。插件会根据群聊中的明确证据整理成员的称呼、兴趣、偏好、学习或工作方向、沟通特点和群内关系，并在回复前注入当前话题相关的简短画像。

当前版本：`0.2.0`

插件标识、目录名与推荐仓库名均为 `astrbot_plugin_glimmer_portraits`。

## 功能

- 按“平台 + 群号 + 用户 ID”隔离画像，不合并不同群的数据。
- 按多人对话上下文增量总结，只保存有消息证据支持的内容。
- 支持称呼、兴趣、游戏偏好、职业或学习方向、工作状态、技能、软件与设备偏好、饮食、粗略地区或时区、沟通风格、近期状态、群内关系和自由观察等字段。
- 只将当前发言者、@对象和回复目标的相关画像注入当前请求，并限制注入人数和字符数。
- 候选称呼、临时状态和群内观察支持自动过期；管理员可以单独归档错误称呼。
- 使用 AstrBot 已配置的 Provider 进行低频画像总结，数据保存在本地 SQLite。

## 安装

将完整插件目录放入 AstrBot 插件目录：

```text
AstrBot/data/plugins/astrbot_plugin_glimmer_portraits/
```

确认目录中直接包含 `main.py`、`metadata.yaml` 和 `_conf_schema.json`，然后在 AstrBot 管理面板中重载插件，或重启 AstrBot。

也可以在 AstrBot 插件管理页面中使用仓库地址安装：

```text
https://github.com/blingblingbing/astrbot_plugin_glimmer_portraits
```

## 配置

在 AstrBot 管理面板中打开本插件配置：

1. 在 `summary_provider_id` 中选择画像总结使用的 Provider；留空则使用 AstrBot 默认 Provider。
2. 根据群消息量调整自动总结阈值，默认累计 `80` 条新消息触发一次总结。
3. 按需设置群白名单、黑名单、画像字段和注入字符上限。
4. 敏感字段默认关闭；即使开启，也只接受用户明确自述，不允许模型自行推断。

常用配置如下：

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| `summary_provider_id` | 空 | 画像总结使用的 Provider；留空使用默认 Provider |
| `summary_every_n_messages` | `80` | 当前群累计多少条新消息后自动总结 |
| `daily_summary_enabled` | `true` | 是否启用每日定时总结 |
| `summary_batch_messages` | `120` | 单次最多处理的消息数量 |
| `enabled_fields` | 见配置页 | 允许更新的画像字段 |
| `min_confidence` | `0.58` | 低于此置信度的结果不会写入数据库 |
| `alias_candidate_ttl_days` | `14` | 未确认候选称呼没有新证据时的保留时间 |
| `observed_alias_ttl_days` | `30` | 观察到的正式称呼没有新实际使用时的保留时间，`0` 表示不过期 |
| `temporary_ttl_days` | `45` | 近期状态、工作状态等临时内容的保留时间，`0` 表示不过期 |
| `observation_ttl_days` | `90` | 沟通风格和自由观察的保留时间，`0` 表示不过期 |
| `injection_max_users` | `4` | 单次最多注入的相关用户数 |
| `injection_max_total_chars` | `1400` | 单次画像注入的总字符上限 |
| `allow_sensitive_fields` | `false` | 是否允许明确自述的敏感字段 |
| `allow_whole_group_reset` | `false` | 是否允许管理员发起全群重置 |

### 画像有效期

- 候选称呼默认 14 天没有新证据后过期。
- `source=observed` 的正式称呼默认 30 天没有新的实际使用后过期。
- 明确自称或直接命名得到的称呼不会按 `observed_alias_ttl_days` 过期。
- 过期采用懒清理：下次读取或更新画像时标记为过期，不运行额外的后台清理任务。
- 称呼的实际使用由画像总结模型在正常总结结果中报告，使用同一次模型调用，不额外增加调用次数。

## 管理员指令

以下指令仅允许 AstrBot 管理员在群聊中使用：

```text
/群画像
```

查看当前群近期活跃用户的画像概览和待总结消息数量。

```text
/群画像 <QQ号>
```

查看指定用户实际会被注入 LLM 的画像文本。

```text
/更新画像
```

立即总结当前群尚未处理的消息；没有新消息时不会调用模型。

```text
/删除称呼 <QQ号> <昵称>
```

归档指定用户的一条正式称呼及同名候选，不物理删除历史记录。该指令不会调用模型。

```text
/重置画像 <QQ号>
/重置画像 全群
```

先预览将要清除的数据并生成确认码，再进行二次确认。全群重置还必须先开启 `allow_whole_group_reset`。

## 隐私与边界

- 插件只处理群聊，不建立私聊画像，也不合并不同群的身份。
- 默认不采集 Bot 自己的消息，避免 Bot 回复反向污染画像。
- 敏感字段默认关闭；开启后也只接受明确自述。
- 总结成功后会删除短期待处理消息，只保留受数量和有效期约束的画像条目及有限证据摘录。
- 模型总结并非绝对准确，管理员可以使用 `/群画像 <QQ号>` 抽查，或使用 `/删除称呼`、`/重置画像` 修正结果。

运行时数据库位于 AstrBot 插件数据目录：

```text
data/plugin_data/astrbot_plugin_glimmer_portraits/group_profiles.sqlite3
```
