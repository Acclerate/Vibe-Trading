---
domain: xueqiu.com
aliases: [雪球]
updated: 2026-04-19
---

## 平台特征
- 投资社交平台，超过 7800 万用户
- 强反爬机制：需登录 Cookie、频率限制、IP 封禁
- 内容通过 JS 动态渲染，非 SSR
- 用户主页 URL 格式：`https://xueqiu.com/{slug}`（slug 可为字母别名或数字 ID）
- 部分用户无字母别名，URL 为 `https://xueqiu.com/{数字ID}`

## 有效模式

### 用户主页帖子列表 DOM 结构
页面容器：`div.profiles__timeline__bd`
单条帖子：`article.timeline__item`

#### 帖子字段提取

| 字段 | 选择器 | 说明 |
|------|--------|------|
| 帖子ID | `a.date-and-source[data-id]` 的 `data-id` 属性 | 帖子唯一标识 |
| 帖子URL | `a.date-and-source[href]` | 格式 `/{userId}/{postId}` |
| 发布时间 | `a.date-and-source` 的文本内容 | 如"5小时前"、"04-17 09:36" |
| 来源 | `a.date-and-source span.source` | 如"来自iPad" |
| 正文内容 | `div.timeline__item__content div.content--description > div:last-child` | 原创帖子正文 |
| 转发/回复内容 | `blockquote.timeline__item__forward div.timeline__item__forward__content` | 被转发的原帖内容 |
| 转发作者 | `blockquote.timeline__item__forward span.user-name` | 被转发帖子作者 |
| 评论数 | `div.timeline__item__ft a.replay-count` 文本 | 如"讨论 423" |
| 点赞数 | `div.timeline__item__ft span.like-count` 的前一个兄弟元素文本 | 在 `timeline__item__ft` 中 |
| 转发数 | `div.timeline__item__ft span.retweet-count` 文本 | 如"转发 27" |
| 收藏数 | `div.timeline__item__ft span.like-count` 文本 | 如"收藏 563" |

#### 帖子类型判断
- **原创帖**：`blockquote.timeline__item__forward` 不存在
- **转发帖**：`blockquote.timeline__item__forward` 存在
  - 正文在 `div.timeline__item__content` 中是转发评论
  - 被转发内容在 `blockquote` 中
- **长文帖**：`blockquote.timeline__item__forward--longtext` 类名
- **含查看对话**：`div.dialogue__wrap` 存在表示有更多对话

### 懒加载
- 页面初始加载 20 条帖子
- 需 `/scroll` 触发加载更多
- 每次滚动约加载 20 条

### 用户信息
| 字段 | 选择器 |
|------|--------|
| 用户名 | `a.user-name` 在 `div.profiles__timeline__hd` 或 `article.timeline__item` 中 |
| 用户ID | `a.user-name[data-tooltip]` 的 `data-tooltip` 属性 |

## 已知陷阱
- 时间格式为相对时间（"5小时前"、"昨天 14:31"），需转换为绝对时间
- 转发帖的"正文"实际上是转发评论，被转发的原帖在 blockquote 中
- 大量帖子为转发+评论形式，需区分原创和转发
- 搜索页面 URL：`https://xueqiu.com/k?q={keyword}&type=user`，type=user 限定用户搜索
- 部分用户无字母别名，需用数字 ID 访问
- 页面标题可验证登录状态：登录后显示"用户名 - 雪球"
