import os

from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.getenv("OPENAI_API_VERSION", "2024-12-01-preview"),
)

resp = client.chat.completions.create(
    model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
    messages=[{"role": "user", "content": "안녕"}],
)
print(resp.choices[0].message.content)
