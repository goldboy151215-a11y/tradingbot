import json

path = "/root/ft_userdata/user_data/data/bybit/futures/leverage_tiers_USDT.json"
with open(path) as f:
    d = json.load(f)

d["data"]["XRP/USDT:USDT"] = [
    {"tier": 1, "symbol": None, "currency": None, "minNotional": 0.0, "maxNotional": 5000000.0, "maintenanceMarginRate": 0.01, "maxLeverage": 50.0}
]

with open(path, "w") as f:
    json.dump(d, f)
print("Updated XRP leverage tiers to 5,000,000 maxNotional")
