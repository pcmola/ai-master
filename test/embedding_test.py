import os

from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.getenv("OPENAI_API_VERSION", "2024-12-01-preview"),
)

r = client.embeddings.create(
    model=os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
    input=["hello world", "안녕하세요"],
)
print(r.data, len(r.data), len(r.data[0].embedding))
