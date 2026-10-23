import os

from azure_openai_rest import chat, load_project_env


load_project_env()
model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
response = chat(model, "안녕")
print(response["choices"][0]["message"].get("content", ""))
