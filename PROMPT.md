писать на пайтоне будем, вот кусок для лангчейна from langchain_nvidia_ai_endpoints import ChatNVIDIA


  client = ChatNVIDIA(
    model="deepseek-ai/deepseek-v4-pro",
    api_key="$NVIDIA_API_KEY",
    temperature=1,
    top_p=0.95,
    max_tokens=16384,
    extra_body={"chat_template_kwargs":{"thinking":False}},
  )

  response = client.invoke([{"role":"user","content":""}])
  print(response.content) ключ у меня в @.env.example он бесплатный так что не волнуйся я хочу чтобы ты полностью проанализировал @GOAL_DESCRIPTION.md подумал сделал ресерч и если хотел уточнить какие-то моменты то задай вопросы

  деплоить буду на vercel бесплатный с вебхуками так что там имей ввиду это по орпеделенному писать в папке надо

  после того как задашь вопросы мне, создай папку ./tickets и там создай несколько тикетов для выполнения этой цели, тикеты должны быть относительно большими то есть тикетов должно быть мало и каждый тикет должен быть целым звеном которое можно протестировать end to end
