# GeminiAPI Mixer

中文说明已合并到 [主文档](README.md)。原 Gemini Web 文档保留在
[docs/upstream-gemini-web_CN.md](docs/upstream-gemini-web_CN.md)。

## 统一 GPT 入口

可选的 OpenAI-only Sub2API 路由可让客户端沿用同一个 Mixer API URL 和 Mixer Key，
访问现有 Gemini / Claude 模型以及明确允许的 GPT 模型。保留已有模型名和来源标记；
新增 GPT 路由支持 Chat Completions、Responses 和 SSE，不自动虚构思考档位。
配置与限制见 [统一入口说明](docs/unified-openai.md)。
