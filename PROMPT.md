# Historical ticket-generation prompt

This file records the original request that produced the ticket directory. It
is retained for context only; `GOAL_DESCRIPTION.md` and
`tickets/00-ARCHITECTURE.md` are the current product and technical contracts.

Use Python. The original LangChain sketch was:

```python
from langchain_nvidia_ai_endpoints import ChatNVIDIA

client = ChatNVIDIA(
    model="deepseek-ai/deepseek-v4-pro",
    api_key="$NVIDIA_API_KEY",
    temperature=1,
    top_p=0.95,
    max_tokens=16384,
    extra_body={"chat_template_kwargs": {"thinking": False}},
)

response = client.invoke([{"role": "user", "content": ""}])
print(response.content)
```

The user requested a full analysis of `GOAL_DESCRIPTION.md`, relevant research,
clarifying questions where necessary, and then a small number of large,
end-to-end-testable tickets in `./tickets` for a free-tier Vercel webhook
deployment.

The historical `extra_body` example is not the current implementation contract.
The architecture now requires the pinned `ChatNVIDIA` wrapper and
`with_thinking_mode(enabled=False)`, validated by a request-shape contract test.
