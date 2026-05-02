# OneBot通知

接入 MoviePilot 普通通知消息，将 `post_message` 通知转发到 OneBot v11 HTTP API，适用于 NapCat、Lagrange、go-cqhttp 兼容端。

## 配置

- `OneBot HTTP 地址`：OneBot 正向 HTTP 根地址，例如 `http://127.0.0.1:5700`。
- `Access Token`：OneBot 端配置了 `access_token` 时填写。
- `Token放入URL参数`：默认使用 `Authorization: Bearer <token>`，少数兼容端需要把 token 放在 URL 参数时再开启。
- `群号`：接收通知的 QQ 群号，多个用逗号分隔。
- `用户QQ号`：接收通知的 QQ 号，多个用逗号分隔。
- `通知类型过滤`：不选择则转发全部 MoviePilot 通知。

## 行为

- 插件只处理普通通知消息，不处理媒体候选列表和种子候选列表。
- 默认把标题、正文、图片和详情链接合并成一条 OneBot 消息。
- 图片使用 OneBot CQ 码 `[CQ:image,file=...]` 发送，需 OneBot 端支持对应图片 URL。
- 详情页提供“发送测试”按钮，也可以调用 `/plugin/OneBotNotify/test?apikey=API_TOKEN`。
