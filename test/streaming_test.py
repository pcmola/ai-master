import os

from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.getenv("OPENAI_API_VERSION", "2024-12-01-preview"),
)

stream = client.chat.completions.create(
    model=os.environ.get("OPENAI_MODEL", "gpt-6.1-sol"),
    messages=[{"role": "user", "content": "1부터 5까지 천천히 세"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
