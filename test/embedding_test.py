import os

from azure_openai_rest import embeddings, load_project_env


load_project_env()
model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
response = embeddings(model, ["hello world", "안녕하세요"])
items = response["data"]
print(f"model={model} items={len(items)} dimensions={len(items[0]['embedding'])}")
