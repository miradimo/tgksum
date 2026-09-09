import json

with open("result.json", "r", encoding="utf-8") as f:
  data = json.load(f)

channels = []

for chat in data.get("chats", {}).get("list", []):
  # Фильтруем только каналы
  if chat.get("type") in ("public_channel", "saved_channel"):
    # Достаем юзернейм или ссылку
    username = chat.get("username")
    name = chat.get("name")
    if username:
      channels.append(username)
    else:
      print(f"Приватный канал без юзернейма: {name}")

print(f"Найдено публичных каналов: {len(channels)}")
print("Список для target_channels:")
print(channels)